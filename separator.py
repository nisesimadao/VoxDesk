"""ボーカル除去（音源分離）。

Demucs のモデルを GPU で走らせて、手持ちの曲からオフボーカルを作る。
入出力は PyAV で行い、Demucs 側のファイル入出力には依存しない
（torchaudio のバックエンド事情に振り回されないため）。

この機能は要件を満たす PC でだけ有効になる。非力な環境で動かすと
1 曲に数十分かかったり途中で落ちたりして、かえって使いにくいため、
最初から無効にして理由を表示する方針にしている。
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
import threading
import wave
from dataclasses import dataclass

import av
import numpy as np

import platform_support

MIN_VRAM_GB = 4.0     # これ未満の GPU では分離させない
MIN_RAM_GB = 8.0
HQ_VRAM_GB = 8.0      # これ以上なら高品質モデルを使う
MPS_MIN_RAM_GB = 16.0  # Apple Silicon は GPU とメモリを共有するので基準を上げる
MPS_HQ_RAM_GB = 32.0
FAST_MODEL = "htdemucs"
HQ_MODEL = "htdemucs_ft"

CACHE_DIR = os.path.join(platform_support.cache_dir(), "offvocal")


@dataclass
class Capability:
    """この PC でボーカル除去が使えるか。"""

    available: bool
    reason: str = ""
    device: str = ""
    gpu_name: str = ""
    vram_gb: float = 0.0
    model: str = FAST_MODEL

    @property
    def summary(self) -> str:
        if not self.available:
            return f"使えません — {self.reason}"
        quality = "高品質" if self.model == HQ_MODEL else "標準"
        return f"{self.gpu_name}（{self.vram_gb:.0f} GB）で使えます / {quality}モデル"


_capability: Capability | None = None
_capability_lock = threading.Lock()
_model_cache: dict = {}


def _total_ram_gb() -> float:
    if platform_support.WINDOWS:
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullTotalPhys / (1024 ** 3)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except (ValueError, AttributeError):
        return 0.0


def capability(refresh: bool = False) -> Capability:
    """使えるかどうかを判定する。torch の import が重いので結果を覚えておく。"""
    global _capability
    with _capability_lock:
        if _capability is not None and not refresh:
            return _capability
        _capability = _detect()
        return _capability


def _detect() -> Capability:
    try:
        import torch
    except ImportError:
        return Capability(False, "ボーカル除去に必要な部品（torch）が入っていません")
    try:
        import demucs.pretrained  # noqa: F401
    except ImportError:
        return Capability(False, "ボーカル除去に必要な部品（demucs）が入っていません")

    ram = _total_ram_gb()

    if torch.cuda.is_available():  # NVIDIA GPU
        props = torch.cuda.get_device_properties(0)
        vram = props.total_memory / (1024 ** 3)
        if vram < MIN_VRAM_GB:
            return Capability(
                False,
                f"GPU のメモリが足りません（{vram:.1f} GB / {MIN_VRAM_GB:.0f} GB 以上必要）",
                gpu_name=props.name, vram_gb=vram)
        if ram and ram < MIN_RAM_GB:
            return Capability(
                False, f"メモリが足りません（{ram:.1f} GB / {MIN_RAM_GB:.0f} GB 以上必要）",
                gpu_name=props.name, vram_gb=vram)
        return Capability(
            True, device="cuda", gpu_name=props.name, vram_gb=vram,
            model=HQ_MODEL if vram >= HQ_VRAM_GB else FAST_MODEL,
        )

    # Apple Silicon。GPU とメモリを共有するので、判定はメモリ量で行う
    mps = getattr(torch.backends, "mps", None)
    if platform_support.MACOS and mps is not None and mps.is_available():
        if ram and ram < MPS_MIN_RAM_GB:
            return Capability(
                False,
                f"メモリが足りません（{ram:.1f} GB / {MPS_MIN_RAM_GB:.0f} GB 以上必要）",
                gpu_name="Apple Silicon GPU", vram_gb=ram)
        return Capability(
            True, device="mps", gpu_name="Apple Silicon GPU", vram_gb=ram,
            model=HQ_MODEL if ram >= MPS_HQ_RAM_GB else FAST_MODEL,
        )

    return Capability(
        False,
        "対応する GPU が見つかりません"
        "（NVIDIA の CUDA 対応 GPU か、Apple Silicon の Mac が必要です）")


def _load_model(name: str, device: str):
    key = (name, device)
    if key in _model_cache:
        return _model_cache[key]
    import torch
    from demucs.pretrained import get_model

    model = get_model(name)
    model.to(device)
    model.eval()
    torch.set_grad_enabled(False)
    _model_cache[key] = model
    return model


def _decode(path: str, rate: int, channels: int) -> np.ndarray:
    """音声を (チャンネル, サンプル) の float32 で読み出す。"""
    layout = "stereo" if channels == 2 else "mono"
    blocks = []
    with av.open(path) as container:
        if not container.streams.audio:
            raise RuntimeError("この ファイルには音声が入っていません")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="flt", layout=layout, rate=rate)
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                blocks.append(resampled.to_ndarray().reshape(-1, channels))
        for resampled in resampler.resample(None):
            blocks.append(resampled.to_ndarray().reshape(-1, channels))
    if not blocks:
        raise RuntimeError("音声を読み取れませんでした")
    return np.concatenate(blocks).T.astype(np.float32)


def _write_wav(path: str, audio: np.ndarray, rate: int) -> None:
    """(チャンネル, サンプル) の float32 を 16bit WAV で書き出す。"""
    data = np.clip(audio.T, -1.0, 1.0)
    pcm = (data * 32767.0).astype(np.int16)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as f:
        f.setnchannels(audio.shape[0])
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm.tobytes())


def cache_path(source: str, model_name: str, keep_vocals: bool = False) -> str:
    """同じ曲・同じ設定なら作り直さないよう、内容から決まる名前にする。"""
    try:
        stat = os.stat(source)
        key = f"{os.path.abspath(source)}|{stat.st_size}|{int(stat.st_mtime)}|{model_name}"
    except OSError:
        key = f"{source}|{model_name}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    stem = os.path.splitext(os.path.basename(source))[0][:40]
    kind = "vocal" if keep_vocals else "offvocal"
    return os.path.join(CACHE_DIR, f"{stem}_{kind}_{digest}.wav")


def separate(source: str, progress=None, keep_vocals: bool = False,
             model_name: str | None = None) -> str:
    """ボーカルを除いた音声を作り、そのファイルパスを返す。

    progress(段階名, 0〜1) が呼ばれる。作成済みならすぐにパスを返す。
    """
    cap = capability()
    if not cap.available:
        raise RuntimeError(cap.reason)
    name = model_name or cap.model

    output = cache_path(source, name, keep_vocals)
    if os.path.exists(output):
        if progress:
            progress("作成済みのものを使います", 1.0)
        return output

    import torch
    from demucs.apply import apply_model

    if progress:
        progress("モデルを読み込み中", 0.05)
    model = _load_model(name)

    if progress:
        progress("音声を読み込み中", 0.15)
    wav = _decode(source, model.samplerate, model.audio_channels)

    tensor = torch.from_numpy(wav)
    reference = tensor.mean(0)
    mean, std = reference.mean(), reference.std()
    if float(std) < 1e-8:
        raise RuntimeError("無音のようです")
    tensor = (tensor - mean) / std

    if progress:
        progress("ボーカルを分離中", 0.25)
    with torch.no_grad():
        sources = apply_model(
            model, tensor[None], device=cap.device, split=True, overlap=0.25, progress=False
        )[0]
    sources = sources * std + mean

    names = list(model.sources)
    if "vocals" not in names:
        raise RuntimeError("このモデルはボーカル分離に対応していません")
    index = names.index("vocals")
    if keep_vocals:
        result = sources[index]
    else:
        result = sum(part for i, part in enumerate(sources) if i != index)

    if progress:
        progress("書き出し中", 0.9)
    _write_wav(output, result.cpu().numpy(), model.samplerate)

    del sources, tensor
    torch.cuda.empty_cache()
    if progress:
        progress("完成", 1.0)
    return output


def cached_files() -> list[str]:
    if not os.path.isdir(CACHE_DIR):
        return []
    return [os.path.join(CACHE_DIR, f) for f in sorted(os.listdir(CACHE_DIR))
            if f.lower().endswith(".wav")]


def cache_size_mb() -> float:
    return sum(os.path.getsize(f) for f in cached_files()) / (1024 ** 2)


def clear_cache() -> int:
    removed = 0
    for path in cached_files():
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed

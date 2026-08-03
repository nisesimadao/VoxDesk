"""AI ボーカル除去（torch / demucs）を、あとから入れる。

配布物に同梱すると 3 GB を超えてしまうため、必要な人だけが後から入れられるようにする。
入れ先は書き込みできる場所（設定フォルダの runtime）で、そこを読み込み先に足して使う。
インストーラ版には site-packages も pip も無いので、この形にしている。

進捗は pip の出力を読まず、入れ先の容量が増えていく様子で測る。
pip の表示は版によって変わるが、容量は必ず増えるので壊れにくい。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

import platform_support
import separator

# CUDA 版の torch は PyPI ではなく専用の配布元から取る
CUDA_INDEX = "https://download.pytorch.org/whl/cu126"
APPROX_TOTAL_MB = 3000.0  # 進捗表示の目安
RNNOISE_MB = 15.0


def target_dir() -> str:
    return separator.RUNTIME_DIR


def installed() -> bool:
    return os.path.isdir(os.path.join(target_dir(), "torch"))


def installed_size_mb() -> float:
    root = target_dir()
    if not os.path.isdir(root):
        return 0.0
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                continue
    return total / (1024 ** 2)


def find_python() -> tuple[str | None, str]:
    """pip が使えて、この実行環境と同じ ABI の Python を探す。

    ソースから動かしているなら自分自身（仮想環境の python）がそのまま使える。
    インストーラ版は Python を内蔵していないので、外から探す必要がある。
    """
    if not getattr(sys, "frozen", False):
        return sys.executable, ""

    want = f"{sys.version_info.major}.{sys.version_info.minor}"
    candidates: list[list[str]] = []
    if platform_support.WINDOWS:
        candidates.append(["py", f"-{want}"])
    candidates += [[f"python{want}"], ["python3"], ["python"]]

    for candidate in candidates:
        exe = shutil.which(candidate[0])
        if not exe:
            continue
        command = [exe, *candidate[1:], "-c",
                   "import sys, pip; print(f'{sys.version_info.major}.{sys.version_info.minor}')"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except Exception:
            continue
        if result.returncode == 0 and result.stdout.strip() == want:
            return exe, ""
    return None, (f"Python {want} が見つかりません。"
                  f"python.org から Python {want} を入れてから、もう一度お試しください。")


def _pip_command(python: str, packages: list[str], index: str | None) -> list[str]:
    command = [python, "-m", "pip", "install", "--upgrade", "--target", target_dir(),
               "--no-warn-script-location", "--disable-pip-version-check"]
    if index:
        command += ["--index-url", index]
    return command + packages


def install(progress=None, cancel: threading.Event | None = None) -> None:
    """torch と demucs を入れる。progress(段階名, 0〜1, 説明) が呼ばれる。

    失敗したら例外を投げる。途中で cancel が立てられたら中断する。
    """
    hardware = separator.hardware_probe()
    if not hardware.eligible:
        raise RuntimeError(hardware.reason or "この PC では使えません")

    python, problem = find_python()
    if python is None:
        raise RuntimeError(problem)

    os.makedirs(target_dir(), exist_ok=True)
    cancel = cancel or threading.Event()
    # CUDA 機は専用の配布元から。Apple Silicon は通常の PyPI 版で MPS が使える
    index = CUDA_INDEX if hardware.kind == "cuda" else None

    steps = [("torch を取得中", ["torch"], index), ("demucs を取得中", ["demucs"], None)]
    for number, (label, packages, step_index) in enumerate(steps):
        if cancel.is_set():
            raise RuntimeError("中止しました")
        _run_pip(_pip_command(python, packages, step_index), label, progress, cancel,
                 base=number / len(steps), span=1 / len(steps))

    if not installed():
        raise RuntimeError("入れ終わりましたが、torch が見つかりません")

    if target_dir() not in sys.path:
        sys.path.insert(0, target_dir())
    separator.capability(refresh=True)  # 判定を取り直す
    if progress:
        progress("完了", 1.0, f"{installed_size_mb():.0f} MB")


def _run_pip(command: list[str], label: str, progress, cancel: threading.Event,
             base: float, span: float) -> None:
    flags = subprocess.CREATE_NO_WINDOW if platform_support.WINDOWS else 0
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )
    tail: list[str] = []

    def read_output() -> None:
        for line in process.stdout:
            line = line.strip()
            if line:
                tail.append(line)
                del tail[:-5]

    threading.Thread(target=read_output, daemon=True).start()

    start_size = installed_size_mb()
    while process.poll() is None:
        if cancel.is_set():
            process.terminate()
            raise RuntimeError("中止しました")
        if progress:
            grown = max(0.0, installed_size_mb() - start_size)
            ratio = base + span * min(1.0, grown / (APPROX_TOTAL_MB * span))
            progress(label, min(0.99, ratio), f"{grown:.0f} MB 取得済み")
        time.sleep(1.0)

    if process.returncode != 0:
        raise RuntimeError(f"{label} に失敗しました: {' / '.join(tail[-3:]) or '原因不明'}")


def install_rnnoise(progress=None) -> None:
    """RNNoise（音声向けノイズ除去）の部品を入れる。約 15 MB。

    pyrnnoise は音声ファイル読み書き用に重い依存を持つが、こちらが使うのは
    同梱の ctypes バインディングだけなので --no-deps で入れる。
    依存を入れると現行の PyAV と衝突して壊れる。
    """
    python, problem = find_python()
    if python is None:
        raise RuntimeError(problem)
    os.makedirs(target_dir(), exist_ok=True)
    command = [python, "-m", "pip", "install", "--upgrade", "--no-deps",
               "--target", target_dir(), "--disable-pip-version-check", "pyrnnoise"]
    _run_pip(command, "RNNoise を取得中", progress, threading.Event(), base=0.0, span=1.0)
    platform_support.use_runtime_dir()
    # 「入っていない」と一度でも判定していると、その結果を覚えている。
    # 捨てておかないと、取得できたのに起動し直すまで使えない
    import mic_chain

    mic_chain.RNNoiseDenoiser.reset()


def uninstall() -> int:
    """入れたものを消す。消した容量（MB）を返す。"""
    size = installed_size_mb()
    root = target_dir()
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    separator.capability(refresh=True)
    return int(size)

"""キー変更（移調）。曲を変換してから再生する。

pedalboard の PitchShift は、ブロックに分けて渡す使い方（reset=False）だと
出力が全て無音になる（32768 サンプル以下のどのブロック長でも実測 0）。
一括なら正しく動く。

ただし 1 本で通すと 4 分の曲に 13 秒かかり、キーを変えるたびに待たされる。
PitchShift は C++ 側で GIL を手放すので、曲を分けて同時に走らせられる。
分けた所で音が途切れないよう、前後に 1 秒の助走を付けて変換し、
30ms だけ重ねて直線で溶かす（等パワーだと重なりで 1.16 倍に膨らむ。実測）。
4 分の曲で 13 秒 → 2.2 秒になり、継ぎ目の段差は曲全体の背景以下に収まる。

結果は残しておき、同じ曲・同じキーなら次から待たない。
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import platform_support
from separator import decode_audio, write_wav

CACHE_DIR = os.path.join(platform_support.cache_dir(), "pitch")
RATE = 48000
CHANNELS = 2

# 分けた塊の前後に付ける助走。ここは捨てるので、変換器が立ち上がるまでの
# 分を吸収できる長さがあればいい
PAD = int(RATE * 1.0)
# 隣の塊と重ねて溶かす長さ
FADE = int(RATE * 0.03)
# これより短い曲は分けない（分ける手間の方が大きい）
MIN_SPLIT = int(RATE * 20)


def cache_path(source: str, semitones: float) -> str:
    """同じ曲・同じキーなら作り直さないよう、内容から決まる名前にする。"""
    try:
        stat = os.stat(source)
        key = f"{os.path.abspath(source)}|{stat.st_size}|{int(stat.st_mtime)}|{semitones}"
    except OSError:
        key = f"{source}|{semitones}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    stem = os.path.splitext(os.path.basename(source))[0][:40]
    sign = f"{int(semitones):+d}".replace("+", "p").replace("-", "m")
    return os.path.join(CACHE_DIR, f"{stem}_key{sign}_{digest}.wav")


def render(source: str, semitones: float, progress=None) -> str:
    """キーを変えた音声を作り、そのファイルパスを返す。

    progress(段階名, 0〜1) が呼ばれる。作成済みならすぐに返す。
    """
    if abs(semitones) < 0.01:
        return source

    output = cache_path(source, semitones)
    if os.path.exists(output):
        if progress:
            progress("作成済みのものを使います", 1.0)
        return output

    if progress:
        progress("音声を読み込み中", 0.1)
    audio = decode_audio(source, RATE, CHANNELS)  # (チャンネル, サンプル)

    if progress:
        progress(f"キーを {semitones:+.0f} に変換中", 0.3)
    shifted = _shift(audio.T.astype(np.float32), float(semitones), progress)

    if progress:
        progress("書き出し中", 0.9)
    os.makedirs(CACHE_DIR, exist_ok=True)
    write_wav(output, shifted.T, RATE)
    if progress:
        progress("完成", 1.0)
    return output


def _shift(audio: np.ndarray, semitones: float, progress=None) -> np.ndarray:
    """(サンプル, チャンネル) の音を移調する。長い曲は分けて同時に走らせる。"""
    from pedalboard import PitchShift

    def whole(block):
        out = np.asarray(PitchShift(semitones=semitones)(block, RATE, reset=True),
                         dtype=np.float32)
        return out[:, None] if out.ndim == 1 else out

    workers = min(8, max(1, (os.cpu_count() or 2) - 1))
    if len(audio) < MIN_SPLIT or workers < 2:
        return whole(audio)

    bounds = [int(x) for x in np.linspace(0, len(audio), workers + 1)]
    ranges = [(bounds[i], bounds[i + 1]) for i in range(workers)]

    def render(args):
        start, end = args
        head = FADE if start > 0 else 0  # 前の塊と重ねる分
        low = max(0, start - head - PAD)
        high = min(len(audio), end + PAD)
        piece = whole(audio[low:high])
        begin = (start - head) - low
        return start, head, piece[begin:begin + (end - start + head)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        pieces = list(pool.map(render, ranges))

    out = np.zeros((len(audio), audio.shape[1] if audio.ndim > 1 else 1),
                   dtype=np.float32)
    for index, (start, head, piece) in enumerate(pieces):
        if head:
            # 重なりは相関しているので直線で溶かす。等パワーだと膨らむ
            ramp = np.linspace(0.0, 1.0, head, dtype=np.float32)[:, None]
            out[start - head:start] = out[start - head:start] * (1 - ramp) \
                + piece[:head] * ramp
            body = piece[head:]
        else:
            body = piece
        out[start:start + len(body)] = body
        if progress:
            progress(f"キーを {semitones:+.0f} に変換中",
                     0.3 + 0.55 * (index + 1) / len(pieces))
    return out


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

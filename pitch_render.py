"""キー変更（移調）。曲まるごと変換してから再生する。

pedalboard の PitchShift は、ブロックに分けて渡す使い方（reset=False）だと
出力が全て無音になる（32768 サンプル以下のどのブロック長でも実測 0）。
一括なら正しく動き、4 分の曲を 10 秒ほどで処理できる。

まるごと変換する形にすると、継ぎ目が 1 つも無いので
ブロック境界のぷつぷつや音の重なりも起きない。
結果は残しておき、同じ曲・同じキーなら次から待たない。
"""

from __future__ import annotations

import hashlib
import os

import numpy as np

import platform_support
from separator import decode_audio, write_wav

CACHE_DIR = os.path.join(platform_support.cache_dir(), "pitch")
RATE = 48000
CHANNELS = 2


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

    from pedalboard import PitchShift

    if progress:
        progress("音声を読み込み中", 0.1)
    audio = decode_audio(source, RATE, CHANNELS)  # (チャンネル, サンプル)

    if progress:
        progress(f"キーを {semitones:+.0f} に変換中", 0.3)
    shifted = PitchShift(semitones=float(semitones))(
        audio.T.astype(np.float32), RATE, reset=True)
    shifted = np.asarray(shifted, dtype=np.float32)
    if shifted.ndim == 1:
        shifted = shifted[:, None]

    if progress:
        progress("書き出し中", 0.9)
    os.makedirs(CACHE_DIR, exist_ok=True)
    write_wav(output, shifted.T, RATE)
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

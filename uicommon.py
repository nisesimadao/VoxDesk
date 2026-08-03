"""画面まわりで共通に使う定数と小物。

画面の作り（Tk か wx か）に依らないものだけを置く。ここに音声や再生の
処理は書かない。
"""

from __future__ import annotations

import os
import sys

import numpy as np

APP_TITLE = "VoxDesk"

PRESETS = {
    "カラオケマイク（有線・直挿し）": dict(
        input_gain_db=24.0, highpass_hz=110.0, hum_hz=50.0, hum_notch_db=-12.0,
        denoise=True, denoise_strength=1.8, gate_db=-42.0,
        comp_threshold_db=-24.0, comp_ratio=4.0, makeup_db=8.0, reverb_wet=0.12,
    ),
    "USBマイク・ヘッドセット": dict(
        input_gain_db=6.0, highpass_hz=80.0, hum_hz=0.0, hum_notch_db=0.0,
        denoise=True, denoise_strength=1.2, gate_db=-52.0,
        comp_threshold_db=-22.0, comp_ratio=3.0, makeup_db=3.0, reverb_wet=0.08,
    ),
    "オーディオインターフェース": dict(
        input_gain_db=0.0, highpass_hz=80.0, hum_hz=0.0, hum_notch_db=0.0,
        denoise=False, denoise_strength=1.0, gate_db=-60.0,
        comp_threshold_db=-20.0, comp_ratio=2.5, makeup_db=0.0, reverb_wet=0.10,
    ),
    "加工しない": dict(
        input_gain_db=0.0, highpass_hz=20.0, hum_hz=0.0, hum_notch_db=0.0,
        denoise=False, denoise_strength=1.0, gate_db=-100.0,
        comp_threshold_db=0.0, comp_ratio=1.0, makeup_db=0.0, reverb_wet=0.0,
    ),
}

# ノイズ除去の強さ。0 で無効、負の値はマイク側（RTX Voice など）に任せる印
AI_MIC_MODE = "マイク側で処理（RTX Voice / Krisp など）"
RNNOISE_MODE = "AI（RNNoise・話し声向け）"
# 「low / high」のままだと何のことか分からないので、日本語で選ばせる
LATENCY_MODES = {
    "少なめ（おすすめ）": "low",
    "多め（途切れるとき）": "high",
}

# 入力の使い方。(チャンネル数, 何本目から使うか)
INPUT_MODES = {
    "モノラル（1 本目）": (1, 0),
    "モノラル（2 本目）": (1, 1),
    "ステレオ（1・2 本目）": (2, 0),
}

DENOISE_MODES = {
    "なし": 0.0,
    "弱め": 0.8,
    "標準": 1.5,
    "強め": 2.5,
    RNNOISE_MODE: -2.0,
    AI_MIC_MODE: -1.0,
}

EFFECT_ROWS = [
    ("input_gain_db", "マイクの音量", -10.0, 40.0, "dB"),
    ("gate_db", "無音カット", -80.0, -20.0, "dB"),
    ("comp_ratio", "音量そろえ", 1.0, 8.0, ":1"),
    ("makeup_db", "仕上げ音量", -6.0, 20.0, "dB"),
    ("reverb_wet", "エコー", 0.0, 0.6, ""),
]


def friendly_error(error: Exception) -> str:
    """英語の例外を、そのまま見せずに日本語の一言にする。"""
    text = str(error)
    lowered = text.lower()
    patterns = [
        ("sign in to confirm", "YouTube 側で確認を求められました。少し時間をおいて試してください。"),
        ("video unavailable", "この動画は再生できません。別の曲を選んでください。"),
        ("private video", "この動画は非公開です。別の曲を選んでください。"),
        ("age", "年齢確認が必要な動画のため再生できません。"),
        ("urlopen error", "ネットにつながっていないようです。接続を確認してください。"),
        ("getaddrinfo", "ネットにつながっていないようです。接続を確認してください。"),
        ("timed out", "接続に時間がかかりすぎました。もう一度試してください。"),
        ("timeout", "接続に時間がかかりすぎました。もう一度試してください。"),
        ("http error 429", "アクセスが多すぎます。少し待ってから試してください。"),
        ("no such file", "ファイルが見つかりませんでした。"),
        ("permission", "ファイルを開く権限がありません。"),
        ("unable to download", "音源を取得できませんでした。別の曲を試してください。"),
        ("unsupported url", "この URL には対応していません。"),
    ]
    for needle, message in patterns:
        if needle in lowered:
            return message
    if len(text) > 120:  # 長い英文をそのまま出しても読めない
        return "うまくいきませんでした。別の曲や設定で試してみてください。"
    return text


def resource_path(*parts: str) -> str:
    """同梱ファイルの場所。凍結ビルドでは展開先が変わる。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def db_of(peak: float) -> float:
    return 20.0 * np.log10(max(peak, 1e-9))


def meter_value(peak: float) -> float:
    """ピーク値を 0〜100 のメーター表示に直す（-60dB を下端とする）。"""
    return float(np.clip((db_of(peak) + 60.0) / 60.0 * 100.0, 0.0, 100.0))


def time_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

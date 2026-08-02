"""設定の保存と読み込み。

デバイスは番号ではなく名前で覚える。番号は機器を挿し直すだけで変わるため、
番号で保存すると次回起動時に別のデバイスに繋がってしまう。
"""

from __future__ import annotations

import json
import os
from typing import Any

import platform_support

APP_DIR = platform_support.app_data_dir()
CONFIG_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS: dict[str, Any] = {
    # 既定の Host API は OS によって違う（Windows は WASAPI、macOS は Core Audio）
    "hostapi": platform_support.default_host_api() or "すべて",
    "mic_device_name": "",
    "output_device_name": "",
    "latency": "low",
    "buffer_ms": 25.0,  # 小さいほど遅延が減る。途切れるときは上げる
    "mic_enabled": True,
    "effects": {
        "input_gain_db": 24.0,
        "highpass_hz": 110.0,
        "hum_hz": 50.0,
        "hum_notch_db": -12.0,
        "denoise": True,
        "denoise_strength": 1.8,
        "gate_db": -42.0,
        "comp_threshold_db": -24.0,
        "comp_ratio": 4.0,
        "makeup_db": 8.0,
        "reverb_wet": 0.12,
    },
    "music_volume": 0.8,
    "mic_volume": 1.0,
    "video_quality": 720,
    "prefer_off_vocal": True,
    "trusted_only": False,
    "last_folder": "",
    "vst3": [],
    "first_run": True,
}


def load() -> dict[str, Any]:
    data = json.loads(json.dumps(DEFAULTS))  # 深いコピー
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return data
    for key, value in saved.items():
        if key == "effects" and isinstance(value, dict):
            data["effects"].update(value)
        else:
            data[key] = value
    return data


def save(data: dict[str, Any]) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)  # 書き込み中に落ちても壊れないように
    except OSError:
        pass  # 設定が保存できなくても動作は続ける

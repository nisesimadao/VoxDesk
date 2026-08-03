"""OS ごとの違いをここに集める。

設定の置き場所、音声 Host API の既定、プラグインの探索先、画面の見た目は
Windows / macOS / Linux でそれぞれ違う。各モジュールに散らすと
片方の OS でしか動かない箇所が増えるので、この 1 ファイルに寄せている。
"""

from __future__ import annotations

import os
import sys

WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")

APP_NAME = "VoxDesk"


# ---------- 置き場所 ----------
def app_data_dir() -> str:
    """設定を置く場所。"""
    if WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif MACOS:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def cache_dir() -> str:
    """作り直せるもの（ボーカル除去の結果など）を置く場所。"""
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif MACOS:
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP_NAME)


# ---------- 音声 ----------
def host_api_names() -> list[str]:
    """この PC で使える Host API の名前。"""
    try:
        import sounddevice as sd
        return [api["name"] for api in sd.query_hostapis()]
    except Exception:
        return []


def default_host_api() -> str | None:
    """既定で選ぶ Host API。None なら絞り込まない。

    Windows は WASAPI が低遅延で素直。macOS は Core Audio しかない。
    Linux は ALSA / JACK / OSS が混在するので、あれば ALSA を既定にする。
    """
    available = host_api_names()
    if not available:
        return None
    preferred = (
        ["Windows WASAPI", "Windows DirectSound", "MME"] if WINDOWS
        else ["Core Audio", "CoreAudio"] if MACOS
        else ["ALSA", "JACK Audio Connection Kit"]
    )
    for name in preferred:
        if name in available:
            return name
    return None


def host_api_hint() -> str:
    """Host API 選択欄に出す補足。"""
    if WINDOWS:
        return "WASAPI が低遅延。うまく動かない機器は MME を試す"
    if MACOS:
        return "macOS は Core Audio のみ。複数機器をまとめるなら「Audio MIDI 設定」で装置セットを作る"
    return "ALSA が基本。JACK があれば低遅延にできる"


# ---------- プラグイン ----------
def plugin_dirs() -> list[str]:
    """VST3（macOS では Audio Unit も）の探索先。"""
    if WINDOWS:
        return [
            r"C:\Program Files\Common Files\VST3",
            r"C:\Program Files (x86)\Common Files\VST3",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Common\VST3"),
        ]
    if MACOS:
        return [
            "/Library/Audio/Plug-Ins/VST3",
            os.path.expanduser("~/Library/Audio/Plug-Ins/VST3"),
            "/Library/Audio/Plug-Ins/Components",
            os.path.expanduser("~/Library/Audio/Plug-Ins/Components"),
        ]
    return [
        os.path.expanduser("~/.vst3"),
        "/usr/lib/vst3",
        "/usr/local/lib/vst3",
        "/usr/lib64/vst3",
    ]


def plugin_extensions() -> tuple[str, ...]:
    """読み込めるプラグインの拡張子。macOS は Audio Unit も扱える。"""
    return (".vst3", ".component") if MACOS else (".vst3",)


# ---------- 画面 ----------
def apply_theme(style) -> str:
    """その OS で自然に見えるテーマを選ぶ。"""
    available = style.theme_names()
    for name in (["vista", "winnative"] if WINDOWS
                 else ["aqua"] if MACOS
                 else ["clam", "alt"]):
        if name in available:
            style.theme_use(name)
            return name
    return style.theme_use()


def ui_font(root, size: int = 10) -> tuple[str, int]:
    """日本語が表示できるフォントを、入っているものから選ぶ。"""
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    candidates = (
        ["Meiryo UI", "Yu Gothic UI", "MS UI Gothic"] if WINDOWS
        else ["Hiragino Sans", "Hiragino Kaku Gothic ProN", "Helvetica Neue"] if MACOS
        else ["Noto Sans CJK JP", "IPAexGothic", "TakaoPGothic", "DejaVu Sans"]
    )
    for name in candidates:
        if name in families:
            return (name, size)
    return ("TkDefaultFont", size)


def alternate_api_hint() -> str:
    """デバイスが開けないときに案内する代替手段。"""
    if WINDOWS:
        return "別の Host API（MME など）を試してください"
    if MACOS:
        return "「Audio MIDI 設定」でフォーマットを確認してください"
    return "別の Host API（ALSA / JACK）を試してください"


def sound_settings_hint() -> str:
    """OS の音声設定でレベルを確かめる方法。"""
    if WINDOWS:
        return "Windows の「サウンド」でこのデバイスのレベルメーターが振れるか"
    if MACOS:
        return "「システム設定 → サウンド → 入力」で入力レベルが振れるか"
    return "pavucontrol や alsamixer で入力レベルが振れるか"


def launcher_hint() -> str:
    """起動方法の案内（エラー時などに出す）。"""
    if WINDOWS:
        return "VoxDesk を起動.bat をダブルクリックしてください"
    return "./start.sh を実行してください"


# あとから入れた部品（RNNoise / torch / demucs）の置き場。
# インストーラ版には site-packages が無いので、書き込める場所へ入れて
# 読み込み先に足す。ここは全部のモジュールが読むので、1 か所で済ませる。
RUNTIME_DIR = os.path.join(app_data_dir(), "runtime")


def use_runtime_dir() -> str:
    """取得した部品の置き場を、読み込み先に入れる（何度呼んでも安全）。"""
    if os.path.isdir(RUNTIME_DIR) and RUNTIME_DIR not in sys.path:
        sys.path.insert(0, RUNTIME_DIR)
    return RUNTIME_DIR


use_runtime_dir()

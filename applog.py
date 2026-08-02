"""記録（ログ）の設定。

画面には分かりやすい日本語だけを出すが、それだけだと
「動きません」と言われたときに手がかりが何も残らない。
本当のエラーはファイルに残しておき、あとから追えるようにする。
"""

from __future__ import annotations

import faulthandler
import logging
import os

import platform_support

LOG_PATH = os.path.join(platform_support.app_data_dir(), "voxdesk.log")
CRASH_PATH = os.path.join(platform_support.app_data_dir(), "crash.log")
MAX_BYTES = 1_000_000

_ready = False
_crash_file = None


def setup() -> str:
    """記録の準備をして、保存先を返す。二度呼んでも安全。"""
    global _ready
    if _ready:
        return LOG_PATH
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        # 大きくなりすぎたら 1 度だけ作り直す（凝った仕組みは持たない）
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"),
                      logging.StreamHandler()],
        )
    except Exception:
        logging.basicConfig(level=logging.INFO)  # 書けなくても動作は続ける

    # 音声や動画の土台は C で書かれているため、そこで落ちると Python の
    # 記録には何も残らない。落ちた瞬間の全スレッドの居場所を別ファイルに残す。
    global _crash_file
    try:
        _crash_file = open(CRASH_PATH, "a", encoding="utf-8", buffering=1)
        _crash_file.write(f"\n===== 起動 {os.getpid()} =====\n")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception:
        pass

    _ready = True
    return LOG_PATH


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)

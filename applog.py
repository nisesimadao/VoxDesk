"""記録（ログ）の設定。

画面には分かりやすい日本語だけを出すが、それだけだと
「動きません」と言われたときに手がかりが何も残らない。
本当のエラーはファイルに残しておき、あとから追えるようにする。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading

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
        handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
        # インストーラ版には端末が無く sys.stderr が None になる。
        # そこへ出そうとすると記録そのものが壊れるので、あるときだけ足す。
        if sys.stderr is not None:
            handlers.append(logging.StreamHandler())
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            handlers=handlers,
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
    _install_handlers()
    logging.getLogger("voxdesk").info("===== 起動 pid=%s =====", os.getpid())
    return LOG_PATH


def _install_handlers() -> None:
    """取りこぼしていた経路の例外も、必ず記録に残す。

    インストーラ版には端末が無いので、標準エラーへ出しても消えてしまう。
    「急に落ちた」と言われたときに手がかりが無くなるため、全部ここへ集める。
    """
    log = logging.getLogger("voxdesk")

    def on_exception(exc_type, exc, tb):
        log.error("拾われなかった例外", exc_info=(exc_type, exc, tb))

    sys.excepthook = on_exception

    def on_thread_exception(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        log.error("スレッド %s で拾われなかった例外", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = on_thread_exception


def closing() -> None:
    """正常に終了したことを残す。

    この行が無いまま次の「起動」が来ていたら、その回は落ちたと分かる。
    """
    logging.getLogger("voxdesk").info("===== 正常に終了 =====")


def get(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)

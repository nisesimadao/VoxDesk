"""ワーカースレッドから音声デバイスを開くための COM 初期化。

Windows の WASAPI は COM を使うため、デバイスを開くスレッドごとに
CoInitializeEx を呼ぶ必要がある。これを怠ると Pa_StartStream が
"Unanticipated host error [-9999]" で失敗する（原因が分かりにくい）。
"""

from __future__ import annotations

import contextlib
import ctypes
import sys

COINIT_MULTITHREADED = 0x0
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -2147417850  # 別のモードで初期化済み。解放してはいけない


@contextlib.contextmanager
def com_initialized():
    """このスレッドで COM を初期化する。Windows 以外では何もしない。"""
    if sys.platform != "win32":
        yield
        return
    hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr >= 0x80000000:
        hr -= 0x100000000  # 符号付きに直す
    owned = hr in (_S_OK, _S_FALSE)
    try:
        yield
    finally:
        if owned:
            ctypes.windll.ole32.CoUninitialize()

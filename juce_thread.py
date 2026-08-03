"""VST3（JUCE）の面倒を見る専用スレッド。

pedalboard は VST3 の読み込みや走査を「JUCE のメッセージスレッド」で
行うことを求める。これは OS のメインスレッドである必要はなく、
最初に JUCE を起こしたスレッドがそのままメッセージスレッドになる。

読み込みには 1 個あたり 0.5 秒ほどかかるため、画面のスレッドで行うと
その間ずっと固まる。そこで専用のスレッドを先に立てて、JUCE に触る操作を
すべてそこへ回す。画面側は待たされない。

「すべて」というのが要点で、1 か所でも直接呼ぶとそのスレッドが
メッセージスレッドになってしまい、以後こちらのスレッドからは読めなくなる。
"""

from __future__ import annotations

import queue
import threading

_jobs: "queue.Queue[tuple]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()
_pending = 0


def busy() -> bool:
    """今このスレッドが仕事を抱えているか。

    読み込み中に画面側から run() を呼ぶと、その分だけ画面が待たされる。
    急がない用事（つまみの表示更新など）は、これを見て見送る。
    """
    return _pending > 0


def _loop() -> None:
    global _pending
    while True:
        func, result = _jobs.get()
        try:
            result.put(("ok", func()))
        except BaseException as e:  # 例外も呼び出し元へそのまま返す
            result.put(("ng", e))
        finally:
            with _lock:
                _pending -= 1


def _ensure() -> threading.Thread:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="juce", daemon=True)
            _thread.start()
        return _thread


def run(func, timeout: float = 180.0):
    """JUCE 用のスレッドで実行して、結果を返す。例外はそのまま送り返す。"""
    global _pending
    thread = _ensure()
    if threading.current_thread() is thread:
        return func()  # すでに JUCE スレッド上（入れ子）
    result: "queue.Queue[tuple]" = queue.Queue()
    with _lock:
        _pending += 1
    _jobs.put((func, result))
    kind, value = result.get(timeout=timeout)
    if kind == "ng":
        raise value
    return value


def dispose(holder: list) -> None:
    """プラグインの後始末を JUCE スレッドで行う。

    VST3 のデストラクタもメッセージスレッドを触るため、他のスレッドで
    最後の参照が消えると落ちることがある。渡すのは「そのプラグインへの
    最後の参照だけが入ったリスト」で、中身を捨てる操作をこちらで行う。
    """
    try:
        run(holder.clear, timeout=30.0)
    except Exception:
        holder.clear()

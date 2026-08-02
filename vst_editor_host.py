"""VST3 のエディタを別プロセスで開くための子プロセス。

pedalboard のエディタはメインスレッドからしか開けず、閉じるまで戻ってこない。
本体プロセスで開くとアプリ全体が固まってしまうので、この小さなプロセスに任せ、
つまみの変化だけを本体へ送り返す。

やりとりは標準入出力の JSON 行:
    親 -> 子   {"cmd": "init", "params": {...}}   最初の状態を反映させる
              {"cmd": "set", "params": {...}}    本体側のスライダー操作を反映
              {"cmd": "close"}                   エディタを閉じさせる
    子 -> 親   {"type": "ready"}                  エディタを開いた
              {"type": "params", "values": {...}} つまみが動いた（変化分のみ）
              {"type": "closed"}                  エディタが閉じられた
              {"type": "error", "message": ...}
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
import time

POLL_SECONDS = 0.05  # つまみの変化を見に行く間隔

# pedalboard が作るプラグインウィンドウには枠が付かず、移動も最小化もできない
#（spotify/pedalboard の issue #386）。Windows では後から枠を付けられるので、
# ウィンドウが出たところでスタイルを足してタイトルも入れる。
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
GWL_STYLE = -16
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020


def _decorate_windows(title: str, offset: int) -> None:
    """自プロセスのプラグインウィンドウに枠とタイトルを付ける（Windows のみ）。"""
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(collect, None)
    for hwnd in found:
        try:
            style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
            user32.SetWindowLongPtrW(
                hwnd, GWL_STYLE, style | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX)
            user32.SetWindowTextW(hwnd, title)
            # 複数開いたときに左上で重ならないよう、少しずつずらす
            user32.SetWindowPos(hwnd, None, 80 + offset * 40, 60 + offset * 40, 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            continue


def window_styles() -> list[int]:
    """検証用。自プロセスの可視ウィンドウのスタイル値を返す。"""
    if sys.platform != "win32":
        return []
    user32 = ctypes.windll.user32
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    styles: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def collect(hwnd, _lparam):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            styles.append(user32.GetWindowLongPtrW(hwnd, GWL_STYLE))
        return True

    user32.EnumWindows(collect, None)
    return styles


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_params(plugin) -> dict:
    values = {}
    for key, param in plugin.parameters.items():
        try:
            values[key] = float(param.raw_value)
        except Exception:
            continue
    return values


def apply_params(plugin, values: dict) -> None:
    for key, value in (values or {}).items():
        param = plugin.parameters.get(key)
        if param is None:
            continue
        try:
            param.raw_value = float(value)
        except Exception:
            continue


def main() -> int:
    if len(sys.argv) < 2:
        send({"type": "error", "message": "プラグインのパスが指定されていません"})
        return 2

    try:
        from pedalboard import load_plugin
        plugin = load_plugin(sys.argv[1])
    except Exception as e:
        send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        return 1

    close_event = threading.Event()
    lock = threading.Lock()

    def handle_commands() -> None:
        """親からの指示を待ち受ける。"""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            command = message.get("cmd")
            if command == "close":
                close_event.set()
                return
            if command in ("init", "set"):
                with lock:
                    apply_params(plugin, message.get("params"))
        close_event.set()  # 親が終了した

    def watch_params() -> None:
        """つまみが動いたら親へ知らせる。"""
        with lock:
            previous = read_params(plugin)
        while not close_event.is_set():
            time.sleep(POLL_SECONDS)
            with lock:
                current = read_params(plugin)
            changed = {k: v for k, v in current.items()
                       if abs(v - previous.get(k, -1.0)) > 1e-5}
            if changed:
                previous = current
                send({"type": "params", "values": changed})

    title = sys.argv[2] if len(sys.argv) > 2 else "プラグイン"
    offset = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    def decorate_when_shown() -> None:
        """ウィンドウが出たら枠を付ける。出るまで少し待つ必要がある。"""
        for _ in range(50):
            if close_event.is_set():
                return
            time.sleep(0.1)
            if window_styles():
                _decorate_windows(title, offset)
                send({"type": "decorated", "styles": window_styles()})
                return

    threading.Thread(target=handle_commands, daemon=True).start()
    threading.Thread(target=watch_params, daemon=True).start()
    threading.Thread(target=decorate_when_shown, daemon=True).start()

    send({"type": "ready", "count": len(plugin.parameters)})
    try:
        # ここでメインスレッドが止まる。閉じられるか close_event が立つまで戻らない
        plugin.show_editor(close_event)
    except Exception as e:
        send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        return 1

    close_event.set()
    with lock:
        send({"type": "closed", "values": read_params(plugin)})
    return 0


if __name__ == "__main__":
    sys.exit(main())

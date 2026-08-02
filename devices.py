"""デバイスの一覧・選択・診断。

PortAudio の番号は機器を挿し直すと変わるため、外向きには名前を使う。
また Windows 側の状態（有効/ミュート/レベル）も見て、
「アプリからは開けるのに音が来ない」といった状況を説明できるようにする。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

import platform_support
from comutil import com_initialized


@dataclass
class Device:
    index: int
    name: str
    hostapi: str
    channels: int
    rate: int
    is_input: bool
    is_default: bool = False

    @property
    def label(self) -> str:
        mark = " ★" if self.is_default else ""
        return f"{self.name}  [{self.hostapi}]{mark}"


def refresh() -> None:
    """PortAudio を初期化し直して、挿し直した機器を認識させる。"""
    try:
        sd._terminate()
    except Exception:
        pass
    sd._initialize()


def _default_name(kind: str) -> str:
    """Windows の既定デバイス名。PortAudio の既定番号は MME のものなので、
    WASAPI などで絞り込むと一致しなくなる。そこで名前で照合できるようにする。"""
    try:
        index = sd.default.device[0 if kind == "input" else 1]
        if index is None or index < 0:
            return ""
        return str(sd.query_devices(index)["name"])
    except Exception:
        return ""


def list_devices(kind: str, hostapi: str | None = None) -> list[Device]:
    """kind は 'input' か 'output'。hostapi が None なら全部返す。"""
    field = "max_input_channels" if kind == "input" else "max_output_channels"
    apis = sd.query_hostapis()
    default = sd.default.device[0 if kind == "input" else 1]
    default_name = _default_name(kind)
    out = []
    for index, info in enumerate(sd.query_devices()):
        if info[field] < 1:
            continue
        api = apis[info["hostapi"]]["name"]
        if hostapi and api != hostapi:
            continue
        out.append(
            Device(
                index=index,
                name=info["name"],
                hostapi=api,
                channels=info[field],
                rate=int(info["default_samplerate"]),
                is_input=(kind == "input"),
                is_default=(index == default
                            or (bool(default_name) and info["name"][:20] == default_name[:20])),
            )
        )
    return out


def find_by_name(name: str, kind: str, hostapi: str | None = None) -> Device | None:
    """名前でデバイスを探す。完全一致 → 前方一致 → 部分一致の順に緩める。"""
    if not name:
        return None
    candidates = list_devices(kind, hostapi)
    for match in (
        lambda d: d.name == name,
        lambda d: d.name.startswith(name[:20]),
        lambda d: name[:12].lower() in d.name.lower(),
    ):
        hit = [d for d in candidates if match(d)]
        if hit:
            return hit[0]
    return None


def default_device(kind: str, hostapi: str | None = None) -> Device | None:
    devices = list_devices(kind, hostapi)
    if not devices:
        return None
    for d in devices:
        if d.is_default:
            return d
    return devices[0]


@dataclass
class Health:
    """デバイスを実際に開いて確かめた結果。"""

    ok: bool
    detail: str
    receives_audio: bool | None = None  # 入力のみ。None は未確認
    peak_db: float | None = None

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"× {self.detail}"
        if self.receives_audio is False:
            return "△ 開けるが音が来ない（機器側の設定を確認）"
        if self.peak_db is not None and self.peak_db < -75:
            return f"△ 使えるが極端に小さい（ピーク {self.peak_db:.0f} dBFS）"
        return f"○ {self.detail}"


def check(device: Device, seconds: float = 1.2, timeout: float = 6.0) -> Health:
    """開けるか、そして入力なら本当にデータが来るかまで確かめる。

    応答しないドライバがあるので、必ず別スレッドで試して時間を区切る。
    """
    result: list[Health] = []

    def attempt():
        with com_initialized():
            try:
                if device.is_input:
                    result.append(_check_input(device, seconds))
                else:
                    stream = sd.OutputStream(
                        device=device.index, samplerate=device.rate,
                        channels=min(2, device.channels), dtype="float32",
                        callback=lambda *a: None,
                    )
                    stream.start()
                    time.sleep(0.2)
                    stream.stop()
                    stream.close()
                    result.append(Health(True, f"{device.rate}Hz で使えます"))
            except Exception as e:
                result.append(Health(False, describe_error(e)))

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(timeout)
    if not result:
        return Health(False, "応答なし（このドライバでは使えません）")
    return result[0]


def _check_input(device: Device, seconds: float) -> Health:
    stats = {"frames": 0, "peak": 0.0}

    def callback(indata, frames, time_info, status):
        stats["frames"] += frames
        peak = float(np.abs(indata).max())
        if peak > stats["peak"]:
            stats["peak"] = peak

    stream = sd.InputStream(
        device=device.index, samplerate=device.rate,
        channels=min(2, device.channels), dtype="float32", callback=callback,
    )
    stream.start()
    time.sleep(seconds)
    stream.stop()
    stream.close()

    if stats["frames"] == 0:
        return Health(True, "データが届きません", receives_audio=False)
    peak_db = 20 * np.log10(max(stats["peak"], 1e-9))
    return Health(True, f"{device.rate}Hz で使えます", receives_audio=True, peak_db=peak_db)


# マイク側で AI ノイズ除去をしてくれる仮想マイク。入っていれば選ぶだけで使える。
AI_MICROPHONES = ("RTX Voice", "NVIDIA Broadcast", "Krisp", "Voice Isolation",
                  "SteelSeries Sonar", "Elgato Wave")


def ai_microphones(hostapi: str | None = None) -> list[Device]:
    """AI ノイズ除去つきの仮想マイクを探す。"""
    return [d for d in list_devices("input", hostapi)
            if any(name.lower() in d.name.lower() for name in AI_MICROPHONES)]


def describe_error(e: Exception) -> str:
    """PortAudio のエラーを利用者向けの文言にする。"""
    if isinstance(e, UnicodeDecodeError):
        # 日本語 Windows では PortAudio のエラー文字列が CP932 で返るため、
        # sounddevice の UTF-8 復号が失敗して本来の理由が読めなくなる
        return "ドライバがエラーを返しました"
    text = str(e)
    if "Invalid sample rate" in text:
        return "このサンプルレートに対応していません"
    if "Device unavailable" in text or "-9985" in text:
        return "他のアプリが使用中です"
    if "Invalid device" in text:
        return "このデバイスは選べません"
    if "Unanticipated host error" in text:
        return "ドライバが応答しませんでした"
    return f"{type(e).__name__}"


def system_status() -> dict[str, dict]:
    """OS 側の有効/ミュート/レベルを名前ごとに返す。取得できなければ空。

    今のところ Windows でのみ取得できる（pycaw 経由）。他の OS では空を返し、
    「OS 側の設定」の欄が出ないだけで、他の診断はそのまま動く。
    """
    if not platform_support.WINDOWS:
        return {}
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except Exception:
        return {}

    status: dict[str, dict] = {}
    try:
        with com_initialized():
            for dev in AudioUtilities.GetAllDevices():
                name = dev.FriendlyName
                if not name:
                    continue
                state = dev.state.value if hasattr(dev.state, "value") else dev.state
                entry = {"active": state == 1, "muted": None, "level": None}
                try:
                    volume = dev._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                    iface = volume.QueryInterface(IAudioEndpointVolume)
                    entry["muted"] = bool(iface.GetMute())
                    entry["level"] = float(iface.GetMasterVolumeLevelScalar())
                except Exception:
                    pass
                status[name] = entry
    except Exception:
        return {}
    return status


def system_hint(device: Device, status: dict[str, dict] | None = None) -> str:
    """OS 側の設定で問題がありそうなら、その説明を返す。"""
    status = status if status is not None else system_status()
    if not status:
        return ""
    label = "Windows" if platform_support.WINDOWS else "OS"
    # PortAudio 名は "マイク (機器名)" のように OS 側の名前と一致することが多いが、
    # 31 文字で切られる API もあるため前方一致で照合する
    for name, entry in status.items():
        if name == device.name or name.startswith(device.name[:28]) or \
                device.name.startswith(name[:28]):
            if not entry["active"]:
                return f"{label} でこのデバイスは「未接続」です"
            if entry["muted"]:
                return f"{label} でミュートされています"
            if entry["level"] is not None and entry["level"] < 0.2:
                return f"{label} の入力レベルが {entry['level']*100:.0f}% と低いです"
            return ""
    return ""

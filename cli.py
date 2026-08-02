r"""コマンドラインからマイクを流す（画面なしで使いたい人向け）。

Windows:        .\.venv\Scripts\python.exe cli.py --list
macOS / Linux:  .venv/bin/python cli.py --list

    cli.py --list                                     デバイス一覧
    cli.py --check                                    全デバイスを診断する
    cli.py --mic Logicool --out JBL                   流す
    cli.py --mic "SE-U33GX" --out JBL --preset karaoke --gain 30

デバイスは名前の一部で指定する（番号は挿し直すと変わるため）。
"""

from __future__ import annotations

import argparse
import sys
import time

import devices as dev
import platform_support
from mic_chain import MicChain
from router import Router

PRESETS = {
    "karaoke": dict(input_gain_db=24.0, highpass_hz=110.0, hum_hz=50.0, hum_notch_db=-12.0,
                    denoise=True, denoise_strength=1.8, gate_db=-42.0,
                    comp_threshold_db=-24.0, comp_ratio=4.0, makeup_db=8.0, reverb_wet=0.12),
    "usb": dict(input_gain_db=6.0, highpass_hz=80.0, hum_hz=0.0, hum_notch_db=0.0,
                denoise=True, denoise_strength=1.2, gate_db=-52.0,
                comp_threshold_db=-22.0, comp_ratio=3.0, makeup_db=3.0, reverb_wet=0.08),
    "flat": dict(input_gain_db=0.0, highpass_hz=20.0, hum_hz=0.0, hum_notch_db=0.0,
                 denoise=False, denoise_strength=1.0, gate_db=-100.0,
                 comp_threshold_db=0.0, comp_ratio=1.0, makeup_db=0.0, reverb_wet=0.0),
}


def print_devices(api: str | None) -> None:
    for kind, title in (("input", "入力"), ("output", "出力")):
        print(f"=== {title} ===")
        for d in dev.list_devices(kind, api):
            mark = "★" if d.is_default else " "
            print(f" {mark} {d.name}  [{d.hostapi}] {d.rate}Hz {d.channels}ch")


def check_devices(api: str | None) -> None:
    status = dev.system_status()
    for kind, title in (("input", "入力"), ("output", "出力")):
        print(f"=== {title} ===")
        for d in dev.list_devices(kind, api):
            health = dev.check(d, seconds=0.8, timeout=5.0)
            hint = dev.system_hint(d, status)
            print(f"  {health.summary:<40} {d.name} [{d.hostapi}]"
                  + (f"  / {hint}" if hint else ""))


def apply_preset(chain: MicChain, name: str) -> None:
    preset = PRESETS[name]
    chain.input_gain.gain_db = preset["input_gain_db"]
    chain.highpass.cutoff_frequency_hz = preset["highpass_hz"]
    if preset["hum_hz"]:
        chain.set_hum_base(preset["hum_hz"])
        chain.hum_notch_db = preset["hum_notch_db"]
    else:
        chain.hum_notch_db = 0.0
    chain.denoise = preset["denoise"]
    chain.denoiser.strength = preset["denoise_strength"]
    chain.gate.threshold_db = preset["gate_db"]
    chain.compressor.threshold_db = preset["comp_threshold_db"]
    chain.compressor.ratio = preset["comp_ratio"]
    chain.makeup.gain_db = preset["makeup_db"]
    chain.reverb.wet_level = preset["reverb_wet"]


def main() -> None:
    p = argparse.ArgumentParser(description="マイクを指定の出力先へ流す")
    p.add_argument("--list", action="store_true", help="デバイス一覧")
    p.add_argument("--check", action="store_true", help="全デバイスを診断")
    default_api = platform_support.default_host_api()
    p.add_argument("--api", default=default_api or "all",
                   help=f"Host API（'all' で全部。既定: {default_api or 'all'}）")
    p.add_argument("--mic", help="入力デバイス名の一部")
    p.add_argument("--out", help="出力デバイス名の一部")
    p.add_argument("--preset", choices=list(PRESETS), default="usb")
    p.add_argument("--gain", type=float, help="マイクの音量(dB)。プリセットより優先")
    p.add_argument("--echo", type=float, help="エコーの量 0〜0.6")
    p.add_argument("--buffer", type=float, default=25.0, help="バッファ(ms)")
    p.add_argument("--latency", default="low", choices=["low", "high"])
    args = p.parse_args()

    api = None if args.api == "all" else args.api

    if args.list:
        print_devices(api)
        return
    if args.check:
        check_devices(api)
        return

    mic = dev.find_by_name(args.mic, "input", api) if args.mic else dev.default_device("input", api)
    out = dev.find_by_name(args.out, "output", api) if args.out else dev.default_device("output", api)
    if mic is None:
        raise SystemExit(f"入力デバイスが見つかりません: {args.mic}（--list で確認）")
    if out is None:
        raise SystemExit(f"出力デバイスが見つかりません: {args.out}（--list で確認）")

    chain = MicChain(mic.rate)
    apply_preset(chain, args.preset)
    if args.gain is not None:
        chain.input_gain.gain_db = args.gain
    if args.echo is not None:
        chain.reverb.wet_level = args.echo

    router = Router(chain=chain, on_state=lambda s, m: print(f"[{s}] {m}"))
    print(f"入力: {mic.name} [{mic.hostapi}] {mic.rate}Hz")
    print(f"出力: {out.name} [{out.hostapi}] {out.rate}Hz")
    print(f"プリセット: {args.preset} / ゲイン {chain.input_gain.gain_db:.0f}dB")
    router.start(mic.index, out.index, latency=args.latency, buffer_ms=args.buffer)

    deadline = time.monotonic() + 10
    while router.state == "opening" and time.monotonic() < deadline:
        time.sleep(0.1)
    if router.state != "running":
        raise SystemExit(f"開始できませんでした: {router.message}")

    print(f"実行中（遅延 {router.latency_ms:.0f} ms）。Ctrl+C で停止します。")
    try:
        while True:
            time.sleep(0.5)
            over, under = router.xruns
            bar = "#" * int(min(1.0, router.in_peak * 4) * 30)
            sys.stdout.write(f"\r  入力 [{bar:<30}] "
                             f"バッファ {router.buffer_ms:5.1f}ms 途切れ {over + under:3d} 回  ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n停止します。")
    finally:
        router.stop()


if __name__ == "__main__":
    main()

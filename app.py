r"""カラオケスタジオ — マイクを流しながら、オフボーカル音源を動画付きで再生する。

起動:
    Windows       カラオケスタジオを起動.bat（または .venv\Scripts\pythonw.exe app.py）
    macOS / Linux ./start.sh（または .venv/bin/python app.py）

構成:
    app.py          この画面
    router.py       マイク → スピーカーの経路（レート変換とドリフト補正つき）
    mic_chain.py    マイクの音作り（ノイズ除去・ゲート・コンプ・エコー・VST3）
    player.py       動画再生（PyAV でデコードし、音は選んだデバイスへ）
    music_search.py YouTube から曲を探す
    devices.py      デバイスの一覧と診断
    platform_support.py  OS ごとの違い（保存先・Host API・フォント・プラグイン探索先）
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading

# プラグイン画面用の子プロセスは、この下の重い読み込み（numpy / 音声 / 動画）を
# 必要としない。インストーラ版では自分自身を起動するため、ここで先に分岐して
# 起動を数秒短くする。
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--vst-editor":
    import vst_editor_host

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    sys.exit(vst_editor_host.main())

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import sounddevice as sd
from PIL import ImageTk

import config
import devices as dev
import music_search
import platform_support
from mic_chain import MicChain, available_vst3, karaoke_preset
from player import AVPlayer
from router import Router

APP_TITLE = "カラオケスタジオ"

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


def db_of(peak: float) -> float:
    return 20.0 * np.log10(max(peak, 1e-9))


def meter_value(peak: float) -> float:
    """ピーク値を 0〜100 のメーター表示に直す（-60dB を下端とする）。"""
    return float(np.clip((db_of(peak) + 60.0) / 60.0 * 100.0, 0.0, 100.0))


def time_text(seconds: float) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


class KaraokeApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x760")
        self.minsize(900, 620)

        self.cfg = config.load()
        self.ui_queue: queue.Queue = queue.Queue()

        self.chain = MicChain(48000)
        self.router = Router(chain=self.chain, on_state=self._on_router_state)
        self.player = AVPlayer(
            on_error=lambda e: self.post(self._player_error, e),
        )

        self.tracks: list[music_search.Track] = []
        self.current_track: music_search.Track | None = None
        self.current_local_path: str | None = None  # ローカル再生中のファイル
        self.separator_capability = None
        self.editors: dict[int, dict] = {}  # 開いているプラグイン画面（別プロセス）
        self._photo: ImageTk.PhotoImage | None = None
        self._seeking = False
        self._busy = 0

        self._build_ui()
        self._apply_effects_from_config()
        self._reload_devices()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(30, self._tick)
        if self.cfg.get("vst3"):
            self.after(200, self._restore_vst)
        self.after(600, self._check_separator)

        if self.cfg.get("first_run"):
            self.after(400, self._first_run)

    # ================= 画面 =================
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        platform_support.apply_theme(style)
        # フォントは OS ごとに入っているものが違う。日本語が豆腐にならないよう
        # 実際に入っている中から選ぶ。
        family, _ = platform_support.ui_font(self)
        self.ui_family = family
        style.configure("Big.TButton", font=(family, 11, "bold"), padding=8)
        style.configure("Head.TLabel", font=(family, 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.karaoke_tab = ttk.Frame(self.notebook, padding=10)
        self.mic_tab = ttk.Frame(self.notebook, padding=10)
        self.vst_tab = ttk.Frame(self.notebook, padding=10)
        self.setup_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.karaoke_tab, text="  カラオケ  ")
        self.notebook.add(self.mic_tab, text="  マイク  ")
        self.notebook.add(self.vst_tab, text="  エフェクト(VST3)  ")
        self.notebook.add(self.setup_tab, text="  設定・診断  ")

        self._build_karaoke_tab()
        self._build_mic_tab()
        self._build_vst_tab()
        self._build_setup_tab()

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=6)
        self.status_label = ttk.Label(bar, text="準備完了", style="Hint.TLabel")
        self.status_label.pack(side="left")
        self.mic_state_label = ttk.Label(bar, text="マイク: 停止中", style="Hint.TLabel")
        self.mic_state_label.pack(side="right")

    # ---------- カラオケタブ ----------
    def _build_karaoke_tab(self) -> None:
        tab = self.karaoke_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        search = ttk.Frame(tab)
        search.grid(row=0, column=0, sticky="ew")
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="曲を探す", style="Head.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.query_var = tk.StringVar()
        entry = ttk.Entry(search, textvariable=self.query_var, font=(self.ui_family, 11))
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<Return>", lambda _e: self.search())
        ttk.Button(search, text="検索", command=self.search).grid(row=0, column=2, padx=6)
        self.offvocal_var = tk.BooleanVar(value=self.cfg.get("prefer_off_vocal", True))
        ttk.Checkbutton(search, text="オフボーカルを優先", variable=self.offvocal_var).grid(
            row=0, column=3, padx=4
        )
        self.trusted_var = tk.BooleanVar(value=self.cfg.get("trusted_only", False))
        ttk.Checkbutton(search, text="公式カラオケ配信元のみ", variable=self.trusted_var,
                        command=self.search).grid(row=0, column=4, padx=4)

        extra = ttk.Frame(tab)
        extra.grid(row=0, column=0, sticky="e", pady=(34, 0))
        self.vocal_button = ttk.Button(extra, text="ボーカルを消す", state="disabled",
                                       command=self.remove_vocals)
        self.vocal_button.pack(side="left")
        self.vocal_hint = ttk.Label(extra, text="（対応環境を確認中…）", style="Hint.TLabel")
        self.vocal_hint.pack(side="left", padx=(6, 12))
        ttk.Button(extra, text="ファイルを開く", command=self.open_local_file).pack(side="left")
        ttk.Button(extra, text="音源の入手先", command=self.show_sources).pack(side="left", padx=6)

        columns = ("score", "time", "title", "channel")
        self.result_tree = ttk.Treeview(tab, columns=columns, show="headings", height=7)
        for col, text, width, anchor in (
            ("score", "オフボーカル度", 100, "center"),
            ("time", "長さ", 60, "center"),
            ("title", "タイトル", 560, "w"),
            ("channel", "チャンネル", 180, "w"),
        ):
            self.result_tree.heading(col, text=text)
            self.result_tree.column(col, width=width, anchor=anchor)
        self.result_tree.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        self.result_tree.bind("<Double-1>", lambda _e: self.play_selected())

        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns", pady=(8, 4))

        self.video_label = tk.Label(tab, bg="#101014", text="ここに映像が出ます（歌詞つき動画ならそのまま歌えます）",
                                    fg="#666", font=(self.ui_family, 10))
        self.video_label.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=4)
        self.video_label.bind("<Configure>", self._on_video_resize)
        self.video_label.bind("<Double-1>", lambda _e: self.toggle_play())

        controls = ttk.Frame(tab)
        controls.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        controls.columnconfigure(3, weight=1)
        self.play_button = ttk.Button(controls, text="▶ 再生", width=10,
                                      command=self.play_selected, style="Big.TButton")
        self.play_button.grid(row=0, column=0)
        ttk.Button(controls, text="■ 停止", width=8, command=self.stop_music).grid(
            row=0, column=1, padx=(6, 12))
        self.time_label = ttk.Label(controls, text="--:-- / --:--")
        self.time_label.grid(row=0, column=2)
        self.position_var = tk.DoubleVar(value=0.0)
        self.seek_scale = ttk.Scale(controls, from_=0, to=1000, variable=self.position_var)
        self.seek_scale.grid(row=0, column=3, sticky="ew", padx=10)
        self.seek_scale.bind("<ButtonPress-1>", lambda _e: setattr(self, "_seeking", True))
        self.seek_scale.bind("<ButtonRelease-1>", self._on_seek_release)

        ttk.Label(controls, text="伴奏").grid(row=0, column=4, padx=(8, 2))
        self.music_volume_var = tk.DoubleVar(value=self.cfg.get("music_volume", 0.8))
        ttk.Scale(controls, from_=0.0, to=1.5, variable=self.music_volume_var, length=110,
                  command=lambda _v: setattr(self.player, "volume",
                                             self.music_volume_var.get())).grid(row=0, column=5)
        self.player.volume = self.music_volume_var.get()

        self.music_status = ttk.Label(tab, text="曲名やアーティスト名で検索してください。",
                                      style="Hint.TLabel")
        self.music_status.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ---------- マイクタブ ----------
    def _build_mic_tab(self) -> None:
        tab = self.mic_tab
        tab.columnconfigure(0, weight=1)

        box = ttk.LabelFrame(tab, text=" 機器 ", padding=10)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="マイク").grid(row=0, column=0, sticky="w", pady=3)
        self.mic_var = tk.StringVar()
        self.mic_combo = ttk.Combobox(box, textvariable=self.mic_var, state="readonly")
        self.mic_combo.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        self.mic_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())
        ttk.Button(box, text="テスト", width=8,
                   command=lambda: self.test_device("input")).grid(row=0, column=2, pady=3)

        ttk.Label(box, text="出力先").grid(row=1, column=0, sticky="w", pady=3)
        self.out_var = tk.StringVar()
        self.out_combo = ttk.Combobox(box, textvariable=self.out_var, state="readonly")
        self.out_combo.grid(row=1, column=1, sticky="ew", padx=6, pady=3)
        self.out_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())
        ttk.Button(box, text="テスト", width=8,
                   command=lambda: self.test_device("output")).grid(row=1, column=2, pady=3)

        ttk.Label(box, text="※ 伴奏もこの出力先から鳴ります", style="Hint.TLabel").grid(
            row=2, column=1, sticky="w", padx=6)

        action = ttk.Frame(tab)
        action.grid(row=1, column=0, sticky="ew", pady=10)
        action.columnconfigure(1, weight=1)
        self.mic_button = ttk.Button(action, text="🎤 マイクを入れる", style="Big.TButton",
                                     command=self.toggle_mic)
        self.mic_button.grid(row=0, column=0, sticky="w")
        ttk.Button(action, text="自動設定", command=self.auto_setup).grid(
            row=0, column=2, sticky="e")
        self.router_note = ttk.Label(action, text="", style="Hint.TLabel")
        self.router_note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        meters = ttk.LabelFrame(tab, text=" レベル ", padding=10)
        meters.grid(row=2, column=0, sticky="ew")
        meters.columnconfigure(1, weight=1)
        ttk.Label(meters, text="入力").grid(row=0, column=0, sticky="w")
        self.in_meter = ttk.Progressbar(meters, maximum=100)
        self.in_meter.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        self.in_db_label = ttk.Label(meters, text="--- dB", width=9)
        self.in_db_label.grid(row=0, column=2)
        ttk.Label(meters, text="出力").grid(row=1, column=0, sticky="w")
        self.out_meter = ttk.Progressbar(meters, maximum=100)
        self.out_meter.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
        self.out_db_label = ttk.Label(meters, text="--- dB", width=9)
        self.out_db_label.grid(row=1, column=2)

        fx = ttk.LabelFrame(tab, text=" 音の調整 ", padding=10)
        fx.grid(row=3, column=0, sticky="ew", pady=10)
        fx.columnconfigure(1, weight=1)

        ttk.Label(fx, text="プリセット").grid(row=0, column=0, sticky="w")
        self.preset_var = tk.StringVar(value=list(PRESETS)[0])
        preset = ttk.Combobox(fx, textvariable=self.preset_var, values=list(PRESETS),
                              state="readonly")
        preset.grid(row=0, column=1, sticky="ew", padx=8, pady=(0, 8))
        preset.bind("<<ComboboxSelected>>", lambda _e: self.apply_preset())
        ttk.Button(fx, text="ノイズを学習", command=self.learn_noise).grid(row=0, column=2)

        self.fx_vars: dict[str, tk.Variable] = {}
        self.fx_labels: dict[str, tuple[ttk.Label, str]] = {}
        rows = [
            ("input_gain_db", "マイクの音量", -10.0, 40.0, "dB"),
            ("denoise_strength", "ノイズ除去", 0.0, 3.0, ""),
            ("gate_db", "無音カット", -80.0, -20.0, "dB"),
            ("comp_ratio", "音量そろえ", 1.0, 8.0, ":1"),
            ("makeup_db", "仕上げ音量", -6.0, 20.0, "dB"),
            ("reverb_wet", "エコー", 0.0, 0.6, ""),
        ]
        for i, (key, label, lo, hi, unit) in enumerate(rows, start=1):
            ttk.Label(fx, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.DoubleVar(value=float(self.cfg["effects"].get(key, lo)))
            self.fx_vars[key] = var
            value_label = ttk.Label(fx, text="", width=8)
            self.fx_labels[key] = (value_label, unit)
            ttk.Scale(fx, from_=lo, to=hi, variable=var, length=280,
                      command=lambda _v, k=key: self._on_fx_change(k)
                      ).grid(row=i, column=1, sticky="ew", padx=8, pady=2)
            value_label.grid(row=i, column=2, sticky="w")
            self._on_fx_change(key)

        self.denoise_var = tk.BooleanVar(value=self.cfg["effects"].get("denoise", True))
        ttk.Checkbutton(fx, text="ノイズ除去を使う", variable=self.denoise_var,
                        command=self._on_denoise_toggle).grid(row=len(rows) + 1, column=1,
                                                              sticky="w", padx=8, pady=(6, 0))
        self.hum_var = tk.StringVar(value=str(int(self.cfg["effects"].get("hum_hz", 50))))
        hum = ttk.Frame(fx)
        hum.grid(row=len(rows) + 2, column=1, sticky="w", padx=8, pady=(4, 0))
        ttk.Label(hum, text="電源ハム除去:").pack(side="left")
        for text, value in (("なし", "0"), ("50Hz 東日本", "50"), ("60Hz 西日本", "60")):
            ttk.Radiobutton(hum, text=text, value=value, variable=self.hum_var,
                            command=self._on_hum_change).pack(side="left", padx=4)

    # ---------- 設定・診断タブ ----------
    def _build_setup_tab(self) -> None:
        tab = self.setup_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        audio = ttk.LabelFrame(tab, text=" オーディオ ", padding=10)
        audio.grid(row=0, column=0, sticky="ew")
        audio.columnconfigure(1, weight=1)

        ttk.Label(audio, text="Host API").grid(row=0, column=0, sticky="w", pady=3)
        apis = ["すべて"] + [a["name"] for a in sd.query_hostapis()]
        saved_api = self.cfg.get("hostapi")
        if saved_api not in apis:  # 別の OS で保存された設定を引き継いだ場合など
            saved_api = platform_support.default_host_api() or "すべて"
        self.api_var = tk.StringVar(value=saved_api)
        api_combo = ttk.Combobox(audio, textvariable=self.api_var, values=apis, state="readonly")
        api_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        api_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_devices())
        ttk.Label(audio, text=platform_support.host_api_hint(),
                  style="Hint.TLabel").grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(audio, text="遅延").grid(row=2, column=0, sticky="w", pady=3)
        self.latency_var = tk.StringVar(value=self.cfg.get("latency", "low"))
        lat = ttk.Combobox(audio, textvariable=self.latency_var, values=["low", "high"],
                           state="readonly", width=8)
        lat.grid(row=2, column=1, sticky="w", padx=8, pady=3)
        lat.bind("<<ComboboxSelected>>", lambda _e: self._on_device_change())

        ttk.Label(audio, text="バッファ").grid(row=3, column=0, sticky="w", pady=3)
        self.buffer_var = tk.DoubleVar(value=self.cfg.get("buffer_ms", 40.0))
        buffer_label = ttk.Label(audio, text="")
        ttk.Scale(audio, from_=10, to=200, variable=self.buffer_var, length=240,
                  command=lambda _v: buffer_label.configure(
                      text=f"{self.buffer_var.get():.0f} ms")).grid(
            row=3, column=1, sticky="w", padx=8)
        buffer_label.grid(row=3, column=2)
        buffer_label.configure(text=f"{self.buffer_var.get():.0f} ms")
        ttk.Label(audio, text="音が途切れるときは大きくする（遅延は増えます）",
                  style="Hint.TLabel").grid(row=4, column=1, sticky="w", padx=8)

        diag = ttk.Frame(tab)
        diag.grid(row=1, column=0, sticky="ew", pady=(12, 4))
        ttk.Label(diag, text="デバイス診断", style="Head.TLabel").pack(side="left")
        ttk.Button(diag, text="すべて調べる", command=self.run_diagnostics).pack(
            side="left", padx=8)
        ttk.Button(diag, text="機器を再検出", command=self.rescan_devices).pack(side="left")

        columns = ("kind", "device", "result", "hint")
        self.diag_tree = ttk.Treeview(tab, columns=columns, show="headings", height=8)
        for col, text, width in (("kind", "種類", 60), ("device", "デバイス", 330),
                                 ("result", "結果", 260), ("hint", "OS 側の設定", 260)):
            self.diag_tree.heading(col, text=text)
            self.diag_tree.column(col, width=width, anchor="w")
        self.diag_tree.grid(row=2, column=0, sticky="nsew")

        vocal = ttk.LabelFrame(tab, text=" AI ボーカル除去 ", padding=10)
        vocal.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        vocal.columnconfigure(0, weight=1)
        self.vocal_setup_label = ttk.Label(vocal, text="確認中…", style="Hint.TLabel",
                                           wraplength=700)
        self.vocal_setup_label.grid(row=0, column=0, sticky="w")
        self.vocal_setup_button = ttk.Button(vocal, text="有効にする", state="disabled")
        self.vocal_setup_button.grid(row=0, column=1, padx=8)
        self.vocal_progress = ttk.Progressbar(vocal, maximum=100)
        self.vocal_progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.vocal_progress.grid_remove()  # 取得中だけ出す

        storage = ttk.Frame(tab)
        storage.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self.cache_label = ttk.Label(storage, text="作成したオフボーカル: 確認中…",
                                     style="Hint.TLabel")
        self.cache_label.pack(side="left")
        ttk.Button(storage, text="まとめて削除", command=self.clear_offvocal_cache).pack(
            side="left", padx=8)
        self.after(1200, self._update_cache_label)

        ttk.Label(tab, text=f"設定の保存先: {config.CONFIG_PATH}", style="Hint.TLabel").grid(
            row=5, column=0, sticky="w", pady=(6, 0))

    # ---------- VST3 タブ ----------
    def _build_vst_tab(self) -> None:
        tab = self.vst_tab
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="プラグイン").grid(row=0, column=0)
        self.vst_var = tk.StringVar()
        self.vst_combo = ttk.Combobox(top, textvariable=self.vst_var, state="readonly")
        self.vst_combo.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(top, text="マイクに追加", command=self.add_vst3).grid(row=0, column=2)
        names = [name for name, _ in available_vst3()]
        self.vst_combo["values"] = names
        if names:
            self.vst_var.set(names[0])
        ttk.Label(top, text=f"{len(names)} 個のプラグインが見つかりました（マイクの後段に入ります）",
                  style="Hint.TLabel").grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))

        rack = ttk.LabelFrame(tab, text=" 挿しているもの ", padding=8)
        rack.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        rack.rowconfigure(0, weight=1)
        self.vst_tree = ttk.Treeview(rack, columns=("name", "state"), show="headings",
                                     height=10, selectmode="browse")
        self.vst_tree.heading("name", text="名前")
        self.vst_tree.heading("state", text="状態")
        self.vst_tree.column("name", width=200)
        self.vst_tree.column("state", width=70, anchor="center")
        self.vst_tree.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.vst_tree.bind("<<TreeviewSelect>>", lambda _e: self._show_vst_parameters())
        self.vst_tree.bind("<Double-1>", lambda _e: self.open_vst_editor())

        buttons = ttk.Frame(rack)
        buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        for text, command in (("▲", lambda: self.move_vst(-1)), ("▼", lambda: self.move_vst(1)),
                              ("バイパス", self.toggle_vst_bypass), ("外す", self.remove_vst)):
            ttk.Button(buttons, text=text, width=8 if len(text) > 2 else 3,
                       command=command).pack(side="left", padx=2)

        self.editor_button = ttk.Button(rack, text="プラグインの画面を開く",
                                        command=self.open_vst_editor, style="Big.TButton")
        self.editor_button.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ttk.Label(rack, text="※ 画面は別ウィンドウで開きます。開いている間もこのアプリは"
                             "そのまま使え、つまみを動かすと音にすぐ反映されます。",
                  style="Hint.TLabel", wraplength=260).grid(row=3, column=0, columnspan=3,
                                                            sticky="w", pady=(4, 0))

        params = ttk.LabelFrame(tab, text=" パラメータ ", padding=8)
        params.grid(row=1, column=1, sticky="nsew")
        params.columnconfigure(0, weight=1)
        params.rowconfigure(1, weight=1)

        head = ttk.Frame(params)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="絞り込み").grid(row=0, column=0)
        self.param_filter_var = tk.StringVar()
        filter_entry = ttk.Entry(head, textvariable=self.param_filter_var)
        filter_entry.grid(row=0, column=1, sticky="ew", padx=8)
        filter_entry.bind("<KeyRelease>", lambda _e: self._show_vst_parameters())

        canvas = tk.Canvas(params, highlightthickness=0, height=380)
        canvas.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(params, orient="vertical", command=canvas.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        canvas.configure(yscrollcommand=scroll.set)
        self.param_frame = ttk.Frame(canvas)
        self.param_window = canvas.create_window((0, 0), window=self.param_frame, anchor="nw")
        self.param_frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(self.param_window, width=e.width))
        canvas.bind_all("<MouseWheel>", self._on_param_scroll)
        self.param_canvas = canvas
        self.param_rows: list[dict] = []
        self._param_dragging = False

        self.vst_hint = ttk.Label(params, text="プラグインを追加すると、ここでつまみを操作できます。",
                                  style="Hint.TLabel")
        self.vst_hint.grid(row=2, column=0, sticky="w", pady=(6, 0))

    # ================= 非同期の下ごしらえ =================
    def post(self, func, *args) -> None:
        """ワーカースレッドから UI 更新を依頼する。"""
        self.ui_queue.put((func, args))

    def run_async(self, work, done=None, busy_text: str = "") -> None:
        """重い処理を別スレッドで実行し、結果をメインスレッドへ返す。"""
        if busy_text:
            self._busy += 1
            self.status_label.configure(text=busy_text)

        def worker():
            try:
                result = work()
                error = None
            except Exception as e:  # noqa: BLE001 - 画面に出して知らせる
                result, error = None, e
            self.post(self._finish_async, done, result, error, bool(busy_text))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_async(self, done, result, error, was_busy) -> None:
        if was_busy:
            self._busy = max(0, self._busy - 1)
            if self._busy == 0:
                self.status_label.configure(text="準備完了")
        if error is not None:
            self.status_label.configure(text=f"失敗: {error}")
            messagebox.showerror(APP_TITLE, str(error))
            return
        if done:
            done(result)

    # ================= デバイス =================
    def _api_filter(self) -> str | None:
        api = self.api_var.get()
        return None if api == "すべて" else api

    def _reload_devices(self) -> None:
        api = self._api_filter()
        self.mic_devices = dev.list_devices("input", api)
        self.out_devices = dev.list_devices("output", api)
        self.mic_combo["values"] = [d.label for d in self.mic_devices]
        self.out_combo["values"] = [d.label for d in self.out_devices]

        saved_mic = dev.find_by_name(self.cfg.get("mic_device_name", ""), "input", api)
        saved_out = dev.find_by_name(self.cfg.get("output_device_name", ""), "output", api)
        mic = saved_mic or dev.default_device("input", api)
        out = saved_out or dev.default_device("output", api)
        if mic:
            self.mic_var.set(mic.label)
        if out:
            self.out_var.set(out.label)

    def selected_device(self, kind: str) -> dev.Device | None:
        label = self.mic_var.get() if kind == "input" else self.out_var.get()
        pool = self.mic_devices if kind == "input" else self.out_devices
        for d in pool:
            if d.label == label:
                return d
        return None

    def rescan_devices(self) -> None:
        was_running = self.router.running
        self.router.stop()
        self.player.stop()
        dev.refresh()
        self._reload_devices()
        self.status_label.configure(text="機器を再検出しました")
        if was_running:
            self.start_mic()

    def _on_device_change(self) -> None:
        mic, out = self.selected_device("input"), self.selected_device("output")
        if mic:
            self.cfg["mic_device_name"] = mic.name
        if out:
            self.cfg["output_device_name"] = out.name
        if self.router.running:
            self.start_mic()  # 選び直したらつなぎ直す

    def test_device(self, kind: str) -> None:
        device = self.selected_device(kind)
        if device is None:
            return
        self.run_async(
            lambda: (dev.check(device), dev.system_hint(device)),
            lambda r: self._show_test_result(device, *r),
            busy_text=f"{device.name} を確認中…",
        )

    def _show_test_result(self, device, health, hint) -> None:
        lines = [device.name, "", health.summary]
        if hint:
            lines.append(hint)
        if health.receives_audio is False:
            lines += [
                "",
                "音が来ていません。次を確認してください:",
                "・機器側の入力切替（LINE / MIC）と録音レベル",
                "・マイクのスイッチが ON か、ケーブルが奥まで挿さっているか",
                f"・{platform_support.sound_settings_hint()}",
            ]
        messagebox.showinfo(APP_TITLE, "\n".join(lines))

    def run_diagnostics(self) -> None:
        api = self._api_filter()
        inputs = dev.list_devices("input", api)
        outputs = dev.list_devices("output", api)

        def work():
            status = dev.system_status()
            rows = []
            for device in inputs + outputs:
                health = dev.check(device, seconds=0.8, timeout=5.0)
                rows.append((device, health, dev.system_hint(device, status)))
            return rows

        self.diag_tree.delete(*self.diag_tree.get_children())
        self.run_async(work, self._show_diagnostics, busy_text="デバイスを診断中…（少し待ってください）")

    def _show_diagnostics(self, rows) -> None:
        self.diag_tree.delete(*self.diag_tree.get_children())
        for device, health, hint in rows:
            self.diag_tree.insert(
                "", "end",
                values=("入力" if device.is_input else "出力",
                        f"{device.name} [{device.hostapi}]", health.summary, hint or "—"),
            )
        usable = sum(1 for _, h, _ in rows if h.ok and h.receives_audio is not False)
        self.status_label.configure(text=f"診断完了: {usable}/{len(rows)} 個が使えます")

    def auto_setup(self) -> None:
        """実際に音が来ているマイクと、開ける出力先を選ぶ。"""
        api = self._api_filter()
        inputs = dev.list_devices("input", api)
        outputs = dev.list_devices("output", api)

        def work():
            best_in = None
            for device in inputs:
                health = dev.check(device, seconds=0.8, timeout=5.0)
                if health.ok and health.receives_audio:
                    score = health.peak_db if health.peak_db is not None else -120
                    if best_in is None or score > best_in[1]:
                        best_in = (device, score)
            best_out = None
            for device in outputs:
                if dev.check(device, timeout=5.0).ok:
                    best_out = device
                    if device.is_default:
                        break
            return best_in[0] if best_in else None, best_out

        self.run_async(work, self._apply_auto_setup, busy_text="使える機器を探しています…")

    def _apply_auto_setup(self, result) -> None:
        mic, out = result
        if mic:
            self.mic_var.set(mic.label)
        if out:
            self.out_var.set(out.label)
        self._on_device_change()
        if mic and ("USB" in mic.name.upper() or "Headset" in mic.name):
            self.preset_var.set("USBマイク・ヘッドセット")
            self.apply_preset()
        message = []
        message.append(f"マイク: {mic.name}" if mic else "音が来ているマイクが見つかりませんでした")
        message.append(f"出力先: {out.name}" if out else "使える出力先が見つかりませんでした")
        if not mic:
            message += ["", "マイクが挿さっているか、機器側のスイッチと録音レベルを確認してください。",
                        "「設定・診断」タブの「すべて調べる」で状況を一覧できます。"]
        messagebox.showinfo(APP_TITLE, "\n".join(message))

    def _first_run(self) -> None:
        self.cfg["first_run"] = False
        if messagebox.askyesno(
            APP_TITLE,
            "はじめての起動です。\n使えるマイクとスピーカーを自動で探しますか？\n"
            "（数十秒かかります。あとから「自動設定」でやり直せます）",
        ):
            self.auto_setup()
        # 機器を選び終えたころに、ボーカル除去を使えるなら案内する
        self.after(6000, self._offer_model_on_first_run)

    def _offer_model_on_first_run(self) -> None:
        cap = self.separator_capability
        if cap is None:  # 判定がまだ終わっていない
            self.after(3000, self._offer_model_on_first_run)
            return
        if not cap.available and getattr(cap, "installable", False):
            self.offer_model_install()

    # ================= マイク =================
    def toggle_mic(self) -> None:
        if self.router.running or self.router.state == "opening":
            self.stop_mic()
        else:
            self.start_mic()

    def start_mic(self) -> None:
        mic, out = self.selected_device("input"), self.selected_device("output")
        if mic is None or out is None:
            messagebox.showwarning(APP_TITLE, "マイクと出力先を選んでください。")
            return
        self.chain.set_rate(mic.rate)
        self.router.start(
            mic.index, out.index,
            latency=self.latency_var.get(),
            buffer_ms=float(self.buffer_var.get()),
        )

    def stop_mic(self) -> None:
        self.router.stop()

    def _on_router_state(self, state: str, message: str) -> None:
        self.post(self._update_router_state, state, message)

    def _update_router_state(self, state: str, message: str) -> None:
        texts = {"stopped": "マイク: 停止中", "opening": "マイク: 接続中…",
                 "running": "マイク: オン", "error": "マイク: エラー"}
        self.mic_state_label.configure(text=texts.get(state, state))
        self.mic_button.configure(
            text="マイクを止める" if state in ("running", "opening") else "🎤 マイクを入れる")
        if state == "error":
            self.router_note.configure(text=f"⚠ {message}", foreground="#b00")
            self.status_label.configure(text=message)
        elif state == "running":
            self.router_note.configure(text=f"{message}", foreground="#060")
        else:
            self.router_note.configure(text="", foreground="#666")

    # ================= 音の調整 =================
    def _apply_effects_from_config(self) -> None:
        fx = self.cfg["effects"]
        self.chain.input_gain.gain_db = fx["input_gain_db"]
        self.chain.highpass.cutoff_frequency_hz = fx["highpass_hz"]
        self.chain.set_hum_base(fx["hum_hz"] or 50.0)
        self.chain.hum_notch_db = fx["hum_notch_db"] if fx["hum_hz"] else 0.0
        self.chain.denoise = fx["denoise"]
        self.chain.denoiser.strength = fx["denoise_strength"]
        self.chain.gate.threshold_db = fx["gate_db"]
        self.chain.compressor.threshold_db = fx["comp_threshold_db"]
        self.chain.compressor.ratio = fx["comp_ratio"]
        self.chain.makeup.gain_db = fx["makeup_db"]
        self.chain.reverb.wet_level = fx["reverb_wet"]

    def _on_fx_change(self, key: str) -> None:
        value = float(self.fx_vars[key].get())
        label, unit = self.fx_labels[key]
        label.configure(text=f"{value:.1f}{unit}")
        self.cfg["effects"][key] = value
        if key == "input_gain_db":
            self.chain.input_gain.gain_db = value
        elif key == "denoise_strength":
            self.chain.denoiser.strength = value
        elif key == "gate_db":
            self.chain.gate.threshold_db = value
        elif key == "comp_ratio":
            self.chain.compressor.ratio = value
        elif key == "makeup_db":
            self.chain.makeup.gain_db = value
        elif key == "reverb_wet":
            self.chain.reverb.wet_level = value

    def _on_denoise_toggle(self) -> None:
        self.chain.denoise = self.denoise_var.get()
        self.cfg["effects"]["denoise"] = self.chain.denoise

    def _on_hum_change(self) -> None:
        hz = float(self.hum_var.get())
        self.cfg["effects"]["hum_hz"] = hz
        if hz:
            self.chain.set_hum_base(hz)
            self.chain.hum_notch_db = self.cfg["effects"].get("hum_notch_db", -12.0) or -12.0
        else:
            self.chain.hum_notch_db = 0.0

    def apply_preset(self) -> None:
        preset = PRESETS.get(self.preset_var.get())
        if not preset:
            return
        self.cfg["effects"].update(preset)
        self.chain.enabled = self.preset_var.get() != "加工しない"
        self._apply_effects_from_config()
        for key, var in self.fx_vars.items():
            if key in preset:
                var.set(preset[key])
        self.denoise_var.set(preset["denoise"])
        self.hum_var.set(str(int(preset["hum_hz"])))
        for key in self.fx_vars:  # スライダー横の数値表示を追従させる
            self._on_fx_change(key)
        self.status_label.configure(text=f"プリセット「{self.preset_var.get()}」を適用しました")

    def learn_noise(self) -> None:
        if not self.router.running:
            messagebox.showinfo(APP_TITLE, "先にマイクを入れてください。\n"
                                           "静かにしている間の音をノイズとして覚えます。")
            return
        self.chain.learn_noise()
        self.status_label.configure(text="ノイズを測定中…（2 秒間、声を出さないでください）")
        self.after(2000, self._finish_learn_noise)

    def _finish_learn_noise(self) -> None:
        ok = self.chain.finish_learning()
        self.status_label.configure(
            text="ノイズを覚えました" if ok else "測定できませんでした（マイクが動いていません）")

    # ================= VST3 =================
    def add_vst3(self) -> None:
        name = self.vst_var.get()
        path = dict(available_vst3()).get(name)
        if not path:
            return
        # pedalboard は VST3 をメインスレッドで読み込む必要がある
        #（別スレッドで作ると "must be reloaded on the main thread" で失敗する）。
        # 読み込みは 1 秒未満なので、ここで直接行う。
        self.status_label.configure(text=f"{name} を読み込み中…")
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            slot = self.chain.add_vst3(path, name)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"{name} を読み込めませんでした:\n{e}")
            self.status_label.configure(text="読み込みに失敗しました")
            return
        finally:
            self.configure(cursor="")
        self.status_label.configure(text=f"{name} を追加しました")
        self._refresh_vst_rack(select=slot)

    def _restore_vst(self) -> None:
        """前回の VST3 構成を戻す。読み込みはメインスレッドで行う必要がある。"""
        self.status_label.configure(text="前回の VST3 を読み込み中…")
        self.update_idletasks()
        failed = self.chain.restore_vst_state(self.cfg.get("vst3", []))
        self._refresh_vst_rack()
        self.status_label.configure(
            text=f"読み込めなかった VST3: {', '.join(failed)}" if failed else "準備完了")

    def selected_slot(self):
        selection = self.vst_tree.selection()
        if not selection:
            return None
        slots = self.chain.vst_slots
        index = self.vst_tree.index(selection[0])
        return slots[index] if index < len(slots) else None

    def _refresh_vst_rack(self, select=None) -> None:
        slots = self.chain.vst_slots
        self.vst_tree.delete(*self.vst_tree.get_children())
        for slot in slots:
            state = "バイパス" if slot.bypass else "適用中"
            if id(slot) in self.editors:
                state += " ◻"  # 画面を開いている
            self.vst_tree.insert("", "end", values=(slot.name, state))
        items = self.vst_tree.get_children()
        target = None
        if select is not None and select in slots:
            target = items[slots.index(select)]
        elif items:
            target = items[0]
        if target:
            self.vst_tree.selection_set(target)
            self.vst_tree.focus(target)
        current = self.selected_slot()
        self.editor_button.configure(
            text="プラグインの画面を閉じる" if current is not None and id(current) in self.editors
            else "プラグインの画面を開く")
        self._show_vst_parameters()

    def remove_vst(self) -> None:
        slot = self.selected_slot()
        if slot:
            if id(slot) in self.editors:  # 画面を開いたまま外さない
                self.close_vst_editor(slot)
            self.chain.remove_vst3(slot)
            self._refresh_vst_rack()

    def toggle_vst_bypass(self) -> None:
        slot = self.selected_slot()
        if slot:
            self.chain.set_bypass(slot, not slot.bypass)
            self._refresh_vst_rack(select=slot)

    def move_vst(self, delta: int) -> None:
        slot = self.selected_slot()
        if slot:
            self.chain.move_vst3(slot, delta)
            self._refresh_vst_rack(select=slot)

    # ---------- プラグインのエディタ（別プロセス） ----------
    def open_vst_editor(self) -> None:
        """プラグイン本体の画面を開く。押すたびに開閉を切り替える。

        pedalboard のエディタはメインスレッドからしか開けず、閉じるまで戻らない。
        本体プロセスで開くとアプリ全体が固まるので、別プロセスに任せて
        つまみの変化だけを受け取る。
        """
        slot = self.selected_slot()
        if slot is None:
            return
        if id(slot) in self.editors:
            self.close_vst_editor(slot)
            return

        # 名前と表示位置を渡す。複数開いても重ならないようずらして出す
        arguments = [slot.path, slot.name, str(len(self.editors))]
        if getattr(sys, "frozen", False):
            # インストーラ版には .py が無いので、自分自身を別モードで起動する
            command = [sys.executable, "--vst-editor", *arguments]
        else:
            host = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "vst_editor_host.py")
            command = [sys.executable, host, *arguments]
        flags = subprocess.CREATE_NO_WINDOW if platform_support.WINDOWS else 0
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1, creationflags=flags,
            )
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"エディタを起動できませんでした:\n{e}")
            return

        entry = {"proc": proc, "slot": slot, "ready": False}
        self.editors[id(slot)] = entry
        self._send_to_editor(slot, "init", slot.parameter_state())
        threading.Thread(target=self._read_editor, args=(slot, proc), daemon=True).start()
        self.status_label.configure(text=f"{slot.name} の画面を開いています…")
        self._refresh_vst_rack(select=slot)

    def close_vst_editor(self, slot=None) -> None:
        """開いているエディタを閉じる。slot 省略で全部閉じる。"""
        targets = [self.editors.get(id(slot))] if slot else list(self.editors.values())
        for entry in [e for e in targets if e]:
            self._send_to_editor(entry["slot"], "close")
            proc = entry["proc"]
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    def _send_to_editor(self, slot, command: str, params: dict | None = None) -> None:
        entry = self.editors.get(id(slot))
        if not entry:
            return
        message = {"cmd": command}
        if params is not None:
            message["params"] = params
        try:
            entry["proc"].stdin.write(json.dumps(message) + "\n")
            entry["proc"].stdin.flush()
        except Exception:
            pass  # 既に閉じられている

    def _read_editor(self, slot, proc) -> None:
        """子プロセスからの通知を受け取る（ワーカースレッド）。"""
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self.post(self._on_editor_event, slot, message)
        finally:
            proc.wait()
            self.post(self._on_editor_closed, slot)

    def _on_editor_event(self, slot, message: dict) -> None:
        kind = message.get("type")
        if kind == "ready":
            entry = self.editors.get(id(slot))
            if entry:
                entry["ready"] = True
            self.status_label.configure(text=f"{slot.name} の画面を表示中（本体はそのまま使えます）")
            self._refresh_vst_rack(select=slot)
        elif kind == "params":
            # エディタで動かしたつまみを、実際に音が通っている方へ反映する
            slot.apply_parameter_state(message.get("values", {}))
            self._refresh_param_values()
        elif kind == "decorated":
            pass  # ウィンドウに枠を付けた。表示上の変化だけなので何もしない
        elif kind == "error":
            messagebox.showerror(APP_TITLE,
                                 f"{slot.name} の画面を開けませんでした:\n{message.get('message')}")

    def _on_editor_closed(self, slot) -> None:
        self.editors.pop(id(slot), None)
        self.status_label.configure(text="準備完了")
        self._refresh_vst_rack(select=slot)
        self._show_vst_parameters()

    # ---------- パラメータ操作 ----------
    def _on_param_scroll(self, event) -> None:
        if self.notebook.index("current") == 2:
            self.param_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _show_vst_parameters(self) -> None:
        for child in self.param_frame.winfo_children():
            child.destroy()
        self.param_rows = []

        slot = self.selected_slot()
        if slot is None:
            self.vst_hint.configure(text="プラグインを追加すると、ここでつまみを操作できます。")
            return

        needle = self.param_filter_var.get().strip().lower()
        self.param_frame.columnconfigure(1, weight=1)
        shown = 0
        for key, param in slot.plugin.parameters.items():
            if needle and needle not in key.lower():
                continue
            self._build_param_row(slot, key, param, shown)
            shown += 1
        total = len(slot.plugin.parameters)
        self.vst_hint.configure(
            text=f"{slot.name}: {shown}/{total} 項目を表示中"
                 + ("（細かい調整は「プラグインの画面を開く」から）" if shown else ""))

    def _build_param_row(self, slot, key: str, param, row: int) -> None:
        frame = self.param_frame
        ttk.Label(frame, text=key.replace("_", " "), width=22).grid(
            row=row, column=0, sticky="w", pady=1)
        value_label = ttk.Label(frame, text=self._param_text(param), width=12)
        value_label.grid(row=row, column=2, sticky="w", padx=(6, 0))

        choices = getattr(param, "valid_values", None)
        entry = {"key": key, "param": param, "label": value_label, "widget": None,
                 "kind": "", "var": None, "slot": slot}

        if choices and isinstance(choices[0], bool):
            var = tk.BooleanVar(value=bool(getattr(slot.plugin, key, False)))
            widget = ttk.Checkbutton(
                frame, variable=var,
                command=lambda v=var, e=entry: self._set_param(e, v.get()))
            entry.update(kind="bool", var=var, widget=widget)
        elif choices and isinstance(choices[0], str) and len(choices) <= 48:
            var = tk.StringVar(value=str(getattr(slot.plugin, key, choices[0])))
            widget = ttk.Combobox(frame, textvariable=var, values=list(choices),
                                  state="readonly")
            widget.bind("<<ComboboxSelected>>",
                        lambda _e, v=var, en=entry: self._set_param(en, v.get()))
            entry.update(kind="enum", var=var, widget=widget)
        else:
            lo, hi = param.min_value, param.max_value
            numeric = isinstance(lo, (int, float)) and isinstance(hi, (int, float)) \
                and not isinstance(lo, bool)
            if numeric:
                current = float(getattr(slot.plugin, key, lo))
                var = tk.DoubleVar(value=current)
                kind = "float"
            else:
                # 単位が取れないものは 0〜1 の生値で操作する（表示は文字列で出す）
                lo, hi = 0.0, 1.0
                var = tk.DoubleVar(value=float(param.raw_value))
                kind = "raw"
            widget = ttk.Scale(frame, from_=lo, to=hi, variable=var,
                               command=lambda _v, v=var, en=entry:
                               self._set_param(en, v.get()))
            widget.bind("<ButtonPress-1>", lambda _e: setattr(self, "_param_dragging", True))
            widget.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_param_dragging", False))
            entry.update(kind=kind, var=var, widget=widget)

        entry["widget"].grid(row=row, column=1, sticky="ew", padx=6, pady=1)
        self.param_rows.append(entry)

    @staticmethod
    def _param_text(param) -> str:
        try:
            return str(param.string_value)
        except Exception:
            return ""

    def _set_param(self, entry: dict, value) -> None:
        """entry が自分の所属スロットを持つので、取り違えようがない形にしている。"""
        try:
            if entry["kind"] == "raw":
                entry["param"].raw_value = float(value)
            else:
                setattr(entry["slot"].plugin, entry["key"], value)
        except Exception:
            try:  # 刻みが合わない値は生値で入れ直す
                entry["param"].raw_value = float(entry["param"].raw_value)
            except Exception:
                pass
        entry["label"].configure(text=self._param_text(entry["param"]))
        slot = entry["slot"]
        if id(slot) in self.editors:  # 開いているエディタにも反映して食い違わせない
            try:
                self._send_to_editor(slot, "set",
                                     {entry["key"]: float(entry["param"].raw_value)})
            except Exception:
                pass

    def _refresh_param_values(self) -> None:
        """プラグイン本体の画面で動かされた値を表示に反映する。"""
        if self._param_dragging or not self.param_rows:
            return
        for entry in self.param_rows:
            param, var, kind = entry["param"], entry["var"], entry["kind"]
            entry["label"].configure(text=self._param_text(param))
            if var is None:
                continue
            try:
                if kind == "raw":
                    current = float(param.raw_value)
                elif kind == "float":
                    current = float(getattr(entry["slot"].plugin, entry["key"]))
                elif kind == "bool":
                    current = bool(getattr(entry["slot"].plugin, entry["key"]))
                else:
                    current = str(getattr(entry["slot"].plugin, entry["key"]))
                if isinstance(current, float):
                    if abs(float(var.get()) - current) > 1e-4:
                        var.set(current)
                elif var.get() != current:
                    var.set(current)
            except Exception:
                continue

    # ================= 音楽 =================
    def search(self) -> None:
        query = self.query_var.get().strip()
        if not query:
            return
        self.cfg["prefer_off_vocal"] = self.offvocal_var.get()
        self.cfg["trusted_only"] = self.trusted_var.get()
        prefer, trusted = self.offvocal_var.get(), self.trusted_var.get()
        self.run_async(
            lambda: music_search.search(query, limit=25 if trusted else 15,
                                        prefer_off_vocal=prefer, trusted_only=trusted),
            self._show_results,
            busy_text=f"「{query}」を検索中…",
        )

    def _show_results(self, tracks) -> None:
        self.tracks = tracks
        self.result_tree.delete(*self.result_tree.get_children())
        for track in tracks:
            mark = "★" * min(5, max(0, (track.score + 2) // 4))
            channel = ("✓ " if track.trusted else "") + track.uploader
            self.result_tree.insert("", "end", values=(
                mark or "—", track.duration_text, track.title, channel))
        if tracks:
            first = self.result_tree.get_children()[0]
            self.result_tree.selection_set(first)
            self.result_tree.focus(first)
            self.music_status.configure(text=f"{len(tracks)} 件見つかりました。曲を選んで再生してください。")
        else:
            self.music_status.configure(text="見つかりませんでした。言葉を変えて試してください。")

    def _update_cache_label(self) -> None:
        import separator
        count, size = len(separator.cached_files()), separator.cache_size_mb()
        self.cache_label.configure(
            text=f"作成したオフボーカル: {count} 件 / {size:.0f} MB"
                 + ("" if count else "（まだありません）"))

    def clear_offvocal_cache(self) -> None:
        import separator
        if not separator.cached_files():
            return
        if messagebox.askyesno(APP_TITLE, "作成したオフボーカル音源をすべて削除しますか？\n"
                                          "（元の曲は消えません。もう一度作り直せます）"):
            removed = separator.clear_cache()
            self._update_cache_label()
            self.status_label.configure(text=f"{removed} 件削除しました")

    # ---------- ボーカル除去 ----------
    def _check_separator(self) -> None:
        """使える環境かを調べる。torch の読み込みが重いので裏で行う。"""
        def work():
            import separator
            return separator.capability()

        self.run_async(work, self._apply_separator_capability)

    def _apply_separator_capability(self, cap) -> None:
        self.separator_capability = cap
        installable = getattr(cap, "installable", False)
        if cap.available:
            self.vocal_button.configure(state="normal")
            self.vocal_hint.configure(text=f"（{cap.gpu_name} で作成できます）")
        elif installable:
            # 機器は対応している。押されたら取得を案内する
            self.vocal_button.configure(state="normal")
            self.vocal_hint.configure(text=f"（{cap.gpu_name} で使えます。初回のみ準備が必要）")
        else:
            self.vocal_button.configure(state="disabled")
            self.vocal_hint.configure(text=f"（{cap.reason}）")
        self._update_vocal_setup_ui()

    # ---------- AI ボーカル除去の導入 ----------
    def _update_vocal_setup_ui(self) -> None:
        """設定タブ側の表示を、いまの状態に合わせる。"""
        if not hasattr(self, "vocal_setup_label"):
            return
        import model_installer

        cap = self.separator_capability
        busy = getattr(self, "_installing", False)
        if cap is None:
            self.vocal_setup_label.configure(text="確認中…")
            self.vocal_setup_button.configure(state="disabled")
            return
        if cap.available:
            size = model_installer.installed_size_mb()
            where = f"（追加で入れたぶん {size:.0f} MB）" if size else "（同梱ぶんで動作中）"
            self.vocal_setup_label.configure(text=f"使えます: {cap.summary} {where}")
            self.vocal_setup_button.configure(
                text="削除する", state="disabled" if busy or not size else "normal",
                command=self.remove_model)
        elif getattr(cap, "installable", False):
            self.vocal_setup_label.configure(
                text=f"{cap.gpu_name} で使えます。有効にするには約 3 GB の取得が必要です。")
            self.vocal_setup_button.configure(
                text="有効にする", state="disabled" if busy else "normal",
                command=self.offer_model_install)
        else:
            self.vocal_setup_label.configure(text=f"この PC では使えません: {cap.reason}")
            self.vocal_setup_button.configure(state="disabled")

    def offer_model_install(self) -> None:
        cap = self.separator_capability
        if cap is None or cap.available:
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"AI によるボーカル除去を有効にします。\n\n"
            f"　使う機器: {cap.gpu_name}\n"
            f"　取得する量: 約 3 GB\n"
            f"　かかる時間: 回線によって 5〜30 分ほど\n\n"
            "この間もアプリは使えます。始めますか？",
        ):
            return
        self.start_model_install()

    def start_model_install(self) -> None:
        import model_installer

        self._installing = True
        self._install_cancel = threading.Event()
        self._update_vocal_setup_ui()
        self.vocal_progress.grid()
        self.vocal_progress["value"] = 0

        def report(stage: str, ratio: float, detail: str = "") -> None:
            self.post(self._show_install_progress, stage, ratio, detail)

        def work():
            model_installer.install(progress=report, cancel=self._install_cancel)
            return separator_capability_after_install()

        def separator_capability_after_install():
            import separator
            return separator.capability(refresh=True)

        self.run_async(work, self._finish_model_install,
                       busy_text="ボーカル除去の準備をしています…")

    def _show_install_progress(self, stage: str, ratio: float, detail: str) -> None:
        self.vocal_progress["value"] = ratio * 100
        self.vocal_setup_label.configure(text=f"{stage}… {detail}")
        self.status_label.configure(text=f"ボーカル除去の準備: {stage} {ratio*100:.0f}%")

    def _finish_model_install(self, cap) -> None:
        self._installing = False
        self.vocal_progress.grid_remove()
        self._apply_separator_capability(cap)
        messagebox.showinfo(
            APP_TITLE,
            "ボーカル除去を有効にしました。\n"
            "曲を選んで「ボーカルを消す」を押すと使えます。\n"
            "（最初の 1 曲目だけ、AI モデルの取得で少し余分に時間がかかります）"
            if cap.available else
            f"有効にできませんでした:\n{cap.reason}")

    def remove_model(self) -> None:
        import model_installer
        import separator

        size = model_installer.installed_size_mb()
        if not messagebox.askyesno(
                APP_TITLE, f"ボーカル除去のために入れたもの（{size:.0f} MB）を削除しますか？"):
            return
        removed = model_installer.uninstall()
        self._apply_separator_capability(separator.capability(refresh=True))
        self.status_label.configure(text=f"{removed} MB を削除しました")

    def remove_vocals(self) -> None:
        """選んだ曲のボーカルを消して、そのまま再生する。"""
        cap = getattr(self, "separator_capability", None)
        if cap is None:
            return
        if not cap.available:
            if getattr(cap, "installable", False):
                self.notebook.select(3)  # 設定・診断タブへ案内する
                self.offer_model_install()
            else:
                messagebox.showinfo(APP_TITLE,
                                    f"この PC ではボーカル除去を使えません。\n{cap.reason}")
            return

        import separator

        source = self.current_local_path
        track = None
        if source is None:
            selection = self.result_tree.selection()
            if not selection:
                messagebox.showinfo(
                    APP_TITLE,
                    "ボーカルを消したい曲を選んでください。\n"
                    "検索結果から選ぶか、「ファイルを開く」で手持ちの曲を読み込みます。")
                return
            index = self.result_tree.index(selection[0])
            if index >= len(self.tracks):
                return
            track = self.tracks[index]

        def report(stage: str, ratio: float) -> None:
            self.post(self.music_status.configure,
                      {"text": f"ボーカル除去: {stage}… {ratio*100:.0f}%"})

        def work():
            path = source
            if path is None:
                report("音源を取得中", 0.02)
                folder = os.path.join(separator.CACHE_DIR, "download")
                os.makedirs(folder, exist_ok=True)
                path = music_search.download(track.id, folder, max_height=480)
            return separator.separate(path, progress=report)

        self.player.stop()
        self.run_async(work, self._play_separated,
                       busy_text="ボーカルを消しています…（GPU で処理中）")

    def _play_separated(self, path: str) -> None:
        out = self.selected_device("output")
        self.current_local_path = path
        self._photo = None
        self.player.open(path, None, {}, device=out.index if out else None)
        name = os.path.basename(path)
        self.music_status.configure(text=f"オフボーカルを再生中: {name}")
        self.play_button.configure(text="⏸ 一時停止")
        self.video_label.configure(image="", text=f"♪ {name}（ボーカル除去済み）")

    def open_local_file(self) -> None:
        """買った音源や自分で作ったオフボーカルを直接再生する。"""
        path = filedialog.askopenfilename(
            title="音源または動画を選ぶ",
            initialdir=self.cfg.get("last_folder") or None,
            filetypes=[("音源・動画", "*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus "
                                     "*.mp4 *.mkv *.webm *.avi *.mov"),
                       ("すべてのファイル", "*.*")],
        )
        if not path:
            return
        self.cfg["last_folder"] = os.path.dirname(path)
        self.current_track = None
        self.current_local_path = path
        out = self.selected_device("output")
        self.player.stop()
        self._photo = None
        self.player.set_display_size(max(2, self.video_label.winfo_width()),
                                     max(2, self.video_label.winfo_height()))
        self.player.open(path, None, {}, device=out.index if out else None)
        name = os.path.basename(path)
        self.music_status.configure(text=f"再生中: {name}")
        self.play_button.configure(text="⏸ 一時停止")
        self.video_label.configure(image="", text=f"♪ {name}")

    def show_sources(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            "オフボーカル音源の入手先\n"
            "\n"
            "■ 公式のカラオケ配信元（検索の「公式カラオケ配信元のみ」で絞れます）\n"
            "　カラオケ歌っちゃ王 / JOYSOUND / DAM CHANNEL / シンガーソングカラオケ\n"
            "　個人投稿よりキー表記と音質が安定しています。\n"
            "\n"
            "■ 買い切りでダウンロードできるサービス\n"
            "　Karaoke Version (karaoke-version.jp)\n"
            "　1 曲ずつ購入して MP3 を保存でき、パートごとのミュートもできます。\n"
            "　保存したファイルは「ファイルを開く」から直接再生できます。\n"
            "\n"
            "■ ボカロ・同人系\n"
            "　ピアプロ / BOOTH / ニコニ・コモンズ\n"
            "　作者本人が off vocal を配布していることが多いです。\n"
            "\n"
            "■ 手持ちの曲から自分で作る\n"
            "　「ボーカルを消す」で、AI がボーカルだけを取り除きます。\n"
            "　検索結果の曲にも、開いたファイルにも使えます。作ったものは保存され、\n"
            "　次からは待ち時間なしで再生されます。\n"
            "\n"
            "※ Apple Music や Spotify は保護がかかっているため取り込めません。",
        )

    def play_selected(self) -> None:
        if self.player.state in ("playing", "paused") and not self.result_tree.selection():
            self.toggle_play()
            return
        selection = self.result_tree.selection()
        if not selection:
            if self.player.state in ("playing", "paused"):
                self.toggle_play()
            return
        index = self.result_tree.index(selection[0])
        if index >= len(self.tracks):
            return
        track = self.tracks[index]
        self.current_track = track
        self.current_local_path = None  # 配信を再生するのでローカルファイルではない
        out = self.selected_device("output")
        quality = int(self.cfg.get("video_quality", 720))

        self.player.stop()
        self.music_status.configure(text=f"読み込み中: {track.title}")
        self.run_async(
            lambda: music_search.resolve(track.id, max_height=quality),
            lambda src: self._start_playback(src, out),
            busy_text="音源を準備中…",
        )

    def _start_playback(self, src, out) -> None:
        self.player.set_display_size(max(2, self.video_label.winfo_width()),
                                    max(2, self.video_label.winfo_height()))
        self.player.open(src.video_url, src.audio_url, src.headers,
                         device=out.index if out else None, duration=src.duration)
        self.music_status.configure(text=f"再生中: {src.title}")
        self.play_button.configure(text="⏸ 一時停止")

    def toggle_play(self) -> None:
        if self.player.state == "playing":
            self.player.pause()
            self.play_button.configure(text="▶ 再生")
        elif self.player.state == "paused":
            self.player.resume()
            self.play_button.configure(text="⏸ 一時停止")

    def stop_music(self) -> None:
        self.player.stop()
        self.play_button.configure(text="▶ 再生")
        self.video_label.configure(image="", text="ここに映像が出ます")
        self._photo = None
        self.music_status.configure(text="停止しました")

    def _player_error(self, error) -> None:
        self.music_status.configure(text=f"再生できませんでした: {error}")

    def _on_video_resize(self, event) -> None:
        self.player.set_display_size(event.width, event.height)

    def _on_seek_release(self, _event) -> None:
        self._seeking = False
        duration = self.player.duration
        if duration:
            self.player.seek(self.position_var.get() / 1000.0 * duration)

    # ================= 定期更新 =================
    def _tick(self) -> None:
        # ワーカースレッドからの依頼を処理する
        try:
            while True:
                func, args = self.ui_queue.get_nowait()
                func(*args)
        except queue.Empty:
            pass

        # レベルメーター
        if self.router.running:
            in_peak = self.chain.in_peak if self.chain.enabled else self.router.in_peak
            self.in_meter["value"] = meter_value(in_peak)
            self.out_meter["value"] = meter_value(self.router.out_peak)
            self.in_db_label.configure(text=f"{db_of(in_peak):.0f} dB")
            self.out_db_label.configure(text=f"{db_of(self.router.out_peak):.0f} dB")
        elif self.in_meter["value"]:
            self.in_meter["value"] = self.out_meter["value"] = 0

        # 映像
        image = self.player.take_frame()
        if image is not None:
            if self._photo is None or self._photo.width() != image.width or \
                    self._photo.height() != image.height:
                self._photo = ImageTk.PhotoImage(image)
            else:
                self._photo.paste(image)  # 同じサイズなら貼り替えるだけで速い
            self.video_label.configure(image=self._photo, text="")

        # VST3 タブを見ている間は、本体の画面で変えた値を表示に反映する
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 10 == 0 and self.notebook.index("current") == 2:
            self._refresh_param_values()

        # 再生位置
        if self.player.state in ("playing", "paused"):
            position, duration = self.player.position, self.player.duration
            self.time_label.configure(text=f"{time_text(position)} / {time_text(duration)}")
            if duration and not self._seeking:
                self.position_var.set(min(1000.0, position / duration * 1000.0))
            if self.player.finished:
                self.stop_music()

        self.after(33, self._tick)

    # ================= 終了 =================
    def on_close(self) -> None:
        self.cfg["music_volume"] = self.music_volume_var.get()
        self.cfg["hostapi"] = self.api_var.get()
        self.cfg["latency"] = self.latency_var.get()
        self.cfg["buffer_ms"] = float(self.buffer_var.get())
        self.cfg["prefer_off_vocal"] = self.offvocal_var.get()
        self.cfg["vst3"] = self.chain.vst_state()
        config.save(self.cfg)
        self.close_vst_editor()  # 開いたままのプラグイン画面を残さない
        self.player.stop()
        self.router.stop()
        self.destroy()


def main() -> None:
    app = KaraokeApp()
    app.mainloop()


if __name__ == "__main__":
    main()

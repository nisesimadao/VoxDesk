"""マイク用のリアルタイム処理チェーン。

pedalboard（JUCE ベース、C++ 実装なのでリアルタイムでも軽い）の内蔵エフェクトを
基本チェーンにして、必要なら手持ちの VST3 を後段に挿せるようにしている。

想定している状況:
    カラオケ用のダイナミックマイクを PC のマイク端子へ直結している。
    出力レベルが 20〜30 dB 足りず、かつ端子側でハムとサーノイズを拾う。
処理順:
    入力ゲイン → ハイパス → ハムノッチ → スペクトルノイズ除去
    → ゲート → コンプ → メイクアップ → (VST3) → リバーブ → リミッタ
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

import numpy as np

import platform_support
from pedalboard import (
    Compressor,
    Gain,
    HighpassFilter,
    Limiter,
    NoiseGate,
    Pedalboard,
    PeakFilter,
    Reverb,
    load_plugin,
)

def available_vst3() -> list[tuple[str, str]]:
    """(表示名, パス) の一覧を返す。

    プラグインはベンダー名のサブフォルダに置かれることがあるため 2 階層まで探す。
    .vst3 や .component 自体がフォルダ形式（バンドル）の場合があるので、
    見つけたらその中までは降りない。探索先と拡張子は OS ごとに違う。
    """
    extensions = platform_support.plugin_extensions()
    found: dict[str, str] = {}
    for directory in platform_support.plugin_dirs():
        if not os.path.isdir(directory):
            continue
        for root, dirs, files in os.walk(directory):
            depth = root[len(directory):].count(os.sep)
            if depth >= 2:
                dirs.clear()
                continue
            for entry in list(dirs) + files:
                lowered = entry.lower()
                match = next((e for e in extensions if lowered.endswith(e)), None)
                if match:
                    found.setdefault(entry[: -len(match)], os.path.join(root, entry))
                    if entry in dirs:
                        dirs.remove(entry)
    return sorted(found.items())


def plugin_names(path: str) -> list[str]:
    """1 つのファイルに複数のプラグインが入っている場合、その名前を返す。

    Serum2 や Reaktor のように本体とエフェクト版が同居しているものがある。
    1 つしか入っていなければ空のリストを返す。
    """
    try:
        from pedalboard import VST3Plugin
        names = VST3Plugin.get_plugin_names_for_file(path)
    except Exception:
        return []  # 走査できない形式もある。その場合は読み込み時のエラーから拾う
    return list(names) if names and len(names) > 1 else []


def names_from_error(message: str) -> list[str]:
    """読み込み失敗のメッセージから候補名を拾う。

    走査に失敗する環境でも、pedalboard は候補名を並べたエラーを返してくれる。
    """
    if "plugin_name" not in message:
        return []
    return re.findall(r'"([^"]+)"', message)


@dataclass
class VstSlot:
    """チェーンに挿した VST3 ひとつぶん。"""

    name: str
    path: str
    plugin: object
    bypass: bool = False
    plugin_name: str | None = None  # 1 ファイルに複数入っている場合の指定

    def parameter_state(self) -> dict:
        """パラメータを 0〜1 の正規化値で書き出す。

        preset_data はプラグインによって復元できないことがあるため、
        保存にはこちらを使う。
        """
        state = {}
        for key, param in self.plugin.parameters.items():
            try:
                state[key] = float(param.raw_value)
            except Exception:
                continue
        return state

    def apply_parameter_state(self, state: dict) -> None:
        for key, value in (state or {}).items():
            param = self.plugin.parameters.get(key)
            if param is None:
                continue
            try:
                param.raw_value = float(value)
            except Exception:
                continue


class RNNoiseDenoiser:
    """RNNoise（Xiph）による音声向けノイズ除去。

    ニューラルネットと従来の信号処理を組み合わせた軽量な手法で、
    定常ノイズだけでなくキーボードの打鍵音のような突発音も落とせる。
    48kHz・10ms（480 サンプル）単位で動くため、内部で貯めてから渡す。

    pyrnnoise が同梱する ctypes バインディングだけを直接読み込む。
    パッケージの __init__ は音声ファイル読み書き用の重い依存
    （audiolab）を引き込み、現行の PyAV と衝突するため通さない。
    """

    RATE = 48000
    _module = None
    _load_error = ""

    @classmethod
    def _binding(cls):
        if cls._module is not None or cls._load_error:
            return cls._module
        try:
            import importlib.util

            spec = importlib.util.find_spec("pyrnnoise")
            if spec is None or not spec.submodule_search_locations:
                raise ImportError("pyrnnoise が入っていません")
            path = os.path.join(list(spec.submodule_search_locations)[0], "rnnoise.py")
            if not os.path.exists(path):
                raise ImportError("rnnoise の binding が見つかりません")
            sub = importlib.util.spec_from_file_location("_voxdesk_rnnoise", path)
            module = importlib.util.module_from_spec(sub)
            sub.loader.exec_module(module)
            cls._module = module
        except Exception as e:
            cls._load_error = f"{type(e).__name__}: {e}"
        return cls._module

    @classmethod
    def available(cls) -> bool:
        return cls._binding() is not None

    @classmethod
    def unavailable_reason(cls) -> str:
        cls._binding()
        return cls._load_error

    def __init__(self, rate: int):
        module = self._binding()
        if module is None:
            raise RuntimeError(self._load_error or "RNNoise を使えません")
        self.rate = rate
        self.frame = int(module.FRAME_SIZE)
        self._module = module
        self._state = module.create()
        self._in = np.zeros(0, dtype=np.float32)
        self._out = np.zeros(0, dtype=np.float32)
        self.speech_probability = 0.0

    @property
    def latency_ms(self) -> float:
        return 1000.0 * self.frame / self.RATE

    def process(self, x: np.ndarray) -> np.ndarray:
        self._in = np.concatenate([self._in, x.astype(np.float32)])
        while len(self._in) >= self.frame:
            block = np.clip(self._in[: self.frame], -1.0, 1.0)
            cleaned, probability = self._module.process_mono_frame(self._state, block)
            self.speech_probability = float(probability)
            self._out = np.concatenate(
                [self._out, cleaned.astype(np.float32) / 32767.0])
            self._in = self._in[self.frame:]

        take = len(x)
        if len(self._out) < take:  # 貯まるまでは無音を返す（最大 10ms）
            self._out = np.concatenate(
                [np.zeros(take - len(self._out), dtype=np.float32), self._out])
        y = self._out[:take].copy()
        self._out = self._out[take:]
        return y

    def reset_buffers(self) -> None:
        self._in = np.zeros(0, dtype=np.float32)
        self._out = np.zeros(0, dtype=np.float32)

    def close(self) -> None:
        if getattr(self, "_state", None) is not None:
            self._module.destroy(self._state)
            self._state = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class SpectralDenoiser:
    """スペクトル減算による定常ノイズ除去。

    周波数ごとにノイズの大きさを推定し、それを上回る分だけ通す。
    推定は最小統計で自動追従し、無音区間を学習させて固定することもできる。
    numpy の FFT だけで完結し、サンプル単位のループを持たないので軽い。
    """

    def __init__(self, rate: int, n_fft: int = 512, strength: float = 1.5,
                 floor_db: float = -18.0):
        self.rate = rate
        self.n_fft = n_fft
        self.hop = n_fft // 2
        self.strength = strength
        self.floor = 10.0 ** (floor_db / 20.0)
        # 分析と合成の両方で掛けるので、平方根をとった窓を使う。
        # ハン窓をそのまま 2 回掛けると重ね合わせが 0.5〜1.0 で脈打ち、
        # 音が震える（ハン窓の 2 乗は 50% 重ねでは一定にならない）。
        self.window = np.sqrt(np.hanning(n_fft + 1)[:-1]).astype(np.float32)
        bins = n_fft // 2 + 1
        self._in = np.zeros(0, dtype=np.float32)
        # 足し合わせ用（常に n_fft 長）と、書き出し待ちの列を分ける。
        # 1 つのバッファで兼ねると、呼び出しごとのブロック長に応じて
        # 足し込む位置がずれ、音量が周期的に揺れる。
        self._acc = np.zeros(n_fft, dtype=np.float32)
        self._ready = np.zeros(0, dtype=np.float32)
        self._prev_gain = np.ones(bins, dtype=np.float32)
        self._learning = False
        self._learn_acc = np.zeros(bins, dtype=np.float32)
        self._learn_frames = 0
        self._fixed_noise: np.ndarray | None = None  # 学習で確定させたプロファイル

        # 最小統計法: 直近 1.2 秒ぶんの平滑パワーを保持し、その最小値をノイズとみなす。
        # 声は途切れるがノイズは途切れない、という前提に立つ推定方法。
        self._hist_len = max(8, int(1.2 * rate / self.hop))
        self._hist = np.full((self._hist_len, bins), 1e-6, dtype=np.float32)
        self._hist_pos = 0
        self._power = np.full(bins, 1e-6, dtype=np.float32)
        self._bias = 1.8  # 最小値は真のノイズより低く出るので補正する

    @property
    def latency_ms(self) -> float:
        return 1000.0 * self.n_fft / self.rate

    def start_learning(self) -> None:
        self._learning = True
        self._learn_frames = 0
        self._learn_acc[:] = 0.0

    def stop_learning(self) -> bool:
        self._learning = False
        if self._learn_frames < 4:
            return False
        self._fixed_noise = self._learn_acc / self._learn_frames
        return True

    @property
    def noise_profile(self) -> np.ndarray:
        """現在ノイズとみなしている振幅スペクトル。"""
        if self._fixed_noise is not None:
            return self._fixed_noise
        return np.sqrt(np.min(self._hist, axis=0) * self._bias)

    def reset_buffers(self) -> None:
        """入出力バッファだけ捨てる。学習したノイズプロファイルは残す。"""
        self._in = np.zeros(0, dtype=np.float32)
        self._acc = np.zeros(self.n_fft, dtype=np.float32)
        self._ready = np.zeros(0, dtype=np.float32)
        self._prev_gain[:] = 1.0

    def process(self, x: np.ndarray) -> np.ndarray:
        self._in = np.concatenate([self._in, x.astype(np.float32)])
        while len(self._in) >= self.n_fft:
            spec = np.fft.rfft(self._in[: self.n_fft] * self.window)
            mag = np.abs(spec).astype(np.float32)

            # 最小統計の履歴は学習中も更新しておく
            self._power = 0.8 * self._power + 0.2 * (mag * mag)
            self._hist[self._hist_pos] = self._power
            self._hist_pos = (self._hist_pos + 1) % self._hist_len

            if self._learning:
                self._learn_acc += mag
                self._learn_frames += 1
                gain = np.ones_like(mag)
            else:
                noise = self.noise_profile
                gain = np.maximum(mag - self.strength * noise, 0.0) / (mag + 1e-12)
                gain = np.maximum(gain, self.floor)
                # 周波数方向と時間方向に均してミュージカルノイズを抑える
                gain = np.convolve(gain, np.array([0.25, 0.5, 0.25], dtype=np.float32),
                                   mode="same")
                gain = 0.5 * gain + 0.5 * self._prev_gain
                self._prev_gain = gain

            self._acc += (np.fft.irfft(spec * gain) * self.window).astype(np.float32)
            # 先頭 hop サンプルは重ね合わせが済んだので払い出し、残りを前へ詰める
            self._ready = np.concatenate([self._ready, self._acc[: self.hop].copy()])
            self._acc = np.concatenate(
                [self._acc[self.hop:], np.zeros(self.hop, dtype=np.float32)]
            )
            self._in = self._in[self.hop:]

        take = len(x)
        if len(self._ready) < take:  # 貯まるまで（最初の n_fft ぶん）は無音を返す
            y = np.zeros(take, dtype=np.float32)
            y[take - len(self._ready):] = self._ready
            self._ready = np.zeros(0, dtype=np.float32)
            return y
        y = self._ready[:take].copy()
        self._ready = self._ready[take:]
        return y


class MicChain:
    """マイク 1 系統ぶんの処理チェーン。ブロック単位で連続処理する。"""

    def __init__(self, rate: int):
        self.rate = rate
        self.enabled = True
        self.denoise = True

        self.input_gain = Gain(gain_db=20.0)      # ダイナミックマイクの不足分を補う
        self.highpass = HighpassFilter(cutoff_frequency_hz=100.0)
        self.notches = [
            PeakFilter(cutoff_frequency_hz=f, gain_db=0.0, q=18.0)
            for f in (50.0, 100.0, 150.0, 200.0)
        ]
        self.gate = NoiseGate(threshold_db=-45.0, ratio=6.0, attack_ms=1.0, release_ms=120.0)
        self.compressor = Compressor(
            threshold_db=-26.0, ratio=4.0, attack_ms=5.0, release_ms=120.0
        )
        self.makeup = Gain(gain_db=6.0)
        self.reverb = Reverb(room_size=0.25, damping=0.5, wet_level=0.0, dry_level=1.0)
        self.limiter = Limiter(threshold_db=-1.0, release_ms=100.0)

        self.denoiser = SpectralDenoiser(rate)
        self.denoise_engine = "spectral"  # spectral / rnnoise
        self.rnnoise: RNNoiseDenoiser | None = None
        self._vst: list[VstSlot] = []
        self._board = Pedalboard([])
        self._lock = threading.Lock()
        self._rebuild()

        self.in_peak = 0.0
        self.out_peak = 0.0

    # ---------- 構成 ----------
    def _rebuild(self) -> None:
        active = [slot.plugin for slot in self._vst if not slot.bypass]
        chain = [self.input_gain, self.highpass, *self.notches, self.gate,
                 self.compressor, self.makeup, *active, self.reverb, self.limiter]
        with self._lock:
            self._board = Pedalboard(chain)

    @property
    def hum_notch_db(self) -> float:
        return self.notches[0].gain_db

    @hum_notch_db.setter
    def hum_notch_db(self, value: float) -> None:
        for n in self.notches:
            n.gain_db = value

    def set_hum_base(self, freq: float) -> None:
        """ハムの基本周波数（50 か 60）に合わせてノッチを並べ直す。"""
        for i, n in enumerate(self.notches):
            n.cutoff_frequency_hz = freq * (i + 1)

    # ---------- VST3 ----------
    def add_vst3(self, path: str, name: str | None = None,
                 plugin_name: str | None = None) -> VstSlot:
        """VST3 を後段に追加する。

        1 つのファイルに複数のプラグインが入っている場合は plugin_name で選ぶ。
        """
        plugin = load_plugin(path, plugin_name=plugin_name) if plugin_name \
            else load_plugin(path)
        label = name or os.path.splitext(os.path.basename(path))[0]
        if plugin_name and plugin_name not in label:
            label = f"{label}（{plugin_name}）"
        slot = VstSlot(
            name=label,
            path=path,
            plugin=plugin,
            plugin_name=plugin_name,
        )
        self._vst.append(slot)
        self._rebuild()
        return slot

    def remove_vst3(self, slot: VstSlot) -> None:
        if slot in self._vst:
            self._vst.remove(slot)
            self._rebuild()

    def set_bypass(self, slot: VstSlot, bypass: bool) -> None:
        slot.bypass = bypass
        self._rebuild()

    def move_vst3(self, slot: VstSlot, delta: int) -> None:
        """チェーン内の順番を入れ替える（前段ほど先に効く）。"""
        if slot not in self._vst:
            return
        index = self._vst.index(slot)
        target = max(0, min(len(self._vst) - 1, index + delta))
        if target == index:
            return
        self._vst.insert(target, self._vst.pop(index))
        self._rebuild()

    @property
    def vst_slots(self) -> list[VstSlot]:
        return list(self._vst)

    def vst_state(self) -> list[dict]:
        """保存用。次回起動時に同じ構成を復元できるようにする。"""
        return [
            {"name": s.name, "path": s.path, "bypass": s.bypass,
             "plugin_name": s.plugin_name, "params": s.parameter_state()}
            for s in self._vst
        ]

    def restore_vst_state(self, saved: list[dict]) -> list[str]:
        """保存した構成を復元する。読み込めなかったものの名前を返す。"""
        failed = []
        catalog = None
        for entry in saved or []:
            path = entry.get("path", "")
            if path and not os.path.exists(path):
                # 別の OS や別のインストール先で保存された設定でも、
                # 同じ名前のプラグインが入っていれば拾い直す
                if catalog is None:
                    catalog = dict(available_vst3())
                path = catalog.get(entry.get("name", ""), "")
            if not path or not os.path.exists(path):
                failed.append(entry.get("name", path))
                continue
            try:
                slot = self.add_vst3(path, entry.get("name"), entry.get("plugin_name"))
                slot.apply_parameter_state(entry.get("params", {}))
                if entry.get("bypass"):
                    self.set_bypass(slot, True)
            except Exception:
                failed.append(entry.get("name", path))
        return failed

    # ---------- 本体 ----------
    def process(self, x: np.ndarray) -> np.ndarray:
        """モノラルブロックを処理して返す。"""
        self.in_peak = float(np.abs(x).max()) if len(x) else 0.0
        if not self.enabled:
            self.out_peak = self.in_peak
            return x

        y = x.astype(np.float32)
        if self.denoise:
            if self.denoise_engine == "rnnoise" and self.rnnoise is not None:
                y = self.rnnoise.process(y)
            else:
                y = self.denoiser.process(y)
        with self._lock:
            board = self._board
        # reset=False で内部状態を保持し、ブロックをまたいで連続処理する
        y = board(y, self.rate, reset=False)
        y = np.asarray(y, dtype=np.float32).reshape(-1)[: len(x)]
        if len(y) < len(x):  # プラグインの遅延で短くなった場合を埋める
            y = np.concatenate([y, np.zeros(len(x) - len(y), dtype=np.float32)])

        self.out_peak = float(np.abs(y).max()) if len(y) else 0.0
        return y

    def learn_noise(self) -> None:
        """これから流れる音をノイズとして覚え始める（静かにしている間に呼ぶ）。"""
        self.denoiser.start_learning()

    def finish_learning(self) -> bool:
        """学習を確定する。十分なデータが集まっていれば True。"""
        return self.denoiser.stop_learning()

    def set_denoise_engine(self, name: str) -> None:
        """ノイズ除去の方式を切り替える。使えないときは例外を投げる。"""
        if name == "rnnoise":
            if self.rate != RNNoiseDenoiser.RATE:
                raise RuntimeError(
                    f"RNNoise は 48kHz のマイクでのみ使えます（今は {self.rate}Hz）")
            if self.rnnoise is None:
                self.rnnoise = RNNoiseDenoiser(self.rate)
        elif self.rnnoise is not None:
            self.rnnoise.close()
            self.rnnoise = None
        self.denoise_engine = name

    def set_rate(self, rate: int) -> None:
        """入力デバイスのサンプルレートに合わせる。ノイズ除去器は作り直す。"""
        if rate == self.rate:
            return
        self.rate = rate
        strength = self.denoiser.strength
        self.denoiser = SpectralDenoiser(rate, strength=strength)
        if self.rnnoise is not None:  # 48kHz 以外では使えないので作り直す
            self.rnnoise.close()
            self.rnnoise = None
            if rate == RNNoiseDenoiser.RATE:
                self.rnnoise = RNNoiseDenoiser(rate)
            else:
                self.denoise_engine = "spectral"
        self.reset()

    def reset(self) -> None:
        """内部状態を捨てる。学習済みノイズプロファイルは維持する。"""
        with self._lock:
            self._board.reset()
        self.denoiser.reset_buffers()


def karaoke_preset(chain: MicChain) -> None:
    """カラオケ用マイク（ダイナミック直結）向けの初期値。"""
    chain.input_gain.gain_db = 24.0
    chain.highpass.cutoff_frequency_hz = 110.0
    chain.set_hum_base(50.0)
    chain.hum_notch_db = -12.0
    chain.denoise = True
    chain.denoiser.strength = 1.8
    chain.gate.threshold_db = -42.0
    chain.gate.ratio = 8.0
    chain.compressor.threshold_db = -24.0
    chain.compressor.ratio = 4.0
    chain.makeup.gain_db = 8.0
    chain.reverb.wet_level = 0.12   # うっすらエコー
    chain.reverb.dry_level = 0.95
    chain.reverb.room_size = 0.3
    chain.limiter.threshold_db = -1.0

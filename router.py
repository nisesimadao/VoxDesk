"""マイク入力 → 処理 → スピーカー出力のルーティングエンジン。

入力と出力を別々のストリームで開き、間をリングバッファでつなぐ。
こうすることで
  - 入力 44100Hz / 出力 48000Hz のように両者のレートが違っても動く
    （WASAPI 共有モードはデバイスごとにレートが固定されるため、これは普通に起きる）
  - 片方のデバイスが応答しなくても、もう片方を巻き込まずに止められる
  - デバイスを開く処理をワーカースレッドに追い出せるので UI が固まらない

2 つのデバイスは別々のクロックで動くため、放っておくとバッファが溢れるか枯れる。
リングバッファの溜まり具合を見てリサンプル比を微調整し、ドリフトを吸収する。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd

import platform_support
from comutil import com_initialized


def _signal():
    """scipy.signal を必要になった時だけ読む。

    起動時に読むと 0.8 秒ほど窓が出るのが遅れる。実際に使うのは
    ダウンサンプル時のローパスだけなので、そこまで引き延ばす。
    """
    from scipy import signal

    return signal


class Resampler:
    """比率を動的に変えられる線形補間リサンプラ。

    ドリフト補正のために比率を連続的に変えたいので、多相フィルタではなく
    線形補間を使う。ダウンサンプル時のみ折り返し防止のローパスを通す。
    """

    def __init__(self, in_rate: int, out_rate: int, channels: int = 1):
        self.in_rate = in_rate
        self.out_rate = out_rate
        self.channels = max(1, channels)
        self.base_ratio = in_rate / out_rate  # 出力 1 サンプルあたりの入力サンプル数
        self.ratio = self.base_ratio
        self._pos = 0.0
        self._tail = np.zeros((1, self.channels), dtype=np.float32)

        self._sos = None
        self._sosfilt = None
        if out_rate < in_rate:
            signal = _signal()
            nyq = 0.5 * in_rate
            cutoff = 0.45 * out_rate
            self._sos = signal.butter(6, cutoff / nyq, btype="low", output="sos")
            self._sosfilt = signal.sosfilt
            # チャンネルごとに状態を持たせる（混ざると位相が崩れる）
            base = signal.sosfilt_zi(self._sos)
            self._zi = np.zeros((base.shape[0], base.shape[1], self.channels))

    def process(self, x: np.ndarray) -> np.ndarray:
        mono_in = x.ndim == 1
        frames = x.reshape(-1, 1) if mono_in else x
        if self._sos is not None:
            frames, self._zi = self._sosfilt(self._sos, frames, axis=0, zi=self._zi)
            frames = frames.astype(np.float32)

        y = self._interpolate(frames)
        return y.reshape(-1) if mono_in else y

    def _interpolate(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self._tail, x])
        # buf[0] は前回の最後のサンプル。読み出し位置は self._pos から始まる
        available = len(buf) - 1
        empty = np.zeros((0, buf.shape[1]), dtype=np.float32)
        if available <= 0:
            self._tail = buf[-1:]
            return empty

        n_out = int(np.floor((available - self._pos) / self.ratio))
        if n_out <= 0:
            self._tail = buf[-1:] if len(buf) else self._tail
            return empty

        idx = self._pos + self.ratio * np.arange(n_out)
        base = idx.astype(np.int64)
        frac = (idx - base).astype(np.float32)[:, None]  # チャンネル方向へ広げる
        y = buf[base] * (1.0 - frac) + buf[base + 1] * frac

        consumed = self._pos + self.ratio * n_out
        keep = int(np.floor(consumed))
        self._pos = consumed - keep
        self._tail = buf[keep:]
        if len(self._tail) < 1:
            self._tail = buf[-1:]
        return y.astype(np.float32)


class RingBuffer:
    """単一の生産者と単一の消費者を想定した float32 のリングバッファ。

    長さはフレーム数（チャンネルをまたいだ 1 時点ぶん）で数える。
    """

    def __init__(self, capacity: int, target: int = 0, channels: int = 1):
        self.channels = max(1, channels)
        self._buf = np.zeros((capacity, self.channels), dtype=np.float32)
        self._capacity = capacity
        self._read = 0
        self._write = 0
        self._fill = 0
        self._lock = threading.Lock()
        self.overruns = 0
        self.underruns = 0
        # 溢れたときに、ここまで捨てて遅延を戻す。
        # 目標を指定しないときは切り詰めない（ただの循環バッファとして使える）
        self.target = target
        # ここを超えたら捨てる。満杯まで待つと最大遅延に張り付き、
        # ドリフト補正（最大 0.3%）では戻すのに何分もかかってしまう
        self.max_fill = min(capacity, max(target * 4, target + 1)) if target else capacity

    @property
    def fill(self) -> int:
        return self._fill

    @property
    def capacity(self) -> int:
        return self._capacity

    def write(self, data: np.ndarray) -> None:
        if data.ndim == 1:  # 1 チャンネルなら 1 次元で渡せる
            data = data.reshape(-1, 1)
        n = len(data)
        if n == 0:
            return
        if data.shape[1] != self.channels:
            raise ValueError(
                f"チャンネル数が違います（{data.shape[1]} / 期待 {self.channels}）")
        with self._lock:
            if self._fill + n > self.max_fill:
                # 溜まりすぎたら目標量まで一気に捨てて、遅延をその場で戻す。
                # 「入る分だけ捨てる」では満杯に張り付いたままになる。
                keep = max(0, min(self._fill, self.target - n))
                drop = self._fill - keep
                self._read = (self._read + drop) % self._capacity
                self._fill -= drop
                self.overruns += 1
            end = min(n, self._capacity - self._write)
            self._buf[self._write:self._write + end] = data[:end]
            if end < n:
                self._buf[: n - end] = data[end:]
            self._write = (self._write + n) % self._capacity
            self._fill += n

    def read(self, n: int) -> np.ndarray:
        out = np.zeros((n, self.channels), dtype=np.float32)
        with self._lock:
            take = min(n, self._fill)
            if take < n:
                self.underruns += 1
            if take:
                end = min(take, self._capacity - self._read)
                out[:end] = self._buf[self._read:self._read + end]
                if end < take:
                    out[end:take] = self._buf[: take - end]
                self._read = (self._read + take) % self._capacity
                self._fill -= take
        return out

    def clear(self) -> None:
        with self._lock:
            self._read = self._write = self._fill = 0


class Router:
    """入力デバイスから出力デバイスへ音を流す。"""

    OPEN_TIMEOUT = 6.0  # デバイスが応答しないと判断するまでの秒数

    def __init__(self, chain=None, on_state=None):
        self.chain = chain          # mic_chain.MicChain（None なら素通し）
        self.on_state = on_state    # (state:str, message:str) を受け取るコールバック
        # 入力の使い方。1 なら 1 本、2 ならステレオのまま扱う。
        # offset は「オーディオインターフェースの 2 本目に挿した」場合などに使う。
        self.in_channels = 1
        self.in_channel_offset = 0
        self.mic_gain = 1.0
        self.muted = False

        self.state = "stopped"      # stopped / opening / running / error
        self.message = ""
        self.in_device: int | None = None
        self.out_device: int | None = None
        self.in_rate = 0
        self.out_rate = 0
        self.out_channels = 1
        self._active_channels = 1
        self._slice = slice(0, 1)
        self.in_peak = 0.0
        self.out_peak = 0.0
        self.output_gain = 1.0

        self._in_stream: sd.InputStream | None = None
        self._out_stream: sd.OutputStream | None = None
        self._ring: RingBuffer | None = None
        self._resampler: Resampler | None = None
        self._target_fill = 0
        self._priming = True  # 開始直後と枯渇後は、貯まるまで無音を出す
        self._lock = threading.Lock()
        self._generation = 0

    # ---------- 状態 ----------
    def _set_state(self, state: str, message: str = "") -> None:
        self.state = state
        self.message = message
        if self.on_state:
            self.on_state(state, message)

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def latency_ms(self) -> float:
        """入力・出力・バッファを合わせたおおよその往復遅延。"""
        total = 0.0
        if self._in_stream is not None:
            total += self._in_stream.latency
        if self._out_stream is not None:
            total += self._out_stream.latency
        if self._ring is not None and self.out_rate:
            total += self._ring.fill / self.out_rate
        return total * 1000.0

    @property
    def xruns(self) -> tuple[int, int]:
        if self._ring is None:
            return (0, 0)
        return (self._ring.overruns, self._ring.underruns)

    @property
    def buffer_ms(self) -> float:
        if self._ring is None or not self.out_rate:
            return 0.0
        return 1000.0 * self._ring.fill / self.out_rate

    # ---------- 開始 / 停止 ----------
    def start(self, in_device: int, out_device: int, latency: str = "low",
              blocksize: int = 0, buffer_ms: float = 40.0,
              in_channels: int | None = None, in_channel_offset: int | None = None) -> None:
        """再生を開始する。実際の open は別スレッドで行い、この呼び出しは即座に戻る。"""
        self.stop()
        if in_channels is not None:
            self.in_channels = max(1, min(2, int(in_channels)))
        if in_channel_offset is not None:
            self.in_channel_offset = max(0, int(in_channel_offset))
        self._generation += 1
        generation = self._generation
        self._set_state("opening", "デバイスを開いています…")
        t = threading.Thread(
            target=self._open_worker,
            args=(generation, in_device, out_device, latency, blocksize, buffer_ms),
            daemon=True,
        )
        t.start()
        # 応答しないデバイス（古いドライバの MME など）は open で固まる。
        # 見張り役を別に立てて、時間内に開かなければ利用者に知らせる。
        threading.Thread(
            target=self._watchdog, args=(generation, t), daemon=True
        ).start()

    def _watchdog(self, generation: int, worker: threading.Thread) -> None:
        worker.join(timeout=self.OPEN_TIMEOUT)
        if worker.is_alive() and generation == self._generation and self.state == "opening":
            self._set_state(
                "error",
                f"デバイスが応答しません。{platform_support.alternate_api_hint()}。",
            )

    def _open_worker(self, generation, in_device, out_device, latency, blocksize,
                     buffer_ms) -> None:
        with com_initialized():  # WASAPI はスレッドごとの COM 初期化を要求する
            self._open_streams(generation, in_device, out_device, latency, blocksize,
                               buffer_ms)

    def _open_streams(self, generation, in_device, out_device, latency, blocksize,
                      buffer_ms) -> None:
        try:
            in_info = sd.query_devices(in_device)
            out_info = sd.query_devices(out_device)
            in_rate = int(in_info["default_samplerate"])
            out_rate = int(out_info["default_samplerate"])
            out_channels = min(2, max(1, out_info["max_output_channels"]))

            # 使いたいチャンネルまで開く必要がある（2 本目を使うなら 2ch 開く）
            wanted = self.in_channel_offset + self.in_channels
            open_channels = min(max(1, in_info["max_input_channels"]), wanted)
            channels = min(self.in_channels, open_channels - self.in_channel_offset)
            if channels < 1:  # 指定した本数が無いデバイスだった
                self.in_channel_offset = 0
                open_channels = min(max(1, in_info["max_input_channels"]), self.in_channels)
                channels = max(1, open_channels)

            target_fill = int(out_rate * buffer_ms / 1000.0)
            ring = RingBuffer(int(out_rate * 2.0), target=target_fill,
                              channels=channels)  # 最大 2 秒
            resampler = (Resampler(in_rate, out_rate, channels)
                         if in_rate != out_rate else None)

            in_stream = out_stream = None
            try:
                in_stream = sd.InputStream(
                    device=in_device, samplerate=in_rate, channels=open_channels,
                    dtype="float32", latency=latency, blocksize=blocksize,
                    callback=self._input_callback,
                )
                out_stream = sd.OutputStream(
                    device=out_device, samplerate=out_rate, channels=out_channels,
                    dtype="float32", latency=latency, blocksize=blocksize,
                    callback=self._output_callback,
                )
            except Exception:
                # 片方だけ開けた状態で失敗すると、そのストリームが閉じられずに残る
                for stream in (in_stream, out_stream):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                raise

            if generation != self._generation:  # 開いている間に停止された
                in_stream.close(); out_stream.close()
                return

            with self._lock:
                self._priming = True
                self.in_device, self.out_device = in_device, out_device
                self.in_rate, self.out_rate = in_rate, out_rate
                self.out_channels = out_channels
                self._active_channels = channels
                self._slice = slice(self.in_channel_offset,
                                    self.in_channel_offset + channels)
                self._ring, self._resampler = ring, resampler
                self._target_fill = target_fill
                self._in_stream, self._out_stream = in_stream, out_stream
            if self.chain is not None and hasattr(self.chain, "set_rate"):
                self.chain.set_rate(in_rate)
                self.chain.set_channels(channels)

            out_stream.start()
            in_stream.start()
            rate_note = (
                f"{in_rate}Hz → {out_rate}Hz 変換" if resampler else f"{in_rate}Hz"
            )
            self._set_state("running", rate_note)
        except Exception as e:
            if generation == self._generation:
                self._set_state("error", self._describe(e))
            self._close_streams()

    @staticmethod
    def _describe(e: Exception) -> str:
        """PortAudio のエラーを利用者向けの文言にする。"""
        if isinstance(e, UnicodeDecodeError):
            # 日本語 Windows だと PortAudio のエラー文字列が CP932 で返り、
            # sounddevice の UTF-8 復号が失敗する。原因自体は隠れてしまう。
            return "デバイスを開けませんでした（ドライバがエラーを返しました）"
        text = str(e)
        if "Invalid sample rate" in text:
            return "このデバイスはそのサンプルレートに対応していません"
        if "Device unavailable" in text or "-9985" in text:
            return "デバイスが他のアプリに使われています"
        if "Invalid device" in text:
            return f"このデバイスは選べません（{platform_support.alternate_api_hint()}）"
        return f"{type(e).__name__}: {text}"

    def stop(self) -> None:
        self._generation += 1
        self._close_streams()
        if self.state != "stopped":
            self._set_state("stopped", "")

    def _close_streams(self) -> None:
        with self._lock:
            streams = [self._in_stream, self._out_stream]
            self._in_stream = self._out_stream = None
        for s in streams:
            if s is None:
                continue
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        if self._ring is not None:
            self._ring.clear()
        self.in_peak = self.out_peak = 0.0

    # ---------- コールバック ----------
    def _input_callback(self, indata, frames, time_info, status) -> None:
        x = indata[:, self._slice]  # 使うチャンネルだけ取り出す
        self.in_peak = float(np.abs(x).max()) if frames else 0.0
        if self.chain is not None:
            x = self.chain.process(x)
        else:
            x = np.ascontiguousarray(x, dtype=np.float32)

        resampler, ring = self._resampler, self._ring
        if ring is None:
            return
        if resampler is not None:
            resampler.ratio = self._drift_ratio(resampler)
            x = resampler.process(x)
        ring.write(x)

    def _drift_ratio(self, resampler: Resampler) -> float:
        """バッファの溜まり具合から比率を微調整して、入出力のクロック差を吸収する。"""
        ring = self._ring
        if ring is None or not self._target_fill:
            return resampler.base_ratio
        error = (ring.fill - self._target_fill) / self._target_fill
        # 溜まりすぎなら入力を速く読む（＝比率を上げる）。補正は最大 ±0.3%
        adjust = float(np.clip(error * 0.002, -0.003, 0.003))
        return resampler.base_ratio * (1.0 + adjust)

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        ring = self._ring
        if ring is None:
            outdata.fill(0)
            return
        # 出力は入力より先に走り出す。目標量まで貯まるのを待たないと、
        # 生産と消費が釣り合っていても毎回枯れてしまう。
        if self._priming:
            if ring.fill < self._target_fill:
                outdata.fill(0)
                self.out_peak = 0.0
                return
            self._priming = False
        elif ring.fill < frames:
            self._priming = True  # 枯れたら貯め直す
            outdata.fill(0)
            self.out_peak = 0.0
            return
        block = ring.read(frames)  # (フレーム, チャンネル)
        gain = 0.0 if self.muted else self.output_gain * self.mic_gain
        if gain != 1.0:
            block = block * gain
        self.out_peak = float(np.abs(block).max()) if frames else 0.0

        got = block.shape[1]
        if got == self.out_channels:
            outdata[:] = block
        elif got == 1:  # モノラルを両方へ配る
            outdata[:] = block[:, :1]
        else:  # ステレオをモノラル出力へまとめる
            outdata[:, 0] = block.mean(axis=1)


def probe_device(device: int, kind: str, timeout: float = 5.0) -> tuple[bool, str]:
    """デバイスを実際に開けるか、固まらずに確かめる。

    check_input_settings は通るのに open で固まるドライバがあるため、
    実際に開いて確認する。応答がなければタイムアウトとして扱う。
    """
    result: list = []

    def attempt():
        with com_initialized():
            try:
                info = sd.query_devices(device)
                rate = int(info["default_samplerate"])
                cls = sd.InputStream if kind == "input" else sd.OutputStream
                stream = cls(device=device, samplerate=rate, channels=1, dtype="float32")
                stream.start()  # 開けるだけでなく開始できるかまで確かめる
                stream.stop()
                stream.close()
                result.append((True, f"{rate}Hz で使えます"))
            except Exception as e:
                result.append((False, Router._describe(e)))

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(timeout)
    if not result:
        return False, "応答なし（このドライバは使えません）"
    return result[0]

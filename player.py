"""PyAV + sounddevice + Pillow だけで動く動画プレイヤー。

外部プレイヤーには依存しない。音声は sounddevice で鳴らすため出力デバイスを選べ、
マイクと同じイヤホンへ伴奏を流せる。映像は PIL.Image としてキューへ流し、
tkinter 側（メインスレッド）が取り出して描画する。

同期は音声クロック基準:
  - 音声コールバックが「いま鳴らしている音の PTS」を self._clock に書く
  - 表示側は PTS が clock 以下になった映像フレームだけを出す
"""

from __future__ import annotations

import queue
import threading
import time

import av
import numpy as np
import sounddevice as sd

from comutil import com_initialized

# ネットワーク再生時の FFmpeg オプション（切断時に再接続する）
_NET_OPTIONS = {
    "reconnect": "1",
    "reconnect_streamed": "1",
    "reconnect_on_network_error": "1",
    "reconnect_delay_max": "5",
    "rw_timeout": "20000000",  # 20 秒
}


def _fit(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """アスペクト比を保ったまま box に収まるサイズを返す。

    元より大きくはしない。引き伸ばしても画質は上がらないのに、
    変換と描画の手間だけが増える（640x360 を 960x540 にすると倍近く重くなる）。
    """
    if src_w <= 0 or src_h <= 0 or box_w <= 1 or box_h <= 1:
        return max(2, box_w), max(2, box_h)
    scale = min(box_w / src_w, box_h / src_h, 1.0)
    return max(2, int(src_w * scale)), max(2, int(src_h * scale))


class AVPlayer:
    """1 本の動画を再生する。スレッド構成は「デコーダ + 音声コールバック + 表示ポーリング」。"""

    def __init__(self, on_error=None, on_end=None):
        self.on_error = on_error
        self.on_end = on_end

        self.volume = 1.0
        self.duration: float | None = None
        self.state = "stopped"  # stopped / opening / playing / paused / ended / error

        self._video_q: queue.Queue = queue.Queue(maxsize=90)
        self._audio_q: queue.Queue = queue.Queue(maxsize=240)
        self._containers: list[av.container.InputContainer] = []
        self._threads: list[threading.Thread] = []
        self._stream: sd.OutputStream | None = None

        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._clock = 0.0
        self._gen = 0  # シーク世代。古いフレームを捨てるために使う
        self._seek_target = 0.0
        self._cur_audio: list | None = None  # [pts, ndarray, offset]
        # シーク直後、指定位置より手前のフレームを捨てるための閾値。
        # コンテナのシークは手前のキーフレームに着地するため、これが無いと
        # 映像と音声が別々の位置から再生されて同期が崩れる。
        self._skip_until = 0.0
        self._display_size = (960, 540)
        self._rate = 48000
        self._channels = 2
        self._eof = 0
        self._n_decoders = 0
        self._has_audio = False
        self._wall_base: float | None = None  # 音声が無い動画用のクロック基準
        self.video_size = (0, 0)  # 元の映像の大きさ（これより拡大しない）
        self.dropped = 0          # 間に合わず捨てたフレーム数

    # ---------- 情報 ----------
    @property
    def position(self) -> float:
        if not self._has_audio and self._wall_base is not None and self.state == "playing":
            return time.perf_counter() - self._wall_base
        return self._clock

    @property
    def finished(self) -> bool:
        return (
            self.state == "playing"
            and self._n_decoders > 0
            and self._eof >= self._n_decoders
            and self._cur_audio is None
            and self._audio_q.empty()
            and self._video_q.empty()
        )

    def set_display_size(self, width: int, height: int) -> None:
        """表示に使う大きさ。細かく変えると画像を作り直す羽目になるので粗く丸める。"""
        width, height = max(2, width), max(2, height)
        quantized = (width - width % 16, height - height % 16)
        if quantized != self._display_size:
            self._display_size = quantized

    # ---------- 開始 / 停止 ----------
    def open(self, video_url: str, audio_url: str | None = None,
             headers: dict | None = None, device: int | None = None,
             duration: float | None = None) -> None:
        """再生を開始する。ネットワーク待ちがあるので実処理は別スレッドで行う。"""
        self.stop()
        self._stop_evt = threading.Event()
        self.duration = duration
        self.state = "opening"
        self._clock = 0.0
        self._gen += 1
        self._eof = 0
        self._wall_base = None
        self._skip_until = 0.0

        out_dev = device if device is not None else sd.default.device[1]
        info = sd.query_devices(out_dev)
        self._rate = int(info["default_samplerate"])
        self._channels = min(2, max(1, info["max_output_channels"]))

        t = threading.Thread(
            target=self._open_worker,
            args=(video_url, audio_url, headers or {}, out_dev),
            daemon=True,
        )
        t.start()
        self._threads.append(t)

    def _av_open(self, url: str, headers: dict):
        options = dict(_NET_OPTIONS) if url.startswith("http") else {}
        if headers:
            ua = headers.get("User-Agent")
            if ua:
                options["user_agent"] = ua
            rest = "".join(
                f"{k}: {v}\r\n" for k, v in headers.items() if k.lower() != "user-agent"
            )
            if rest:
                options["headers"] = rest
        return av.open(url, options=options, timeout=(20.0, 20.0))

    def _open_worker(self, video_url, audio_url, headers, out_dev) -> None:
        # WASAPI の出力はスレッドごとに COM 初期化が要る
        with com_initialized():
            self._open_sources(video_url, audio_url, headers, out_dev)

    def _open_sources(self, video_url, audio_url, headers, out_dev) -> None:
        opened: list = []
        try:
            vc = self._av_open(video_url, headers)
            opened.append(vc)
            if self.duration is None and vc.duration:
                self.duration = vc.duration / av.time_base

            if audio_url:
                ac = self._av_open(audio_url, headers)
                opened.append(ac)
                self._has_audio = bool(ac.streams.audio)
                decoders = [(vc, video_url, True, False), (ac, audio_url, False, True)]
            else:
                self._has_audio = bool(vc.streams.audio)
                decoders = [(vc, video_url, True, True)]

            if self._stop_evt.is_set():  # 開いている途中で停止された
                self._close_all(opened)
                return

            self._n_decoders = len(decoders)
            stop_evt = self._stop_evt  # この再生ぶんの停止合図を捕まえておく
            for container, source, do_video, do_audio in decoders:
                t = threading.Thread(
                    target=self._decode_worker,
                    args=(container, source, headers, do_video, do_audio, stop_evt),
                    daemon=True,
                )
                t.start()
                self._threads.append(t)

            if self._has_audio:
                self._stream = sd.OutputStream(
                    device=out_dev,
                    samplerate=self._rate,
                    channels=self._channels,
                    dtype="float32",
                    latency="high",  # 動画再生は途切れない方を優先する
                    callback=self._audio_callback,
                )
                self._stream.start()
            else:
                self._wall_base = time.perf_counter()
            self.state = "playing"
        except Exception as e:
            self._close_all(opened)
            self.state = "error"
            if self.on_error:
                self.on_error(e)

    @staticmethod
    def _close_all(containers) -> None:
        for c in containers:
            try:
                c.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_evt.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        # コンテナはそれを使っているデコードスレッド自身が閉じる。
        # ここで閉じると、まだ読んでいるスレッドが解放済みメモリを触って落ちる。
        for t in list(self._threads):
            if t is not threading.current_thread():
                t.join(timeout=2.0)
        self._threads.clear()
        self._containers.clear()
        self._drain(self._video_q)
        self._drain(self._audio_q)
        self._cur_audio = None
        self._clock = 0.0
        self._wall_base = None
        self._n_decoders = 0
        self._eof = 0
        self.state = "stopped"

    def pause(self) -> None:
        if self.state != "playing":
            return
        if self._stream is not None:
            self._stream.stop()
        elif self._wall_base is not None:
            self._clock = time.perf_counter() - self._wall_base
        self.state = "paused"

    def resume(self) -> None:
        if self.state != "paused":
            return
        if self._stream is not None:
            self._stream.start()
        else:
            self._wall_base = time.perf_counter() - self._clock
        self.state = "playing"

    def toggle_pause(self) -> None:
        self.resume() if self.state == "paused" else self.pause()

    def seek(self, seconds: float) -> None:
        if self.state not in ("playing", "paused"):
            return
        with self._lock:
            self._seek_target = max(0.0, seconds)
            self._skip_until = self._seek_target
            self._gen += 1
            self._clock = self._seek_target
            self._cur_audio = None
            if self._wall_base is not None:
                self._wall_base = time.perf_counter() - self._seek_target
            self._eof = 0
        self._drain(self._video_q)
        self._drain(self._audio_q)

    # ---------- デコード ----------
    def _decode_worker(self, container, source: str, headers: dict,
                       do_video: bool, do_audio: bool, stop_evt: threading.Event) -> None:
        """1 本のソースをデコードし続ける。

        コンテナは自分で開き直し、自分で閉じる。他スレッドから閉じると
        読み込み中のメモリを触って落ちるため、所有権はこのスレッドにある。

        停止合図は引数で受け取る。self から読むと、次の曲が始まったときに
        差し替えられた新しい合図を見てしまい、止めたはずのスレッドが
        次の曲のバッファへ混ざり込む。
        """
        state = {"container": container}
        try:
            self._decode_loop(state, source, headers, do_video, do_audio, stop_evt)
        finally:
            try:
                if state["container"] is not None:
                    state["container"].close()
            except Exception:
                pass

    def _prepare(self, container, do_video: bool, do_audio: bool):
        vstream = container.streams.video[0] if do_video and container.streams.video else None
        astream = container.streams.audio[0] if do_audio and container.streams.audio else None
        if vstream is not None:
            vstream.thread_type = "AUTO"  # FFmpeg 側のマルチスレッドデコード
        streams = [s for s in (vstream, astream) if s is not None]
        resampler = None
        if astream is not None:
            resampler = av.AudioResampler(
                format="flt",  # パックド float32。sounddevice にそのまま渡せる
                layout="stereo" if self._channels == 2 else "mono",
                rate=self._rate,
            )
        return streams, resampler

    def _decode_loop(self, state: dict, source: str, headers: dict,
                     do_video: bool, do_audio: bool,
                     stop_evt: threading.Event) -> None:
        try:
            container = state["container"]
            streams, resampler = self._prepare(container, do_video, do_audio)
            if not streams:
                self._eof += 1
                return

            while not stop_evt.is_set():
                gen = self._gen
                completed = True
                try:
                    for packet in container.demux(*streams):
                        if stop_evt.is_set():
                            return
                        if self._gen != gen:
                            completed = False
                            break
                        try:
                            frames = packet.decode()
                        except av.FFmpegError:
                            continue
                        for frame in frames:
                            if stop_evt.is_set():
                                return
                            if isinstance(frame, av.VideoFrame):
                                self._push_video(frame, gen)
                            elif resampler is not None:
                                self._push_audio(frame, resampler, gen)
                except av.FFmpegError:
                    completed = True  # 読めなくなった。下で開き直す

                if completed:
                    # EOF。resampler に残った音を流し切る
                    if resampler is not None:
                        try:
                            for rf in resampler.resample(None):
                                self._push_audio_frame(rf, gen)
                        except av.FFmpegError:
                            pass
                    self._eof += 1
                    # ここで抜けるとコンテナが閉じてしまい、あとから巻き戻せなくなる。
                    # 曲の終わりから聴き直すのはよくある操作なので、シークを待つ。
                    while not stop_evt.is_set() and self._gen == gen:
                        time.sleep(0.05)
                    if stop_evt.is_set():
                        return
                    # 最後まで読んだコンテナはシークしても EOF のままになるので開き直す
                    container = self._reopen(state, source, headers)
                    streams, resampler = self._prepare(container, do_video, do_audio)

                with self._lock:
                    target = self._seek_target
                try:
                    container.seek(int(target * av.time_base))
                    for s in streams:
                        s.codec_context.flush_buffers()
                except av.FFmpegError:
                    container = self._reopen(state, source, headers)
                    streams, resampler = self._prepare(container, do_video, do_audio)
                    container.seek(int(target * av.time_base))
        except Exception as e:
            if not stop_evt.is_set():
                self.state = "error"
                if self.on_error:
                    self.on_error(e)

    def _reopen(self, state: dict, source: str, headers: dict):
        old = state["container"]
        state["container"] = None
        try:
            if old is not None:
                old.close()
        except Exception:
            pass
        container = self._av_open(source, headers)
        state["container"] = container
        return container

    def _push_video(self, frame, gen: int) -> None:
        pts = float(frame.pts * frame.time_base) if frame.pts is not None else self._clock
        if pts < self._skip_until - 0.02:
            return  # 変換前に捨てる（reformat は重い）
        # 既に再生位置を過ぎたフレームは、変換せずに捨てる。
        # 表示されないものに 5〜15ms かけるのは無駄で、それが積もると重くなる
        if self._has_audio and pts < self._clock - 0.12:
            self.dropped += 1
            return
        self.video_size = (frame.width, frame.height)
        box_w, box_h = self._display_size
        w, h = _fit(frame.width, frame.height, box_w, box_h)
        image = frame.reformat(width=w, height=h, format="rgb24").to_image()
        self._put(self._video_q, (gen, pts, image), gen)

    def _push_audio(self, frame, resampler, gen: int) -> None:
        for rf in resampler.resample(frame):
            self._push_audio_frame(rf, gen)

    def _push_audio_frame(self, rf, gen: int) -> None:
        pts = float(rf.pts * rf.time_base) if rf.pts is not None else self._clock
        if pts < self._skip_until - 0.02:
            return
        arr = rf.to_ndarray().reshape(-1, self._channels)
        self._put(self._audio_q, (gen, pts, arr.copy()), gen)

    def _put(self, q: queue.Queue, item, gen: int) -> None:
        """満杯なら待つ（先読みしすぎない）。停止・シークが来たら諦める。"""
        while not self._stop_evt.is_set() and self._gen == gen:
            try:
                q.put(item, timeout=0.2)
                return
            except queue.Full:
                continue

    @staticmethod
    def _drain(q: queue.Queue) -> None:
        with q.mutex:
            q.queue.clear()
            q.not_full.notify_all()

    # ---------- 音声出力 ----------
    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        filled = 0
        volume = self.volume
        while filled < frames:
            current = self._cur_audio
            if current is None:
                try:
                    gen, pts, arr = self._audio_q.get_nowait()
                except queue.Empty:
                    outdata[filled:] = 0  # バッファ不足。無音で埋める
                    return
                if gen != self._gen:
                    continue
                current = [gen, pts, arr, 0]
                self._cur_audio = current

            gen, pts, arr, offset = current
            if gen != self._gen:  # 再生中にシークされた
                self._cur_audio = None
                continue
            take = min(frames - filled, len(arr) - offset)
            chunk = arr[offset:offset + take]
            if volume == 1.0:
                outdata[filled:filled + take] = chunk
            else:
                np.multiply(chunk, volume, out=outdata[filled:filled + take])
            filled += take
            offset += take
            # シーク直後に古い PTS でクロックを巻き戻さないよう世代を確認する
            if gen == self._gen:
                self._clock = pts + offset / self._rate
            if offset >= len(arr):
                self._cur_audio = None
            else:
                current[3] = offset

    # ---------- 表示（メインスレッドから呼ぶ） ----------
    def take_frame(self):
        """再生位置に達した映像フレームを返す。無ければ None。"""
        clock = self.position
        image = None
        q = self._video_q
        with q.mutex:
            while q.queue:
                gen, pts, frame = q.queue[0]
                if gen == self._gen and pts > clock + 0.005:
                    break
                q.queue.popleft()
                q.not_full.notify()
                if gen == self._gen:
                    image = frame
        return image

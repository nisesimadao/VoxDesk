"""スマホから操作するためのリモコン用サーバ。

同じ Wi-Fi につないだスマホのブラウザから、曲を探して予約したり、
キーを変えたり、手元で歌詞と映像を見たりできるようにする。
カラオケは複数人で使うものなので、PC の前に居る人だけが操作できる
状態だと不便、というのがこれを作る理由。

作りは意図的に素朴にしている:
  - 標準ライブラリの http.server だけを使う（追加の依存を増やさない）
  - 画面への通知は Server-Sent Events（WebSocket を実装せずに済む）
  - 操作は POST 1 本

音声・VST3・再生の処理には一切触らない。ここは既存の仕組みを
外から呼ぶだけの層で、止めてもアプリはそのまま動く。
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import applog

LOG = applog.get(__name__)

DEFAULT_PORT = 8730
# 凍結ビルドでは同梱ファイルの展開先が変わる
WEB_DIR = os.path.join(
    getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "web")
# 映像を JPEG にして流すときの間引き（元が 30fps でも、手元で歌詞を
# 追う用途ならこれで十分。CPU と帯域を無駄にしない）
MJPEG_FPS = 12
MJPEG_QUALITY = 70


def _image(frame):
    """再生側から来た絵を、JPEG にできる形にする。"""
    from PIL import Image

    if hasattr(frame, "shape"):  # (高さ, 幅, 3) の生データ
        return Image.fromarray(frame, "RGB")
    return frame.convert("RGB")  # 以前の画面（Tk 版）は画像で渡してくる


def lan_address() -> str:
    """同じネットワークの端末から見た、この PC の住所を返す。

    外へ実際の通信は行わない。経路表を引くために宛先を設定するだけ。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # 到達しない前提の文書用アドレス
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


class Remote:
    """アプリの状態を読み、操作を届ける係。

    画面（Tk）に触る操作は必ず app.post() 経由でメインスレッドへ渡す。
    読み取りだけのものは、別スレッドから触っても安全な値に限っている。
    """

    def __init__(self, app):
        self.app = app
        # 予約は本体が持っている。リモコンを閉じても残るし、
        # 本体の画面とスマホで同じものを見ることになる
        self.queue = app.queue

    # ---------- 読み取り ----------
    def snapshot(self) -> dict:
        app = self.app
        player = app.player
        track = getattr(app, "current_track", None)
        lyrics = getattr(app, "current_lyrics", None)
        position = player.position if player.state in ("playing", "paused") else 0.0
        current_line = next_line = ""
        sung = 0
        if lyrics is not None and lyrics.synced:
            index = lyrics.index_at(position)
            if index >= 0:
                line = lyrics.lines[index]
                current_line, sung = line.text, line.sung(position)
                if index + 1 < len(lyrics.lines):
                    next_line = lyrics.lines[index + 1].text
            elif lyrics.lines:
                next_line = lyrics.lines[0].text
        queue = [entry.as_dict() for entry in self.queue.list()]
        return {
            "state": player.state,
            "title": (track.title if track is not None else "")
                     or os.path.basename(getattr(app, "current_local_path", "") or ""),
            "position": round(position, 2),
            "duration": round(player.duration or 0.0, 2),
            "key": int(app.cfg.get("pitch_semitones", 0)),
            "volume": float(app.cfg.get("music_volume", 1.0)),
            "queue": queue,
            "lyric": current_line,
            "lyric_sung": sung,  # いま歌い終えた文字数（1 文字ずつの曲だけ）
            "lyric_next": next_line,
            "video": self.video_kind(),
        }

    def video_kind(self) -> str:
        """手元の画面へ映像をどう届けるか。

        ファイルが手元にあるならブラウザに直接デコードさせる（こちらは
        何もしなくて済む）。配信をそのまま鳴らしている場合はそれが
        できないので、描いた絵を JPEG にして流す。
        """
        path = getattr(self.app, "current_local_path", None)
        if path and os.path.exists(path):
            return "file"
        if getattr(self.app, "last_frame", None) is not None:
            return "mjpeg"
        return "none"

    # ---------- 操作 ----------
    def control(self, action: str, value=None) -> dict:
        app = self.app
        if action == "play_pause":
            app.post(app.toggle_play)
        elif action == "stop":
            app.post(app.stop_music)
        elif action == "next":
            # 予約が空のときは何もしない。歌っている最中に押されて
            # 曲が止まると、取り返しがつかない
            if not len(self.queue):
                return {"ok": False, "error": "予約がありません"}
            app.post(self.play_next)
        elif action == "seek":
            position = float(value or 0) * (app.player.duration or 0)
            app.post(app.player.seek, position)
        elif action == "key":
            app.post(self._set_key, int(value))
        elif action == "volume":
            app.post(self._set_volume, float(value))
        else:
            return {"ok": False, "error": f"知らない操作です: {action}"}
        return {"ok": True}

    def _set_key(self, semitones: int) -> None:
        # 画面側と同じ道を通す（表示の更新や、止まっているときの扱いも任せる）
        current = int(self.app.cfg.get("pitch_semitones", 0))
        delta = max(-6, min(6, semitones)) - current
        if delta:
            self.app.change_key(delta)

    def _set_volume(self, volume: float) -> None:
        # 画面側にも反映させたいので、つまみの実装は本体に任せる
        self.app.set_music_volume(max(0.0, min(1.5, volume)))

    # ---------- 予約 ----------
    def enqueue(self, video_id: str, title: str) -> dict:
        import playqueue

        waiting = self.queue.add(playqueue.Entry(
            title=title, video_id=video_id, added_by="スマホ"))
        # 何も鳴っていなければ、そのまま歌い始められるようにする
        if self.app.player.state in ("stopped", "ended", "error"):
            self.app.post(self.play_next)
            return {"ok": True, "started": True}
        return {"ok": True, "waiting": waiting}

    def dequeue(self, index: int) -> dict:
        self.queue.remove(index)
        return {"ok": True}

    def play_next(self) -> None:
        """予約の先頭を再生する。メインスレッドから呼ばれる。"""
        if not self.app.play_next_in_queue():
            self.app.stop_music("予約はもうありません")

    def song_finished(self) -> None:
        """1 曲終わったときにアプリから呼ばれる（本体が予約を進める）。"""
        if len(self.queue):
            self.play_next()

    # ---------- 検索 ----------
    def search(self, query: str) -> list[dict]:
        import music_search

        tracks = music_search.search(
            query, limit=15,
            prefer_off_vocal=bool(self.app.cfg.get("prefer_off_vocal", True)),
            trusted_only=bool(self.app.cfg.get("trusted_only", False)))
        return [{"id": t.id, "title": t.title, "uploader": t.uploader,
                 "duration": t.duration_text, "trusted": t.trusted}
                for t in tracks]

    def ranking(self, source: str) -> list[dict]:
        import ranking

        return [{"rank": s.rank, "title": s.title, "artist": s.artist,
                 "query": s.query}
                for s in ranking.fetch(source)]

    @staticmethod
    def ranking_sources() -> list[str]:
        import ranking

        return list(ranking.SOURCES)


class _Handler(BaseHTTPRequestHandler):
    server_version = "VoxDesk"
    protocol_version = "HTTP/1.1"

    @property
    def remote(self) -> Remote:
        return self.server.remote

    def log_message(self, fmt, *args):  # 既定の標準エラーへの出力は止める
        pass

    # ---------- 返し方 ----------
    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------- GET ----------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._send_page()
            if path == "/state":
                return self._json(self.remote.snapshot())
            if path == "/events":
                return self._send_events()
            if path == "/ranking":
                source = (query.get("source") or ["総合"])[0]
                return self._json({"sources": self.remote.ranking_sources(),
                                   "songs": self.remote.ranking(source)})
            if path == "/video.mp4":
                return self._send_video_file()
            if path == "/video.mjpeg":
                return self._send_mjpeg()
            self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # スマホ側が画面を閉じただけ
        except Exception as e:
            LOG.warning("リモコンの要求で失敗 %s: %s", path, e)
            try:
                self._json({"ok": False, "error": str(e)}, code=500)
            except Exception:
                pass

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        try:
            if parsed.path == "/search":
                return self._json({"songs": self.remote.search(payload.get("query", ""))})
            if parsed.path == "/queue":
                return self._json(self.remote.enqueue(payload.get("id", ""),
                                                      payload.get("title", "")))
            if parsed.path == "/unqueue":
                return self._json(self.remote.dequeue(int(payload.get("index", -1))))
            if parsed.path == "/control":
                return self._json(self.remote.control(payload.get("action", ""),
                                                      payload.get("value")))
            self._send(404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            LOG.warning("リモコンの操作で失敗 %s: %s", parsed.path, e)
            try:
                self._json({"ok": False, "error": str(e)}, code=500)
            except Exception:
                pass

    # ---------- それぞれの中身 ----------
    def _send_page(self) -> None:
        path = os.path.join(WEB_DIR, "remote.html")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._send(500, "画面のファイルが見つかりません".encode("utf-8"),
                              "text/plain; charset=utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _send_events(self) -> None:
        """状態を送り続ける（Server-Sent Events）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        while not self.server.stopping:
            snapshot = self.remote.snapshot()
            # 秒より細かい位置の変化だけでは送らない（無駄な通信を減らす）
            key = json.dumps({**snapshot, "position": int(snapshot["position"])},
                             ensure_ascii=False)
            if key != last:
                last = key
                self.wfile.write(
                    f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            time.sleep(0.4)

    def _send_video_file(self) -> None:
        """手元のファイルをそのまま渡す（途中から再生できるようにする）。"""
        path = getattr(self.remote.app, "current_local_path", None)
        if not path or not os.path.exists(path):
            return self._send(404, b"no video", "text/plain")
        size = os.path.getsize(path)
        kind = mimetypes.guess_type(path)[0] or "video/mp4"
        start, end = 0, size - 1
        header = self.headers.get("Range")
        partial = False
        if header and header.startswith("bytes="):
            piece = header[6:].split("-")
            try:
                start = int(piece[0]) if piece[0] else 0
                if len(piece) > 1 and piece[1]:
                    end = int(piece[1])
                partial = True
            except ValueError:
                partial = False
        end = min(end, size - 1)
        length = max(0, end - start + 1)

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_mjpeg(self) -> None:
        """アプリが描いている絵を、そのまま連番の JPEG として流す。"""
        boundary = "voxdeskframe"
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        interval = 1.0 / MJPEG_FPS
        last_id = None
        while not self.server.stopping:
            frame = getattr(self.remote.app, "last_frame", None)
            if frame is not None and id(frame) != last_id:
                last_id = id(frame)
                buffer = io.BytesIO()
                # 再生側は (高さ, 幅, 3) の生データで渡してくる。
                # JPEG にするのはここだけなので、この場で画像にする
                _image(frame).save(buffer, "JPEG", quality=MJPEG_QUALITY)
                data = buffer.getvalue()
                self.wfile.write(
                    f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            time.sleep(interval)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, remote: Remote):
        super().__init__(address, _Handler)
        self.remote = remote
        self.stopping = False

    def handle_error(self, request, client_address) -> None:
        """つなぎが切れただけのときに、大げさな記録を残さない。

        スマホが画面を閉じる・別のページへ移る、は普通に起きること。
        既定では標準エラーへ長い traceback が出てしまう。
        """
        kind = sys.exc_info()[0]
        if kind is not None and issubclass(
                kind, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        LOG.warning("リモコンの接続で問題", exc_info=True)


class RemoteServer:
    """リモコン用サーバの入切をまとめた入れ物。"""

    def __init__(self, app, port: int = DEFAULT_PORT):
        self.app = app
        self.port = port
        self.remote = Remote(app)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def url(self) -> str:
        return f"http://{lan_address()}:{self.port}/"

    def start(self) -> str:
        if self._server is not None:
            return self.url
        # 同じ Wi-Fi の端末から入れるようにするため、全てのあて先で受ける
        self._server = _Server(("0.0.0.0", self.port), self.remote)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="remote", daemon=True)
        self._thread.start()
        LOG.info("リモコンを開きました: %s", self.url)
        return self.url

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.stopping = True
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        LOG.info("リモコンを閉じました")

    def song_finished(self) -> None:
        if self._server is not None:
            self.remote.song_finished()

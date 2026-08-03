"""VoxDesk の画面（wxPython 版）。

Tk はボタンもスライダーも自前で描いていて、部品を 1 つ作るのに数十
ミリ秒かかっていた。映像の貼り替えも 1280x720 で 15ms 近くかかり、
歌っている間じゅう画面が重かった。ここでは OS 本物の部品を使う
（Windows なら Win32 の BUTTON や msctls_trackbar32 がそのまま出る）。

音声・再生・VST3 の処理はこの下の層（router / mic_chain / player /
music_search / lyrics / ranking / devices / separator）がすべて持っていて、
ここはそれを呼んで並べるだけ。
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import threading
import time

import numpy as np
import wx

import applog
import config
import devices as dev
import juce_thread
import mic_chain
import music_search
import platform_support
import playqueue
import webserver
from mic_chain import MicChain, available_vst3, karaoke_preset
from player import AVPlayer
from router import Router
from uicommon import (
    AI_MIC_MODE,
    APP_TITLE,
    DENOISE_MODES,
    EFFECT_ROWS,
    INPUT_MODES,
    LATENCY_MODES,
    PRESETS,
    RNNOISE_MODE,
    db_of,
    friendly_error,
    meter_value,
    resource_path,
    time_text,
)

LOG = applog.get(__name__)


def _enable_high_dpi() -> None:
    """画面の拡大率をアプリ側で扱うと OS に伝える。

    これを言わないと Windows は 100% 用に描かせてから引き伸ばすので、
    150% の画面では文字も部品も全部ぼやける。窓を 1 つも作る前に
    呼ぶ必要があるため、この場所（読み込み時）で行う。
    """
    if not sys.platform.startswith("win"):
        return  # macOS と Linux(GTK) は既定で正しく扱われる
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 画面ごとに対応
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # 古い Windows 向け
        except Exception:
            pass


_enable_high_dpi()

MANUAL_PRESET = "手動調整"

DARK = wx.Colour(0x10, 0x10, 0x14)
LYRIC_NOW = wx.Colour(0xF2, 0xF4, 0xF8)
LYRIC_NEXT = wx.Colour(0x8A, 0x90, 0xA0)
LYRIC_SUNG = wx.Colour(0xFF, 0x5A, 0x7A)  # 歌い終えたところ
HINT = wx.Colour(0x66, 0x66, 0x72)


# ---------------------------------------------------------------- 小さな部品
class FloatSlider(wx.Slider):
    """小数を扱えるスライダー。

    wx.Slider は整数しか持てないので、内部では 1000 倍した整数で扱う。
    """

    STEPS = 1000

    def __init__(self, parent, low: float, high: float, value: float, **kwargs):
        # VST3 のパラメータには上下限や現在値が NaN のものが混ざっている
        self.low = self._finite(low, 0.0)
        self.high = self._finite(high, 1.0)
        if self.high <= self.low:
            self.high = self.low + 1.0
        super().__init__(parent, value=self._to_int(value), minValue=0,
                         maxValue=self.STEPS, **kwargs)

    @staticmethod
    def _finite(value, fallback: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if number == number and abs(number) != float("inf") else fallback

    def _to_int(self, value: float) -> int:
        span = self.high - self.low or 1.0
        ratio = (self._finite(value, self.low) - self.low) / span
        return int(round(max(0.0, min(1.0, ratio)) * self.STEPS))

    def GetFloat(self) -> float:
        return self.low + (self.high - self.low) * (self.GetValue() / self.STEPS)

    def SetFloat(self, value: float) -> None:
        self.SetValue(self._to_int(value))


class VideoView(wx.Panel):
    """映像を出す枠。

    枠の大きさは外から決め、中の絵は中央に置く。元より大きくは映さない
    （拡大すると見た目が悪くなるうえ、変換の手間だけが増える）。
    """

    def __init__(self, parent):
        super().__init__(parent, style=wx.FULL_REPAINT_ON_RESIZE)
        self.SetBackgroundColour(DARK)
        self.SetMinSize(self.FromDIP(wx.Size(-1, 240)))
        self._bitmap: wx.Bitmap | None = None
        self._message = "ここに映像が出ます（歌詞つき動画ならそのまま歌えます）"
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)  # ちらつきを防ぐ
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def show_frame(self, array) -> None:
        """(高さ, 幅, 3) の生データを受け取って、その場で描く。

        Refresh() で塗り直しに任せると、毎コマ 1562x533 の裏紙を用意して
        全面を黒で塗ってから絵を載せることになる。絵で埋まらない縁だけを
        塗り、絵は直接転送する。
        """
        height, width = array.shape[0], array.shape[1]
        self._bitmap = wx.Bitmap.FromBuffer(width, height, array)
        self._message = ""
        dc = wx.ClientDC(self)
        self._draw(dc, clear_all=False)

    def clear(self, message: str = "ここに映像が出ます") -> None:
        self._bitmap = None
        self._message = message
        self.Refresh()

    def _on_paint(self, _event) -> None:
        self._draw(wx.AutoBufferedPaintDC(self), clear_all=True)

    def _draw(self, dc, clear_all: bool) -> None:
        width, height = self.GetClientSize()
        if self._bitmap is None:
            dc.SetBackground(wx.Brush(DARK))
            dc.Clear()
            if self._message:
                dc.SetTextForeground(HINT)
                text_width, text_height = dc.GetTextExtent(self._message)
                dc.DrawText(self._message, (width - text_width) // 2,
                            (height - text_height) // 2)
            return

        image_width, image_height = self._bitmap.GetWidth(), self._bitmap.GetHeight()
        x = max(0, (width - image_width) // 2)
        y = max(0, (height - image_height) // 2)
        if clear_all:
            dc.SetBackground(wx.Brush(DARK))
            dc.Clear()
        else:
            # 絵で埋まらない縁だけを塗る。全面を塗ると、その分だけ毎コマ無駄
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(DARK))
            if y > 0:
                dc.DrawRectangle(0, 0, width, y)
                dc.DrawRectangle(0, y + image_height, width, height - y - image_height)
            if x > 0:
                dc.DrawRectangle(0, y, x, image_height)
                dc.DrawRectangle(x + image_width, y, width - x - image_width, image_height)
        dc.DrawBitmap(self._bitmap, x, y, False)


def hint_label(parent, text: str = "") -> wx.StaticText:
    label = wx.StaticText(parent, label=text)
    label.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
    return label


class LyricView(wx.Panel):
    """歌詞を出す枠。

    1 文字ずつの頭出しがある曲では、歌い終えた分だけ色を変えて進める
    （カラオケの色変わりと同じ）。無ければ行がそのまま出るだけ。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(DARK)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self._now = ""
        self._sung = 0
        self._next = ""
        base = self.GetFont()
        self._now_font = wx.Font(base)
        self._now_font.SetPointSize(base.GetPointSize() + 8)
        self._now_font.MakeBold()
        self._next_font = wx.Font(base)
        self._next_font.SetPointSize(base.GetPointSize() + 1)
        self.SetMinSize(self.FromDIP(wx.Size(-1, 78)))
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def show(self, now: str, sung: int, following: str) -> None:
        if (now, sung, following) == (self._now, self._sung, self._next):
            return
        self._now, self._sung, self._next = now, sung, following
        self.Refresh(eraseBackground=False)

    def _on_paint(self, _event) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(DARK))
        dc.Clear()
        width, height = self.GetClientSize()

        dc.SetFont(self._now_font)
        text_width, text_height = dc.GetTextExtent(self._now or " ")
        x, y = (width - text_width) // 2, 6
        if self._sung > 0:
            # 歌い終えた分と、これからの分を続けて描く
            done = self._now[:self._sung]
            done_width, _ = dc.GetTextExtent(done)
            dc.SetTextForeground(LYRIC_SUNG)
            dc.DrawText(done, x, y)
            dc.SetTextForeground(LYRIC_NOW)
            dc.DrawText(self._now[self._sung:], x + done_width, y)
        else:
            dc.SetTextForeground(LYRIC_NOW)
            dc.DrawText(self._now, x, y)

        dc.SetFont(self._next_font)
        next_width, _ = dc.GetTextExtent(self._next or " ")
        dc.SetTextForeground(LYRIC_NEXT)
        dc.DrawText(self._next, (width - next_width) // 2, y + text_height + 4)


def wrapped_label(parent, text: str) -> wx.StaticText:
    """幅に合わせて折り返す説明文。

    決め打ちの幅で折ると、画面の拡大率や窓の大きさによって
    「同じ Wi-Fi」で改行されるような妙な切れ方をする。
    """
    label = hint_label(parent, text)
    label._source = text

    def rewrap(event):
        width = event.GetSize().width - 12
        if width > 80 and getattr(label, "_wrapped_at", 0) != width:
            label._wrapped_at = width
            label.SetLabel(label._source)
            label.Wrap(width)
        event.Skip()

    parent.Bind(wx.EVT_SIZE, rewrap)
    return label


def head_label(parent, text: str) -> wx.StaticText:
    """区画の見出し。太字にして、どこが何なのかを目で追えるようにする。"""
    label = wx.StaticText(parent, label=text)
    font = label.GetFont()
    font.MakeBold()
    label.SetFont(font)
    return label


def stretch_column(listing: wx.ListCtrl, index: int) -> None:
    """余った横幅を、指定した列に足して一覧を端まで使う。"""
    others = sum(listing.GetColumnWidth(i) for i in range(listing.GetColumnCount())
                 if i != index)
    room = listing.GetClientSize().width - others - 4  # 4 は枠のぶん
    if room > 60 and abs(room - listing.GetColumnWidth(index)) > 4:
        listing.SetColumnWidth(index, room)


def emphasize(widget, points: int = 1) -> None:
    """よく押す操作を、少し大きく太くする。"""
    font = widget.GetFont()
    font.SetPointSize(font.GetPointSize() + points)
    font.MakeBold()
    widget.SetFont(font)


def boxed(parent, title: str) -> tuple[wx.StaticBoxSizer, wx.Window]:
    """枠付きの区画を作り、(sizer, 中に入れる親) を返す。"""
    box = wx.StaticBox(parent, label=title)
    return wx.StaticBoxSizer(box, wx.VERTICAL), box


# ---------------------------------------------------------------- 本体
class VoxDesk(wx.Frame):
    URL_PATTERN = re.compile(r"https?://\S+", re.I)
    VIDEO_EXTENSIONS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v")

    def dip(self, width: int, height: int = -1) -> wx.Size:
        """画面の拡大率に合わせた大きさを返す。

        拡大率を自分で扱うと決めた以上、数値は実際の点の数になる。
        150% の画面では 1.5 倍にしないと、部品が全部小さくなる。
        """
        return self.FromDIP(wx.Size(width, height))

    def __init__(self):
        super().__init__(None, title=APP_TITLE)
        # 画面に収まる大きさで開く。ノートでも下の再生ボタンが隠れないように
        wanted = self.FromDIP(wx.Size(1080, 780))
        display = wx.Display().GetClientArea()
        self.SetSize(min(wanted.width, display.width - 80),
                     min(wanted.height, display.height - 80))
        self.SetMinSize(self.dip(760, 520))
        self.Centre()

        self.cfg = config.load()
        self.chain = MicChain(48000)
        self.player = AVPlayer(on_error=lambda e: wx.CallAfter(self._player_error, e))
        self.router = Router(
            chain=self.chain,
            on_state=lambda state, message: wx.CallAfter(self._router_state, state, message))

        self.tracks: list[music_search.Track] = []
        self.current_track: music_search.Track | None = None
        self.current_local_path: str | None = None
        self.current_lyrics = None
        # 次に歌う曲の並び。本体からもスマホからも同じものを触る
        self.queue = playqueue.PlayQueue(
            on_change=lambda: wx.CallAfter(self._refresh_queue))
        self.separator_capability = None
        self.remote = None
        self.last_frame = None
        self.editors: dict[int, dict] = {}
        self.mic_devices: list[dev.Device] = []
        self.out_devices: list[dev.Device] = []
        self._busy = 0
        self._last_xruns = 0
        self._seeking = False
        self._transport_state = ""
        self._tick_count = 0
        self._diag_running = False
        self._diag_cancel = False
        self._diag_rows: dict[int, dev.Device] = {}
        self._diag_status = None
        self._param_rows: list[dict] = []
        self._param_signature = None
        self._param_build = 0
        self._param_show_all = False
        self._param_last_key = None
        self._param_pending = False
        self._param_dragging = False
        self._key_timer = None  # ♯♭ の連打をまとめるための待ち
        self._key_job = 0       # 途中でキーが変わったら古い結果は捨てる

        self._ready = False
        self._set_icon()
        self._build()
        self._apply_effects_from_config()

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_tick, self.timer)
        # Windows のタイマーは 15.6ms 単位に丸められる。33ms を頼むと実際は
        # 46ms 間隔になり、30fps の映像が 21fps までしか出せない。
        self.timer.Start(15)

        self.Show()
        # 残りのタブと機器の読み込みは、窓を出してから。
        # 部品はネイティブなので 1 つ作るごとに OS を呼ぶ。4 タブ分をまとめて
        # 作ってから出すと、その間ずっと何も出ない。
        wx.CallLater(1, self._build_rest)

    def _build_rest(self) -> None:
        self._build_mic_tab()
        self._build_vst_tab()
        self._build_setup_tab()
        # 窓を出したあとに作った頁には「大きさが決まった」の通知が来ない。
        # ここで並べ直さないと、部品が左上に重なったまま出る
        for tab in (self.mic_tab, self.vst_tab, self.setup_tab):
            tab.Layout()
        self._ready = True
        self._reload_devices()
        wx.CallLater(150, self._restore_vst)
        wx.CallLater(550, self._check_separator)
        if self.cfg.get("first_run"):
            wx.CallLater(400, self._first_run)

    def _set_icon(self) -> None:
        try:
            path = resource_path("assets", "icon.ico" if platform_support.WINDOWS
                                 else "icon_128.png")
            if os.path.exists(path):
                self.SetIcon(wx.Icon(path))
        except Exception:
            pass

    # ---------------------------------------------------------- 土台
    def _build(self) -> None:
        panel = wx.Panel(self)
        outer = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(panel)
        self.karaoke_tab = wx.Panel(self.notebook)
        self.mic_tab = wx.Panel(self.notebook)
        self.vst_tab = wx.Panel(self.notebook)
        self.setup_tab = wx.Panel(self.notebook)
        self.notebook.AddPage(self.karaoke_tab, "カラオケ")
        self.notebook.AddPage(self.mic_tab, "マイク")
        self.notebook.AddPage(self.vst_tab, "エフェクト(VST3)")
        self.notebook.AddPage(self.setup_tab, "設定・診断")
        self.notebook.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._on_tab_changed)
        outer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 6)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.status_label = hint_label(panel, "準備完了")
        bar.Add(self.status_label, 0, wx.ALIGN_CENTER_VERTICAL)
        self.busy_bar = wx.Gauge(panel, range=100, size=self.dip(110, 12))
        bar.Add(self.busy_bar, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 8)
        self.busy_bar.Hide()
        bar.AddStretchSpacer()
        self.mic_state_label = hint_label(panel, "マイク: 停止中")
        bar.Add(self.mic_state_label, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        panel.SetSizer(outer)

        self._build_karaoke_tab()  # 残りのタブは窓を出してから組む

        # よく使う操作はキーでも
        accelerators = []
        for key, handler in ((wx.WXK_SPACE, self._accel_space),
                             (wx.WXK_LEFT, lambda _e: self._nudge(-5)),
                             (wx.WXK_RIGHT, lambda _e: self._nudge(5))):
            ident = wx.NewIdRef()
            self.Bind(wx.EVT_MENU, handler, id=ident)
            accelerators.append((wx.ACCEL_NORMAL, key, ident))
        self.SetAcceleratorTable(wx.AcceleratorTable(accelerators))

    def _accel_space(self, _event) -> None:
        # 文字入力の最中は邪魔しない
        focus = wx.Window.FindFocus()
        if isinstance(focus, (wx.TextCtrl, wx.ComboBox)):
            focus.WriteText(" ") if isinstance(focus, wx.TextCtrl) else None
            return
        self.toggle_play()

    def _nudge(self, seconds: float) -> None:
        focus = wx.Window.FindFocus()
        if isinstance(focus, (wx.TextCtrl, wx.ComboBox)):
            return
        if self.player.state in ("playing", "paused"):
            self.player.seek(max(0.0, self.player.position + seconds))

    def post(self, func, *args) -> None:
        """ワーカースレッドから画面の更新を依頼する。"""
        wx.CallAfter(func, *args)

    def set_status(self, text: str) -> None:
        self.status_label.SetLabel(text)
        self.status_label.GetContainingSizer().Layout()

    def run_async(self, work, done=None, busy_text: str = "", on_error=None) -> None:
        """重い処理を別スレッドで実行し、結果を画面のスレッドへ返す。"""
        if busy_text:
            self._busy += 1
            self.set_status(busy_text)
            self._set_busy(True)

        def worker():
            try:
                result, error = work(), None
            except Exception as e:  # noqa: BLE001 - 画面に出して知らせる
                result, error = None, e
            wx.CallAfter(self._finish_async, done, result, error, bool(busy_text), on_error)

        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        """待たせている間だけ、動いていることが目で分かる帯を出す。

        wx の帯は Tk と違って勝手には動かない。Pulse() を呼んで初めて
        流れ出すので、出した時点で 1 回、そのあとも定期的に叩く。
        """
        self.busy_bar.Show(busy)
        if busy:
            self.busy_bar.Pulse()
        else:
            self.busy_bar.SetValue(0)
        self.busy_bar.GetContainingSizer().Layout()

    def _finish_async(self, done, result, error, was_busy, on_error) -> None:
        if was_busy:
            self._busy = max(0, self._busy - 1)
            if self._busy == 0:
                self.set_status("準備完了")
                self._set_busy(False)
        if error is not None:
            LOG.error("処理に失敗しました",
                      exc_info=(type(error), error, error.__traceback__))
            if on_error is not None:
                on_error(error)
                return
            message = friendly_error(error)
            self.set_status(message)
            wx.MessageBox(message, APP_TITLE, wx.OK | wx.ICON_ERROR, self)
            return
        if done:
            done(result)

    def ask(self, message: str) -> bool:
        return wx.MessageBox(message, APP_TITLE,
                             wx.YES_NO | wx.ICON_QUESTION, self) == wx.YES

    # ---------------------------------------------------------- カラオケタブ
    def _build_karaoke_tab(self) -> None:
        tab = self.karaoke_tab
        sizer = wx.BoxSizer(wx.VERTICAL)

        search = wx.BoxSizer(wx.HORIZONTAL)
        search.Add(head_label(tab, "曲を探す"), 0,
                   wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        # 曲名でも、YouTube の URL を貼っても同じ欄で受ける
        self.query = wx.TextCtrl(tab, style=wx.TE_PROCESS_ENTER)
        self.query.Bind(wx.EVT_TEXT_ENTER, lambda _e: self.search())
        search.Add(self.query, 1, wx.ALIGN_CENTER_VERTICAL)
        go = wx.Button(tab, label="検索")
        go.Bind(wx.EVT_BUTTON, lambda _e: self.search())
        search.Add(go, 0, wx.LEFT, 6)
        enqueue = wx.Button(tab, label="予約に追加")
        enqueue.Bind(wx.EVT_BUTTON, lambda _e: self.enqueue_selected())
        search.Add(enqueue, 0, wx.LEFT, 6)
        self.offvocal = wx.CheckBox(tab, label="オフボーカルを優先")
        self.offvocal.SetValue(bool(self.cfg.get("prefer_off_vocal", True)))
        search.Add(self.offvocal, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self.trusted = wx.CheckBox(tab, label="公式カラオケ配信元のみ")
        self.trusted.SetValue(bool(self.cfg.get("trusted_only", False)))
        self.trusted.Bind(wx.EVT_CHECKBOX, lambda _e: self.search())
        search.Add(self.trusted, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        sizer.Add(search, 0, wx.EXPAND | wx.ALL, 8)

        extra = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (("ランキングから選ぶ", self.show_ranking),
                               ("ファイルを開く", self.open_local_file),
                               ("音源の入手先", self.show_sources)):
            button = wx.Button(tab, label=label)
            button.Bind(wx.EVT_BUTTON, lambda _e, h=handler: h())
            extra.Add(button, 0, wx.RIGHT, 6)
        self.vocal_button = wx.Button(tab, label="ボーカルを消す")
        self.vocal_button.Enable(False)
        self.vocal_button.Bind(wx.EVT_BUTTON, lambda _e: self.remove_vocals())
        extra.Add(self.vocal_button, 0, wx.RIGHT, 6)
        self.vocal_hint = hint_label(tab, "（対応環境を確認中…）")
        extra.Add(self.vocal_hint, 0, wx.ALIGN_CENTER_VERTICAL)
        extra.AddStretchSpacer()
        self.show_lyrics = wx.CheckBox(tab, label="歌詞を出す")
        self.show_lyrics.SetValue(bool(self.cfg.get("show_lyrics", True)))
        self.show_lyrics.Bind(wx.EVT_CHECKBOX, lambda _e: self._on_lyrics_toggle())
        extra.Add(self.show_lyrics, 0, wx.ALIGN_CENTER_VERTICAL)
        choose = wx.Button(tab, label="曲を指定")
        choose.Bind(wx.EVT_BUTTON, lambda _e: self.choose_lyrics())
        extra.Add(choose, 0, wx.LEFT, 6)
        self.lyric_status = hint_label(tab)
        extra.Add(self.lyric_status, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        sizer.Add(extra, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # 検索結果と予約は同じ場所を分け合う。縦幅は限られていて、
        # 同時に見たいものでもない
        self.lists = wx.Notebook(tab)
        self.results = wx.ListCtrl(self.lists,
                                   style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (text, width, align) in enumerate(
                (("オフボーカル度", 110, wx.LIST_FORMAT_CENTRE),
                 ("長さ", 60, wx.LIST_FORMAT_CENTRE),
                 ("タイトル", 560, wx.LIST_FORMAT_LEFT),
                 ("チャンネル", 200, wx.LIST_FORMAT_LEFT))):
            self.results.InsertColumn(index, text, format=align,
                                      width=self.FromDIP(width))
        self.results.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self.play_selected())
        # 幅が余ると右端に空白の列ができて間の抜けた見た目になる。
        # 余った分はタイトルに渡す
        self.results.Bind(wx.EVT_SIZE, lambda e: (stretch_column(self.results, 2), e.Skip()))
        self.lists.AddPage(self.results, "検索結果")
        self.lists.AddPage(self._build_queue_page(self.lists), "予約")
        self.lists.SetMinSize(self.dip(-1, 170))
        # 一覧を固定の高さにすると、曲が 6 行ちょっとしか見えないのに
        # 映像の黒い枠が画面の半分以上を占める。両方を伸ばして分け合う
        sizer.Add(self.lists, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.video = VideoView(tab)
        self.video.Bind(wx.EVT_LEFT_DCLICK, lambda _e: self.toggle_play())
        sizer.Add(self.video, 2, wx.EXPAND | wx.ALL, 8)

        self.lyric_panel = LyricView(tab)
        self.lyric_panel.Hide()  # 歌詞が取れたときだけ出す
        sizer.Add(self.lyric_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        controls = wx.BoxSizer(wx.HORIZONTAL)
        self.play_button = wx.Button(tab, label="▶ 再生", size=self.dip(130, 38))
        emphasize(self.play_button)  # いちばん押すもの
        self.play_button.Bind(wx.EVT_BUTTON, lambda _e: self.toggle_play())
        controls.Add(self.play_button, 0, wx.ALIGN_CENTER_VERTICAL)
        stop = wx.Button(tab, label="■ 停止", size=self.dip(80, -1))
        stop.Bind(wx.EVT_BUTTON, lambda _e: self.stop_music())
        controls.Add(stop, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 6)
        self.time_label = wx.StaticText(tab, label="--:-- / --:--", size=self.dip(96, -1))
        controls.Add(self.time_label, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, 12)
        self.seek = wx.Slider(tab, value=0, minValue=0, maxValue=1000)
        self.seek.Bind(wx.EVT_SCROLL_THUMBTRACK, lambda _e: setattr(self, "_seeking", True))
        self.seek.Bind(wx.EVT_SCROLL_CHANGED, self._on_seek_release)
        self.seek.Bind(wx.EVT_SCROLL_THUMBRELEASE, self._on_seek_release)
        controls.Add(self.seek, 1, wx.LEFT | wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 10)

        controls.Add(wx.StaticText(tab, label="キー"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        flat = wx.Button(tab, label="♭", size=self.dip(34, -1))
        flat.Bind(wx.EVT_BUTTON, lambda _e: self.change_key(-1))
        controls.Add(flat, 0, wx.ALIGN_CENTER_VERTICAL)
        saved_key = int(self.cfg.get("pitch_semitones", 0))
        self.player.semitones = float(saved_key)
        self.key_label = wx.StaticText(tab, label=f"{saved_key:+d}" if saved_key else "±0",
                                       size=self.dip(34, -1), style=wx.ALIGN_CENTER)
        controls.Add(self.key_label, 0, wx.ALIGN_CENTER_VERTICAL)
        sharp = wx.Button(tab, label="♯", size=self.dip(34, -1))
        sharp.Bind(wx.EVT_BUTTON, lambda _e: self.change_key(1))
        controls.Add(sharp, 0, wx.ALIGN_CENTER_VERTICAL)

        controls.Add(wx.StaticText(tab, label="伴奏"), 0,
                     wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
        self.music_volume = FloatSlider(tab, 0.0, 1.5,
                                        float(self.cfg.get("music_volume", 0.8)),
                                        size=self.dip(110, -1))
        self.music_volume.Bind(wx.EVT_SLIDER, self._on_music_volume)
        controls.Add(self.music_volume, 0, wx.ALIGN_CENTER_VERTICAL)
        self.player.volume = self.music_volume.GetFloat()
        sizer.Add(controls, 0, wx.EXPAND | wx.ALL, 8)

        self.music_status = hint_label(tab, "曲名やアーティスト名で検索してください。")
        sizer.Add(self.music_status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        tab.SetSizer(sizer)

    # ---------- 予約 ----------
    def _build_queue_page(self, parent) -> wx.Panel:
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.queue_list = wx.ListCtrl(page, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (text, width, align) in enumerate(
                (("順", 46, wx.LIST_FORMAT_CENTRE),
                 ("曲", 700, wx.LIST_FORMAT_LEFT),
                 ("入れた人", 90, wx.LIST_FORMAT_CENTRE))):
            self.queue_list.InsertColumn(index, text, format=align,
                                         width=self.FromDIP(width))
        self.queue_list.Bind(wx.EVT_SIZE,
                             lambda e: (stretch_column(self.queue_list, 1), e.Skip()))
        self.queue_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED,
                             lambda _e: self.play_queued_now())
        sizer.Add(self.queue_list, 1, wx.EXPAND)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler, width in (
                ("▲", lambda: self.move_queued(-1), 36),
                ("▼", lambda: self.move_queued(1), 36),
                ("この曲をすぐ歌う", self.play_queued_now, 150),
                ("取り消す", self.remove_queued, 90),
                ("全部消す", self.clear_queue, 90)):
            button = wx.Button(page, label=label, size=self.dip(width, -1))
            button.Bind(wx.EVT_BUTTON, lambda _e, h=handler: h())
            buttons.Add(button, 0, wx.RIGHT, 4)
        buttons.AddStretchSpacer()
        self.queue_hint = hint_label(page, "検索結果で曲を選んで「予約に追加」。"
                                           "曲が終わると自動で次へ進みます")
        buttons.Add(self.queue_hint, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.TOP, 4)
        page.SetSizer(sizer)
        return page

    def _refresh_queue(self) -> None:
        """予約の一覧と見出しを、いまの中身に合わせる。"""
        entries = self.queue.list()
        selected = self.queue_list.GetFirstSelected()
        self.queue_list.DeleteAllItems()
        for row, entry in enumerate(entries):
            self.queue_list.InsertItem(row, str(row + 1))
            self.queue_list.SetItem(row, 1, entry.title)
            self.queue_list.SetItem(row, 2, entry.added_by or "本体")
        if entries:
            target = min(max(0, selected), len(entries) - 1)
            self.queue_list.Select(target)
        self.lists.SetPageText(1, f"予約 ({len(entries)})" if entries else "予約")

    def enqueue_selected(self) -> None:
        """検索結果で選んでいる曲を予約に足す。"""
        index = self.results.GetFirstSelected()
        if index < 0 or index >= len(self.tracks):
            self.music_status.SetLabel("一覧から曲を選んでください。")
            return
        track = self.tracks[index]
        position = self.queue.add(
            playqueue.Entry(title=track.title, video_id=track.id, added_by="本体"))
        self.music_status.SetLabel(f"予約しました（{position} 番目）: {track.title}")
        # 何も鳴っていなければ、そのまま歌い始められるようにする
        if self.player.state in ("stopped", "ended", "error"):
            self.play_next_in_queue()

    def selected_queue_index(self) -> int:
        return self.queue_list.GetFirstSelected()

    def move_queued(self, delta: int) -> None:
        index = self.selected_queue_index()
        if index < 0:
            return
        moved = self.queue.move(index, delta)
        self.queue_list.Select(moved)

    def remove_queued(self) -> None:
        index = self.selected_queue_index()
        if index < 0:
            return
        entry = self.queue.remove(index)
        if entry:
            self.music_status.SetLabel(f"予約を取り消しました: {entry.title}")

    def clear_queue(self) -> None:
        if not len(self.queue):
            return
        if self.ask(f"予約を {len(self.queue)} 件すべて取り消しますか？"):
            self.music_status.SetLabel(f"{self.queue.clear()} 件の予約を取り消しました")

    def play_queued_now(self) -> None:
        """選んでいる予約を、順番を飛ばしてすぐ歌う。"""
        index = self.selected_queue_index()
        if index < 0:
            return
        entry = self.queue.remove(index)
        if entry:
            self._play_entry(entry)

    def play_next_in_queue(self) -> bool:
        """予約の先頭を再生する。予約が無ければ False。"""
        entry = self.queue.pop()
        if entry is None:
            return False
        self._play_entry(entry)
        return True

    def _play_entry(self, entry) -> None:
        if entry.path:
            self.current_track = None
            self.current_local_path = entry.path
            out = self.selected_device("output")
            self.player.stop(wait=False)
            self.player.set_display_size(*self.video.GetClientSize())
            self.player.open(entry.path, None, {}, device=out.index if out else None)
            self.music_status.SetLabel(f"再生中: {entry.title}")
            self.play_button.SetLabel("⏸ 一時停止")
            self.request_lyrics(entry.title, self.player.duration)
            return
        self.play_url(entry.url)

    def _on_song_finished(self) -> None:
        """1 曲終わったとき。予約があれば次へ、無ければ止める。"""
        if len(self.queue):
            self.music_status.SetLabel("次の曲へ進みます…")
            self.play_next_in_queue()
            return
        self.stop_music("再生が終わりました")

    def _on_music_volume(self, _event) -> None:
        self.cfg["music_volume"] = self.music_volume.GetFloat()
        self.player.volume = self.music_volume.GetFloat()

    def set_music_volume(self, volume: float) -> None:
        """伴奏の音量を変える（スマホのリモコンからも呼ばれる）。"""
        self.cfg["music_volume"] = volume
        self.music_volume.SetFloat(volume)
        self.player.volume = volume

    def _on_seek_release(self, _event) -> None:
        if not self._seeking:
            return
        self._seeking = False
        duration = self.player.duration
        if duration:
            self.player.seek(self.seek.GetValue() / 1000.0 * duration)

    # ---------- 検索と再生 ----------
    def search(self) -> None:
        query = self.query.GetValue().strip()
        if not query:
            return
        if self.URL_PATTERN.match(query):  # URL を貼られたら、そのまま再生する
            self.play_url(query)
            return
        self.cfg["prefer_off_vocal"] = self.offvocal.GetValue()
        self.cfg["trusted_only"] = self.trusted.GetValue()
        prefer, trusted = self.offvocal.GetValue(), self.trusted.GetValue()
        self.run_async(
            lambda: music_search.search(query, limit=25 if trusted else 15,
                                        prefer_off_vocal=prefer, trusted_only=trusted),
            self._show_results,
            busy_text=f"「{query}」を検索中…")

    def _show_results(self, tracks) -> None:
        self.tracks = tracks
        self.results.Freeze()
        self.results.DeleteAllItems()
        for row, track in enumerate(tracks):
            mark = "★" * min(5, max(0, (track.score + 2) // 4))
            self.results.InsertItem(row, mark or "—")
            self.results.SetItem(row, 1, track.duration_text)
            self.results.SetItem(row, 2, track.title)
            self.results.SetItem(row, 3, ("✓ " if track.trusted else "") + track.uploader)
        self.results.Thaw()
        if tracks:
            self.results.Select(0)
            self.results.Focus(0)
            self.music_status.SetLabel(
                f"{len(tracks)} 件見つかりました。曲を選んで再生してください。")
        else:
            self.music_status.SetLabel("見つかりませんでした。言葉を変えて試してください。")

    def play_selected(self) -> None:
        index = self.results.GetFirstSelected()
        if index < 0 or index >= len(self.tracks):
            self.music_status.SetLabel("一覧から曲を選んでください。")
            return
        track = self.tracks[index]
        self.current_track = track
        self.current_local_path = None  # 配信を再生するのでローカルファイルではない
        out = self.selected_device("output")
        quality = int(self.cfg.get("video_quality", 720))
        self.player.stop(wait=False)
        self.music_status.SetLabel(f"読み込み中: {track.title}")
        self.run_async(lambda: music_search.resolve(track.id, max_height=quality),
                       lambda src: self._start_playback(src, out),
                       busy_text="音源を準備中…")

    def play_url(self, url: str) -> None:
        """URL を直接受け取って再生する（YouTube のリンクを貼り付けたとき）。"""
        out = self.selected_device("output")
        quality = int(self.cfg.get("video_quality", 720))
        self.current_local_path = None
        self.player.stop(wait=False)
        self.music_status.SetLabel("読み込み中…")

        def done(source):
            self.current_track = music_search.Track(
                id=url, title=source.title or url, duration=source.duration)
            self._start_playback(source, out)

        self.run_async(lambda: music_search.resolve(url, max_height=quality), done,
                       busy_text="URL から読み込んでいます…")

    def _start_playback(self, src, out) -> None:
        if int(self.cfg.get("pitch_semitones", 0)):
            # キーを変える場合は、手元に取り込んでから変換して鳴らす
            self.apply_key_to_current()
            return
        size = self.video.GetClientSize()
        self.player.set_display_size(max(2, size.width), max(2, size.height))
        self.player.open(src.video_url, src.audio_url, src.headers,
                         device=out.index if out else None, duration=src.duration)
        self.music_status.SetLabel(f"再生中: {src.title}")
        self.play_button.SetLabel("⏸ 一時停止")
        self.request_lyrics(src.title, src.duration)

    def open_local_file(self) -> None:
        wildcard = ("音源・動画|*.mp3;*.wav;*.flac;*.m4a;*.aac;*.ogg;*.opus;"
                    "*.mp4;*.mkv;*.webm;*.avi;*.mov;*.m4v|すべて|*.*")
        with wx.FileDialog(self, "曲を選ぶ", wildcard=wildcard,
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        self.current_track = None
        self.current_local_path = path
        if int(self.cfg.get("pitch_semitones", 0)):
            self.apply_key_to_current()
            return
        out = self.selected_device("output")
        self.player.stop(wait=False)
        size = self.video.GetClientSize()
        self.player.set_display_size(max(2, size.width), max(2, size.height))
        self.player.open(path, None, {}, device=out.index if out else None)
        name = os.path.basename(path)
        self.music_status.SetLabel(f"再生中: {name}")
        self.play_button.SetLabel("⏸ 一時停止")
        # ファイル名から曲を推し量る（外したら「曲を指定」で直せる）
        self.request_lyrics(os.path.splitext(name)[0], self.player.duration)

    def toggle_play(self) -> None:
        state = self.player.state
        if state == "playing":
            self.player.pause()
        elif state == "paused":
            self.player.resume()
        elif state == "opening":
            return  # 読み込み中は触らない
        else:
            self.play_selected()
        self._refresh_transport()

    def _refresh_transport(self) -> None:
        """ボタンや目盛りの表示を、いまの再生状態に合わせる。"""
        state = self.player.state
        self.play_button.SetLabel("⏸ 一時停止" if state == "playing" else "▶ 再生")
        if state == "opening":
            self.play_button.SetLabel("… 準備中")
        self.seek.Enable(state in ("playing", "paused") and bool(self.player.duration))

    def stop_music(self, message: str = "停止しました") -> None:
        self.player.stop(wait=False)
        self.video.clear()
        self.last_frame = None
        self.seek.SetValue(0)
        self.time_label.SetLabel("--:-- / --:--")
        self.music_status.SetLabel(message)
        self._refresh_transport()

    def _player_error(self, error) -> None:
        LOG.error("再生に失敗しました: %r", error)
        self.music_status.SetLabel(f"再生できませんでした: {friendly_error(error)}")
        self._refresh_transport()

    # ---------- キー ----------
    # ♯♭ は続けて押されることが多い。1 回ごとに作り直すと、そのたびに
    # 曲が止まる。押し終わるのを少し待ってから、最後のキーだけ用意する
    KEY_DELAY = 600

    def change_key(self, delta: int) -> None:
        """伴奏のキーを半音単位で上げ下げする。

        曲まるごと変換してから鳴らす。ブロックごとに変換すると継ぎ目で
        ぷつぷつ鳴ったり音が重なったりするため、その方式は採らない。
        """
        value = int(np.clip(self.cfg.get("pitch_semitones", 0) + delta, -6, 6))
        if value == self.cfg.get("pitch_semitones", 0):
            return
        self.cfg["pitch_semitones"] = value
        self.key_label.SetLabel(f"{value:+d}" if value else "±0")
        if self.player.state not in ("playing", "paused"):
            self.music_status.SetLabel(
                f"キー {value:+d} で次から再生します" if value else "キーを元に戻しました")
            return
        if self._key_timer is not None:
            self._key_timer.Stop()
        self._key_timer = wx.CallLater(self.KEY_DELAY, self.apply_key_to_current)
        self.music_status.SetLabel(f"キー {value:+d} を準備します…（このまま歌えます）")

    def apply_key_to_current(self) -> None:
        """いま鳴っている曲に、選んだキーを適用し直す。

        用意ができるまで曲は止めない。止めてから作ると、作っている間ずっと
        無音になり、作り置きが効いている場合でも開き直すぶんだけ待たされる。
        """
        import pitch_render

        self._key_timer = None
        key = int(self.cfg.get("pitch_semitones", 0))
        track = self.current_track
        source = self.current_local_path
        if source is None and track is None:
            return
        self._key_job += 1
        job = self._key_job

        def report(stage: str, ratio: float) -> None:
            wx.CallAfter(self.music_status.SetLabel,
                         f"キー {key:+d} を準備中: {stage}… {ratio*100:.0f}%"
                         "（このまま歌えます）")

        def work():
            local = source
            if local is None:  # 配信中の曲は、先に手元へ取り込む
                report("音源を取得中", 0.05)
                folder = os.path.join(pitch_render.CACHE_DIR, "download")
                os.makedirs(folder, exist_ok=True)
                local = music_search.download(track.id, folder, max_height=480)
            rendered = pitch_render.render(local, key, progress=report) if key else local
            return local, rendered

        def done(result):
            if job != self._key_job:
                return  # 待っている間にまたキーが変わった
            # 差し替える直前の位置から続ける
            self._play_with_key(*result, self.player.position)

        self.run_async(work, done, busy_text=f"キー {key:+d} を準備しています…")

    def _play_with_key(self, source: str, rendered: str, position: float) -> None:
        """映像は元のまま、音声だけ差し替えて再生する。"""
        out = self.selected_device("output")
        device = out.index if out else None
        self.current_local_path = source
        has_video = source.lower().endswith(self.VIDEO_EXTENSIONS)
        self.player.stop(wait=False)  # ここで初めて止める

        if rendered == source:
            self.player.open(source, None, {}, device=device)
        elif has_video:
            self.player.open(source, rendered, {}, device=device)
        else:
            self.player.open(rendered, None, {}, device=device)

        key = int(self.cfg.get("pitch_semitones", 0))
        name = os.path.basename(source)
        self.music_status.SetLabel(
            f"再生中: {name}" + (f"（キー {key:+d}）" if key else ""))
        self.play_button.SetLabel("⏸ 一時停止")
        title = self.current_track.title if self.current_track else name
        self.request_lyrics(title, self.player.duration)
        if position > 1.0:
            self._seek_when_ready(position, time.monotonic())

    def _seek_when_ready(self, position: float, started: float) -> None:
        """開き終わった時点で、元の位置へ飛ばす。

        決め打ちで 1.2 秒待つと、その分だけ余計に無音が伸びる。
        鳴り出したらすぐ飛ばす。
        """
        if self.player.state == "playing":
            self.player.seek(position)
            return
        if self.player.state == "error" or time.monotonic() - started > 8.0:
            return
        wx.CallLater(60, lambda: self._seek_when_ready(position, started))

    # ---------- 歌詞 ----------
    def request_lyrics(self, title: str, duration: float | None) -> None:
        """曲名から時刻つき歌詞を探して、映像の下に出す。"""
        self.current_lyrics = None
        self._hide_lyrics()
        if not self.cfg.get("show_lyrics", True) or not title:
            return

        def done(result):
            self.current_lyrics = result
            if result is None or not result.synced:
                self.lyric_status.SetLabel("歌詞が見つかりませんでした（「曲を指定」で探せます）")
                return
            self.lyric_panel.Show()
            self.karaoke_tab.Layout()
            self.lyric_status.SetLabel(
                f"歌詞: {result.track} / {result.artist}（{len(result.lines)} 行）")

        def work():
            import lyrics
            return lyrics.best_match(title, duration)

        def failed(error):
            # 歌詞はおまけなので、取れなくても歌の邪魔をしない。
            # 窓を出して「ネットを確認してください」と言うと、
            # 実際には繋がっているのに疑わせることになる
            LOG.info("歌詞を取得できませんでした: %s", error)
            self.lyric_status.SetLabel(
                "歌詞は出せませんでした（「曲を指定」で探せます）")

        self.lyric_status.SetLabel("歌詞を探しています…")
        self.run_async(work, done, on_error=failed)

    def _hide_lyrics(self) -> None:
        self.lyric_panel.show("", 0, "")
        if self.lyric_panel.IsShown():
            self.lyric_panel.Hide()
            self.karaoke_tab.Layout()

    def _on_lyrics_toggle(self) -> None:
        self.cfg["show_lyrics"] = bool(self.show_lyrics.GetValue())
        if not self.cfg["show_lyrics"]:
            self._hide_lyrics()
            self.lyric_status.SetLabel("")
        elif self.current_track is not None:
            self.request_lyrics(self.current_track.title, self.player.duration)

    def _update_lyric_display(self) -> None:
        entry = self.current_lyrics
        if entry is None or not entry.synced:
            return
        position = self.player.position
        index = entry.index_at(position)
        if index < 0:
            self.lyric_panel.show("", 0, entry.lines[0].text if entry.lines else "")
            return
        line = entry.lines[index]
        following = entry.lines[index + 1].text if index + 1 < len(entry.lines) else ""
        # 1 文字ずつの頭出しがある曲は、歌った分だけ色を進める
        self.lyric_panel.show(line.text, line.sung(position), following)

    def choose_lyrics(self) -> None:
        """歌詞を手で選び直す。"""
        import lyrics

        title = self.current_track.title if self.current_track else (
            os.path.basename(self.current_local_path or ""))
        guess = " ".join(lyrics.clean_title(title)).strip()

        dialog = wx.Dialog(self, title="歌詞を探す", size=self.dip(560, 420))
        sizer = wx.BoxSizer(wx.VERTICAL)
        row = wx.BoxSizer(wx.HORIZONTAL)
        query = wx.TextCtrl(dialog, value=guess, style=wx.TE_PROCESS_ENTER)
        row.Add(query, 1, wx.ALIGN_CENTER_VERTICAL)
        find = wx.Button(dialog, label="探す")
        row.Add(find, 0, wx.LEFT, 6)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 10)

        listing = wx.ListCtrl(dialog, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (text, width) in enumerate((("曲名", 200), ("アーティスト", 150),
                                               ("長さ", 60), ("時刻", 60))):
            listing.InsertColumn(index, text, width=self.FromDIP(width))
        sizer.Add(listing, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        buttons = wx.StdDialogButtonSizer()
        ok = wx.Button(dialog, wx.ID_OK, "これにする")
        buttons.AddButton(ok)
        buttons.AddButton(wx.Button(dialog, wx.ID_CANCEL, "やめる"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        dialog.SetSizer(sizer)

        candidates: list = []

        def do_search(_event=None):
            text = query.GetValue().strip()
            if not text:
                return
            duration = self.player.duration
            parts = text.split(" ", 1)
            track = parts[0] if len(parts) == 1 else text
            found = lyrics.search(track, "", duration, limit=12)
            candidates.clear()
            candidates.extend(found)
            listing.DeleteAllItems()
            for row_index, item in enumerate(found):
                listing.InsertItem(row_index, item.track)
                listing.SetItem(row_index, 1, item.artist)
                listing.SetItem(row_index, 2,
                                time_text(item.duration) if item.duration else "-")
                listing.SetItem(row_index, 3, "あり" if item.synced else "なし")

        find.Bind(wx.EVT_BUTTON, do_search)
        query.Bind(wx.EVT_TEXT_ENTER, do_search)
        do_search()

        if dialog.ShowModal() == wx.ID_OK:
            index = listing.GetFirstSelected()
            if 0 <= index < len(candidates):
                self.current_lyrics = candidates[index]
                self.lyric_panel.Show()
                self.karaoke_tab.Layout()
                self.lyric_status.SetLabel(
                    f"歌詞: {self.current_lyrics.track} / {self.current_lyrics.artist}")
        dialog.Destroy()

    # ---------- ランキング・入手先 ----------
    def show_ranking(self) -> None:
        """カラオケの人気曲一覧を出して、選んだ曲を検索欄へ入れる。"""
        import ranking

        dialog = wx.Dialog(self, title="カラオケ ランキング", size=self.dip(620, 520))
        sizer = wx.BoxSizer(wx.VERTICAL)
        head = wx.BoxSizer(wx.HORIZONTAL)
        head.Add(wx.StaticText(dialog, label="種類"), 0, wx.ALIGN_CENTER_VERTICAL)
        source = wx.ComboBox(dialog, choices=list(ranking.SOURCES),
                             style=wx.CB_READONLY)
        source.SetSelection(0)
        head.Add(source, 0, wx.LEFT, 6)
        status = hint_label(dialog, "読み込んでいます…")
        head.Add(status, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        sizer.Add(head, 0, wx.EXPAND | wx.ALL, 10)

        listing = wx.ListCtrl(dialog, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (text, width) in enumerate((("順位", 50), ("曲名", 280),
                                               ("アーティスト", 220))):
            listing.InsertColumn(index, text, width=self.FromDIP(width))
        sizer.Add(listing, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(wx.Button(dialog, wx.ID_OK, "この曲を検索"))
        buttons.AddButton(wx.Button(dialog, wx.ID_CANCEL, "閉じる"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)
        dialog.SetSizer(sizer)

        songs: list = []

        def load(_event=None):
            name = source.GetValue()  # 画面の値は必ずこのスレッドで読む
            status.SetLabel("読み込んでいます…")
            listing.DeleteAllItems()

            def done(found):
                songs.clear()
                songs.extend(found)
                for row, song in enumerate(found):
                    listing.InsertItem(row, str(song.rank))
                    listing.SetItem(row, 1, song.title)
                    listing.SetItem(row, 2, song.artist)
                status.SetLabel(f"{len(found)} 曲" if found else "読み込めませんでした")

            self.run_async(lambda: ranking.fetch(name), done)

        source.Bind(wx.EVT_COMBOBOX, load)
        listing.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: dialog.EndModal(wx.ID_OK))
        load()

        if dialog.ShowModal() == wx.ID_OK:
            index = listing.GetFirstSelected()
            if 0 <= index < len(songs):
                self.query.SetValue(songs[index].query)
                self.notebook.SetSelection(0)
                self.search()
        dialog.Destroy()

    def show_sources(self) -> None:
        wx.MessageBox(
            "オフボーカル音源の入手先\n\n"
            "・YouTube の「カラオケ」「off vocal」「instrumental」つきの動画\n"
            "・公式のカラオケ配信チャンネル（一覧の ✓ 印）\n"
            "・手持ちの音源ファイル（「ファイルを開く」）\n"
            "・対応する GPU があれば「ボーカルを消す」でどんな曲からでも作れます\n\n"
            "いずれも、権利者が配信を許可しているものだけを使ってください。",
            APP_TITLE, wx.OK | wx.ICON_INFORMATION, self)

    # ---------------------------------------------------------- マイクタブ
    def _build_mic_tab(self) -> None:
        tab = self.mic_tab
        sizer = wx.BoxSizer(wx.VERTICAL)

        box, parent = boxed(tab, " 機器 ")
        grid = wx.FlexGridSizer(0, 3, 4, 6)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(parent, label="マイク"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.mic_combo = wx.ComboBox(parent, style=wx.CB_READONLY)
        self.mic_combo.Bind(wx.EVT_COMBOBOX, lambda _e: self._on_device_change())
        grid.Add(self.mic_combo, 1, wx.EXPAND)
        mic_test = wx.Button(parent, label="テスト", size=self.dip(70, -1))
        mic_test.Bind(wx.EVT_BUTTON, lambda _e: self.test_device("input"))
        grid.Add(mic_test, 0)

        grid.Add(wx.StaticText(parent, label="出力先"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.out_combo = wx.ComboBox(parent, style=wx.CB_READONLY)
        self.out_combo.Bind(wx.EVT_COMBOBOX, lambda _e: self._on_device_change())
        grid.Add(self.out_combo, 1, wx.EXPAND)
        out_test = wx.Button(parent, label="テスト", size=self.dip(70, -1))
        out_test.Bind(wx.EVT_BUTTON, lambda _e: self.test_device("output"))
        grid.Add(out_test, 0)

        grid.Add(wx.StaticText(parent, label="入力の使い方"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.input_mode = wx.ComboBox(parent, choices=list(INPUT_MODES),
                                      style=wx.CB_READONLY)
        self.input_mode.SetValue(self.cfg.get("input_mode", list(INPUT_MODES)[0]))
        self.input_mode.Bind(wx.EVT_COMBOBOX, lambda _e: self._on_device_change())
        grid.Add(self.input_mode, 0)
        grid.Add(hint_label(parent, "※ 伴奏もこの出力先から鳴ります"), 0,
                 wx.ALIGN_CENTER_VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)

        action = wx.BoxSizer(wx.HORIZONTAL)
        self.mic_button = wx.Button(tab, label="🎤 マイクを入れる", size=self.dip(190, 38))
        emphasize(self.mic_button)
        self.mic_button.Bind(wx.EVT_BUTTON, lambda _e: self.toggle_mic())
        action.Add(self.mic_button, 0)
        action.AddStretchSpacer()
        auto = wx.Button(tab, label="自動設定")
        auto.Bind(wx.EVT_BUTTON, lambda _e: self.auto_setup())
        action.Add(auto, 0)
        sizer.Add(action, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.router_note = hint_label(tab)
        sizer.Add(self.router_note, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        box, parent = boxed(tab, " マイクの音量 ")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.mute = wx.CheckBox(parent, label="ミュート")
        self.mute.Bind(wx.EVT_CHECKBOX, lambda _e: self._on_mute())
        row.Add(self.mute, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.mic_volume = FloatSlider(parent, 0.0, 2.0,
                                      float(self.cfg.get("mic_volume", 1.0)))
        self.mic_volume.Bind(wx.EVT_SLIDER, lambda _e: self._on_mic_volume())
        row.Add(self.mic_volume, 1, wx.ALIGN_CENTER_VERTICAL)
        self.mic_volume_label = wx.StaticText(parent, label="100%", size=self.dip(56, -1))
        row.Add(self.mic_volume_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        box.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)
        self._on_mic_volume()

        box, parent = boxed(tab, " レベル ")
        meters = wx.FlexGridSizer(0, 3, 4, 8)
        meters.AddGrowableCol(1, 1)
        meters.Add(wx.StaticText(parent, label="入力"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.in_meter = wx.Gauge(parent, range=100)
        meters.Add(self.in_meter, 1, wx.EXPAND)
        self.in_db_label = wx.StaticText(parent, label="--- dB", size=self.dip(70, -1))
        meters.Add(self.in_db_label, 0, wx.ALIGN_CENTER_VERTICAL)
        meters.Add(wx.StaticText(parent, label="出力"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.out_meter = wx.Gauge(parent, range=100)
        meters.Add(self.out_meter, 1, wx.EXPAND)
        self.out_db_label = wx.StaticText(parent, label="--- dB", size=self.dip(70, -1))
        meters.Add(self.out_db_label, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(meters, 0, wx.EXPAND | wx.ALL, 6)
        self.quality_label = hint_label(parent)
        box.Add(self.quality_label, 0, wx.LEFT | wx.BOTTOM, 6)
        sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)

        box, parent = boxed(tab, " 音の調整 ")
        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticText(parent, label="プリセット"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        # 保存された値がどのプリセットとも違うことがある。そのときに
        # 先頭の名前を出すと、実際の音と表示が食い違って嘘になる
        self.preset = wx.ComboBox(parent, choices=list(PRESETS) + [MANUAL_PRESET],
                                  style=wx.CB_READONLY)
        self.preset.SetValue(self._current_preset_name())
        self.preset.Bind(wx.EVT_COMBOBOX, lambda _e: self.apply_preset())
        top.Add(self.preset, 1, wx.ALIGN_CENTER_VERTICAL)
        top.Add(wx.StaticText(parent, label="ノイズ除去"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
        self.denoise_mode = wx.ComboBox(parent, choices=list(DENOISE_MODES),
                                        style=wx.CB_READONLY, size=self.dip(230, -1))
        self.denoise_mode.SetValue(self.cfg["effects"].get("denoise_mode", "標準"))
        self.denoise_mode.Bind(wx.EVT_COMBOBOX, lambda _e: self.apply_denoise_mode())
        top.Add(self.denoise_mode, 0, wx.ALIGN_CENTER_VERTICAL)
        learn = wx.Button(parent, label="ノイズを学習")
        learn.Bind(wx.EVT_BUTTON, lambda _e: self.learn_noise())
        top.Add(learn, 0, wx.LEFT, 8)
        box.Add(top, 0, wx.EXPAND | wx.ALL, 6)

        self.fx_sliders: dict[str, FloatSlider] = {}
        self.fx_labels: dict[str, tuple[wx.StaticText, str]] = {}
        grid = wx.FlexGridSizer(0, 3, 3, 8)
        grid.AddGrowableCol(1, 1)
        for key, label, low, high, unit in EFFECT_ROWS:
            grid.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            slider = FloatSlider(parent, low, high,
                                 float(self.cfg["effects"].get(key, low)))
            slider.Bind(wx.EVT_SLIDER, lambda _e, k=key: self._on_fx_change(k))
            self.fx_sliders[key] = slider
            grid.Add(slider, 1, wx.EXPAND)
            value_label = wx.StaticText(parent, label="", size=self.dip(70, -1))
            self.fx_labels[key] = (value_label, unit)
            grid.Add(value_label, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)

        hum = wx.BoxSizer(wx.HORIZONTAL)
        hum.Add(wx.StaticText(parent, label="電源ハム除去:"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.hum_buttons: dict[str, wx.RadioButton] = {}
        saved_hum = str(int(self.cfg["effects"].get("hum_hz", 50)))
        for index, (text, value) in enumerate(
                (("なし", "0"), ("50Hz 東日本", "50"), ("60Hz 西日本", "60"))):
            button = wx.RadioButton(parent, label=text,
                                    style=wx.RB_GROUP if index == 0 else 0)
            button.SetValue(value == saved_hum)
            button.Bind(wx.EVT_RADIOBUTTON, lambda _e, v=value: self._on_hum_change(v))
            self.hum_buttons[value] = button
            hum.Add(button, 0, wx.RIGHT, 8)
        box.Add(hum, 0, wx.ALL, 6)
        sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)
        tab.SetSizer(sizer)
        for key, *_ in EFFECT_ROWS:
            self._on_fx_change(key, apply_now=False)

    # ---------- 機器 ----------
    def _api_filter(self) -> str | None:
        api = self.api_combo.GetValue() if hasattr(self, "api_combo") else "すべて"
        return None if api == "すべて" else api

    def _reload_devices(self) -> None:
        api = self._api_filter()
        self.mic_devices = dev.list_devices("input", api)
        self.out_devices = dev.list_devices("output", api)
        self.mic_combo.Set([d.label for d in self.mic_devices])
        self.out_combo.Set([d.label for d in self.out_devices])

        saved_mic = dev.find_by_name(self.cfg.get("mic_device_name", ""), "input", api)
        saved_out = dev.find_by_name(self.cfg.get("output_device_name", ""), "output", api)
        mic = saved_mic or dev.default_device("input", api)
        out = saved_out or dev.default_device("output", api)
        if mic:
            self.mic_combo.SetValue(mic.label)
        if out:
            self.out_combo.SetValue(out.label)

    def selected_device(self, kind: str) -> dev.Device | None:
        label = self.mic_combo.GetValue() if kind == "input" else self.out_combo.GetValue()
        pool = self.mic_devices if kind == "input" else self.out_devices
        return next((d for d in pool if d.label == label), None)

    def rescan_devices(self) -> None:
        was_running = self.router.running
        self.router.stop()
        self.player.stop(wait=False)
        dev.refresh()
        self._reload_devices()
        self.set_status("機器を再検出しました")
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
        self.run_async(lambda: (dev.check(device), dev.system_hint(device)),
                       lambda r: self._show_test_result(device, *r),
                       busy_text=f"{device.name} を確認中…")

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
        wx.MessageBox("\n".join(lines), APP_TITLE, wx.OK | wx.ICON_INFORMATION, self)

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
            self.mic_combo.SetValue(mic.label)
        if out:
            self.out_combo.SetValue(out.label)
        self._on_device_change()
        if mic and ("USB" in mic.name.upper() or "Headset" in mic.name):
            self.preset.SetValue("USBマイク・ヘッドセット")
            self.apply_preset()

        if not mic:
            wx.MessageBox(
                "音が来ているマイクが見つかりませんでした。\n\n"
                "・マイクが挿さっているか\n"
                "・機器側のスイッチと録音レベル\n"
                "を確認してから、もう一度「自動設定」を押してください。\n\n"
                "「設定・診断」タブの「すべて調べる」で、機器ごとの状況を一覧できます。",
                APP_TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return

        # 見つけただけで終わると次に何をすればいいか分からないので、そのまま繋ぐ
        if self.ask(f"マイク: {mic.name}\n"
                    f"出力先: {out.name if out else '（見つかりません）'}\n\n"
                    "この組み合わせでマイクを入れますか？\n"
                    "（入れたあと、カラオケタブで曲を探して歌えます）"):
            self.start_mic()
            wx.CallLater(1500, lambda: self.notebook.SetSelection(0))

    def _first_run(self) -> None:
        self.cfg["first_run"] = False
        if self.ask("はじめての起動です。\n使えるマイクとスピーカーを自動で探しますか？\n"
                    "（数十秒かかります。あとから「自動設定」でやり直せます）"):
            self.auto_setup()
        # 機器を選び終えたころに、ボーカル除去を使えるなら案内する
        wx.CallLater(6000, self._offer_model_on_first_run)

    def _offer_model_on_first_run(self) -> None:
        cap = self.separator_capability
        if cap is None:  # 判定がまだ終わっていない
            wx.CallLater(3000, self._offer_model_on_first_run)
            return
        if not cap.available and getattr(cap, "installable", False):
            self.offer_model_install()

    # ---------- マイク ----------
    SPEAKER_WORDS = ("スピーカー", "speaker", "内蔵", "built-in", "realtek", "モニター",
                     "monitor", "display", "hdmi")
    HEADPHONE_WORDS = ("ヘッドホン", "ヘッドセット", "headphone", "headset", "earphone",
                       "イヤホン", "airpods", "buds")

    def _howling_risk(self, out_device) -> bool:
        name = out_device.name.lower()
        if any(word in name for word in self.HEADPHONE_WORDS):
            return False
        return any(word.lower() in name for word in self.SPEAKER_WORDS)

    def toggle_mic(self) -> None:
        if self.router.running or self.router.state == "opening":
            self.stop_mic()
        else:
            self.start_mic()

    def start_mic(self) -> None:
        mic, out = self.selected_device("input"), self.selected_device("output")
        if mic is None or out is None:
            wx.MessageBox("マイクと出力先を選んでください。", APP_TITLE,
                          wx.OK | wx.ICON_WARNING, self)
            return
        if self._howling_risk(out) and not self.cfg.get("howling_ok"):
            if not self.ask(f"出力先が「{out.name}」になっています。\n\n"
                            "スピーカーから出すと、その音をマイクが拾って\n"
                            "「キーン」という大きな音（ハウリング）が出ることがあります。\n\n"
                            "イヤホンかヘッドホンを使うことをおすすめします。\n"
                            "このまま続けますか？"):
                return
            self.cfg["howling_ok"] = True  # 一度確認したら次から聞かない
        # 先に止めてから作り直す。動いているコールバックの裏で
        # ノイズ除去器やプラグインの状態を差し替えると壊れる
        self.router.stop()
        self.chain.set_rate(mic.rate)
        self.router.mic_gain = self.mic_volume.GetFloat()
        self.router.muted = bool(self.mute.GetValue())
        channels, offset = INPUT_MODES.get(self.input_mode.GetValue(), (1, 0))
        self.cfg["input_mode"] = self.input_mode.GetValue()
        self.router.start(
            mic.index, out.index,
            latency=LATENCY_MODES.get(self.latency.GetValue(), "low"),
            buffer_ms=self.buffer_slider.GetFloat(),
            in_channels=channels, in_channel_offset=offset)

    def stop_mic(self) -> None:
        self.router.stop()

    def _router_state(self, state: str, message: str) -> None:
        texts = {"stopped": "マイク: 停止中", "opening": "マイク: 接続中…",
                 "running": "マイク: オン", "error": "マイク: エラー"}
        self.mic_state_label.SetLabel(texts.get(state, state))
        self.mic_button.SetLabel(
            "マイクを止める" if state in ("running", "opening") else "🎤 マイクを入れる")
        if state == "error":
            self.router_note.SetForegroundColour(wx.Colour(0xB0, 0, 0))
            self.router_note.SetLabel(f"⚠ {message}")
            self.set_status(message)
        elif state == "running":
            self.router_note.SetForegroundColour(wx.Colour(0, 0x60, 0))
            self.router_note.SetLabel(message)
        else:
            self.router_note.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            self.router_note.SetLabel("")

    def _on_mic_volume(self) -> None:
        value = self.mic_volume.GetFloat()
        self.cfg["mic_volume"] = value
        self.router.mic_gain = value
        self.mic_volume_label.SetLabel(f"{value*100:.0f}%")

    def _on_mute(self) -> None:
        self.router.muted = bool(self.mute.GetValue())
        self.set_status("マイクをミュートしました" if self.router.muted
                        else "ミュートを解除しました")

    # ---------- 音の調整 ----------
    def _apply_effects_from_config(self) -> None:
        fx = self.cfg["effects"]
        self.chain.input_gain.gain_db = fx["input_gain_db"]
        self.chain.highpass.cutoff_frequency_hz = fx["highpass_hz"]
        self.chain.set_hum_base(fx["hum_hz"] or 50.0)
        self.chain.hum_notch_db = fx["hum_notch_db"] if fx["hum_hz"] else 0.0
        self.chain.denoise = fx["denoise"]
        self.chain.denoise_strength = fx["denoise_strength"]
        self.chain.gate.threshold_db = fx["gate_db"]
        self.chain.compressor.threshold_db = fx["comp_threshold_db"]
        self.chain.compressor.ratio = fx["comp_ratio"]
        self.chain.makeup.gain_db = fx["makeup_db"]
        self.chain.reverb.wet_level = fx["reverb_wet"]

    def _on_fx_change(self, key: str, apply_now: bool = True) -> None:
        value = self.fx_sliders[key].GetFloat()
        label, unit = self.fx_labels[key]
        label.SetLabel(f"{value:.1f}{unit}")
        if not apply_now:
            return
        self.cfg["effects"][key] = value
        self._sync_preset_name()  # 手で動かしたらプリセット名も実態に合わせる
        if key == "input_gain_db":
            self.chain.input_gain.gain_db = value
        elif key == "denoise_strength":
            self.chain.denoise_strength = value
        elif key == "gate_db":
            self.chain.gate.threshold_db = value
        elif key == "comp_ratio":
            self.chain.compressor.ratio = value
        elif key == "makeup_db":
            self.chain.makeup.gain_db = value
        elif key == "reverb_wet":
            self.chain.reverb.wet_level = value

    def _on_hum_change(self, value: str) -> None:
        hz = float(value)
        self.cfg["effects"]["hum_hz"] = hz
        if hz:
            self.chain.set_hum_base(hz)
            self.chain.hum_notch_db = self.cfg["effects"].get("hum_notch_db", -12.0) or -12.0
        else:
            self.chain.hum_notch_db = 0.0

    def apply_preset(self) -> None:
        name = self.preset.GetValue()
        preset = PRESETS.get(name)
        if not preset:  # 「手動調整」は選んでも何も起きない
            self._sync_preset_name()
            return
        self.cfg["effects"].update(preset)
        self.chain.enabled = name != "加工しない"
        self._apply_effects_from_config()
        for key, slider in self.fx_sliders.items():
            if key in preset:
                slider.SetFloat(preset[key])
        self._sync_denoise_mode()
        hum = str(int(preset["hum_hz"]))
        if hum in self.hum_buttons:
            self.hum_buttons[hum].SetValue(True)
        for key in self.fx_sliders:  # スライダー横の数値表示を追従させる
            self._on_fx_change(key)
        self.set_status(f"プリセット「{name}」を適用しました")

    def _current_preset_name(self) -> str:
        """いまの設定に一致するプリセット名。どれとも違えば「手動調整」。"""
        fx = self.cfg["effects"]
        for name, preset in PRESETS.items():
            if all(abs(float(fx.get(key, 0.0)) - float(value)) < 0.05
                   for key, value in preset.items()
                   if isinstance(value, (int, float)) and not isinstance(value, bool)):
                return name
        return MANUAL_PRESET

    def _sync_preset_name(self) -> None:
        name = self._current_preset_name()
        if self.preset.GetValue() != name:
            self.preset.SetValue(name)

    def _sync_denoise_mode(self) -> None:
        """効果の値から、種類の表示を合わせる。"""
        if not self.chain.denoise:
            if self.denoise_mode.GetValue() != AI_MIC_MODE:
                self.denoise_mode.SetValue("なし")
            return
        strength = self.chain.denoise_strength
        best = min((m for m in DENOISE_MODES if DENOISE_MODES[m] > 0),
                   key=lambda m: abs(DENOISE_MODES[m] - strength))
        self.denoise_mode.SetValue(best)

    def apply_denoise_mode(self) -> None:
        """ノイズ除去の種類を切り替える。"""
        mode = self.denoise_mode.GetValue()
        strength = DENOISE_MODES.get(mode, 1.5)
        self.cfg["effects"]["denoise_mode"] = mode
        if strength == -1.0:  # マイク側の機能に任せる
            self.chain.set_denoise_engine("spectral")
            self.chain.denoise = False
            self._offer_ai_microphone()
        elif strength == -2.0:  # RNNoise
            if not self._enable_rnnoise():
                return
        else:
            self.chain.set_denoise_engine("spectral")
            self.chain.denoise = strength > 0
            if strength > 0:
                self.chain.denoise_strength = strength
                self.cfg["effects"]["denoise_strength"] = strength
        self.cfg["effects"]["denoise"] = self.chain.denoise

    def _enable_rnnoise(self) -> bool:
        """RNNoise に切り替える。使えなければ取得を案内して False を返す。"""
        import model_installer

        if not mic_chain.RNNoiseDenoiser.available():
            if not self.ask("RNNoise（音声向けのノイズ除去）を使うには、\n"
                            "追加の部品が必要です（約 15 MB）。\n\n"
                            "キーボードの打鍵音のような突発的な音にも効きます。\n"
                            "今すぐ取得しますか？"):
                self._sync_denoise_mode()
                return False
            try:
                self.set_status("RNNoise を取得しています…")
                wx.Yield()
                model_installer.install_rnnoise()
            except Exception as e:
                wx.MessageBox(f"取得できませんでした:\n{e}", APP_TITLE,
                              wx.OK | wx.ICON_ERROR, self)
                self._sync_denoise_mode()
                return False
        try:
            self.chain.set_denoise_engine("rnnoise")
        except Exception as e:
            wx.MessageBox(str(e), APP_TITLE, wx.OK | wx.ICON_INFORMATION, self)
            self._sync_denoise_mode()
            return False
        self.chain.denoise = True
        self.cfg["effects"]["denoise"] = True
        self.set_status("RNNoise に切り替えました")
        return True

    def _offer_ai_microphone(self) -> None:
        """RTX Voice や Krisp の仮想マイクがあれば、そちらへ切り替える。"""
        mics = dev.ai_microphones(self._api_filter())
        if not mics:
            wx.MessageBox(
                "マイク側で処理する仮想マイクが見つかりませんでした。\n\n"
                "NVIDIA Broadcast（RTX Voice）や Krisp を入れると、\n"
                "処理済みの音を出す専用のマイクが増えます。\n"
                "それを選ぶと、このアプリ側の処理なしでノイズを消せます。\n\n"
                "※ どちらもアプリに組み込むことはできません（仮想マイクとして使います）",
                APP_TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        choice = self.ask_choice("マイク側で処理", "使うマイクを選んでください",
                                 [m.label for m in mics])
        if not choice:
            return
        self.mic_combo.SetValue(choice)
        self._on_device_change()
        self.set_status(f"{choice} に切り替えました")

    def ask_choice(self, title: str, prompt: str, options: list[str]) -> str | None:
        """選択肢から 1 つ選ばせる小さな窓。選ばれた文字列か None を返す。"""
        with wx.SingleChoiceDialog(self, prompt, title, options) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                return dialog.GetStringSelection()
        return None

    def learn_noise(self) -> None:
        if not self.router.running:
            wx.MessageBox("先にマイクを入れてください。\n"
                          "静かにしている間の音をノイズとして覚えます。",
                          APP_TITLE, wx.OK | wx.ICON_INFORMATION, self)
            return
        self.chain.learn_noise()
        self.set_status("ノイズを測定中…（2 秒間、声を出さないでください）")
        wx.CallLater(2000, self._finish_learn_noise)

    def _finish_learn_noise(self) -> None:
        ok = self.chain.finish_learning()
        self.set_status("ノイズを覚えました" if ok
                        else "測定できませんでした（マイクが動いていません）")

    # ---------------------------------------------------------- VST3 タブ
    def _build_vst_tab(self) -> None:
        tab = self.vst_tab
        sizer = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticText(tab, label="プラグイン"), 0,
                wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        names = [name for name, _ in available_vst3()]
        self.vst_combo = wx.ComboBox(tab, choices=names, style=wx.CB_READONLY)
        if names:
            self.vst_combo.SetSelection(0)
        top.Add(self.vst_combo, 1, wx.ALIGN_CENTER_VERTICAL)
        add = wx.Button(tab, label="マイクに追加")
        add.Bind(wx.EVT_BUTTON, lambda _e: self.add_vst3())
        top.Add(add, 0, wx.LEFT, 6)
        sizer.Add(top, 0, wx.EXPAND | wx.ALL, 8)
        sizer.Add(hint_label(tab, f"{len(names)} 個のプラグインが見つかりました"
                                  "（マイクの後段に入ります）"),
                  0, wx.LEFT | wx.BOTTOM, 10)

        columns = wx.BoxSizer(wx.HORIZONTAL)

        box, parent = boxed(tab, " 挿しているもの ")
        self.vst_list = wx.ListCtrl(parent, style=wx.LC_REPORT | wx.LC_SINGLE_SEL,
                                    size=self.dip(280, -1))
        self.vst_list.InsertColumn(0, "名前", width=self.FromDIP(190))
        self.vst_list.InsertColumn(1, "状態", width=self.FromDIP(80))
        self.vst_list.Bind(wx.EVT_SIZE,
                           lambda e: (stretch_column(self.vst_list, 0), e.Skip()))
        self.vst_list.Bind(wx.EVT_LIST_ITEM_SELECTED,
                           lambda _e: self._show_vst_parameters())
        self.vst_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda _e: self.open_vst_editor())
        box.Add(self.vst_list, 1, wx.EXPAND | wx.ALL, 4)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler, width in (("▲", lambda: self.move_vst(-1), 36),
                                      ("▼", lambda: self.move_vst(1), 36),
                                      ("バイパス", self.toggle_vst_bypass, 80),
                                      ("外す", self.remove_vst, 60)):
            button = wx.Button(parent, label=label, size=self.dip(width, -1))
            button.Bind(wx.EVT_BUTTON, lambda _e, h=handler: h())
            buttons.Add(button, 0, wx.RIGHT, 3)
        box.Add(buttons, 0, wx.ALL, 4)
        self.editor_button = wx.Button(parent, label="プラグインの画面を開く",
                                       size=self.dip(-1, 34))
        emphasize(self.editor_button, 0)
        self.editor_button.Bind(wx.EVT_BUTTON, lambda _e: self.open_vst_editor())
        box.Add(self.editor_button, 0, wx.EXPAND | wx.ALL, 4)
        box.Add(wrapped_label(parent, "※ 画面は別ウィンドウで開きます。開いている間も"
                                      "このアプリはそのまま使え、つまみを動かすと"
                                      "音にすぐ反映されます。"),
                0, wx.EXPAND | wx.ALL, 4)
        columns.Add(box, 0, wx.EXPAND | wx.RIGHT, 8)

        box, parent = boxed(tab, " パラメータ ")
        head = wx.BoxSizer(wx.HORIZONTAL)
        head.Add(wx.StaticText(parent, label="絞り込み"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.param_filter = wx.TextCtrl(parent)
        self.param_filter.Bind(wx.EVT_TEXT, lambda _e: self._show_vst_parameters())
        head.Add(self.param_filter, 1, wx.ALIGN_CENTER_VERTICAL)
        # つまみが何十個もあるプラグインでは、全部並べると出るまでに間が空く。
        # よく使う分だけ先に出して、残りは求められたときに出す
        self.param_more = wx.Button(parent, label="すべて表示", size=self.dip(100, -1))
        self.param_more.Bind(wx.EVT_BUTTON, lambda _e: self._show_all_params())
        head.Add(self.param_more, 0, wx.LEFT, 6)
        self.param_more.Hide()
        box.Add(head, 0, wx.EXPAND | wx.ALL, 4)

        from wx.lib.scrolledpanel import ScrolledPanel
        self.param_area = ScrolledPanel(parent, style=wx.TAB_TRAVERSAL)
        self.param_area.SetupScrolling(scroll_x=False)
        self.param_area.SetSizer(wx.FlexGridSizer(0, 3, 2, 8))
        box.Add(self.param_area, 1, wx.EXPAND | wx.ALL, 4)
        self.vst_hint = hint_label(parent, "プラグインを追加すると、ここでつまみを操作できます。")
        box.Add(self.vst_hint, 0, wx.ALL, 4)
        columns.Add(box, 1, wx.EXPAND)

        sizer.Add(columns, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        tab.SetSizer(sizer)

    # ---------- ラックの操作 ----------
    def selected_slot(self):
        index = self.vst_list.GetFirstSelected()
        slots = self.chain.vst_slots
        return slots[index] if 0 <= index < len(slots) else None

    def _refresh_vst_rack(self, select=None) -> None:
        slots = self.chain.vst_slots
        self.vst_list.DeleteAllItems()
        for row, slot in enumerate(slots):
            state = "バイパス" if slot.bypass else "適用中"
            if slot.key in self.editors:
                state += " ◻"  # 画面を開いている
            self.vst_list.InsertItem(row, slot.name)
            self.vst_list.SetItem(row, 1, state)
        target = slots.index(select) if select in slots else (0 if slots else -1)
        if target >= 0:
            self.vst_list.Select(target)
            self.vst_list.Focus(target)
        current = self.selected_slot()
        self.editor_button.SetLabel(
            "プラグインの画面を閉じる"
            if current is not None and current.key in self.editors
            else "プラグインの画面を開く")
        self._show_vst_parameters()

    def add_vst3(self) -> None:
        name = self.vst_combo.GetValue()
        path = dict(available_vst3()).get(name)
        if not path:
            return

        # 走査も読み込みも JUCE 用のスレッドで行われるので、ここでは待たない。
        # 1 つのファイルに複数入っているもの（Serum2 など）は選ばせる
        def after_scan(candidates):
            choice = None
            if candidates:
                choice = self.ask_choice(f"{name} には複数のプラグインが入っています",
                                         "どれを使いますか？", candidates)
                if choice is None:
                    self.set_status("追加をやめました")
                    return
            self._load_vst3(name, path, choice)

        self.run_async(lambda: mic_chain.plugin_names(path), after_scan,
                       busy_text=f"{name} を調べています…")

    def _load_vst3(self, name: str, path: str, choice: str | None,
                   retried: bool = False) -> None:
        def done(slot):
            self.set_status(f"{name} を追加しました")
            self._refresh_vst_rack(select=slot)

        def failed(error):
            # 走査できない環境では、失敗のメッセージに候補名が並ぶ
            fallback = [] if retried else mic_chain.names_from_error(str(error))
            if fallback:
                pick = self.ask_choice(f"{name} には複数のプラグインが入っています",
                                       "どれを使いますか？", fallback)
                if pick is not None:
                    self._load_vst3(name, path, pick, retried=True)
                    return
            wx.MessageBox(f"{name} を読み込めませんでした:\n{error}", APP_TITLE,
                          wx.OK | wx.ICON_ERROR, self)
            self.set_status("読み込みに失敗しました")

        self.run_async(lambda: self.chain.add_vst3(path, name, choice), done,
                       busy_text=f"{name} を読み込んでいます…", on_error=failed)

    def remove_vst(self) -> None:
        slot = self.selected_slot()
        if slot is None:
            return
        if slot.key in self.editors:  # 画面を開いたまま外さない
            self.close_vst_editor(slot)
        self.chain.remove_vst3(slot)
        self._refresh_vst_rack()

    def move_vst(self, delta: int) -> None:
        slot = self.selected_slot()
        if slot is None:
            return
        self.chain.move_vst3(slot, delta)
        self._refresh_vst_rack(select=slot)

    def toggle_vst_bypass(self) -> None:
        slot = self.selected_slot()
        if slot is None:
            return
        self.chain.set_bypass(slot, not slot.bypass)
        self._refresh_vst_rack(select=slot)

    def _restore_vst(self) -> None:
        """前回の VST3 構成を戻す。読み込みは JUCE 用のスレッドで行う。"""
        saved = self.cfg.get("vst3", [])
        if not saved:
            return

        def done(failed):
            self._refresh_vst_rack()
            self.set_status(f"読み込めなかった VST3: {', '.join(failed)}"
                            if failed else "準備完了")

        self.run_async(lambda: self.chain.restore_vst_state(saved), done,
                       busy_text="前回の VST3 を読み込んでいます…")

    # ---------- プラグインの画面（別プロセス）----------
    def open_vst_editor(self) -> None:
        """プラグイン本体の画面を開く。押すたびに開閉を切り替える。"""
        import json

        slot = self.selected_slot()
        if slot is None:
            return
        if slot.key in self.editors:
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
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                bufsize=1, creationflags=flags)
        except Exception as e:
            wx.MessageBox(f"エディタを起動できませんでした:\n{e}", APP_TITLE,
                          wx.OK | wx.ICON_ERROR, self)
            return

        self.editors[slot.key] = {"proc": proc, "slot": slot, "ready": False}
        self._send_to_editor(slot, "init", slot.parameter_state())
        threading.Thread(target=self._read_editor, args=(slot, proc),
                         daemon=True).start()
        self.set_status(f"{slot.name} の画面を開いています…")
        self._refresh_vst_rack(select=slot)

    def close_vst_editor(self, slot=None) -> None:
        """開いているエディタを閉じる。slot 省略で全部閉じる。"""
        targets = [self.editors.get(slot.key)] if slot else list(self.editors.values())
        for entry in [e for e in targets if e]:
            self._send_to_editor(entry["slot"], "close")
            proc = entry["proc"]
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()

    def _send_to_editor(self, slot, command: str, params: dict | None = None) -> None:
        import json

        entry = self.editors.get(slot.key)
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
        import json

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                wx.CallAfter(self._on_editor_event, slot, message)
        finally:
            proc.wait()
            wx.CallAfter(self._on_editor_closed, slot)

    def _on_editor_event(self, slot, message: dict) -> None:
        kind = message.get("type")
        if kind == "ready":
            entry = self.editors.get(slot.key)
            if entry:
                entry["ready"] = True
            self.set_status(f"{slot.name} の画面を表示中（本体はそのまま使えます）")
            self._refresh_vst_rack(select=slot)
        elif kind == "params":
            # エディタで動かしたつまみを、実際に音が通っている方へ反映する
            slot.apply_parameter_state(message.get("values", {}))
            self._refresh_param_values()
        elif kind == "error":
            wx.MessageBox(f"{slot.name} の画面を開けませんでした:\n{message.get('message')}",
                          APP_TITLE, wx.OK | wx.ICON_ERROR, self)

    def _on_editor_closed(self, slot) -> None:
        self.editors.pop(slot.key, None)
        self.set_status("準備完了")
        self._refresh_vst_rack(select=slot)

    # ---------- つまみ ----------
    VST_TAB = 2
    PARAM_LIMIT = 16

    def _show_all_params(self) -> None:
        self._param_show_all = True
        self._param_signature = None
        self._show_vst_parameters()

    def _show_vst_parameters(self) -> None:
        if self.notebook.GetSelection() != self.VST_TAB:
            # 見えていない画面のために時間を使わない。開かれたときに組む
            self._param_pending = True
            self._param_signature = None
            return
        slot = self.selected_slot()
        key = slot.key if slot is not None else None
        if key != self._param_last_key:  # 別のプラグインなら絞り込みからやり直す
            self._param_last_key = key
            self._param_show_all = False
        needle = self.param_filter.GetValue().strip().lower()
        signature = (key, needle, self._param_show_all)
        if signature == self._param_signature:
            return
        self._param_signature = signature
        self._param_build += 1
        generation = self._param_build

        area = self.param_area
        area.Freeze()
        area.GetSizer().Clear(delete_windows=True)
        self._param_rows = []
        area.Thaw()

        if slot is None:
            self.param_more.Hide()
            self.vst_hint.SetLabel("プラグインを追加すると、ここでつまみを操作できます。")
            return

        self.vst_hint.SetLabel(f"{slot.name}: つまみを読み込んでいます…")
        # つまみの読み出しは JUCE スレッドで行われる。ここで待つと、
        # 読み込み中のプラグインがあるぶんだけ画面が固まる
        self.run_async(
            lambda: self._param_snapshot(slot),
            lambda snapshot: self._build_param_rows(slot, snapshot, needle, generation))

    @staticmethod
    def _param_snapshot(slot) -> list[dict]:
        """つまみの情報を JUCE スレッドでまとめて読む。"""
        def read():
            rows = []
            for key, param in slot.plugin.parameters.items():
                info = {"key": key, "param": param, "text": "", "choices": None,
                        "min": 0.0, "max": 1.0, "raw": 0.0, "value": None}
                for name, getter in (
                        ("text", lambda p=param: str(p.string_value)),
                        ("choices", lambda p=param: list(p.valid_values or [])),
                        ("raw", lambda p=param: float(p.raw_value)),
                        ("value", lambda k=key: getattr(slot.plugin, k))):
                    try:
                        info[name] = getter()
                    except Exception:
                        pass
                try:
                    info["min"], info["max"] = param.min_value, param.max_value
                except Exception:
                    pass
                rows.append(info)
            return rows

        try:
            return juce_thread.run(read)
        except Exception:
            return []

    def _build_param_rows(self, slot, snapshot: list[dict], needle: str,
                          generation: int) -> None:
        if generation != self._param_build:
            return  # 読んでいる間に別のプラグインへ切り替わった
        matched = [i for i in snapshot if not needle or needle in i["key"].lower()]
        hidden = 0
        if not self._param_show_all and len(matched) > self.PARAM_LIMIT:
            hidden = len(matched) - self.PARAM_LIMIT
            rows = matched[:self.PARAM_LIMIT]
            self.param_more.SetLabel(f"すべて表示（{len(matched)}）")
            self.param_more.Show()
        else:
            rows = matched
            self.param_more.Hide()

        area = self.param_area
        area.Freeze()
        sizer = area.GetSizer()
        for info in rows:
            self._build_param_row(slot, info, sizer)
        area.Layout()
        area.SetupScrolling(scroll_x=False, scrollToTop=False)
        area.Thaw()
        self.vst_hint.SetLabel(
            f"{slot.name}: {len(rows)}/{len(snapshot)} 項目を表示中"
            + (f"（あと {hidden} 項目は「すべて表示」か絞り込みで）" if hidden
               else "（細かい調整は「プラグインの画面を開く」から）" if rows else ""))
        self.vst_tab.Layout()

    def _build_param_row(self, slot, info: dict, sizer) -> None:
        area = self.param_area
        key, param = info["key"], info["param"]
        sizer.Add(wx.StaticText(area, label=key.replace("_", " "), size=self.dip(150, -1)), 0,
                  wx.ALIGN_CENTER_VERTICAL)
        entry = {"key": key, "param": param, "slot": slot, "kind": "",
                 "widget": None, "label": None}
        choices = info["choices"]

        if choices and isinstance(choices[0], bool):
            widget = wx.CheckBox(area)
            widget.SetValue(bool(info["value"]))
            widget.Bind(wx.EVT_CHECKBOX,
                        lambda _e, e=entry: self._set_param(e, e["widget"].GetValue()))
            entry["kind"] = "bool"
        elif choices and isinstance(choices[0], str) and len(choices) <= 48:
            widget = wx.ComboBox(area, choices=list(choices), style=wx.CB_READONLY)
            widget.SetValue(str(info["value"] if info["value"] is not None else choices[0]))
            widget.Bind(wx.EVT_COMBOBOX,
                        lambda _e, e=entry: self._set_param(e, e["widget"].GetValue()))
            entry["kind"] = "enum"
        else:
            low, high = info["min"], info["max"]
            numeric = isinstance(low, (int, float)) and isinstance(high, (int, float)) \
                and not isinstance(low, bool)
            if numeric:
                try:
                    current = float(info["value"])
                except (TypeError, ValueError):
                    current = float(low)
                widget = FloatSlider(area, low, high, current)
                entry["kind"] = "float"
            else:
                # 単位が取れないものは 0〜1 の生値で操作する
                widget = FloatSlider(area, 0.0, 1.0, info["raw"])
                entry["kind"] = "raw"
            widget.Bind(wx.EVT_SLIDER,
                        lambda _e, e=entry: self._set_param(e, e["widget"].GetFloat()))
            widget.Bind(wx.EVT_SCROLL_THUMBTRACK,
                        lambda e: (setattr(self, "_param_dragging", True), e.Skip()))
            widget.Bind(wx.EVT_SCROLL_THUMBRELEASE,
                        lambda e: (setattr(self, "_param_dragging", False), e.Skip()))

        entry["widget"] = widget
        sizer.Add(widget, 1, wx.EXPAND)
        value_label = wx.StaticText(area, label=info["text"], size=self.dip(90, -1))
        entry["label"] = value_label
        sizer.Add(value_label, 0, wx.ALIGN_CENTER_VERTICAL)
        self._param_rows.append(entry)

    def _set_param(self, entry: dict, value) -> None:
        def apply():
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
            try:
                return str(entry["param"].string_value), float(entry["param"].raw_value)
            except Exception:
                return "", None

        try:
            text, raw = juce_thread.run(apply)
        except Exception:
            return
        entry["label"].SetLabel(text)
        slot = entry["slot"]
        if raw is not None and slot.key in self.editors:
            try:
                self._send_to_editor(slot, "set", {entry["key"]: raw})
            except Exception:
                pass

    def _refresh_param_values(self) -> None:
        """プラグイン本体の画面で動かされた値を表示に反映する。"""
        if self._param_dragging or not self._param_rows or juce_thread.busy():
            return
        rows = list(self._param_rows)

        def read():
            out = []
            for entry in rows:
                param, kind = entry["param"], entry["kind"]
                item = {"text": "", "current": None}
                try:
                    item["text"] = str(param.string_value)
                except Exception:
                    pass
                try:
                    if kind == "raw":
                        item["current"] = float(param.raw_value)
                    elif kind == "float":
                        item["current"] = float(getattr(entry["slot"].plugin, entry["key"]))
                    elif kind == "bool":
                        item["current"] = bool(getattr(entry["slot"].plugin, entry["key"]))
                    else:
                        item["current"] = str(getattr(entry["slot"].plugin, entry["key"]))
                except Exception:
                    pass
                out.append(item)
            return out

        try:
            values = juce_thread.run(read)
        except Exception:
            return
        for entry, item in zip(rows, values):
            entry["label"].SetLabel(item["text"])
            widget, current = entry["widget"], item["current"]
            if current is None:
                continue
            try:
                if isinstance(current, float):
                    if abs(widget.GetFloat() - current) > 1e-4:
                        widget.SetFloat(current)
                elif isinstance(current, bool):
                    if widget.GetValue() != current:
                        widget.SetValue(current)
                elif widget.GetValue() != current:
                    widget.SetValue(current)
            except Exception:
                continue

    # ---------------------------------------------------------- 設定・診断タブ
    def _build_setup_tab(self) -> None:
        tab = self.setup_tab
        sizer = wx.BoxSizer(wx.VERTICAL)

        box, parent = boxed(tab, " オーディオ ")
        grid = wx.FlexGridSizer(0, 3, 4, 8)
        grid.AddGrowableCol(1, 1)
        grid.Add(wx.StaticText(parent, label="Host API"), 0, wx.ALIGN_CENTER_VERTICAL)
        import sounddevice as sd
        apis = ["すべて"] + [a["name"] for a in sd.query_hostapis()]
        saved_api = self.cfg.get("hostapi")
        if saved_api not in apis:  # 別の OS で保存された設定を引き継いだ場合など
            saved_api = platform_support.default_host_api() or "すべて"
        self.api_combo = wx.ComboBox(parent, choices=apis, style=wx.CB_READONLY)
        self.api_combo.SetValue(saved_api)
        self.api_combo.Bind(wx.EVT_COMBOBOX, lambda _e: self._reload_devices())
        grid.Add(self.api_combo, 1, wx.EXPAND)
        grid.Add(hint_label(parent, platform_support.host_api_hint()), 0,
                 wx.ALIGN_CENTER_VERTICAL)

        grid.Add(wx.StaticText(parent, label="声の遅れ"), 0, wx.ALIGN_CENTER_VERTICAL)
        saved_latency = self.cfg.get("latency", "low")
        self.latency = wx.ComboBox(parent, choices=list(LATENCY_MODES),
                                   style=wx.CB_READONLY)
        self.latency.SetValue(next((k for k, v in LATENCY_MODES.items()
                                    if v == saved_latency), list(LATENCY_MODES)[0]))
        self.latency.Bind(wx.EVT_COMBOBOX, lambda _e: self._on_device_change())
        grid.Add(self.latency, 0)
        grid.AddSpacer(1)

        grid.Add(wx.StaticText(parent, label="音の安定"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.buffer_slider = FloatSlider(parent, 10.0, 200.0,
                                         float(self.cfg.get("buffer_ms", 40.0)))
        self.buffer_slider.Bind(wx.EVT_SLIDER, lambda _e: self._on_buffer_change())
        grid.Add(self.buffer_slider, 1, wx.EXPAND)
        self.buffer_label = wx.StaticText(parent, label="", size=self.dip(70, -1))
        grid.Add(self.buffer_label, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)
        box.Add(hint_label(parent, "音が途切れるときは右へ。声の遅れは少し増えます"),
                0, wx.LEFT | wx.BOTTOM, 6)
        sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)
        self._on_buffer_change()

        box, parent = boxed(tab, " スマホから操作する ")
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.remote_check = wx.CheckBox(parent, label="リモコンを使う")
        self.remote_check.Bind(wx.EVT_CHECKBOX, lambda _e: self._toggle_remote())
        row.Add(self.remote_check, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.remote_url = wx.TextCtrl(parent, style=wx.TE_READONLY)
        row.Add(self.remote_url, 1, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row, 0, wx.EXPAND | wx.ALL, 6)
        box.Add(wrapped_label(
            parent, "同じ Wi-Fi のスマホでこの住所を開くと、曲の予約・キー・歌詞・映像が"
                    "手元で使えます（開いている間は、同じ回線の人なら誰でも操作できます）"),
            0, wx.LEFT | wx.BOTTOM | wx.RIGHT, 6)
        sizer.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        diag = wx.BoxSizer(wx.HORIZONTAL)
        diag.Add(head_label(tab, "機器の状態"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.diag_button = wx.Button(tab, label="すべて調べる")
        self.diag_button.Bind(wx.EVT_BUTTON, lambda _e: self.run_diagnostics())
        diag.Add(self.diag_button, 0, wx.RIGHT, 6)
        for label, handler in (("機器を再検出", self.rescan_devices),
                               ("選んだものを使う", self.use_diagnosed_device)):
            button = wx.Button(tab, label=label)
            button.Bind(wx.EVT_BUTTON, lambda _e, h=handler: h())
            diag.Add(button, 0, wx.RIGHT, 6)
        sizer.Add(diag, 0, wx.EXPAND | wx.ALL, 8)

        self.diag_list = wx.ListCtrl(tab, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for index, (text, width) in enumerate((("種類", 60), ("デバイス", 330),
                                               ("結果", 260), ("OS 側の設定", 260))):
            self.diag_list.InsertColumn(index, text, width=self.FromDIP(width))
        self.diag_list.Bind(wx.EVT_SIZE,
                            lambda e: (stretch_column(self.diag_list, 3), e.Skip()))
        sizer.Add(self.diag_list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        box, parent = boxed(tab, " AI ボーカル除去 ")
        self.vocal_setup_label = hint_label(parent, "確認中…")
        self.vocal_setup_label.Wrap(700)
        box.Add(self.vocal_setup_label, 0, wx.ALL, 6)
        self.vocal_setup_button = wx.Button(parent, label="有効にする")
        self.vocal_setup_button.Enable(False)
        self.vocal_setup_button.Bind(wx.EVT_BUTTON, lambda _e: self.offer_model_install())
        box.Add(self.vocal_setup_button, 0, wx.LEFT | wx.BOTTOM, 6)
        self.vocal_progress = wx.Gauge(parent, range=100)
        box.Add(self.vocal_progress, 0, wx.EXPAND | wx.ALL, 6)
        self.vocal_progress.Hide()
        sizer.Add(box, 0, wx.EXPAND | wx.ALL, 8)

        storage = wx.BoxSizer(wx.HORIZONTAL)
        self.cache_label = hint_label(tab, "作成したオフボーカル: 確認中…")
        # あとで長い文字に差し替わる。幅を取っておかないとボタンに重なる
        storage.Add(self.cache_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        clear = wx.Button(tab, label="まとめて削除")
        clear.Bind(wx.EVT_BUTTON, lambda _e: self.clear_offvocal_cache())
        storage.Add(clear, 0)
        sizer.Add(storage, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        sizer.Add(hint_label(tab, f"設定の保存先: {config.CONFIG_PATH}"), 0,
                  wx.ALL, 8)
        tab.SetSizer(sizer)
        wx.CallLater(1200, self._update_cache_label)

    def _on_buffer_change(self) -> None:
        self.buffer_label.SetLabel(f"{self.buffer_slider.GetFloat():.0f} ms")

    # ---------- 診断 ----------
    def run_diagnostics(self) -> None:
        """機器を 1 台ずつ調べて、終わったものから表に出す。"""
        if self._diag_running:
            self._diag_cancel = True
            return
        api = self._api_filter()
        targets = dev.list_devices("input", api) + dev.list_devices("output", api)
        if not targets:
            return
        self.diag_list.DeleteAllItems()
        self._diag_running = True
        self._diag_cancel = False
        self._diag_rows = {}
        self._diag_status = None
        self.diag_button.SetLabel("中止する")
        self.set_status(f"機器を確認中… 0/{len(targets)} 台")

        def load_status():
            # OS 側の設定を読むのは 2 秒ほどかかる。機器の確認より先に置くと
            # 最初の 1 台が出るまで無反応に見えるので、並行して取って後から埋める。
            try:
                status = dev.system_status()
            except Exception:
                status = {}
            wx.CallAfter(self._fill_diagnostic_hints, status)

        def work():
            for index, device in enumerate(targets, start=1):
                if self._diag_cancel:
                    break
                health = dev.check(device, seconds=0.5, timeout=4.0)
                status = self._diag_status
                hint = dev.system_hint(device, status) if status else ""
                wx.CallAfter(self._add_diagnostic_row, device, health, hint,
                             index, len(targets))
            wx.CallAfter(self._finish_diagnostics, len(targets))

        threading.Thread(target=load_status, daemon=True).start()
        threading.Thread(target=work, daemon=True).start()

    def _add_diagnostic_row(self, device, health, hint, index: int, total: int) -> None:
        row = self.diag_list.GetItemCount()
        self.diag_list.InsertItem(row, "入力" if device.is_input else "出力")
        self.diag_list.SetItem(row, 1, f"{device.name} [{device.hostapi}]")
        self.diag_list.SetItem(row, 2, health.summary)
        self.diag_list.SetItem(row, 3, hint or "—")
        self._diag_rows[row] = device
        self.diag_list.EnsureVisible(row)
        self.set_status(f"機器を確認中… {index}/{total} 台")

    def _fill_diagnostic_hints(self, status: dict) -> None:
        """OS 側の設定が読めたら、すでに出ている行の欄を埋める。"""
        self._diag_status = status
        if not status:
            return
        for row, device in self._diag_rows.items():
            hint = dev.system_hint(device, status)
            if hint and row < self.diag_list.GetItemCount():
                self.diag_list.SetItem(row, 3, hint)

    def _finish_diagnostics(self, total: int) -> None:
        self._diag_running = False
        self.diag_button.SetLabel("すべて調べる")
        count = self.diag_list.GetItemCount()
        usable = sum(1 for row in range(count)
                     if self.diag_list.GetItemText(row, 2).startswith("○"))
        self.set_status(f"確認おわり: {count}/{total} 台を調べて {usable} 台が使えます")

    def use_diagnosed_device(self) -> None:
        """診断の表で選んだ機器を、そのまま設定に反映する。"""
        row = self.diag_list.GetFirstSelected()
        if row < 0:
            wx.MessageBox("表から使いたい機器を選んでから押してください。", APP_TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        kind = self.diag_list.GetItemText(row, 0)
        name = self.diag_list.GetItemText(row, 1).split(" [")[0]
        pool = self.mic_devices if kind == "入力" else self.out_devices
        target = next((d for d in pool if d.name == name), None)
        if target is None:
            wx.MessageBox("この機器は今の Host API の一覧にありません。", APP_TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
            return
        if kind == "入力":
            self.mic_combo.SetValue(target.label)
        else:
            self.out_combo.SetValue(target.label)
        self._on_device_change()
        self.set_status(f"{target.name} を{kind}に設定しました")
        self.notebook.SetSelection(1)

    # ---------- スマホからのリモコン ----------
    def _toggle_remote(self) -> None:
        if self.remote_check.GetValue():
            try:
                if self.remote is None:
                    self.remote = webserver.RemoteServer(self)
                url = self.remote.start()
            except Exception as e:
                self.remote_check.SetValue(False)
                LOG.error("リモコンを開けませんでした", exc_info=e)
                wx.MessageBox(f"リモコンを開けませんでした:\n{e}", APP_TITLE,
                              wx.OK | wx.ICON_ERROR, self)
                return
            self.remote_url.SetValue(url)
            self.set_status(f"リモコンを開きました: {url}")
        else:
            if self.remote is not None:
                self.remote.stop()
            self.remote_url.SetValue("")
            self.set_status("リモコンを閉じました")

    # ---------- ボーカル除去 ----------
    def _check_separator(self) -> None:
        """使える環境かを調べる。torch は別プロセスで読ませる。"""
        def work():
            import separator
            return separator.capability_in_subprocess()

        self.run_async(work, self._apply_separator_capability)

    def _apply_separator_capability(self, cap) -> None:
        self.separator_capability = cap
        installable = getattr(cap, "installable", False)
        if cap.available:
            self.vocal_button.Enable(True)
            self.vocal_hint.SetLabel(f"（{cap.gpu_name} で作成できます）")
        elif installable:
            self.vocal_button.Enable(True)
            self.vocal_hint.SetLabel(f"（{cap.gpu_name} で使えます。初回のみ準備が必要）")
        else:
            self.vocal_button.Enable(False)
            self.vocal_hint.SetLabel(f"（{cap.reason}）")
        self.vocal_setup_label.SetLabel(cap.summary)
        self.vocal_setup_button.Enable(bool(installable))

    def offer_model_install(self) -> None:
        import model_installer

        cap = self.separator_capability
        if cap is None or cap.available:
            return
        if not self.ask(
                "AI によるボーカル除去を有効にします。\n\n"
                f"　使う機器: {cap.gpu_name}\n"
                f"　取得する量: 約 {model_installer.APPROX_TOTAL_MB / 1000:.0f} GB\n"
                "　かかる時間: 回線によって 5〜30 分ほど\n\n"
                "この間もアプリは使えます。始めますか？"):
            return

        import separator

        self._install_cancel = threading.Event()
        self.vocal_progress.Show()
        self.vocal_progress.SetValue(0)
        self.vocal_setup_button.Enable(False)
        self.setup_tab.Layout()

        def report(stage: str, ratio: float, detail: str = "") -> None:
            wx.CallAfter(self._report_install, stage, ratio, detail)

        def work():
            model_installer.install(progress=report, cancel=self._install_cancel)
            return separator.capability(refresh=True)

        def done(cap_after):
            self.vocal_progress.Hide()
            self.setup_tab.Layout()
            self._apply_separator_capability(cap_after)
            self.set_status("ボーカル除去が使えるようになりました"
                            if cap_after.available else cap_after.reason)

        def failed(error):
            self.vocal_progress.Hide()
            self.vocal_setup_button.Enable(True)
            self.setup_tab.Layout()
            wx.MessageBox(f"取得できませんでした:\n{error}", APP_TITLE,
                          wx.OK | wx.ICON_ERROR, self)

        self.run_async(work, done, busy_text="ボーカル除去の準備をしています…",
                       on_error=failed)

    def _report_install(self, stage: str, ratio: float, detail: str = "") -> None:
        self.vocal_progress.SetValue(int(max(0.0, min(1.0, ratio)) * 100))
        self.vocal_setup_label.SetLabel(f"{stage}… {detail}" if detail else f"{stage}…")
        self.set_status(f"ボーカル除去の準備: {stage} {ratio*100:.0f}%")

    def remove_vocals(self) -> None:
        """いま選んでいる曲からボーカルを消す。"""
        import separator

        cap = self.separator_capability
        if cap is not None and not cap.available and getattr(cap, "installable", False):
            self.offer_model_install()
            return
        source = self.current_local_path
        track = self.current_track
        if source is None and track is None:
            wx.MessageBox("先に曲を選んでください。", APP_TITLE,
                          wx.OK | wx.ICON_INFORMATION, self)
            return

        def report(stage: str, ratio: float) -> None:
            wx.CallAfter(self.music_status.SetLabel,
                         f"ボーカルを消しています: {stage}… {ratio*100:.0f}%")

        def work():
            local = source
            if local is None:  # 配信中の曲は、先に手元へ取り込む
                import pitch_render
                folder = os.path.join(pitch_render.CACHE_DIR, "download")
                os.makedirs(folder, exist_ok=True)
                local = music_search.download(track.id, folder, max_height=480)
            return separator.separate(local, progress=report)

        self.player.stop(wait=False)
        self.run_async(work, self._play_offvocal,
                       busy_text="ボーカルを消しています…")

    def _play_offvocal(self, path: str) -> None:
        out = self.selected_device("output")
        self.current_local_path = path
        self.player.open(path, None, {}, device=out.index if out else None)
        name = os.path.basename(path)
        self.music_status.SetLabel(f"オフボーカルを再生中: {name}")
        self.play_button.SetLabel("⏸ 一時停止")
        self.video.clear(f"♪ {name}（ボーカル除去済み）")

    def _update_cache_label(self) -> None:
        import separator

        count, size = len(separator.cached_files()), separator.cache_size_mb()
        self.cache_label.SetLabel(
            f"作成したオフボーカル: {count} 件 / {size:.0f} MB"
            + ("" if count else "（まだありません）"))
        self.setup_tab.Layout()  # 文字が伸びた分を並べ直す

    def clear_offvocal_cache(self) -> None:
        import separator

        if not separator.cached_files():
            return
        if self.ask("作成したオフボーカル音源をすべて削除しますか？\n"
                    "（元の曲は消えません。もう一度作り直せます）"):
            removed = separator.clear_cache()
            self._update_cache_label()
            self.set_status(f"{removed} 件削除しました")

    # ---------- 定期処理 ----------
    def _on_tab_changed(self, event) -> None:
        """VST3 タブを開いたときに、先送りしていたつまみを組み立てる。"""
        event.Skip()
        if self.notebook.GetSelection() == self.VST_TAB and self._param_pending:
            self._param_pending = False
            self._show_vst_parameters()

    @staticmethod
    def _set_text(label, text: str) -> None:
        """同じ文字なら書き換えない（毎回書くと描き直しが走る）。"""
        if label.GetLabel() != text:
            label.SetLabel(text)

    def _on_tick(self, _event) -> None:
        # 映像は毎回。ここが 30fps に届かないと、歌詞つき動画がかくつく
        image = self.player.take_frame()
        if image is not None:
            self.last_frame = image  # リモコンへ流す用
            self.video.show_frame(image)

        self._tick_count += 1
        if self._busy and self._tick_count % 4 == 0:
            # 待たせている間、帯を流し続ける（Windows 以外は叩き続けが要る）
            self.busy_bar.Pulse()
        if self._tick_count % 2 or not self._ready:  # 以下は 30Hz で十分
            return

        if self.router.running:
            in_peak = self.chain.in_peak if self.chain.enabled else self.router.in_peak
            self.in_meter.SetValue(int(meter_value(in_peak)))
            self.out_meter.SetValue(int(meter_value(self.router.out_peak)))
            self._set_text(self.in_db_label, f"{db_of(in_peak):.0f} dB")
            self._set_text(self.out_db_label, f"{db_of(self.router.out_peak):.0f} dB")
            if self._tick_count % 30 == 0:
                self._update_quality_label()
        elif self.in_meter.GetValue():
            self.in_meter.SetValue(0)
            self.out_meter.SetValue(0)
            self._set_text(self.quality_label, "")

        if self._transport_state != self.player.state:
            self._transport_state = self.player.state
            self._refresh_transport()
        if self.player.state in ("playing", "paused"):
            position, duration = self.player.position, self.player.duration
            self._set_text(self.time_label,
                           f"{time_text(position)} / {time_text(duration)}")
            if duration and not self._seeking:
                self.seek.SetValue(int(min(1000, position / duration * 1000)))
            self._update_lyric_display()
            if self.player.finished:
                self._on_song_finished()  # 予約があれば次の曲へ

    def _update_quality_label(self) -> None:
        """遅延と途切れ回数を出す。途切れが増えたら直し方も添える。"""
        over, under = self.router.xruns
        total = over + under
        channels = "ステレオ" if getattr(self.router, "_active_channels", 1) > 1 else "モノラル"
        text = (f"遅延 {self.router.latency_ms:.0f} ms（声が返ってくるまで）"
                f" / {channels} / 途切れ {total} 回")
        if total > self._last_xruns and total > 3:
            text += " ← 「設定・診断」でバッファを大きくすると直ります"
            self.quality_label.SetForegroundColour(wx.Colour(0xB0, 0, 0))
        else:
            self.quality_label.SetForegroundColour(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        self._last_xruns = total
        self.quality_label.SetLabel(text)

    # ---------- 終了 ----------
    def _on_close(self, event) -> None:
        self.cfg["music_volume"] = self.music_volume.GetFloat()
        self.cfg["prefer_off_vocal"] = self.offvocal.GetValue()
        config.save(self.cfg)
        self.timer.Stop()
        if self.remote is not None:
            self.remote.stop()
        self.player.stop(wait=False)
        self.router.stop()
        applog.closing()
        event.Skip()
        self.Destroy()


class VoxDeskApp(wx.App):
    """画面の処理で起きた例外を、消さずに記録へ残すための入れ物。

    既定では標準エラーへ出るだけで、インストーラ版には端末が無いので
    消えてしまう。「押しても何も起きない」の原因が追えなくなる。
    """

    def OnExceptionInMainLoop(self) -> bool:
        LOG.error("画面の処理で例外", exc_info=True)
        frame = self.GetTopWindow()
        if frame is not None:
            try:
                frame.set_status(f"問題が起きました（{sys.exc_info()[0].__name__}）")
            except Exception:
                pass
        return True  # 落とさずに続ける


def main() -> None:
    app = VoxDeskApp(False)
    VoxDesk()
    app.MainLoop()


if __name__ == "__main__":
    main()

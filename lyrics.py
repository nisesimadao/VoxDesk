"""時刻つき歌詞の取り込み（LRCLIB）。

いちばん難しいのは「どの曲か」を当てるところ。
オフボーカル動画のタイトルは
    【カラオケ】Lemon / 米津玄師【ガイドメロディなし】
のように飾りが多く、曲名とアーティストがそのままでは取り出せない。
飾りを削って候補を作り、曲の長さと突き合わせて選ぶ。

歌詞そのものはこのプログラムでは持たず、取得したものを表示するだけ。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import applog

LOG = applog.get(__name__)

API = "https://lrclib.net/api"
USER_AGENT = "VoxDesk (https://github.com/nisesimadao/VoxDesk)"
CACHE_SECONDS = 3600

# タイトルから取り除く飾り。これが入った括弧ごと落とす。
DECORATIONS = (
    "カラオケ", "karaoke", "オフボーカル", "off vocal", "offvocal", "off-vocal",
    "instrumental", "インスト", "伴奏", "ガイドメロディ", "ガイドメロ", "ガイドなし",
    "歌詞付", "歌詞あり", "字幕", "音程バー", "原曲キー", "キー変更", "女性キー", "男性キー",
    "cover", "カバー", "mv", "pv", "full", "フル", "高音質", "練習用", "生音", "音源",
)
BRACKETS = r"[【\[（(「『][^】\]）)」』]*[】\]）)」』]"

_cache: dict[str, tuple[float, object]] = {}


@dataclass
class Line:
    time: float
    text: str


@dataclass
class Lyrics:
    track: str
    artist: str
    duration: float | None
    lines: list[Line] = field(default_factory=list)
    plain: str = ""

    @property
    def synced(self) -> bool:
        return bool(self.lines)

    def at(self, position: float) -> tuple[str, str]:
        """いまの行と次の行を返す。"""
        if not self.lines:
            return "", ""
        index = -1
        for i, line in enumerate(self.lines):
            if line.time <= position:
                index = i
            else:
                break
        current = self.lines[index].text if index >= 0 else ""
        following = self.lines[index + 1].text if index + 1 < len(self.lines) else ""
        return current, following


def clean_title(raw: str) -> tuple[str, str]:
    """動画のタイトルから (曲名, アーティスト) を推定する。"""
    text = raw or ""
    # 飾りが入っている括弧を丸ごと落とす
    def drop(match: re.Match) -> str:
        inner = match.group(0).lower()
        return "" if any(word in inner for word in DECORATIONS) else match.group(0)

    text = re.sub(BRACKETS, drop, text)
    # 残った飾り語も削る
    for word in DECORATIONS:
        text = re.sub(re.escape(word), " ", text, flags=re.I)
    # 中身が消えて空になった括弧と、その残骸を落とす
    #（【女性キー(+5)】 のような入れ子だと閉じ括弧だけ残ることがある）
    text = re.sub(r"[【\[（(「『]\s*[】\]）)」』]", " ", text)
    text = re.sub(r"^[\s】\]）)」』・,-]+", "", text)
    text = re.sub(r"[\s【\[（(「『・,-]+$", "", text)
    text = re.sub(r"[/／|｜]{2,}", "/", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—・,")

    # 「アーティスト「曲名」」のように鉤括弧で曲名を囲む書き方も多い
    quoted = re.search(r"[「『\"”]([^」』\"”]{1,60})[」』\"”]", text)
    if quoted:
        song = quoted.group(1).strip()
        rest = (text[:quoted.start()] + " " + text[quoted.end():]).strip(" -–—・/|")
        rest = re.sub(r"\s+", " ", rest).strip()
        if song:
            return song, rest

    # 「曲名 / アーティスト」または「アーティスト - 曲名」
    for separator in ("/", "／", " - ", " – ", "｜", "|"):
        if separator in text:
            left, _, right = text.partition(separator)
            left, right = left.strip(), right.strip()
            if not left or not right:
                continue
            # 日本語の慣習では「曲名 / アーティスト」が多い
            return left, right
    return text.strip(), ""


def _request(path: str, params: dict) -> object:
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = None
        else:
            raise RuntimeError(f"歌詞を取得できませんでした（{e.code}）") from e
    except Exception as e:
        raise RuntimeError("歌詞を取得できませんでした（ネットの接続を確認してください）") from e
    _cache[url] = (time.time(), data)
    return data


def parse_lrc(text: str) -> list[Line]:
    """[mm:ss.xx] 形式の行を時刻つきに直す。"""
    lines: list[Line] = []
    for row in (text or "").splitlines():
        stamps = re.findall(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", row)
        if not stamps:
            continue
        body = re.sub(r"\[[^\]]*\]", "", row).strip()
        for minute, second in stamps:
            lines.append(Line(int(minute) * 60 + float(second.replace(":", ".")), body))
    lines.sort(key=lambda line: line.time)
    return lines


def _to_lyrics(entry: dict) -> Lyrics:
    return Lyrics(
        track=entry.get("trackName") or "",
        artist=entry.get("artistName") or "",
        duration=entry.get("duration"),
        lines=parse_lrc(entry.get("syncedLyrics") or ""),
        plain=entry.get("plainLyrics") or "",
    )


def search(track: str, artist: str = "", duration: float | None = None,
           limit: int = 8) -> list[Lyrics]:
    """候補を探す。曲の長さが近いもの・時刻つきのものを上に並べる。"""
    if not track:
        return []
    params = {"track_name": track}
    if artist:
        params["artist_name"] = artist
    entries = _request("search", params) or []
    if not entries and artist:
        # 「曲名 / アーティスト」と「アーティスト / 曲名」は書き手によって逆になる
        entries = _request("search", {"track_name": artist, "artist_name": track}) or []
    if not entries and artist:  # アーティスト指定を外すと見つかることがある
        entries = _request("search", {"q": f"{track} {artist}".strip()}) or []
    if not entries:
        entries = _request("search", {"q": track}) or []

    results = [_to_lyrics(e) for e in entries if isinstance(e, dict)]

    def score(item: Lyrics) -> tuple:
        gap = abs((item.duration or 0) - duration) if duration and item.duration else 999
        return (0 if item.synced else 1, gap)

    results.sort(key=score)
    return results[:limit]


def best_match(title: str, duration: float | None = None) -> Lyrics | None:
    """動画のタイトルから、いちばんそれらしい歌詞を選ぶ。"""
    track, artist = clean_title(title)
    if not track:
        return None
    LOG.info("歌詞を探します: 曲名=%r アーティスト=%r 長さ=%s", track, artist, duration)
    candidates = search(track, artist, duration)
    if not candidates:
        return None
    top = candidates[0]
    # 長さが大きく違うものは、別の曲を掴んでいる可能性が高い
    if duration and top.duration and abs(top.duration - duration) > 45:
        LOG.info("長さが合いません（%.0f 秒差）", abs(top.duration - duration))
    return top

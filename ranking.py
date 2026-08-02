"""カラオケのランキングを取り込む。

「何を歌おうか」を決めるところが、実際にはいちばん時間を使う。
配信元のランキングを一覧できると、そこから曲名で検索へ流せる。

取得先のページはサーバー側で組み立てられているので、そのまま読める。
負荷をかけないよう、一度取ったら一定時間は使い回す。
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
CACHE_SECONDS = 1800  # 30 分は取り直さない

# 表示名 -> ページの URL
BASE = "https://www.joysound.com/web/karaoke/ranking/"
SOURCES = {
    # 新曲トレンド（trends/all）はページの作りが違って読めないので入れていない
    "総合": BASE + "all",
    "急上昇": BASE + "hot",
    "アニメ": BASE + "anime/weekly",
    "ボカロ": BASE + "vocaloid/weekly",
    "洋楽": BASE + "foreign/weekly",
    "演歌": BASE + "enka/weekly",
}

_cache: dict[str, tuple[float, list]] = {}


@dataclass
class RankedSong:
    rank: int
    title: str
    artist: str

    @property
    def query(self) -> str:
        """検索欄へ入れる文字列。"""
        return f"{self.title} {self.artist}".strip()


def _clean(fragment: str) -> list[str]:
    """タグを外して、中身の文字列を順に取り出す。"""
    parts = re.sub(r"<[^>]+>", "\x00", fragment).split("\x00")
    return [html.unescape(p).strip() for p in parts if p.strip()]


def parse(page: str) -> list[RankedSong]:
    """ページから順位・曲名・アーティストを取り出す。

    1 曲につき複数のリンクが出てくる（順位の絵、曲名、関連情報）。
    そのうち「曲名」と「アーティスト」の 2 つが並ぶものだけを拾う。
    """
    songs: list[RankedSong] = []
    seen: set[str] = set()
    for href, inner in re.findall(
            r'<a[^>]+href="(/web/search/song/\d+)"[^>]*>(.*?)</a>', page, re.S):
        song_id = href.rsplit("/", 1)[-1]
        if song_id in seen:
            continue
        parts = _clean(inner)
        if len(parts) != 2:
            continue
        title, artist = parts
        if not title or title.isdigit() or "位" in title:
            continue
        seen.add(song_id)
        songs.append(RankedSong(rank=len(songs) + 1, title=title, artist=artist))
    return songs


def fetch(source: str = "JOYSOUND 総合", refresh: bool = False) -> list[RankedSong]:
    """ランキングを取ってくる。失敗したら例外を投げる。"""
    url = SOURCES.get(source)
    if url is None:
        raise ValueError(f"知らないランキングです: {source}")

    cached = _cache.get(url)
    if cached and not refresh and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]

    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ランキングを取得できませんでした（{e.code}）") from e
    except Exception as e:
        raise RuntimeError("ランキングを取得できませんでした（ネットの接続を確認してください）") from e

    songs = parse(page)
    if not songs:
        raise RuntimeError("ランキングを読み取れませんでした（配信元の作りが変わった可能性）")
    _cache[url] = (time.time(), songs)
    return songs

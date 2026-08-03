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
from dataclasses import dataclass, field, replace

import applog

LOG = applog.get(__name__)

# 行単位の歌詞の元。アカウント不要で、曲名だけでも探せる（当たりが広い）
API = "https://lrclib.net/api"
# こちらは LRCLIB を元に、1 文字ずつの頭出しを足しているところ。
# 曲名とアーティストの両方が要るぶん厳しいので、当てた後の上乗せに使う
HUB_API = "https://lrchub.coreone.work/api"
USER_AGENT = "VoxDesk (https://github.com/nisesimadao/VoxDesk)"
CACHE_SECONDS = 3600

# タイトルから取り除く飾り。これが入った括弧ごと落とす。
DECORATIONS = (
    # カラオケ音源そのものを指す語
    "ニコカラ", "ニコカラver", "nicokara", "カラオケ", "karaoke", "オフボーカル",
    "off vocal", "offvocal", "off-vocal", "instrumental", "インスト", "伴奏",
    "ガイドメロディ", "ガイドメロ", "ガイドなし", "ガイド無し", "メロディなし",
    # ボーカル入りの版も同じ飾り
    "オンボーカル", "on vocal", "onvocal", "on-vocal",
    # 表示に関する語（「歌詞付き」の「き」が残らないよう長い方から並べる）
    "歌詞付き", "歌詞付", "歌詞あり", "歌詞入り", "歌詞表示", "字幕", "音程バー",
    "ルビ", "ふりがな",
    # キーに関する語
    "原曲キー", "キー変更", "女性キー", "男性キー", "原曲", "移調", "キー",
    # 種類・素材に関する語
    "cover", "カバー", "mv", "pv", "full", "フル", "tvサイズ", "tv size", "short",
    "高音質", "練習用", "生音", "音源", "本家", "本家様", "音源本家様", "utaite",
    "歌ってみた用", "修正版", "リメイク", "公式", "official", "hd", "4k", "1080p",
    "ボカロ", "vocaloid", "アニメ", "主題歌", "op", "ed", "フリー", "無料",
)
# 「+5」「-3」「±0」「(-2)」のようなキー表記
KEY_MARK = r"[（(\[]?\s*[±+＋\-−]\s?\d{1,2}\s*(?:key|キー|半音)?\s*[）)\]]?"
BRACKETS = r"[【\[（(「『][^】\]）)」』]*[】\]）)」』]"

_cache: dict[str, tuple[float, object]] = {}


@dataclass
class Line:
    time: float
    text: str
    # 1 文字ずつの頭出し（LRCHub2 が持っている場合だけ入る）。
    # (その文字が始まる時刻, 文字) の並び
    marks: list[tuple[float, str]] = field(default_factory=list)

    def sung(self, position: float) -> int:
        """その行のうち、いま何文字目まで歌い終えたか。"""
        if not self.marks:
            return 0
        count = 0
        for time_at, text in self.marks:
            if time_at <= position:
                count += len(text)
            else:
                break
        return count


@dataclass
class Lyrics:
    track: str
    artist: str
    duration: float | None
    lines: list[Line] = field(default_factory=list)
    plain: str = ""
    source: str = "LRCLIB"

    @property
    def synced(self) -> bool:
        return bool(self.lines)

    @property
    def word_by_word(self) -> bool:
        """1 文字ずつの頭出しを持っているか。"""
        return any(line.marks for line in self.lines)

    def index_at(self, position: float) -> int:
        """いま歌っている行の番号。まだ始まっていなければ -1。"""
        index = -1
        for i, line in enumerate(self.lines):
            if line.time <= position:
                index = i
            else:
                break
        return index

    def at(self, position: float) -> tuple[str, str]:
        """いまの行と次の行を返す。"""
        if not self.lines:
            return "", ""
        index = self.index_at(position)
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
    # 残った飾り語も削る（長いものから消さないと部分一致で崩れる）
    for word in sorted(DECORATIONS, key=len, reverse=True):
        if word.isascii():
            # 英字は語の区切りを見る。そうしないと Official髭男dism のような
            # アーティスト名まで削ってしまう
            text = re.sub(rf"\b{re.escape(word)}\b", " ", text, flags=re.I)
        else:
            text = re.sub(re.escape(word), " ", text, flags=re.I)
    text = re.sub(KEY_MARK, " ", text)
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
        # ここは歌詞の置き場に届かなかっただけ。利用者のネットが切れて
        # いるとは限らない（相手側の不調や締め出しもある）ので、そう書く
        raise RuntimeError(f"歌詞の配信元につながりませんでした（{type(e).__name__}）") from e
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


def parse_dynamic(text: str) -> list[Line]:
    """1 文字ずつ頭出しの付いた歌詞を読む。

    LRCHub2 が返す形はこう:
        [00:00.00]<00:00.00>夢<00:00.38>な<00:00.60>ら<00:00.77>ば
    行の頭の時刻に続いて、文字ごとの始まりが入っている。
    """
    lines: list[Line] = []
    for row in (text or "").splitlines():
        head = re.match(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", row)
        if not head:
            continue
        start = int(head.group(1)) * 60 + float(head.group(2).replace(":", "."))
        marks: list[tuple[float, str]] = []
        for minute, second, piece in re.findall(
                r"<(\d+):(\d+(?:[.:]\d+)?)>([^<]*)", row[head.end():]):
            if piece:
                marks.append((int(minute) * 60 + float(second.replace(":", ".")), piece))
        body = "".join(piece for _, piece in marks)
        if not body:  # 文字ごとの印が無い行は、ただの行として扱う
            body = re.sub(r"\[[^\]]*\]|<[^>]*>", "", row).strip()
        if body:
            lines.append(Line(start, body, marks))
    lines.sort(key=lambda line: line.time)
    return lines


def _hub_request(params: dict) -> dict | None:
    """LRCHub2 に聞く。落ちていても歌の邪魔をしないよう、黙って None を返す。"""
    url = f"{HUB_API}/lyrics?" + urllib.parse.urlencode(params)
    cached = _cache.get(url)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        LOG.info("LRCHub2 に届きませんでした: %s", e)
        return None
    _cache[url] = (time.time(), data)
    return data


def upgrade_to_word_by_word(entry: Lyrics) -> Lyrics:
    """1 文字ずつの頭出しを持っていたら、そちらに差し替える。

    LRCHub2 は LRCLIB を元にしていて、そこに文字ごとの時刻を足したものを
    持っていることがある。カラオケでは、いま歌う文字が色で進む方が分かり
    やすいので、あれば使う。無ければ行単位のまま。
    """
    if not entry.track or not entry.artist or entry.word_by_word:
        return entry
    data = _hub_request({"track": entry.track, "artist": entry.artist})
    if not data or not data.get("ok"):
        return entry
    lines = parse_dynamic(data.get("dynamic_lyrics") or data.get("dynamic_lrc") or "")
    if not any(line.marks for line in lines):
        return entry
    offset = float(data.get("offset_ms") or 0) / 1000.0
    if offset:
        for line in lines:
            line.time += offset
            line.marks = [(at + offset, text) for at, text in line.marks]
    LOG.info("LRCHub2 の 1 文字ずつの歌詞に切り替えました（%d 行）", len(lines))
    return replace(entry, lines=lines, source="LRCHub2")


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
    return upgrade_to_word_by_word(top)

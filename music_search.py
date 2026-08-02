"""YouTube から曲を検索し、オフボーカル音源らしさで並べ替える。

yt-dlp をライブラリとして呼ぶだけで、外部プロセスは起動しない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from yt_dlp import YoutubeDL

# タイトルに含まれるとオフボーカルらしいキーワード（語, 加点）
POSITIVE = [
    ("オフボーカル", 6), ("off vocal", 6), ("offvocal", 6), ("off-vocal", 6),
    ("カラオケ", 5), ("karaoke", 5), ("インストゥルメンタル", 5), ("instrumental", 5),
    ("伴奏", 4), ("インスト", 4), ("backing track", 4), ("オケ", 2),
    ("ガイドメロなし", 3), ("ガイドメロディなし", 3), ("ガイドなし", 3),
    ("歌詞付", 2), ("歌詞あり", 2), ("字幕", 1), ("lyrics", 1), ("歌詞", 1),
]

# カラオケ音源を継続的に出している配信元。個人投稿より品質とキー表記が安定している。
TRUSTED_CHANNELS = [
    "カラオケ歌っちゃ王", "歌っちゃ王", "JOYSOUND", "ジョイサウンド", "DAM", "第一興商",
    "シンガーソングカラオケ", "カラオケ音源", "KARAOKE", "Karaoke Version",
    "ピアプロ", "カラオケStaR", "カラオケ で 歌おう", "オフボーカル",
]

# 含まれるとボーカル入りらしいキーワード（語, 減点）
NEGATIVE = [
    ("歌ってみた", 6), ("ボーカル入り", 6), ("vocal ver", 5), ("ボーカルver", 5),
    ("cover", 3), ("カバー", 3), ("music video", 3), ("mv", 2), ("pv", 2),
    ("ライブ", 3), ("live", 2), ("生歌", 4), ("弾いてみた", 3), ("reaction", 4),
    ("ガイドメロディ入り", 3), ("ガイドメロあり", 3),
]


@dataclass
class Track:
    """検索結果 1 件。"""

    id: str
    title: str
    uploader: str = ""
    duration: float | None = None
    score: int = 0
    trusted: bool = False

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return "--:--"
        m, s = divmod(int(self.duration), 60)
        return f"{m}:{s:02d}"


@dataclass
class Sources:
    """再生に必要な URL 一式。audio_url が None なら video_url に音声も含まれる。"""

    title: str
    video_url: str
    audio_url: str | None = None
    duration: float | None = None
    headers: dict = field(default_factory=dict)


def score_title(title: str, duration: float | None = None) -> int:
    """タイトルからオフボーカルらしさを採点する。"""
    text = title.lower()
    score = 0
    for word, weight in POSITIVE:
        if word.lower() in text:
            score += weight
    for word, weight in NEGATIVE:
        # 「オフボーカル」を「ボーカル入り」等と誤判定しないよう単語境界を見る
        if word in ("mv", "pv", "live"):
            if re.search(rf"(?<![a-z]){word}(?![a-z])", text):
                score -= weight
        elif word.lower() in text:
            score -= weight
    if duration is not None:
        if duration < 60:  # 短すぎるものは切り抜きの可能性が高い
            score -= 4
        elif duration > 900:
            score -= 2
    return score


def is_trusted(uploader: str) -> bool:
    """カラオケ音源の配信元として知られているチャンネルか。"""
    text = (uploader or "").lower()
    return any(name.lower() in text for name in TRUSTED_CHANNELS)


def search(query: str, limit: int = 15, prefer_off_vocal: bool = True,
           trusted_only: bool = False) -> list[Track]:
    """曲を検索して Track のリストを返す。オフボーカルらしい順に並ぶ。

    trusted_only を立てると、カラオケ音源の配信元だけに絞り込む。
    """
    query = original = query.strip()
    if not query:
        return []
    if prefer_off_vocal and not any(w in query for w, _ in POSITIVE[:8]):
        query = f"{query} カラオケ オフボーカル"

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",  # 各動画の詳細取得を省いて高速化
        "noplaylist": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    tracks = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("id"):
            continue
        title = entry.get("title") or "(タイトル不明)"
        duration = entry.get("duration")
        uploader = entry.get("uploader") or entry.get("channel") or ""
        trusted = is_trusted(uploader)
        if trusted_only and not trusted:
            continue
        tracks.append(
            Track(
                id=entry["id"],
                title=title,
                uploader=uploader,
                duration=duration,
                # 配信元が確かなものは少し上に来るようにする
                score=score_title(title, duration) + (4 if trusted else 0),
                trusted=trusted,
            )
        )
    if trusted_only:
        # 絞り込むと件数を稼ぐために検索語と無関係な曲まで残りやすい。
        # 検索語のどれかを含むものだけにする（全部消えたら元に戻す）。
        tokens = [w.lower() for w in re.split(r"[\s　]+", original) if len(w) >= 2]
        if tokens:
            relevant = [
                t for t in tracks
                if any(w in t.title.lower() for w in tokens)
            ]
            tracks = relevant or tracks

    if prefer_off_vocal:
        tracks.sort(key=lambda t: t.score, reverse=True)
    return tracks


def _pick_formats(formats: list[dict], max_height: int) -> tuple[dict, dict | None]:
    """(映像フォーマット, 音声フォーマット) を選ぶ。

    映像と音声が一体の progressive 形式があればそれを優先する（コンテナ 1 つで済む）。
    無ければ映像のみ・音声のみを個別に選び、再生側で 2 本を同期させる。
    """
    usable = [f for f in formats if f.get("url")]

    progressive = [
        f
        for f in usable
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and (f.get("height") or 0) <= max_height
    ]
    video_only = [
        f
        for f in usable
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") in (None, "none")
        and (f.get("height") or 0) <= max_height
    ]
    audio_only = [
        f for f in usable
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]

    def by_height(f):
        return (f.get("height") or 0, f.get("tbr") or 0)

    def by_bitrate(f):
        return f.get("abr") or f.get("tbr") or 0

    best_prog = max(progressive, key=by_height, default=None)
    best_video = max(video_only, key=by_height, default=None)
    best_audio = max(audio_only, key=by_bitrate, default=None)

    # 分離ストリームのほうが明らかに高画質なときはそちらを使う
    if best_video is not None and best_audio is not None:
        if best_prog is None or (best_video.get("height") or 0) > (best_prog.get("height") or 0):
            return best_video, best_audio
    if best_prog is not None:
        return best_prog, None
    if best_video is not None and best_audio is not None:
        return best_video, best_audio
    raise RuntimeError("再生できる形式が見つかりませんでした")


def resolve(video_id_or_url: str, max_height: int = 720) -> Sources:
    """動画の直リンクを解決する。ダウンロードはしない。"""
    url = (
        video_id_or_url
        if "://" in video_id_or_url
        else f"https://www.youtube.com/watch?v={video_id_or_url}"
    )
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    video_fmt, audio_fmt = _pick_formats(info.get("formats") or [], max_height)
    headers = dict(video_fmt.get("http_headers") or info.get("http_headers") or {})
    return Sources(
        title=info.get("title") or "",
        video_url=video_fmt["url"],
        audio_url=audio_fmt["url"] if audio_fmt else None,
        duration=info.get("duration"),
        headers=headers,
    )


def download(video_id_or_url: str, dest_dir: str, max_height: int = 720,
             progress_hook=None) -> str:
    """動画をローカルへ保存し、そのパスを返す。

    ネットワークが不安定な環境ではストリーミングより安定する。
    保存した動画の扱いは各サービスの利用規約と著作権法に従うこと。
    """
    url = (
        video_id_or_url
        if "://" in video_id_or_url
        else f"https://www.youtube.com/watch?v={video_id_or_url}"
    )
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # 画面なしで動くので進捗表示は邪魔になるだけ
        "noplaylist": True,
        "outtmpl": f"{dest_dir}/%(id)s.%(ext)s",
        # 結合に外部 ffmpeg を必要としない一体型を優先する
        "format": f"best[height<=?{max_height}][acodec!=none][vcodec!=none]/best",
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return ydl.prepare_filename(info)

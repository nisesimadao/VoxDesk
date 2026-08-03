"""曲の予約（次に歌う順番）。

本体の画面からもスマホのリモコンからも同じものを触る。以前は
リモコンの中に持っていたので、リモコンを入にしていないと予約が
存在しなかった。ここに出して、どちらからでも使えるようにする。

変更があったら知らせる（画面の一覧を並べ直すため）。知らせる先は
画面のスレッドとは限らないので、受け取る側で受け渡しをすること。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class Entry:
    """予約 1 件。"""

    title: str
    video_id: str = ""   # YouTube から取るとき
    path: str = ""       # 手元のファイルを予約したとき
    added_by: str = ""   # "本体" か "スマホ"（誰が入れたか分かると揉めない）

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}" if self.video_id else ""

    def as_dict(self) -> dict:
        return {"title": self.title, "id": self.video_id, "path": self.path,
                "added_by": self.added_by}


class PlayQueue:
    """次に歌う曲の並び。どのスレッドから触っても壊れない。"""

    def __init__(self, on_change=None):
        self._entries: list[Entry] = []
        self._lock = threading.Lock()
        self.on_change = on_change

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def list(self) -> list[Entry]:
        with self._lock:
            return list(self._entries)

    def add(self, entry: Entry) -> int:
        """末尾に足して、何番目に入ったかを返す。"""
        with self._lock:
            self._entries.append(entry)
            position = len(self._entries)
        self._changed()
        return position

    def insert_next(self, entry: Entry) -> None:
        """次に歌うところへ割り込ませる。"""
        with self._lock:
            self._entries.insert(0, entry)
        self._changed()

    def remove(self, index: int) -> Entry | None:
        with self._lock:
            if not 0 <= index < len(self._entries):
                return None
            entry = self._entries.pop(index)
        self._changed()
        return entry

    def move(self, index: int, delta: int) -> int:
        """順番を入れ替えて、移動後の位置を返す。"""
        with self._lock:
            if not 0 <= index < len(self._entries):
                return index
            target = max(0, min(len(self._entries) - 1, index + delta))
            if target != index:
                self._entries.insert(target, self._entries.pop(index))
            index = target
        self._changed()
        return index

    def pop(self) -> Entry | None:
        """先頭を取り出す。空なら None。"""
        with self._lock:
            entry = self._entries.pop(0) if self._entries else None
        if entry is not None:
            self._changed()
        return entry

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        if count:
            self._changed()
        return count

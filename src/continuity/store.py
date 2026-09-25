"""事件存储：按聚合流维护版本，按内容指纹去重，支持 JSONL 持久化与重启恢复。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .clock import Clock
from .errors import Conflict, NotFound
from .events import Event


@dataclass
class _Record:
    event: Event
    seq: int  # 接收序号，同一时刻事件的稳定先后依据


class EventStore:
    """内存事件存储，可选 JSONL 追加写盘。

    不变量：
    - 每个聚合的 ``version`` 从 1 连续递增；
    - 内容指纹相同的重复提交直接返回已存事件（重放判定见 ``events`` 模块）；
    - 已接收事件永不修改。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._records: list[_Record] = []
        self._versions: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        if self._path is not None and self._path.exists():
            self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = Event.from_dict(json.loads(line))
            self._accept(event, persist=False)

    def _append_line(self, event: Event) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")

    def _accept(self, event: Event, persist: bool) -> None:
        key = f"{event.aggregate_type}:{event.aggregate_id}"
        expected = self._versions.get(key, 0) + 1
        if event.version != expected:
            raise Conflict(
                f"聚合 {key} 版本冲突：期望 {expected}，收到 {event.version}"
            )
        self._versions[key] = event.version
        seq = len(self._records)
        self._records.append(_Record(event=event, seq=seq))
        self._fingerprints[event.fingerprint()] = event.event_id
        if persist:
            self._append_line(event)

    # ------------------------------------------------------------------ 写入

    def append(self, event: Event) -> Event:
        """接收事件；内容相同的重放返回既有事件而不是再次落库。"""
        existing = self._fingerprints.get(event.fingerprint())
        if existing is not None:
            return self.get_by_id(existing)
        self._accept(event, persist=True)
        return event

    # ------------------------------------------------------------------ 读取

    def get_by_id(self, event_id: str) -> Event:
        for record in self._records:
            if record.event.event_id == event_id:
                return record.event
        raise NotFound(f"事件不存在：{event_id}")

    def stream(self, aggregate_type: str, aggregate_id: str) -> list[Event]:
        return [
            record.event
            for record in self._records
            if record.event.aggregate_type == aggregate_type
            and record.event.aggregate_id == aggregate_id
        ]

    def stream_as_of(
        self, aggregate_type: str, aggregate_id: str, as_of
    ) -> list[Event]:
        as_of = Clock.coerce(as_of)
        return [
            event
            for event in self.stream(aggregate_type, aggregate_id)
            if Clock.coerce(event.occurred_at) <= as_of
        ]

    def all(self) -> list[Event]:
        return [record.event for record in self._records]

    def all_as_of(self, as_of) -> list[Event]:
        as_of = Clock.coerce(as_of)
        return [
            record.event
            for record in self._records
            if Clock.coerce(record.event.occurred_at) <= as_of
        ]

    def streams_by_type(self, aggregate_type: str) -> Iterable[tuple[str, list[Event]]]:
        grouped: dict[str, list[Event]] = {}
        for record in self._records:
            if record.event.aggregate_type == aggregate_type:
                grouped.setdefault(record.event.aggregate_id, []).append(record.event)
        return grouped.items()

    def version_of(self, aggregate_type: str, aggregate_id: str) -> int:
        return self._versions.get(f"{aggregate_type}:{aggregate_id}", 0)

    def has_fingerprint(self, fingerprint: str) -> Optional[str]:
        return self._fingerprints.get(fingerprint)

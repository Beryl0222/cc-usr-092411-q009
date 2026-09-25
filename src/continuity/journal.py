"""事件日志：追加写、按序重放、JSONL 持久化。

服务重启后从日志重建全部状态，使未决确认、候补顺序、补贴账本
都不因进程重启而丢失或被重新计算。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.continuity.events import DomainEvent


class EventJournal:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[DomainEvent] = []
        self._seq = 0
        if self._path and self._path.exists():
            self._load()

    @property
    def events(self) -> tuple[DomainEvent, ...]:
        return tuple(self._events)

    def append(self, event: DomainEvent) -> None:
        self._seq += 1
        self._events.append(event)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False))
                fh.write("\n")

    def replay(self, aggregate_id: str | None = None, as_of=None) -> tuple[DomainEvent, ...]:
        """按历史时点读取事件。

        ``as_of`` 为 None 时返回全部（严格按追加顺序）；
        否则只返回 occurred_at <= as_of 的事件，用于历史时点查询。
        """
        result = self._events
        if aggregate_id is not None:
            result = [e for e in result if e.aggregate_id == aggregate_id]
        if as_of is not None:
            result = [e for e in result if e.occurred_at <= as_of]
        return tuple(result)

    def seen_keys(self) -> dict[str, DomainEvent]:
        """已处理过的命令幂等键 -> 对应事件。"""
        return {e.event_key: e for e in self._events if e.event_key}

    def _load(self) -> None:
        assert self._path is not None
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                self._events.append(DomainEvent.from_dict(json.loads(line)))
        self._seq = len(self._events)

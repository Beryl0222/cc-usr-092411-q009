"""可控制的业务时钟。

所有截止点、课次是否已发生、授权是否有效，都只能经由本时钟判定，
绝不直接读取系统时间，以便测试在任意历史时点重放业务。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_CST = timezone(timedelta(hours=8))


def cst(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """构造东八区感知时间。"""
    return datetime(year, month, day, hour, minute, tzinfo=_CST)


class ControlledClock:
    """可冻结、可推进的业务时钟。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = cst(2026, 9, 1, 8, 0)
        self._now = start.astimezone(_CST)

    def now(self) -> datetime:
        return self._now

    def freeze(self, moment: datetime) -> None:
        self._now = moment.astimezone(_CST)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now

    def is_before(self, moment: datetime) -> bool:
        """当前业务时间是否严格早于某时点。"""
        return self._now < _aware(moment)

    def is_past(self, moment: datetime) -> bool:
        """当前业务时间是否已越过（含等于）截止点。"""
        return self._now >= _aware(moment)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_CST)
    return moment.astimezone(_CST)

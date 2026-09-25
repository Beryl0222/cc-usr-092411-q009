"""可控业务时钟。

候补提升与确认期限一律以业务时钟为准，测试可自由拨动或固定，
绝不允许领域规则直接读取系统墙钟。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional


class Clock:
    """单调的业务时钟。

    - 无固定时刻时，``real`` 委托给可替换的取时函数（默认 UTC 墙钟）；
    - :meth:`freeze` 固定时刻；:meth:`advance` 仅推进，禁止回拨；
    - 服务重启后可凭持久化的固定时刻重新接管。
    """

    def __init__(self, real: Optional[Callable[[], datetime]] = None) -> None:
        self._real = real or (lambda: datetime.now(timezone.utc))
        self._fixed: Optional[datetime] = None

    @staticmethod
    def coerce(value: datetime) -> datetime:
        """统一为带时区的 UTC ``datetime``。"""
        if value.tzinfo is None:
            raise ValueError("业务时刻必须带时区信息")
        return value.astimezone(timezone.utc)

    def now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return self.coerce(self._real())

    def freeze(self, value: datetime) -> datetime:
        value = self.coerce(value)
        if self._fixed is not None and value < self._fixed:
            raise ValueError(f"业务时钟不可回拨：{value.isoformat()} 早于 {self._fixed.isoformat()}")
        self._fixed = value
        return value

    def advance(self, **delta) -> datetime:
        from datetime import timedelta

        if not delta:
            raise ValueError("advance 至少需要一个时间增量参数")
        return self.freeze(self.now() + timedelta(**delta))

    def resume_real(self) -> None:
        """放弃固定时刻，回到委托取时（测试模拟服务重启时使用）。"""
        self._fixed = None

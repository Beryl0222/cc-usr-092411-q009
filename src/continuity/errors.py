"""领域错误。"""

from __future__ import annotations


class ContinuityError(Exception):
    """接续服务领域错误基类。"""


class NotFound(ContinuityError):
    """引用的聚合不存在，或在指定历史时点尚不存在。"""


class Conflict(ContinuityError):
    """业务规则冲突（名额、守恒、重复操作等）。"""


class CompatibilityError(Conflict):
    """目标课程无法承接该迁移（容量、课次区间或支持能力不足）。"""


class PendingReview(Conflict):
    """并行确认意见不一致，提案进入待核，不允许抢先覆盖。"""

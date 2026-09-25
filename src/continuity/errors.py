"""领域错误。业务更正必须通过后继事件表达，不允许原地改写。"""


class DomainError(Exception):
    """所有业务规则违例的基类。"""


class AuthorizationError(DomainError):
    """确认人既非学员本人，也不是当时有效的授权照护人。"""


class IncompatibleCourseError(DomainError):
    """目标课程无法承接：支持等级不足等硬性不兼容。"""


class ReplayConflictError(DomainError):
    """同一业务键收到内容不一致的重放请求（如同一课次不同签到结果）。"""


class ProposalClosedError(DomainError):
    """方案已结算或确认窗口已关闭。"""

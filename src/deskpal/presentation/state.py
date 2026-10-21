"""Fixed neutral labels and expressions for authoritative task/host statuses."""


_EXPRESSIONS = {
    "Planning": "thinking",
    "Running": "focused",
    "Completed": "pleased",
    "Failed": "alert",
}
_LABELS = {
    "Idle": "空闲",
    "Created": "待处理",
    "Planning": "规划中",
    "Running": "执行中",
    "Waiting": "等待确认",
    "Completed": "已完成",
    "Failed": "执行失败",
    "Cancelled": "已取消",
    "NeedsReview": "需要审查",
    "Uncertain": "结果待确认",
    "Unknown": "状态未知",
    "Closing": "正在关闭",
    "Closed": "已关闭",
}


def expression_for_status(status: str) -> str:
    """Choose a fixed expression; feedback timing belongs to the GUI owner."""
    return _EXPRESSIONS.get(status, "neutral")


def status_label(status: str) -> str:
    """Never echo unknown input as a label or infer emotion from task content."""
    return _LABELS.get(status, _LABELS["Unknown"])

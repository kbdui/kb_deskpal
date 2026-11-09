"""Identity and capabilities declared by a trusted capability provider."""

from dataclasses import dataclass
import re


def valid_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is not None


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    description: str
    event_sources: tuple[str, ...] = ()
    storage: bool = False
    page_title: str | None = None
    api_version: int = 1

    def __post_init__(self) -> None:
        if (not valid_id(self.id) or self.id in {"builtin", "host_registered"}
                or type(self.api_version) is not int or self.api_version != 1
                or not isinstance(self.event_sources, tuple) or len(self.event_sources) > 16
                or any(not valid_id(source) for source in self.event_sources)
                or len(set(self.event_sources)) != len(self.event_sources)):
            raise ValueError("Invalid plugin manifest")
        for value in (self.name, self.description, self.name if self.page_title is None else self.page_title):
            if not isinstance(value, str) or not value.strip() or len(value) > 240:
                raise ValueError("Invalid plugin display text")


GENERAL = PluginManifest("general", "基础工具", "读取当前时间和回显文本")
FILES = PluginManifest("workspace", "工作区文件", "在工作区内读取、查找和操作文件")
SCREEN = PluginManifest("screen", "屏幕截图", "经授权将截图保存到工作区")
AGENT = PluginManifest("agent", "助手管理", "管理当前档案中的任务、通知、主动关注、记忆和角色")
SCHEDULE = PluginManifest(
    "schedule", "日程与计时器", "管理本地日程和计时器，查看最近的到期记录",
    event_sources=("calendar_due", "timer_due"), storage=True, page_title="日程与计时器",
)

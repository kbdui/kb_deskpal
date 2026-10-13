"""Composition root: concrete adapters and resource ownership live here."""

from deskpal.agent.core import AgentCore
from deskpal.config import AppConfig, ConfigError, load_config
from deskpal.execution_log import JsonlExecutionLog
from deskpal.llm.ports import LLMProvider
from deskpal.llm.providers import OfflineLLMProvider, OpenAILLMProvider
from deskpal.memory.manager import SQLiteMemoryManager
from deskpal.planner.llm_planner import LLMPlanner
from deskpal.sandbox.workspace import WorkspaceSandbox
from deskpal.tasks.manager import InMemoryTaskManager
from deskpal.tools.manager import BuiltinToolManager


def create_agent(config: AppConfig | None = None) -> AgentCore:
    settings = load_config() if config is None else config
    if not isinstance(settings, AppConfig):
        raise ConfigError("config must be an AppConfig")
    llm: LLMProvider
    if settings.llm.provider == "offline":
        llm = OfflineLLMProvider()
    elif settings.llm.provider == "openai":
        llm = OpenAILLMProvider(settings.llm)
    else:
        raise ConfigError("llm.provider must be offline or openai")
    planner = LLMPlanner(llm, max_tool_steps=settings.agent.max_tool_steps)
    sandbox = WorkspaceSandbox(settings.sandbox.workspace_dir,
                               max_file_bytes=settings.sandbox.max_file_bytes)
    settings.storage.data_dir.mkdir(parents=True, exist_ok=True)
    sandbox.workspace_dir.mkdir(parents=True, exist_ok=True)
    execution_log = JsonlExecutionLog(settings.storage.data_dir / "execution.jsonl")
    memory = SQLiteMemoryManager(settings.storage.data_dir / "memory.sqlite3")
    try:
        tools = BuiltinToolManager(sandbox, execution_log=execution_log)
        tools.register_builtin()
        return AgentCore(llm=llm, planner=planner, tasks=InMemoryTaskManager(), memory=memory,
                         tools=tools, workspace_dir=sandbox.workspace_dir,
                         max_tool_steps=settings.agent.max_tool_steps, execution_log=execution_log,
                         on_close=memory.close)
    except BaseException:
        memory.close()
        raise

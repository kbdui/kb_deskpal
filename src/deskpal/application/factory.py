"""Composition root: concrete adapters and resource ownership live here."""

from contextlib import ExitStack

from deskpal.agent.core import AgentCore
from deskpal.config import AppConfig, ConfigError, load_config
from deskpal.execution_log import JsonlExecutionLog
from deskpal.llm.ports import LLMProvider
from deskpal.llm.providers import OfflineLLMProvider, OpenAILLMProvider
from deskpal.memory.contracts import EmbeddingProvider, MemoryEvaluator
from deskpal.memory.repository import SQLiteMemoryRepository
from deskpal.memory.coordinator import MemoryCoordinator
from deskpal.memory.retrieval import MemoryRetriever
from deskpal.memory.embedding import FastEmbedProvider
from deskpal.memory.evaluation import LLMMemoryEvaluator
from deskpal.planner.llm_planner import LLMPlanner
from deskpal.planner.ports import Planner
from deskpal.scheduling import SchedulingService
from deskpal.sandbox.workspace import WorkspaceSandbox
from deskpal.tasks.manager import InMemoryTaskManager
from deskpal.tools.manager import BuiltinToolManager
from deskpal.tools.ports import Tool
from deskpal.tools.calendar import calendar_tools
from deskpal.tools.timer import timer_tools
from deskpal.tools.documents import DocumentSearchTool, DocumentClassifyTool, DocumentCopyTool, DocumentMoveTool, DocumentDeleteTool
from deskpal.tools.screenshot import ScreenshotBackend, ScreenshotCaptureTool


def create_agent(config: AppConfig | None = None, *, planner: Planner | None = None,
                 screenshot_backend: ScreenshotBackend | None = None,
                 embedding: EmbeddingProvider | None = None,
                 memory_evaluator: MemoryEvaluator | None = None) -> AgentCore:
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
    selected_planner = planner if planner is not None else LLMPlanner(llm, max_tool_steps=settings.agent.max_tool_steps)
    sandbox = WorkspaceSandbox(settings.sandbox.workspace_dir,
                               max_file_bytes=settings.sandbox.max_file_bytes)
    settings.storage.data_dir.mkdir(parents=True, exist_ok=True)
    sandbox.workspace_dir.mkdir(parents=True, exist_ok=True)
    execution_log = JsonlExecutionLog(settings.storage.data_dir / "execution.jsonl")
    with ExitStack() as resources:
        repository = SQLiteMemoryRepository(settings.storage.data_dir / "memory.sqlite3")
        resources.callback(repository.close)
        startup_warnings: tuple[str, ...] = ()
        selected_embedding = embedding
        if selected_embedding is None and settings.memory.embedding_provider == "fastembed":
            try:
                selected_embedding = FastEmbedProvider(cache_dir=settings.memory.model_cache_dir)
                resources.callback(selected_embedding.close)
            except Exception:
                startup_warnings = ("Semantic model unavailable; using lexical retrieval (no automatic download)",)
        retriever = MemoryRetriever(repository, embedding=selected_embedding,
                                   cloud=settings.llm.provider != "offline",
                                   max_bytes=settings.memory.max_context_bytes,
                                   semantic_threshold=settings.memory.semantic_threshold,
                                   startup_warnings=startup_warnings)
        evaluator = memory_evaluator
        if evaluator is None and settings.memory.mode == "candidate_review":
            evaluation_llm: LLMProvider = llm
            if settings.llm.provider == "openai":
                evaluation_llm = OpenAILLMProvider(settings.llm,
                    timeout_seconds=settings.memory.evaluation_timeout_seconds,
                    max_output_tokens=settings.memory.evaluation_max_output_tokens)
            evaluator = LLMMemoryEvaluator(evaluation_llm, max_candidates=settings.memory.max_candidates)
        memory = MemoryCoordinator(repository, retriever=retriever, evaluator=evaluator,
                                   mode=settings.memory.mode, cloud=settings.llm.provider != "offline")
        scheduling = SchedulingService(settings.storage.data_dir / "scheduling.sqlite3",
                                       execution_log=execution_log,
                                       poll_interval=settings.scheduling.poll_interval_seconds)
        resources.callback(scheduling.close)
        tools = BuiltinToolManager(sandbox, execution_log=execution_log)
        tools.register_builtin()
        new_tools: tuple[Tool, ...] = (*calendar_tools(scheduling), *timer_tools(scheduling),
                     DocumentSearchTool(sandbox), DocumentClassifyTool(sandbox), DocumentCopyTool(sandbox),
                     DocumentMoveTool(sandbox), DocumentDeleteTool(sandbox),
                     ScreenshotCaptureTool(sandbox, backend=screenshot_backend,
                                           max_pixels=settings.screenshot.max_pixels))
        for tool in new_tools:
            tools.register(tool)
        scheduling.start()
        owned = resources
        agent = AgentCore(llm=llm, planner=selected_planner, tasks=InMemoryTaskManager(), memory=memory,
                         tools=tools, workspace_dir=sandbox.workspace_dir,
                         max_tool_steps=settings.agent.max_tool_steps, execution_log=execution_log,
                         on_close=lambda: owned.close(), memory_runtime=memory)
        # Transfer ownership only after every constructor/start has succeeded.
        owned = resources.pop_all()
        return agent

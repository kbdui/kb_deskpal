"""Single-file operations and bounded discovery for the trusted workspace."""

from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from deskpal.sandbox.workspace import WorkspaceSandbox

from .models import PermissionLevel, ToolContext, ToolResult, ToolSpec


DEFAULT_EXTENSION_CATEGORIES: Mapping[str, str] = MappingProxyType({
    extension: category
    for category, extensions in (
        ("text", (".txt", ".md", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".log", ".xml", ".html", ".css", ".py", ".js", ".ts")),
        ("image", (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".svg", ".ico", ".heic")),
        ("audio", (".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus")),
        ("video", (".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".mpeg", ".mpg", ".m4v")),
        ("document", (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".epub")),
        ("archive", (".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz")),
    )
    for extension in extensions
})


class DocumentSearchTool:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("document_search", "Discover workspace files by filename substring; never follow links.", {
            "type": "object", "properties": {
                "path": {"type": "string", "minLength": 1},
                "query": {"type": "string", "default": ""},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
            }, "additionalProperties": False,
        })

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(True, output=self._sandbox.search_files(
            arguments.get("path"), query=arguments.get("query", ""), limit=arguments.get("limit", 100)))


class DocumentClassifyTool(DocumentSearchTool):
    """Classify extensions only; labels do not assert the actual content type."""

    def __init__(self, sandbox: WorkspaceSandbox, *, extension_categories: Mapping[str, str] | None = None) -> None:
        super().__init__(sandbox)
        source = DEFAULT_EXTENSION_CATEGORIES if extension_categories is None else extension_categories
        normalized = {}
        for extension, category in source.items():
            if (not isinstance(extension, str) or not extension.startswith(".") or len(extension) < 2
                    or any(char in extension for char in "/\\:") or "." in extension[1:]
                    or not isinstance(category, str) or not category.strip()):
                raise ValueError("Extension categories require single suffix keys and non-empty string labels")
            key = extension.casefold()
            if key in normalized:
                raise ValueError("Extension category keys must be unique without regard to case")
            normalized[key] = category
        self._extension_categories = MappingProxyType(normalized)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("document_classify", "Classify workspace filenames by extension only; do not read or move content.",
                        super().spec.parameters, PermissionLevel.AUTOMATIC)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        discovery = super().execute(arguments, context).output
        files = [{**item, "category": self._extension_categories.get(
            PurePosixPath(item["path"]).suffix.casefold(), "other")} for item in discovery["files"]]
        return ToolResult(True, output={**discovery, "files": files})


class DocumentCopyTool:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("document_copy", "Copy one workspace file; explicit overwrite is required to replace.", {
            "type": "object", "properties": {
                "source": {"type": "string", "minLength": 1},
                "destination": {"type": "string", "minLength": 1},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["source", "destination"], "additionalProperties": False,
        }, PermissionLevel.CONFIRM)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(True, output=self._sandbox.copy_file(
            arguments["source"], arguments["destination"], overwrite=arguments.get("overwrite", False)))


class DocumentMoveTool:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("document_move", "Move one workspace file; source removal failure may leave both paths.", {
            "type": "object", "properties": {
                "source": {"type": "string", "minLength": 1},
                "destination": {"type": "string", "minLength": 1},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["source", "destination"], "additionalProperties": False,
        }, PermissionLevel.HIGH_RISK)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        output = self._sandbox.move_file(
            arguments["source"], arguments["destination"], overwrite=arguments.get("overwrite", False))
        if not output["completed"]:
            return ToolResult(False, output=output,
                              error="Destination created, but source removal failed; both files remain")
        return ToolResult(True, output=output)


class DocumentDeleteTool:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self._sandbox = sandbox

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("document_delete", "Delete one regular workspace file; directories are forbidden.", {
            "type": "object", "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"], "additionalProperties": False,
        }, PermissionLevel.HIGH_RISK)

    def execute(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(True, output=self._sandbox.delete_file(arguments["path"]))

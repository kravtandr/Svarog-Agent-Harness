"""Файловые tools: read_file, write_file, edit_file, list_dir, search_files (§6.5).

Все пути — относительные и разрешаются строго внутри workspace: попытка
выйти наружу (`..`, абсолютный путь, symlink) — ошибка tool, а не policy.
Это инвариант файловых операций, не зависящий от режима автономии.
"""

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from svarog_harness.tools.base import RiskLevel, Tool, ToolError, ToolResult, truncate_text

_MAX_OUTPUT_CHARS = 50_000
_MAX_SEARCH_MATCHES = 200
_SKIP_DIRS = {".git", ".svarog", "__pycache__", "node_modules", ".venv"}


def resolve_in_workspace(workspace: Path, raw: str) -> Path:
    """Разрешить путь из аргументов tool внутри workspace или упасть.

    resolve() раскрывает и `..`, и symlink'и — значит, ссылка, ведущая
    наружу workspace, тоже будет отвергнута.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ToolError(f"абсолютные пути запрещены, используйте относительный: {raw}")
    resolved = (workspace / candidate).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise ToolError(f"путь выходит за пределы workspace: {raw}")
    return resolved


class _WorkspaceTool[ArgsT: BaseModel](Tool[ArgsT]):
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def _resolve(self, raw: str) -> Path:
        return resolve_in_workspace(self.workspace, raw)


class ReadFileArgs(BaseModel):
    path: str = Field(description="Путь к файлу относительно workspace")


class ReadFileTool(_WorkspaceTool[ReadFileArgs]):
    name = "read_file"
    description = "Прочитать текстовый файл внутри workspace"
    risk_level = RiskLevel.LOW
    args_model = ReadFileArgs

    async def execute(self, args: ReadFileArgs) -> ToolResult:
        path = self._resolve(args.path)
        if not path.is_file():
            raise ToolError(f"файл не найден: {args.path}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"файл не является текстовым (utf-8): {args.path}") from None
        return ToolResult.success(truncate_text(content, _MAX_OUTPUT_CHARS))


class WriteFileArgs(BaseModel):
    path: str = Field(description="Путь к файлу относительно workspace")
    content: str = Field(description="Полное новое содержимое файла")


class WriteFileTool(_WorkspaceTool[WriteFileArgs]):
    name = "write_file"
    description = "Создать или перезаписать файл внутри workspace"
    risk_level = RiskLevel.MEDIUM
    args_model = WriteFileArgs

    async def execute(self, args: WriteFileArgs) -> ToolResult:
        path = self._resolve(args.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.content, encoding="utf-8")
        return ToolResult.success(f"записано {len(args.content)} символов в {args.path}")


class EditFileArgs(BaseModel):
    path: str = Field(description="Путь к файлу относительно workspace")
    old_string: str = Field(description="Точный текст, который нужно заменить")
    new_string: str = Field(description="Текст замены")
    replace_all: bool = Field(default=False, description="Заменить все вхождения, а не одно")


class EditFileTool(_WorkspaceTool[EditFileArgs]):
    name = "edit_file"
    description = (
        "Заменить точное вхождение old_string на new_string в файле. "
        "old_string должен встречаться ровно один раз, либо укажите replace_all"
    )
    risk_level = RiskLevel.MEDIUM
    args_model = EditFileArgs

    async def execute(self, args: EditFileArgs) -> ToolResult:
        path = self._resolve(args.path)
        if not path.is_file():
            raise ToolError(f"файл не найден: {args.path}")
        content = path.read_text(encoding="utf-8")
        count = content.count(args.old_string)
        if count == 0:
            raise ToolError(f"old_string не найден в {args.path}")
        if count > 1 and not args.replace_all:
            raise ToolError(
                f"old_string встречается {count} раз в {args.path}; "
                f"уточните контекст или передайте replace_all=true"
            )
        replaced = count if args.replace_all else 1
        content = content.replace(args.old_string, args.new_string, replaced)
        path.write_text(content, encoding="utf-8")
        return ToolResult.success(f"заменено вхождений: {replaced} в {args.path}")


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="Директория относительно workspace")


class ListDirTool(_WorkspaceTool[ListDirArgs]):
    name = "list_dir"
    description = "Показать содержимое директории (директории помечены '/')"
    risk_level = RiskLevel.LOW
    args_model = ListDirArgs

    async def execute(self, args: ListDirArgs) -> ToolResult:
        path = self._resolve(args.path)
        if not path.is_dir():
            raise ToolError(f"директория не найдена: {args.path}")
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        lines = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries]
        return ToolResult.success("\n".join(lines) or "(пусто)")


class SearchFilesArgs(BaseModel):
    pattern: str = Field(description="Регулярное выражение для поиска по содержимому")
    path: str = Field(default=".", description="Директория поиска относительно workspace")
    glob: str = Field(default="**/*", description="Glob-фильтр имен файлов, например '**/*.py'")


class SearchFilesTool(_WorkspaceTool[SearchFilesArgs]):
    name = "search_files"
    description = "Поиск по содержимому файлов регулярным выражением; вывод path:line: текст"
    risk_level = RiskLevel.LOW
    args_model = SearchFilesArgs

    async def execute(self, args: SearchFilesArgs) -> ToolResult:
        root = self._resolve(args.path)
        if not root.is_dir():
            raise ToolError(f"директория не найдена: {args.path}")
        try:
            regex = re.compile(args.pattern)
        except re.error as exc:
            raise ToolError(f"невалидное регулярное выражение: {exc}") from None

        matches: list[str] = []
        for file in sorted(root.glob(args.glob)):
            if not file.is_file() or _SKIP_DIRS.intersection(file.parts):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = file.relative_to(self.workspace)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{rel}:{lineno}: {line.strip()}")
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        matches.append(f"… [достигнут лимит {_MAX_SEARCH_MATCHES} совпадений]")
                        return ToolResult.success("\n".join(matches))
        return ToolResult.success("\n".join(matches) or "совпадений не найдено")


def file_tools(workspace: Path) -> list[Tool[Any]]:
    """Все файловые tools для регистрации в ToolRegistry."""
    return [
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        EditFileTool(workspace),
        ListDirTool(workspace),
        SearchFilesTool(workspace),
    ]

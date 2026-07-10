"""Файловые tools: read_file, write_file, edit_file, list_dir, search_files (§6.5).

Все пути — относительные и разрешаются строго внутри workspace: попытка
выйти наружу (`..`, абсолютный путь, symlink) — ошибка tool, а не policy.
Это инвариант файловых операций, не зависящий от режима автономии.
"""

import asyncio
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from svarog_harness.paths import PathTraversalError, safe_join
from svarog_harness.tools.base import RiskLevel, Tool, ToolError, ToolResult

_MAX_OUTPUT_CHARS = 50_000
_MAX_SEARCH_MATCHES = 200
_SKIP_DIRS = {".git", ".svarog", "__pycache__", "node_modules", ".venv"}
# Управляющее дерево, недоступное на запись из-под агента (ADR-0015 §0.2):
# `.git` = host-git hooks/config (escape из sandbox), `.svarog` = trace/spill.
# Чтение не запрещаем — spill-файлы 1.2 лежат в `.svarog` и читаются read_file.
_WRITE_DENY_PREFIXES = (".git", ".svarog")


def resolve_in_workspace(workspace: Path, raw: str, *, for_write: bool = False) -> Path:
    """Разрешить путь из аргументов tool внутри workspace или упасть.

    safe_join раскрывает `..` и symlink'и — ссылка наружу workspace отвергается.
    Для записи (`for_write`) дополнительно запрещены управляющие префиксы
    (`.git`, `.svarog`): инвариант симметричен пропуску их из поиска.
    """
    try:
        resolved = safe_join(workspace, raw)
    except PathTraversalError as exc:
        raise ToolError(str(exc)) from None
    if for_write:
        rel_parts = resolved.relative_to(workspace.resolve()).parts
        if rel_parts and rel_parts[0] in _WRITE_DENY_PREFIXES:
            raise ToolError(
                f"запись в управляющий каталог запрещена: {raw} "
                f"(префикс '{rel_parts[0]}' зарезервирован runtime)"
            )
    return resolved


class _WorkspaceTool[ArgsT: BaseModel](Tool[ArgsT]):
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def _resolve(self, raw: str, *, for_write: bool = False) -> Path:
        return resolve_in_workspace(self.workspace, raw, for_write=for_write)


class ReadFileArgs(BaseModel):
    path: str = Field(description="Путь к файлу относительно workspace")
    offset: int = Field(default=1, ge=1, description="Начальная строка (1-based)")
    limit: int | None = Field(default=None, ge=1, description="Сколько строк вернуть")


class ReadFileTool(_WorkspaceTool[ReadFileArgs]):
    name = "read_file"
    action_type = "file.read"
    description = (
        "Прочитать текстовый файл внутри workspace; большие файлы "
        "дочитываются частями через offset/limit"
    )
    risk_level = RiskLevel.LOW
    args_model = ReadFileArgs

    def is_read_only(self, args: ReadFileArgs) -> bool:
        return True

    async def execute(self, args: ReadFileArgs) -> ToolResult:
        path = self._resolve(args.path)
        if not path.is_file():
            raise ToolError(f"файл не найден: {args.path}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"файл не является текстовым (utf-8): {args.path}") from None

        lines = content.splitlines()
        total = len(lines)
        start = args.offset - 1
        if not lines:
            if start:
                raise ToolError(f"offset {args.offset} за концом файла: {args.path} пуст")
            return ToolResult.success(content)
        if start >= total:
            raise ToolError(f"offset {args.offset} за концом файла ({total} строк): {args.path}")

        # Окно по строкам (offset/limit) + потолок по символам: обрезка честная —
        # по границе строки, с маркером «что показано и как дочитать» (ADR-0015 §1.2).
        end = min(total, start + args.limit) if args.limit is not None else total
        shown: list[str] = []
        size = 0
        for line in lines[start:end]:
            size += len(line) + 1
            if shown and size > _MAX_OUTPUT_CHARS:
                break
            shown.append(line)
        shown_end = start + len(shown)
        body = "\n".join(shown)
        if start == 0 and shown_end == total:
            return ToolResult.success(content)
        marker = f"[показаны строки {start + 1}–{shown_end} из {total}"
        if shown_end < total:
            marker += f"; продолжение: offset={shown_end + 1}"
        return ToolResult.success(f"{body}\n{marker}]")


class WriteFileArgs(BaseModel):
    path: str = Field(description="Путь к файлу относительно workspace")
    content: str = Field(description="Полное новое содержимое файла")


class WriteFileTool(_WorkspaceTool[WriteFileArgs]):
    name = "write_file"
    action_type = "file.write"
    description = "Создать или перезаписать файл внутри workspace"
    risk_level = RiskLevel.MEDIUM
    args_model = WriteFileArgs

    async def execute(self, args: WriteFileArgs) -> ToolResult:
        path = self._resolve(args.path, for_write=True)
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
    action_type = "file.edit"
    description = (
        "Заменить точное вхождение old_string на new_string в файле. "
        "old_string должен встречаться ровно один раз, либо укажите replace_all"
    )
    risk_level = RiskLevel.MEDIUM
    args_model = EditFileArgs

    async def execute(self, args: EditFileArgs) -> ToolResult:
        path = self._resolve(args.path, for_write=True)
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
    action_type = "file.list"
    description = "Показать содержимое директории (директории помечены '/')"
    risk_level = RiskLevel.LOW
    args_model = ListDirArgs

    def is_read_only(self, args: ListDirArgs) -> bool:
        return True

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
    max_results: int = Field(
        default=_MAX_SEARCH_MATCHES,
        ge=1,
        le=5000,
        description="Максимум совпадений в ответе (пагинация: уточни pattern/glob или подними)",
    )


class SearchFilesTool(_WorkspaceTool[SearchFilesArgs]):
    """Поиск по содержимому: ripgrep-backend с Python-fallback (ADR-0015 фаза 4).

    rg быстрее и корректнее с игнором (уважает .gitignore внутри git-репо);
    без rg в системе или на непереваренном им паттерне (backreferences и пр.
    вне rust-regex) работает прежний Python-обход. Контракт вывода одинаковый:
    `path:line: текст` + честный маркер усечения.
    """

    name = "search_files"
    action_type = "file.search"
    description = "Поиск по содержимому файлов регулярным выражением; вывод path:line: текст"
    risk_level = RiskLevel.LOW
    args_model = SearchFilesArgs

    def is_read_only(self, args: SearchFilesArgs) -> bool:
        return True

    async def execute(self, args: SearchFilesArgs) -> ToolResult:
        root = self._resolve(args.path)
        if not root.is_dir():
            raise ToolError(f"директория не найдена: {args.path}")
        # Python-валидация паттерна всегда: и как ранняя ошибка модели, и как
        # гарантия работоспособности fallback-ветки.
        try:
            regex = re.compile(args.pattern)
        except re.error as exc:
            raise ToolError(f"невалидное регулярное выражение: {exc}") from None

        found = await self._ripgrep_search(root, args)
        if found is not None:
            matches, total = found
            if not matches:
                return ToolResult.success("совпадений не найдено")
            if total > len(matches):
                matches.append(
                    f"… [показано {len(matches)} из {total} совпадений; "
                    f"уточни pattern/glob или подними max_results]"
                )
            return ToolResult.success("\n".join(matches))
        return self._python_search(root, args, regex)

    async def _ripgrep_search(
        self, root: Path, args: SearchFilesArgs
    ) -> tuple[list[str], int] | None:
        """Поиск через rg; None — rg недоступен или не понял паттерн (fallback).

        `--hidden` выравнивает охват с Python-обходом (он не пропускает
        dot-файлы), служебные каталоги закрыты glob'ами, `--sort path` даёт
        детерминированный порядок, как у сортированного обхода.
        """
        rg = shutil.which("rg")
        if rg is None:
            return None
        cmd = [
            rg,
            "--line-number",
            "--no-heading",
            "--color=never",
            "--hidden",
            "--sort",
            "path",
            "--regexp",
            args.pattern,
        ]
        # Позитивный --glob у rg — whitelist поверх ignore-правил: дефолтный
        # '**/*' не передаём (иначе .gitignore перестал бы действовать), а
        # явный glob пользователя — осознанное «ищи именно эти файлы».
        if args.glob != "**/*":
            cmd += ["--glob", args.glob]
        for skip in sorted(_SKIP_DIRS):
            cmd += ["--glob", f"!{skip}/**"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode not in (0, 1):  # 0 — есть совпадения, 1 — нет
            return None
        prefix = root.resolve().relative_to(self.workspace.resolve())
        matches: list[str] = []
        total = 0
        for raw in stdout.decode(errors="replace").splitlines():
            file_part, _, rest = raw.partition(":")
            lineno, _, text = rest.partition(":")
            if not lineno.isdigit():
                continue
            total += 1
            if len(matches) >= args.max_results:
                continue
            rel = file_part if str(prefix) == "." else f"{prefix}/{file_part}"
            matches.append(f"{rel}:{lineno}: {text.strip()}")
        return matches, total

    def _python_search(
        self, root: Path, args: SearchFilesArgs, regex: re.Pattern[str]
    ) -> ToolResult:
        matches: list[str] = []
        total = 0
        for file in sorted(root.glob(args.glob)):
            if not file.is_file() or _SKIP_DIRS.intersection(file.parts):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = file.relative_to(self.workspace)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not regex.search(line):
                    continue
                # После потолка совпадения только считаются — маркер честный.
                total += 1
                if len(matches) < args.max_results:
                    matches.append(f"{rel}:{lineno}: {line.strip()}")
        if not matches:
            return ToolResult.success("совпадений не найдено")
        if total > len(matches):
            matches.append(
                f"… [показано {len(matches)} из {total} совпадений; "
                f"уточни pattern/glob или подними max_results]"
            )
        return ToolResult.success("\n".join(matches))


def file_tools(workspace: Path) -> list[Tool[Any]]:
    """Все файловые tools для регистрации в ToolRegistry."""
    return [
        ReadFileTool(workspace),
        WriteFileTool(workspace),
        EditFileTool(workspace),
        ListDirTool(workspace),
        SearchFilesTool(workspace),
    ]

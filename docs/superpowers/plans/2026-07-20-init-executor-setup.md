# Расширение `svarog init`: настройка Claude Code и OpenCode как исполнителя — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `svarog init` может настроить Claude Code (api-key или subscription
через `claude setup-token`) и/или OpenCode (модель/base_url/api-key, либо
переиспользование кредов нативного provider'а) как `executor.external`, не
трогая поведение для тех, кто внешнего исполнителя не просит.

**Architecture:** Три слоя, каждый — отдельный файл:
1. `scaffold.py` — чистый рендеринг `svarog.yaml` из уже готового
   `ExecutorSetup` (dataclasses), без какой-либо валидации выбора.
2. Новый `cli/init_executor.py` — чистая (без I/O) валидация и сборка
   `ExecutorSetup` из уже собранных ответов (флаги ИЛИ интерактив — не важно
   откуда), с понятными ошибками на конфликты.
3. `cli/main.py` (`init()`) — сбор ответов (флаги/интерактивные вопросы),
   вызов слоя 2, передача результата в слой 1, сохранение секретов.

**Tech Stack:** Python 3.12, Typer (CLI), Rich (вывод), pydantic (не
используется в новых модулях — просто dataclasses), pytest + `CliRunner`.

## Global Constraints

- Обратная совместимость: без новых флагов `svarog.yaml` не меняется ни
  байтом (существующие тесты `tests/test_init.py` должны продолжать
  проходить без изменений).
- `mypy --strict` уже включён проектом (`pyproject.toml: [tool.mypy] strict
  = true`) — все новые сигнатуры типизированы полностью, никаких `Any`.
- Значение секрета никогда не пишется в `svarog.yaml` — только имя ref
  (`*_ref`), сам секрет — в `FileSecretStore` (`~/.svarog/secrets.json` по
  умолчанию), как и для нативного `models.api_key` уже сегодня.
- Имена ref по умолчанию (точные строки): `CLAUDE_CODE_KEY` (Claude
  api-key), `CLAUDE_CODE_OAUTH_TOKEN` (Claude subscription),
  `OPENCODE_API_KEY` (OpenCode собственный ключ).
- Образы по умолчанию (точные строки): `svarog/agent-claude:latest`,
  `svarog/agent-opencode:latest` (см. `docker/agent-claude/README.md:13`,
  `docker/agent-opencode/README.md:18`).
- Схема (`config/schema.py: ExternalExecutorConfig`) не меняется — один
  активный адаптер (`native | claude-code | opencode | codex`); codex в
  интерактив/флаги `init` не добавляется (не запрошено).
- Спека: `docs/superpowers/specs/2026-07-20-init-executor-setup-design.md`.

---

## Task 1: Рендеринг `executor.external` в `scaffold.py`

**Files:**
- Modify: `src/svarog_harness/scaffold.py`
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: ничего нового из других задач.
- Produces (используется в Task 2 и Task 3):
  - `ClaudeExecutorSetup(auth: Literal["api-key", "subscription"], api_key_ref: str | None, oauth_token_ref: str | None)`
  - `OpencodeExecutorSetup(model: str, base_url: str, api_key_ref: str | None)`
  - `ExecutorSetup(active: Literal["claude-code", "opencode"], claude: ClaudeExecutorSetup | None = None, opencode: OpencodeExecutorSetup | None = None)`
  - `scaffold_agent_home(..., executor: ExecutorSetup | None = None)` — новый keyword-параметр, дефолт `None` (поведение не меняется).
  - Константы: `DEFAULT_CLAUDE_IMAGE`, `DEFAULT_OPENCODE_IMAGE`,
    `DEFAULT_CLAUDE_API_KEY_REF`, `DEFAULT_CLAUDE_OAUTH_TOKEN_REF`,
    `DEFAULT_OPENCODE_API_KEY_REF`.

- [ ] **Step 1: Дописать падающие тесты в `tests/test_init.py`**

Добавить в конец файла (после существующих тестов), импорт новых имён в
блок импортов сверху файла:

```python
from svarog_harness.scaffold import (
    ClaudeExecutorSetup,
    ExecutorSetup,
    OpencodeExecutorSetup,
    scaffold_agent_home,
)
```

(Заменить существующую строку `from svarog_harness.scaffold import
scaffold_agent_home` на этот расширенный импорт.)

Тесты:

```python
def test_scaffold_no_executor_omits_section(tmp_path: Path) -> None:
    scaffold_agent_home(tmp_path)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "executor:" not in yaml


def test_scaffold_claude_api_key_executor_block(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="api-key", api_key_ref="CLAUDE_CODE_KEY", oauth_token_ref=None
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "executor:\n  type: external\n  external:\n    adapter: claude-code" in yaml
    assert "    image: svarog/agent-claude:latest" in yaml
    assert "    auth: api-key" in yaml
    assert "    api_key_ref: CLAUDE_CODE_KEY" in yaml
    assert "тоже настроен" not in yaml


def test_scaffold_claude_api_key_without_value_comments_ref(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(auth="api-key", api_key_ref=None, oauth_token_ref=None),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    # api_key_ref: CLAUDE_CODE_KEY" in yaml
    assert "\n    api_key_ref:" not in yaml


def test_scaffold_claude_subscription_ref_always_active(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="subscription", api_key_ref=None, oauth_token_ref="CLAUDE_CODE_OAUTH_TOKEN"
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    auth: subscription" in yaml
    assert "\n    oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN" in yaml


def test_scaffold_opencode_own_creds(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="opencode",
        opencode=OpencodeExecutorSetup(
            model="qwen3-coder",
            base_url="https://openrouter.ai/api/v1",
            api_key_ref="OPENCODE_API_KEY",
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    adapter: opencode" in yaml
    assert "    image: svarog/agent-opencode:latest" in yaml
    assert "    model: qwen3-coder" in yaml
    assert "    base_url: https://openrouter.ai/api/v1" in yaml
    assert "    api_key_ref: OPENCODE_API_KEY" in yaml


def test_scaffold_opencode_without_ref_comments_line(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="opencode",
        opencode=OpencodeExecutorSetup(
            model="qwen3-coder", base_url="http://localhost:8000/v1", api_key_ref=None
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "    # api_key_ref: OPENCODE_API_KEY" in yaml


def test_scaffold_standby_adapter_rendered_as_comment(tmp_path: Path) -> None:
    executor = ExecutorSetup(
        active="claude-code",
        claude=ClaudeExecutorSetup(
            auth="api-key", api_key_ref="CLAUDE_CODE_KEY", oauth_token_ref=None
        ),
        opencode=OpencodeExecutorSetup(
            model="qwen3-coder", base_url="http://localhost:8000/v1", api_key_ref="PROVIDER_API_KEY"
        ),
    )
    scaffold_agent_home(tmp_path, executor=executor)
    yaml = (tmp_path / "svarog.yaml").read_text(encoding="utf-8")
    assert "OpenCode тоже настроен и готов" in yaml
    assert "# executor:" in yaml
    assert "#   external:" in yaml
    assert "#     adapter: opencode" in yaml
    assert "#     api_key_ref: PROVIDER_API_KEY" in yaml
    # активный блок остаётся некомментированным
    assert "\n    adapter: claude-code" in yaml
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_init.py -k scaffold_claude or scaffold_opencode or scaffold_no_executor or scaffold_standby -v`
Expected: FAIL — `ImportError: cannot import name 'ClaudeExecutorSetup'`
(новые имена ещё не существуют в `scaffold.py`).

- [ ] **Step 3: Реализовать в `scaffold.py`**

Добавить импорт `Literal` (после `from dataclasses import dataclass`):

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from svarog_harness.secrets import gitignore_block
```

Добавить константы сразу после существующих `DEFAULT_MODEL` /
`DEFAULT_BASE_URL` / `DEFAULT_API_KEY_REF` (`scaffold.py:33-35`):

```python
DEFAULT_MODEL = "qwen3-coder"
DEFAULT_BASE_URL = "http://localhost:8000/v1"
DEFAULT_API_KEY_REF = "PROVIDER_API_KEY"
DEFAULT_CLAUDE_IMAGE = "svarog/agent-claude:latest"
DEFAULT_OPENCODE_IMAGE = "svarog/agent-opencode:latest"
DEFAULT_CLAUDE_API_KEY_REF = "CLAUDE_CODE_KEY"
DEFAULT_CLAUDE_OAUTH_TOKEN_REF = "CLAUDE_CODE_OAUTH_TOKEN"
DEFAULT_OPENCODE_API_KEY_REF = "OPENCODE_API_KEY"
```

Добавить dataclass'ы и функции рендеринга **после** `ScaffoldResult` (после
строки `result: list[Path]` блока, т.е. сразу под существующим
`@dataclass(frozen=True) class ScaffoldResult: ...`, перед `_write`):

```python
@dataclass(frozen=True)
class ClaudeExecutorSetup:
    auth: Literal["api-key", "subscription"]
    api_key_ref: str | None = None
    oauth_token_ref: str | None = None


@dataclass(frozen=True)
class OpencodeExecutorSetup:
    model: str
    base_url: str
    api_key_ref: str | None = None


@dataclass(frozen=True)
class ExecutorSetup:
    active: Literal["claude-code", "opencode"]
    claude: ClaudeExecutorSetup | None = None
    opencode: OpencodeExecutorSetup | None = None


_ADAPTER_LABELS: dict[str, str] = {"claude-code": "Claude Code", "opencode": "OpenCode"}


def _claude_block_lines(setup: ClaudeExecutorSetup) -> list[str]:
    lines = [
        "    adapter: claude-code",
        f"    image: {DEFAULT_CLAUDE_IMAGE}",
        f"    auth: {setup.auth}",
    ]
    if setup.auth == "subscription":
        # Схема требует непустой oauth_token_ref для subscription — строка
        # активна всегда, даже если значение токена ещё не сохранено.
        lines.append(f"    oauth_token_ref: {setup.oauth_token_ref}")
    elif setup.api_key_ref:
        lines.append(f"    api_key_ref: {setup.api_key_ref}")
    else:
        lines.append(
            f"    # api_key_ref: {DEFAULT_CLAUDE_API_KEY_REF}   # имя секрета в SecretStore"
        )
    return lines


def _opencode_block_lines(setup: OpencodeExecutorSetup) -> list[str]:
    lines = [
        "    adapter: opencode",
        f"    image: {DEFAULT_OPENCODE_IMAGE}",
        f"    model: {setup.model}",
        f"    base_url: {setup.base_url}",
    ]
    if setup.api_key_ref:
        lines.append(f"    api_key_ref: {setup.api_key_ref}")
    else:
        lines.append(
            f"    # api_key_ref: {DEFAULT_OPENCODE_API_KEY_REF}   # имя секрета в SecretStore"
        )
    return lines


def _adapter_block_lines(
    adapter: Literal["claude-code", "opencode"], executor: ExecutorSetup
) -> list[str]:
    if adapter == "claude-code":
        assert executor.claude is not None
        return _claude_block_lines(executor.claude)
    assert executor.opencode is not None
    return _opencode_block_lines(executor.opencode)


def _full_block_lines(
    adapter: Literal["claude-code", "opencode"], executor: ExecutorSetup
) -> list[str]:
    return [
        "executor:",
        "  type: external",
        "  external:",
        *_adapter_block_lines(adapter, executor),
    ]


def _render_executor_yaml(executor: ExecutorSetup | None) -> str:
    """Секция executor.external активного адаптера + закомментированный блок
    второго, если он тоже настроен (переключение — правкой пары строк)."""
    if executor is None:
        return ""
    active_text = "\n".join(_full_block_lines(executor.active, executor))
    standby: Literal["claude-code", "opencode"] = (
        "opencode" if executor.active == "claude-code" else "claude-code"
    )
    standby_setup: ClaudeExecutorSetup | OpencodeExecutorSetup | None = (
        executor.opencode if standby == "opencode" else executor.claude
    )
    if standby_setup is None:
        return f"\n{active_text}\n"
    label = _ADAPTER_LABELS[standby]
    commented = "\n".join(f"# {line}" for line in _full_block_lines(standby, executor))
    return (
        f"\n{active_text}\n"
        f"\n# {label} тоже настроен и готов — чтобы переключиться, замените блок "
        f"executor выше на:\n{commented}\n"
    )
```

Изменить сигнатуру и тело `scaffold_agent_home` (`scaffold.py:132-146`):

```python
def scaffold_agent_home(
    target: Path,
    *,
    force: bool = False,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_ref: str | None = None,
    executor: ExecutorSetup | None = None,
) -> ScaffoldResult:
    """Создать структуру agent-home; существующие файлы не трогаются без force.

    model/base_url/api_key_ref подставляются в секцию models сгенерированного
    svarog.yaml. Значение ключа в конфиг не пишется — только имя (api_key_ref).
    executor — опциональная секция executor.external (Claude Code/OpenCode).
    """
    result = ScaffoldResult(created=[], skipped=[])
    config_yaml = _render_config_yaml(model, base_url, api_key_ref) + _render_executor_yaml(
        executor
    )
    _write(target / "AGENTS.md", _AGENTS_MD, result, force=force)
```

(Остальные строки функции — без изменений, только добавленная строка
`_render_executor_yaml(executor)` в конкатенации `config_yaml`.)

- [ ] **Step 4: Запустить тесты, убедиться, что проходят**

Run: `uv run pytest tests/test_init.py -v`
Expected: PASS — все тесты в файле, включая старые и 7 новых.

- [ ] **Step 5: Проверить типы и линт**

Run: `uv run mypy src/svarog_harness/scaffold.py`
Expected: `Success: no issues found`

Run: `uv run ruff check src/svarog_harness/scaffold.py tests/test_init.py`
Expected: без ошибок (авто-фиксы — `uv run ruff check --fix ...`, если нужно).

- [ ] **Step 6: Commit**

```bash
git add src/svarog_harness/scaffold.py tests/test_init.py
git commit -m "feat(scaffold): render executor.external for Claude Code/OpenCode setup"
```

---

## Task 2: Чистый резолвер `cli/init_executor.py`

**Files:**
- Create: `src/svarog_harness/cli/init_executor.py`
- Test: `tests/test_init_executor.py` (новый файл)

**Interfaces:**
- Consumes: `ClaudeExecutorSetup`, `OpencodeExecutorSetup`, `ExecutorSetup`,
  `DEFAULT_CLAUDE_API_KEY_REF`, `DEFAULT_CLAUDE_OAUTH_TOKEN_REF`,
  `DEFAULT_OPENCODE_API_KEY_REF` из `svarog_harness.scaffold` (Task 1).
- Produces (используется в Task 3):
  - `ClaudeAnswers(requested: bool, auth: str = "api-key", api_key: str | None = None, oauth_token: str | None = None)`
  - `OpencodeAnswers(requested: bool, reuse_native: bool = True, model: str | None = None, base_url: str | None = None, api_key: str | None = None)`
  - `ExecutorSetupError(ValueError)`
  - `resolve_executor_setup(*, executor: str | None, claude: ClaudeAnswers, opencode: OpencodeAnswers, native_model: str, native_base_url: str, native_api_key_ref: str | None) -> ExecutorSetup | None`

- [ ] **Step 1: Написать падающий тест-файл**

Создать `tests/test_init_executor.py`:

```python
"""Тесты чистого резолвера executor-настроек `svarog init` (без CLI/I-O)."""

import pytest

from svarog_harness.cli.init_executor import (
    ClaudeAnswers,
    ExecutorSetupError,
    OpencodeAnswers,
    resolve_executor_setup,
)

_NO_CLAUDE = ClaudeAnswers(requested=False)
_NO_OPENCODE = OpencodeAnswers(requested=False)


def test_nothing_requested_returns_none() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is None


def test_executor_native_with_claude_requested_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="native"):
        resolve_executor_setup(
            executor="native",
            claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
            opencode=_NO_OPENCODE,
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_unknown_executor_value_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="--executor"):
        resolve_executor_setup(
            executor="bogus",
            claude=_NO_CLAUDE,
            opencode=_NO_OPENCODE,
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_claude_api_key_with_value_sets_ref() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is not None
    assert result.active == "claude-code"
    assert result.claude is not None
    assert result.claude.auth == "api-key"
    assert result.claude.api_key_ref == "CLAUDE_CODE_KEY"
    assert result.claude.oauth_token_ref is None


def test_claude_api_key_without_value_leaves_ref_none() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=ClaudeAnswers(requested=True, auth="api-key", api_key=None),
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is not None
    assert result.claude is not None
    assert result.claude.api_key_ref is None


def test_claude_subscription_always_sets_oauth_ref() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=ClaudeAnswers(requested=True, auth="subscription", oauth_token=None),
        opencode=_NO_OPENCODE,
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref=None,
    )
    assert result is not None
    assert result.claude is not None
    assert result.claude.oauth_token_ref == "CLAUDE_CODE_OAUTH_TOKEN"
    assert result.claude.api_key_ref is None


def test_invalid_claude_auth_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="claude-auth"):
        resolve_executor_setup(
            executor=None,
            claude=ClaudeAnswers(requested=True, auth="bogus"),
            opencode=_NO_OPENCODE,
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_opencode_reuse_native_uses_native_values() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=OpencodeAnswers(requested=True, reuse_native=True),
        native_model="qwen3-coder",
        native_base_url="http://localhost:8000/v1",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.active == "opencode"
    assert result.opencode is not None
    assert result.opencode.model == "qwen3-coder"
    assert result.opencode.base_url == "http://localhost:8000/v1"
    assert result.opencode.api_key_ref == "PROVIDER_API_KEY"


def test_opencode_own_creds_with_values_sets_own_ref() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=OpencodeAnswers(
            requested=True,
            reuse_native=False,
            model="m2",
            base_url="http://y",
            api_key="sk-y",
        ),
        native_model="qwen3-coder",
        native_base_url="http://localhost:8000/v1",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.opencode is not None
    assert result.opencode.model == "m2"
    assert result.opencode.base_url == "http://y"
    assert result.opencode.api_key_ref == "OPENCODE_API_KEY"


def test_opencode_own_creds_without_model_falls_back_to_native() -> None:
    result = resolve_executor_setup(
        executor=None,
        claude=_NO_CLAUDE,
        opencode=OpencodeAnswers(requested=True, reuse_native=False, api_key=None),
        native_model="qwen3-coder",
        native_base_url="http://localhost:8000/v1",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.opencode is not None
    assert result.opencode.model == "qwen3-coder"
    assert result.opencode.base_url == "http://localhost:8000/v1"
    assert result.opencode.api_key_ref is None


def test_both_requested_without_executor_errors() -> None:
    with pytest.raises(ExecutorSetupError, match="Claude Code.*OpenCode|OpenCode.*Claude"):
        resolve_executor_setup(
            executor=None,
            claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
            opencode=OpencodeAnswers(requested=True, reuse_native=True),
            native_model="m",
            native_base_url="http://x",
            native_api_key_ref=None,
        )


def test_both_requested_with_explicit_executor_builds_standby() -> None:
    result = resolve_executor_setup(
        executor="opencode",
        claude=ClaudeAnswers(requested=True, auth="api-key", api_key="sk-x"),
        opencode=OpencodeAnswers(requested=True, reuse_native=True),
        native_model="m",
        native_base_url="http://x",
        native_api_key_ref="PROVIDER_API_KEY",
    )
    assert result is not None
    assert result.active == "opencode"
    assert result.claude is not None  # standby, но собран
    assert result.opencode is not None
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_init_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.cli.init_executor'`

- [ ] **Step 3: Реализовать `src/svarog_harness/cli/init_executor.py`**

```python
"""Чистая валидация и сборка executor-настроек `svarog init` (Claude Code /
OpenCode) из уже собранных ответов.

Ничего не спрашивает и не печатает — сбор ответов (флаги CLI, интерактивные
вопросы) остаётся в `cli/main.py`; сюда попадают финальные значения.
Конфликты — `ExecutorSetupError` с готовым для пользователя текстом.
"""

from dataclasses import dataclass
from typing import Literal

from svarog_harness.scaffold import (
    DEFAULT_CLAUDE_API_KEY_REF,
    DEFAULT_CLAUDE_OAUTH_TOKEN_REF,
    DEFAULT_OPENCODE_API_KEY_REF,
    ClaudeExecutorSetup,
    ExecutorSetup,
    OpencodeExecutorSetup,
)

_VALID_EXECUTORS = ("native", "claude-code", "opencode")
_VALID_CLAUDE_AUTH = ("api-key", "subscription")


class ExecutorSetupError(ValueError):
    """Невалидная или противоречивая комбинация флагов/ответов `init`."""


@dataclass(frozen=True)
class ClaudeAnswers:
    requested: bool
    auth: str = "api-key"
    api_key: str | None = None
    oauth_token: str | None = None


@dataclass(frozen=True)
class OpencodeAnswers:
    requested: bool
    reuse_native: bool = True
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


def resolve_executor_setup(
    *,
    executor: str | None,
    claude: ClaudeAnswers,
    opencode: OpencodeAnswers,
    native_model: str,
    native_base_url: str,
    native_api_key_ref: str | None,
) -> ExecutorSetup | None:
    if executor is not None and executor not in _VALID_EXECUTORS:
        raise ExecutorSetupError(
            f"--executor: неизвестное значение {executor!r} ({'|'.join(_VALID_EXECUTORS)})"
        )
    if executor == "native" and (claude.requested or opencode.requested):
        raise ExecutorSetupError(
            "--executor native конфликтует с --claude-*/--opencode-* флагами"
        )
    if not claude.requested and not opencode.requested:
        return None

    if claude.requested and claude.auth not in _VALID_CLAUDE_AUTH:
        raise ExecutorSetupError(
            f"--claude-auth: неизвестное значение {claude.auth!r} "
            f"({'|'.join(_VALID_CLAUDE_AUTH)})"
        )

    active: Literal["claude-code", "opencode"]
    if executor == "claude-code":
        active = "claude-code"
    elif executor == "opencode":
        active = "opencode"
    elif claude.requested and opencode.requested:
        raise ExecutorSetupError(
            "настроены и Claude Code, и OpenCode — уточните "
            "`--executor claude-code|opencode`"
        )
    elif claude.requested:
        active = "claude-code"
    else:
        active = "opencode"

    claude_setup: ClaudeExecutorSetup | None = None
    if claude.requested:
        if claude.auth == "subscription":
            claude_setup = ClaudeExecutorSetup(
                auth="subscription",
                api_key_ref=None,
                oauth_token_ref=DEFAULT_CLAUDE_OAUTH_TOKEN_REF,
            )
        else:
            claude_setup = ClaudeExecutorSetup(
                auth="api-key",
                api_key_ref=DEFAULT_CLAUDE_API_KEY_REF if claude.api_key else None,
                oauth_token_ref=None,
            )

    opencode_setup: OpencodeExecutorSetup | None = None
    if opencode.requested:
        if opencode.reuse_native:
            opencode_setup = OpencodeExecutorSetup(
                model=native_model,
                base_url=native_base_url,
                api_key_ref=native_api_key_ref,
            )
        else:
            opencode_setup = OpencodeExecutorSetup(
                model=opencode.model or native_model,
                base_url=opencode.base_url or native_base_url,
                api_key_ref=DEFAULT_OPENCODE_API_KEY_REF if opencode.api_key else None,
            )

    return ExecutorSetup(active=active, claude=claude_setup, opencode=opencode_setup)
```

- [ ] **Step 4: Запустить тесты, убедиться, что проходят**

Run: `uv run pytest tests/test_init_executor.py -v`
Expected: PASS — 12 тестов.

- [ ] **Step 5: Проверить типы и линт**

Run: `uv run mypy src/svarog_harness/cli/init_executor.py`
Expected: `Success: no issues found`

Run: `uv run ruff check src/svarog_harness/cli/init_executor.py tests/test_init_executor.py`
Expected: без ошибок.

- [ ] **Step 6: Commit**

```bash
git add src/svarog_harness/cli/init_executor.py tests/test_init_executor.py
git commit -m "feat(cli): pure resolver for init's Claude Code/OpenCode executor setup"
```

---

## Task 3: Флаги, интерактив и секреты в `svarog init` (`cli/main.py`)

**Files:**
- Modify: `src/svarog_harness/cli/main.py:190-280` (функция `init`)
- Modify: `README.md:76-84` (короткое упоминание новых флагов)
- Test: `tests/test_init.py` (новые CLI-тесты через `CliRunner`)

**Interfaces:**
- Consumes: `ExecutorSetup`, `ClaudeExecutorSetup`, `OpencodeExecutorSetup`
  из Task 1; `ClaudeAnswers`, `OpencodeAnswers`, `ExecutorSetupError`,
  `resolve_executor_setup` из Task 2.
- Produces: ничего (терминальный слой — команда CLI).

- [ ] **Step 1: Дописать падающие CLI-тесты в `tests/test_init.py`**

Добавить в конец файла:

```python
def test_init_no_executor_flags_omits_executor_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    result = runner.invoke(cli_main.app, ["init", str(home), "--no-input"])
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "executor:" not in yaml


def test_init_claude_api_key_writes_executor_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "claude-code",
            "--claude-auth",
            "api-key",
            "--claude-api-key",
            "sk-claude-x",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "adapter: claude-code" in yaml
    assert "auth: api-key" in yaml
    assert "api_key_ref: CLAUDE_CODE_KEY" in yaml
    assert "sk-claude-x" not in yaml
    secrets = (tmp_path / "fakehome" / ".svarog" / "secrets.json").read_text(encoding="utf-8")
    assert "sk-claude-x" in secrets


def test_init_claude_subscription_without_token_reminds_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "claude-code",
            "--claude-auth",
            "subscription",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "auth: subscription" in yaml
    assert "oauth_token_ref: CLAUDE_CODE_OAUTH_TOKEN" in yaml
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result.output  # напоминание сохранить токен
    secrets_path = tmp_path / "fakehome" / ".svarog" / "secrets.json"
    assert not secrets_path.exists() or "CLAUDE_CODE_OAUTH_TOKEN" not in secrets_path.read_text(
        encoding="utf-8"
    )


def test_init_opencode_same_as_native_reuses_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--model",
            "m",
            "--base-url",
            "http://localhost:9000/v1",
            "--api-key",
            "sk-native-x",
            "--executor",
            "opencode",
            "--opencode-same-as-native",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "adapter: opencode" in yaml
    assert "model: m" in yaml
    assert "base_url: http://localhost:9000/v1" in yaml
    # OpenCode ссылается на тот же ref, что и нативная модель — не создаёт новый
    assert "OPENCODE_API_KEY" not in yaml
    assert yaml.count("api_key_ref: PROVIDER_API_KEY") == 2  # models + executor


def test_init_opencode_own_creds_writes_separate_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "opencode",
            "--opencode-own-creds",
            "--opencode-model",
            "m2",
            "--opencode-base-url",
            "http://localhost:9100/v1",
            "--opencode-api-key",
            "sk-opencode-y",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "model: m2" in yaml
    assert "api_key_ref: OPENCODE_API_KEY" in yaml
    secrets = (tmp_path / "fakehome" / ".svarog" / "secrets.json").read_text(encoding="utf-8")
    assert "sk-opencode-y" in secrets


def test_init_both_adapters_writes_standby_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--executor",
            "claude-code",
            "--claude-auth",
            "api-key",
            "--claude-api-key",
            "sk-claude-x",
            "--opencode-own-creds",
            "--opencode-model",
            "m2",
        ],
    )
    assert result.exit_code == 0, result.output
    yaml = (home / "svarog.yaml").read_text(encoding="utf-8")
    assert "\n    adapter: claude-code" in yaml
    assert "тоже настроен и готов" in yaml
    assert "#     adapter: opencode" in yaml


def test_init_both_adapters_without_executor_flag_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--claude-api-key",
            "sk-claude-x",
            "--opencode-model",
            "m2",
        ],
    )
    assert result.exit_code != 0


def test_init_conflicting_opencode_creds_flags_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        [
            "init",
            str(home),
            "--no-input",
            "--opencode-same-as-native",
            "--opencode-own-creds",
        ],
    )
    assert result.exit_code != 0


def test_init_executor_native_with_claude_flags_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    home = tmp_path / "home"
    result = runner.invoke(
        cli_main.app,
        ["init", str(home), "--no-input", "--executor", "native", "--claude-api-key", "sk-x"],
    )
    assert result.exit_code != 0
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_init.py -k "test_init_claude or test_init_opencode or test_init_both or test_init_executor or test_init_no_executor_flags" -v`
Expected: FAIL — `Error: No such option: --executor` (флаги ещё не добавлены
в `init()`), либо AssertionError по exit_code/содержимому.

- [ ] **Step 3: Реализовать в `cli/main.py`**

Изменить блок импортов (`main.py:54-59`):

```python
from svarog_harness.cli.init_executor import (
    ClaudeAnswers,
    ExecutorSetupError,
    OpencodeAnswers,
    resolve_executor_setup,
)
from svarog_harness.scaffold import (
    DEFAULT_API_KEY_REF,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    scaffold_agent_home,
)
```

Заменить сигнатуру и тело `init()` (`main.py:190-280`) целиком на:

```python
@app.command()
def init(
    path: Annotated[
        Path | None, typer.Argument(help="Каталог agent-home (по умолчанию ./agent-home)")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Перезаписать существующие файлы")] = False,
    no_input: Annotated[
        bool,
        typer.Option("--no-input", "-y", help="Не задавать вопросов, взять значения по умолчанию"),
    ] = False,
    model: Annotated[str | None, typer.Option(help="Имя модели")] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="Base URL OpenAI-совместимого endpoint")
    ] = None,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="API-ключ (сохраняется в secret store, не в svarog.yaml)"),
    ] = None,
    executor: Annotated[
        str | None,
        typer.Option(
            "--executor", help="Активный исполнитель: native (по умолчанию) | claude-code | opencode"
        ),
    ] = None,
    claude_auth: Annotated[
        str | None,
        typer.Option("--claude-auth", help="Режим авторизации Claude Code: api-key | subscription"),
    ] = None,
    claude_api_key: Annotated[
        str | None, typer.Option("--claude-api-key", help="Anthropic API-ключ (auth=api-key)")
    ] = None,
    claude_oauth_token: Annotated[
        str | None,
        typer.Option(
            "--claude-oauth-token",
            help="OAuth-токен подписки (`claude setup-token`, auth=subscription)",
        ),
    ] = None,
    opencode_model: Annotated[
        str | None, typer.Option("--opencode-model", help="Модель для OpenCode")
    ] = None,
    opencode_base_url: Annotated[
        str | None, typer.Option("--opencode-base-url", help="Base URL endpoint для OpenCode")
    ] = None,
    opencode_api_key: Annotated[
        str | None, typer.Option("--opencode-api-key", help="API-ключ для OpenCode")
    ] = None,
    opencode_same_as_native: Annotated[
        bool,
        typer.Option(
            "--opencode-same-as-native",
            help="OpenCode использует те же креды (модель/base_url/ключ), что и нативный provider",
        ),
    ] = False,
    opencode_own_creds: Annotated[
        bool,
        typer.Option("--opencode-own-creds", help="OpenCode настраивается отдельными кредами"),
    ] = False,
) -> None:
    """Создать agent-home: skills, memory (Flow A), policies, .gitignore (§8).

    Без --no-input задаёт интерактивные вопросы (путь, модель, base_url, ключ,
    Claude Code, OpenCode).
    """
    interactive = not no_input and sys.stdin.isatty()

    if path is None and interactive:
        path = Path(typer.prompt("Каталог agent-home", default="./agent-home"))
    target = (path or Path.cwd() / "agent-home").expanduser().resolve()

    if interactive:
        model = model or typer.prompt("Модель", default=DEFAULT_MODEL)
        base_url = base_url or typer.prompt("Base URL endpoint", default=DEFAULT_BASE_URL)
        if api_key is None:
            api_key = (
                typer.prompt(
                    "API-ключ (Enter — пропустить; для локальной модели не нужен)",
                    default="",
                    hide_input=True,
                    show_default=False,
                )
                or None
            )
    model = model or DEFAULT_MODEL
    base_url = base_url or DEFAULT_BASE_URL
    api_key_ref = DEFAULT_API_KEY_REF if api_key else None

    claude_requested = bool(
        claude_auth or claude_api_key or claude_oauth_token or executor == "claude-code"
    )
    opencode_requested = bool(
        opencode_model
        or opencode_base_url
        or opencode_api_key
        or opencode_same_as_native
        or opencode_own_creds
        or executor == "opencode"
    )

    if interactive and not claude_requested:
        claude_requested = typer.confirm(
            "Настроить Claude Code как исполнителя?", default=False
        )
    if interactive and not opencode_requested:
        opencode_requested = typer.confirm(
            "Настроить OpenCode как исполнителя?", default=False
        )

    if claude_requested:
        if interactive and claude_auth is None:
            claude_auth = typer.prompt(
                "Режим авторизации Claude Code (api-key/subscription)", default="api-key"
            )
        claude_auth = claude_auth or "api-key"
        if claude_auth == "subscription":
            if interactive and claude_oauth_token is None:
                claude_oauth_token = (
                    typer.prompt(
                        "OAuth-токен подписки (`claude setup-token`, Enter — пропустить, "
                        "добавить позже)",
                        default="",
                        hide_input=True,
                        show_default=False,
                    )
                    or None
                )
        else:
            if interactive and claude_api_key is None:
                claude_api_key = (
                    typer.prompt(
                        "Anthropic API-ключ (Enter — пропустить, добавить позже)",
                        default="",
                        hide_input=True,
                        show_default=False,
                    )
                    or None
                )

    opencode_reuse_native = True
    if opencode_requested:
        if opencode_same_as_native and opencode_own_creds:
            console.print(
                "[red]--opencode-same-as-native и --opencode-own-creds "
                "взаимоисключающие[/red]"
            )
            raise typer.Exit(code=1)
        if opencode_own_creds:
            opencode_reuse_native = False
        elif opencode_same_as_native:
            opencode_reuse_native = True
        elif interactive:
            opencode_reuse_native = typer.confirm(
                "OpenCode: использовать те же креды, что и у нативного provider'а?",
                default=True,
            )
        else:
            opencode_reuse_native = True

        if not opencode_reuse_native:
            if interactive and opencode_model is None:
                opencode_model = typer.prompt("Модель для OpenCode", default=model)
            if interactive and opencode_base_url is None:
                opencode_base_url = typer.prompt(
                    "Base URL endpoint для OpenCode", default=base_url
                )
            if interactive and opencode_api_key is None:
                opencode_api_key = (
                    typer.prompt(
                        "API-ключ для OpenCode (Enter — пропустить, добавить позже)",
                        default="",
                        hide_input=True,
                        show_default=False,
                    )
                    or None
                )

    if interactive and claude_requested and opencode_requested and executor is None:
        choice = typer.prompt(
            "Какой сделать активным исполнителем (claude-code/opencode)",
            default="claude-code",
        ).strip()
        executor = choice if choice in ("claude-code", "opencode") else "claude-code"

    claude_answers = ClaudeAnswers(
        requested=claude_requested,
        auth=claude_auth or "api-key",
        api_key=claude_api_key,
        oauth_token=claude_oauth_token,
    )
    opencode_answers = OpencodeAnswers(
        requested=opencode_requested,
        reuse_native=opencode_reuse_native,
        model=opencode_model,
        base_url=opencode_base_url,
        api_key=opencode_api_key,
    )
    try:
        executor_setup = resolve_executor_setup(
            executor=executor,
            claude=claude_answers,
            opencode=opencode_answers,
            native_model=model,
            native_base_url=base_url,
            native_api_key_ref=api_key_ref,
        )
    except ExecutorSetupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    result = scaffold_agent_home(
        target,
        force=force,
        model=model,
        base_url=base_url,
        api_key_ref=api_key_ref,
        executor=executor_setup,
    )
    for created in result.created:
        console.print(f"[green]+[/green] {created.relative_to(target)}")
    for skipped in result.skipped:
        console.print(f"[dim]= {skipped.relative_to(target)} (существует, пропущено)[/dim]")

    secrets_to_store: list[tuple[str, str, str]] = []
    if api_key and api_key_ref:
        secrets_to_store.append((api_key_ref, api_key, "модели"))
    if executor_setup is not None and executor_setup.claude is not None:
        claude_setup = executor_setup.claude
        if claude_setup.auth == "api-key" and claude_api_key and claude_setup.api_key_ref:
            secrets_to_store.append((claude_setup.api_key_ref, claude_api_key, "Claude Code"))
        if (
            claude_setup.auth == "subscription"
            and claude_oauth_token
            and claude_setup.oauth_token_ref
        ):
            secrets_to_store.append(
                (claude_setup.oauth_token_ref, claude_oauth_token, "Claude Code (OAuth)")
            )
    if executor_setup is not None and executor_setup.opencode is not None:
        opencode_setup = executor_setup.opencode
        if opencode_api_key and opencode_setup.api_key_ref:
            secrets_to_store.append((opencode_setup.api_key_ref, opencode_api_key, "OpenCode"))

    if secrets_to_store:
        secrets_path = SecretsConfig().path
        assert secrets_path is not None  # дефолт схемы всегда задан
        store = FileSecretStore(secrets_path.expanduser())
        for ref, value, label in secrets_to_store:
            store.set(ref, value)
            console.print(f"[green]ключ сохранён[/green] ({label}) в {secrets_path} (ref: {ref})")

    ignored = _ensure_gitignored(target)
    if ignored is not None:
        console.print(f"[dim]agent-home добавлен в {ignored}[/dim]")

    async def init_git_subrepo(path: Path, message: str) -> None:
        repo = GitRepo(path)
        if not await repo.is_repo():
            # separate-git-dir по умолчанию (ADR-0015 §0.2): объекты git —
            # вне дерева репозитория, недостижимы из-под агента.
            await repo.init(separate_git_dir=separate_gitdir_for(path))
            await repo.ensure_identity()
            await repo.add_all()
            with contextlib.suppress(GitError):
                await repo.commit(message)

    async def init_subrepos() -> None:
        # memory — Flow A (ADR-0004); skills — Flow B базовая ветка для proposals (§18).
        await init_git_subrepo(target / "memory", "svarog init: memory repo")
        await init_git_subrepo(target / "skills", "svarog init: skills repo")

    asyncio.run(init_subrepos())

    pending_refs: list[str] = []
    if api_key_ref and not api_key:
        pending_refs.append(api_key_ref)
    if executor_setup is not None and executor_setup.claude is not None:
        claude_setup = executor_setup.claude
        if claude_setup.auth == "api-key" and claude_setup.api_key_ref and not claude_api_key:
            pending_refs.append(claude_setup.api_key_ref)
        if (
            claude_setup.auth == "subscription"
            and claude_setup.oauth_token_ref
            and not claude_oauth_token
        ):
            pending_refs.append(claude_setup.oauth_token_ref)
    if (
        executor_setup is not None
        and executor_setup.opencode is not None
        and not opencode_reuse_native
        and executor_setup.opencode.api_key_ref
        and not opencode_api_key
    ):
        pending_refs.append(executor_setup.opencode.api_key_ref)

    if pending_refs:
        reminders = ", ".join(f"`svarog secrets set {ref}`" for ref in pending_refs)
        next_step = f"добавьте ключи: {reminders}"
    else:
        next_step = 'запустите `svarog run "…"`'
    executor_note = (
        f"; исполнитель: {executor_setup.active}" if executor_setup is not None else ""
    )
    console.print(
        f"\n[bold]agent-home готов:[/bold] {target}\n"
        f"[dim]модель {model} @ {base_url}{executor_note}; {next_step}[/dim]"
    )
```

- [ ] **Step 4: Запустить весь тестовый файл**

Run: `uv run pytest tests/test_init.py -v`
Expected: PASS — все тесты (старые + новые), включая
`test_init_command_creates_home_and_memory_repo`,
`test_init_stores_api_key_outside_config`,
`test_init_adds_home_to_project_gitignore` (регрессия) и 8 новых.

- [ ] **Step 5: Прогнать весь набор тестов проекта**

Run: `uv run pytest -q`
Expected: PASS — ни один существующий тест не сломан.

- [ ] **Step 6: Проверить типы и линт**

Run: `uv run mypy src/svarog_harness/cli/main.py`
Expected: `Success: no issues found`

Run: `uv run ruff check src/svarog_harness/cli/main.py tests/test_init.py`
Expected: без ошибок.

- [ ] **Step 7: Обновить README**

В `README.md` после строки 83 (абзац про secret store) добавить:

```markdown
Чтобы сразу настроить Claude Code или OpenCode как исполнителя
(`executor.external`, ADR-0016) — независимо друг от друга, credentials
можно не вводить и добавить позже:

```bash
uv run svarog init ./agent-home --no-input \
  --executor claude-code --claude-auth subscription   # OAuth-токен потом: svarog secrets set CLAUDE_CODE_OAUTH_TOKEN

uv run svarog init ./agent-home --no-input \
  --executor opencode --opencode-same-as-native        # те же креды, что и у models.local
```
```

- [ ] **Step 8: Commit**

```bash
git add src/svarog_harness/cli/main.py tests/test_init.py README.md
git commit -m "feat(cli): svarog init configures Claude Code/OpenCode executor"
```

---

## Self-Review

**Spec coverage:**
- Интерактивный флоу с независимыми вопросами Claude/OpenCode → Task 3,
  Step 3 (`claude_requested`/`opencode_requested` gating + sub-prompts).
- Флаги для `--no-input` → Task 3, Step 3 (полный список опций).
- Reuse нативных кредов для OpenCode → Task 2 (`OpencodeAnswers.reuse_native`)
  + Task 3 (запрос/дефолт `opencode_reuse_native`).
- Рендеринг активного блока + standby-комментария → Task 1.
- Секреты не в yaml, только ref → Task 1 (`_claude_block_lines`/
  `_opencode_block_lines`) + Task 3 (`secrets_to_store`).
- Все 9 сценариев конфликтов/ошибок из спеки → Task 2 unit-тесты +
  Task 3 CLI-тесты (`test_init_both_adapters_without_executor_flag_errors`,
  `test_init_conflicting_opencode_creds_flags_errors`,
  `test_init_executor_native_with_claude_flags_errors`).
- Обратная совместимость (executor не пишется без флагов) →
  `test_scaffold_no_executor_omits_section` +
  `test_init_no_executor_flags_omits_executor_section`.

**Placeholder scan:** нет TBD/TODO; весь код в шагах — полный, не
сокращённый («…» встречается только внутри строковых литералов вывода CLI,
не как плейсхолдер кода).

**Type consistency:** `ExecutorSetup.active: Literal["claude-code",
"opencode"]` используется одинаково в `scaffold.py` (Task 1),
`init_executor.py` (Task 2, ветки `active = "claude-code"` /
`active = "opencode"` — литералы совпадают) и `main.py` (Task 3, обращение
`executor_setup.active`). `ClaudeAnswers`/`OpencodeAnswers` — одни и те же
имена полей во всех трёх задачах (`requested`, `auth`, `api_key`,
`oauth_token`, `reuse_native`, `model`, `base_url`).

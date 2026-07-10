"""Scaffolding agent-home для `svarog init` (§8, ADR-0006).

Создаёт структуру agent-home: AGENTS.md, skills/, memory/, workspaces/,
artifacts/, policies/, дефолтный svarog.yaml и .gitignore с denylist
секретов. Git-инициализацию memory-репозитория делает CLI (Flow A).
"""

from dataclasses import dataclass
from pathlib import Path

from svarog_harness.secrets import gitignore_block

_AGENTS_MD = """\
# Agent instructions

- Перед началом работы делай git pull.
- Работай в task-ветке, не коммить напрямую в main/production.
- Не пуш без approval; protected ветки требуют подтверждения всегда.
- Используй подходящий skill, если он есть (read_skill перед применением).
- Переиспользуемые процедуры сохраняй как skill proposals, а не правь skills/ напрямую.
- Не запрашивай и не раскрывай секреты без явного approval.
- Прогоняй проверки перед тем, как отчитаться о завершении.
"""

_README_MD = """\
# Agent home

Домашний репозиторий Svarog-агента (§8): скиллы, память, политики, артефакты.
Память (`memory/`) — Flow A: single writer, прямые коммиты (ADR-0004).
Скиллы (`skills/`) — Flow B: изменения через proposals (ADR-0003).
"""

DEFAULT_MODEL = "qwen3-coder"
DEFAULT_BASE_URL = "http://localhost:8000/v1"
DEFAULT_API_KEY_REF = "PROVIDER_API_KEY"


def _render_config_yaml(model: str, base_url: str, api_key_ref: str | None) -> str:
    """Собрать svarog.yaml под endpoint (значение ключа в конфиг не пишется, ADR-0006)."""
    if api_key_ref:
        key_line = f"      api_key_ref: {api_key_ref}   # имя секрета в SecretStore (не сам ключ)"
    else:
        key_line = f"      # api_key_ref: {DEFAULT_API_KEY_REF}   # имя env-переменной с ключом"
    return f"""\
# Конфигурация Svarog (§13). Отредактируйте секцию models под свой endpoint.
models:
  default: local
  providers:
    local:
      type: openai-compatible
      base_url: {base_url}   # vLLM, llama.cpp, LiteLLM, OpenRouter…
      model: {model}
{key_line}

runtime:
  autonomy: yolo

sandbox:
  type: docker            # docker (изоляция) | local-trusted (без изоляции, §17)
  image: python:3.12-slim

memory:
  path: ./memory          # Flow A memory-репозиторий (ADR-0004)

skills:
  paths:
    - ./skills

policies:
  protected_branches:
    - main
    - production

storage:
  db_path: ./.svarog/svarog.db

gateway:
  token_ref: null          # задайте имя секрета для serve --host 0.0.0.0
"""


_SECURITY_POLICY = """\
# Пользовательские policy-правила (§6.6). Могут только ужесточать поведение:
# decision — deny | require_approval | notify (allow запрещён схемой).
rules:
  - match: "bash.exec"
    decision: notify
    reason: "видеть все shell-команды агента в trace"
"""

_EXAMPLE_SKILL = """\
---
name: example-note
description: Записать короткую заметку в файл note.md в workspace.
version: 0.1.0
risk: low
allowed_tools:
  - write_file
provenance: official
---

# When to use

Когда пользователь просит быстро сохранить заметку.

# Workflow

1. Составь текст заметки.
2. Запиши его в note.md через write_file.
3. Подтверди, что файл создан.
"""

_PROFILE_MD = "# Профиль пользователя\n\n(агент дополняет этот файл через remember)\n"


@dataclass(frozen=True)
class ScaffoldResult:
    created: list[Path]
    skipped: list[Path]


def _write(path: Path, content: str, result: ScaffoldResult, *, force: bool) -> None:
    if path.exists() and not force:
        result.skipped.append(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    result.created.append(path)


def scaffold_agent_home(
    target: Path,
    *,
    force: bool = False,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_ref: str | None = None,
) -> ScaffoldResult:
    """Создать структуру agent-home; существующие файлы не трогаются без force.

    model/base_url/api_key_ref подставляются в секцию models сгенерированного
    svarog.yaml. Значение ключа в конфиг не пишется — только имя (api_key_ref).
    """
    result = ScaffoldResult(created=[], skipped=[])
    config_yaml = _render_config_yaml(model, base_url, api_key_ref)
    _write(target / "AGENTS.md", _AGENTS_MD, result, force=force)
    _write(target / "README.md", _README_MD, result, force=force)
    _write(target / "svarog.yaml", config_yaml, result, force=force)
    _write(target / ".gitignore", gitignore_block(), result, force=force)
    _write(target / "policies" / "security.yaml", _SECURITY_POLICY, result, force=force)
    _write(target / "skills" / "example-note" / "SKILL.md", _EXAMPLE_SKILL, result, force=force)
    _write(target / "memory" / "user" / "profile.md", _PROFILE_MD, result, force=force)
    # Пустые каталоги под будущее наполнение.
    for sub in ("memory/projects", "memory/decisions", "workspaces/tasks", "artifacts"):
        _write(target / sub / ".gitkeep", "", result, force=force)
    return result

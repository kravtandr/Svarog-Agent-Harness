# Блок E: подсказки для жёстких границ — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Отказы, повтор которых заведомо бесполезен, должны сообщать модели, что граница жёсткая и что делать вместо повтора.

**Architecture:** Класс отказа определяется типизированно в момент отказа (`BoundaryKind`), а текст подсказки подставляется один раз при рендере результата для модели. В trace уходит чистая причина отказа. Enforcement не меняется — подсказка ничего не разрешает.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, pytest (`asyncio_mode=auto`), ruff, mypy.

**Спек:** `docs/superpowers/specs/2026-07-21-boundary-guidance-design.md`

## Global Constraints

- Комментарии, docstring'и, сообщения об ошибках и тексты подсказок — на русском; код и идентификаторы — на английском.
- Conventional Commits, заголовок ≤72 символов, на английском в императиве: `feat|fix|docs|refactor|test|chore(scope): описание`. Scope — модуль (`tools`, `runtime`, `sandbox`, `docs`).
- Перед каждым коммитом: `uv run ruff check`, `uv run ruff format`, `uv run mypy`, `uv run pytest` — всё зелёное.
- Известное пред-существующее падение, не связанное с блоком: `tests/test_external_docker.py::test_external_run_once_in_docker` (загрязнение docker-окружения). Не чинить, отмечать в отчёте.
- Набор тестов флейкает: полный прогон может дать одно падение в случайном тесте, в изоляции проходящем. Это воспроизводится и до блока E. Упавший тест перезапустить изолированно; если проходит — отметить в отчёте и продолжать.
- Правила зависимостей (`docs/repo-structure.md`): `cli` → `runtime` → компоненты → `storage`/`trace`. Импортов из `cli` в ядро нет.
- Никакого мёртвого кода: неиспользуемые функции, закомментированные блоки — не проходят review.
- Секретов в коде и тестах нет.
- Подсказка ничего не разрешает и не влияет на исход отказа — только объясняет уже принятое решение.

---

## File Structure

**Создаётся:**
- `src/svarog_harness/tools/guidance.py` — перечисление классов жёстких границ и словарь текстов подсказок. Ни от чего не зависит, кроме stdlib.
- `tests/test_guidance.py` — юнит-тесты словаря.

**Модифицируется:**
- `src/svarog_harness/tools/base.py` — `ToolError.kind`, `ToolResult.boundary`, `ToolResult.failure(..., boundary=...)`.
- `src/svarog_harness/tools/file_tools.py` — `resolve_in_workspace` проставляет класс границы обоим своим отказам.
- `src/svarog_harness/runtime/loop.py` — проставление класса в ветке `PolicyAction.DENY` и в `_consume_approval`; подстановка текста в `_render_tool_result`.
- `tests/test_file_tools.py`, `tests/test_loop.py`, `tests/test_approval_flow.py`, `tests/test_sandbox.py` — тесты.
- `docs/adr/0002-security-enforcement-over-classification.md`, `docs/reference-analysis.md` — документация.

---

## Task 1: Словарь границ

**Files:**
- Create: `src/svarog_harness/tools/guidance.py`
- Create: `tests/test_guidance.py`
- Test: `tests/test_guidance.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `BoundaryKind(StrEnum)` со значениями `WORKSPACE_ESCAPE = "workspace_escape"`, `CONTROL_DIR_WRITE = "control_dir_write"`, `POLICY_DENY = "policy_deny"`, `APPROVAL_DENIED = "approval_denied"`; функция `note_for(kind: BoundaryKind) -> str`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_guidance.py`:

```python
"""Словарь подсказок для жёстких границ (блок E §1)."""

from svarog_harness.tools.guidance import BoundaryKind, note_for


def test_every_kind_has_a_note() -> None:
    """У каждого класса границы есть непустая подсказка."""
    for kind in BoundaryKind:
        note = note_for(kind)
        assert note.strip()


def test_notes_are_distinct() -> None:
    """Подсказки различаются: одинаковый текст на разные границы бесполезен."""
    notes = {note_for(kind) for kind in BoundaryKind}
    assert len(notes) == len(list(BoundaryKind))


def test_workspace_escape_note_names_futile_workarounds() -> None:
    """Подсказка про workspace перечисляет бесполезные обходы и даёт выход."""
    note = note_for(BoundaryKind.WORKSPACE_ESCAPE)
    assert "симлинк" in note
    assert "ask_user" in note


def test_control_dir_note_names_reserved_dirs() -> None:
    note = note_for(BoundaryKind.CONTROL_DIR_WRITE)
    assert ".git" in note
    assert ".svarog" in note


def test_policy_deny_note_points_to_approval() -> None:
    """Отказ политики: повтор бесполезен, но есть легальный путь."""
    note = note_for(BoundaryKind.POLICY_DENY)
    assert "request_approval" in note


def test_approval_denied_note_discourages_identical_retry() -> None:
    note = note_for(BoundaryKind.APPROVAL_DENIED)
    assert "повтор" in note.lower()
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_guidance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'svarog_harness.tools.guidance'`

- [ ] **Step 3: Написать модуль**

Создать `src/svarog_harness/tools/guidance.py`:

```python
"""Подсказки для жёстких границ (блок E).

Enforcement решает, что произойдёт; подсказка объясняет модели уже принятое
решение и говорит, что делать вместо повтора. Она ничего не разрешает и на
исход не влияет (ADR-0002).

Область — только те отказы, повтор которых заведомо бесполезен: обычная
ошибка tool сюда не входит, там повтор с другими аргументами осмыслен.
"""

from enum import StrEnum


class BoundaryKind(StrEnum):
    """Классы отказов, повтор которых не имеет смысла."""

    WORKSPACE_ESCAPE = "workspace_escape"
    CONTROL_DIR_WRITE = "control_dir_write"
    POLICY_DENY = "policy_deny"
    APPROVAL_DENIED = "approval_denied"


_NOTES: dict[BoundaryKind, str] = {
    BoundaryKind.WORKSPACE_ESCAPE: (
        "Это жёсткая граница workspace, а не временный сбой. Обходить её "
        "симлинком, абсолютным путём или сменой рабочей директории "
        "бесполезно — такие пути отвергаются так же. Если задача "
        "действительно требует файл снаружи workspace, спроси пользователя "
        "через ask_user."
    ),
    BoundaryKind.CONTROL_DIR_WRITE: (
        "Каталоги .git и .svarog зарезервированы runtime: запись туда не "
        "разрешится ни при каких аргументах. Работа с историей Git "
        "выполняется не тобой, а привилегированными host-flow'ами."
    ),
    BoundaryKind.POLICY_DENY: (
        "Решение принято политикой этого run'а: режим автономии и правила "
        "зафиксированы при старте и в этом ходе не изменятся. Повтор того же "
        "вызова даст тот же отказ. Если действие необходимо для задачи — "
        "запроси подтверждение через request_approval."
    ),
    BoundaryKind.APPROVAL_DENIED: (
        "Человек отказал или запрос истёк. Повторять тот же запрос без "
        "изменения условий не следует: продолжи задачу другим способом или "
        "уточни у пользователя, что делать дальше."
    ),
}


def note_for(kind: BoundaryKind) -> str:
    """Текст подсказки для класса границы."""
    return _NOTES[kind]
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `uv run pytest tests/test_guidance.py -v`
Expected: PASS, 6 passed

- [ ] **Step 5: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/tools/guidance.py tests/test_guidance.py
git commit -m "feat(tools): add boundary guidance vocabulary"
```

---

## Task 2: Классификация в момент отказа

**Files:**
- Modify: `src/svarog_harness/tools/base.py` (`ToolError`, `ToolResult`)
- Modify: `src/svarog_harness/tools/file_tools.py` (`resolve_in_workspace`)
- Modify: `src/svarog_harness/runtime/loop.py` (ветка `PolicyAction.DENY` около строки 962; `_consume_approval` около строки 700)
- Test: `tests/test_file_tools.py`, `tests/test_loop.py`, `tests/test_approval_flow.py`

**Interfaces:**
- Consumes: `BoundaryKind` из `svarog_harness.tools.guidance`.
- Produces: `ToolError(message, *, kind: BoundaryKind | None = None)` с атрибутом `kind`; поле `ToolResult.boundary: BoundaryKind | None = None`; `ToolResult.failure(error: str, *, boundary: BoundaryKind | None = None)`.

- [ ] **Step 1: Написать падающий тест на файловые границы**

В конец `tests/test_file_tools.py` добавить:

```python
def test_workspace_escape_carries_boundary_kind(tmp_path: Path) -> None:
    """Выход за workspace классифицируется как жёсткая граница (блок E §2)."""
    from svarog_harness.tools.guidance import BoundaryKind

    with pytest.raises(ToolError) as excinfo:
        resolve_in_workspace(tmp_path, "../снаружи.txt")

    assert excinfo.value.kind is BoundaryKind.WORKSPACE_ESCAPE


def test_control_dir_write_carries_boundary_kind(tmp_path: Path) -> None:
    """Запись в управляющий каталог классифицируется отдельным видом."""
    from svarog_harness.tools.guidance import BoundaryKind

    with pytest.raises(ToolError) as excinfo:
        resolve_in_workspace(tmp_path, ".git/config", for_write=True)

    assert excinfo.value.kind is BoundaryKind.CONTROL_DIR_WRITE


def test_ordinary_tool_error_has_no_boundary_kind() -> None:
    """Обычная ошибка tool не классифицируется как жёсткая граница."""
    assert ToolError("что-то пошло не так").kind is None
```

Реализующему: `ToolError` и `resolve_in_workspace` в этом файле уже импортируются существующими тестами — новых импортов на уровне модуля добавлять не нужно, кроме тех, что показаны внутри тестов.

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_file_tools.py -k boundary_kind -v`
Expected: FAIL — `AttributeError: 'ToolError' object has no attribute 'kind'`

- [ ] **Step 3: Расширить ToolError и ToolResult**

В `src/svarog_harness/tools/base.py` добавить импорт рядом с остальными импортами модуля:

```python
from svarog_harness.tools.guidance import BoundaryKind
```

Заменить `ToolError`:

```python
class ToolError(Exception):
    """Ожидаемая ошибка исполнения tool — превращается в error-результат для модели.

    `kind` заполняется, когда отказ упирается в жёсткую границу и повтор
    заведомо бесполезен: по нему рендер подставит подсказку модели (блок E).
    """

    def __init__(self, message: str, *, kind: BoundaryKind | None = None) -> None:
        super().__init__(message)
        self.kind = kind
```

В `ToolResult` добавить поле и расширить `failure`:

```python
class ToolResult(BaseModel):
    ok: bool
    # Текст для модели: содержимое файла, stdout, сообщение об успехе.
    output: str = ""
    error: str | None = None
    # Класс жёсткой границы, если отказ упёрся в неё (блок E). В trace не
    # попадает — используется только при рендере результата для модели.
    boundary: BoundaryKind | None = None

    @classmethod
    def success(cls, output: str) -> "ToolResult":
        return cls(ok=True, output=output)

    @classmethod
    def failure(cls, error: str, *, boundary: BoundaryKind | None = None) -> "ToolResult":
        return cls(ok=False, error=error, boundary=boundary)
```

- [ ] **Step 4: Проставить класс в файловых отказах**

В `src/svarog_harness/tools/file_tools.py` добавить импорт:

```python
from svarog_harness.tools.guidance import BoundaryKind
```

В `resolve_in_workspace` заменить оба возбуждения ошибки:

```python
    try:
        resolved = safe_join(workspace, raw)
    except PathTraversalError as exc:
        raise ToolError(str(exc), kind=BoundaryKind.WORKSPACE_ESCAPE) from None
    if for_write:
        rel_parts = resolved.relative_to(workspace.resolve()).parts
        if rel_parts and rel_parts[0] in _WRITE_DENY_PREFIXES:
            raise ToolError(
                f"запись в управляющий каталог запрещена: {raw} "
                f"(префикс '{rel_parts[0]}' зарезервирован runtime)",
                kind=BoundaryKind.CONTROL_DIR_WRITE,
            )
    return resolved
```

- [ ] **Step 5: Запустить тест**

Run: `uv run pytest tests/test_file_tools.py -v`
Expected: PASS

- [ ] **Step 6: Пробросить класс из ToolError в ToolResult**

Найти в `src/svarog_harness/runtime/loop.py` место, где перехваченный `ToolError` превращается в результат (метод `call` базового класса `Tool` в `tools/base.py` либо обработчик в loop — реализующему нужно найти фактическую точку через `grep -n "ToolError" src/svarog_harness/tools/base.py src/svarog_harness/runtime/loop.py`).

В этой точке заменить построение результата так, чтобы класс границы переносился из исключения:

```python
        except ToolError as exc:
            return ToolResult.failure(str(exc), boundary=exc.kind)
```

- [ ] **Step 7: Написать падающий тест на policy DENY**

В `tests/test_loop.py` добавить:

```python
async def test_policy_deny_result_carries_boundary_kind(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Запрет политикой классифицируется как жёсткая граница (блок E §2)."""
    from svarog_harness.tools.guidance import BoundaryKind

    calls: list[ToolResult] = []
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(id="c1", name="bash", arguments_json='{"command": "rm -rf /"}')
            ),
            _final("готово"),
        ]
    )
    loop = _loop(provider, db, tmp_path, autonomy=AutonomyMode.READONLY)
    original = loop._render_tool_result

    def capture(run: Run, call: ToolCallRequest, result: ToolResult) -> str:
        calls.append(result)
        return original(run, call, result)

    loop._render_tool_result = capture  # type: ignore[method-assign]
    await loop.run("удали всё", AutonomyMode.READONLY)

    denied = [r for r in calls if not r.ok]
    assert denied
    assert denied[0].boundary is BoundaryKind.POLICY_DENY
```

Реализующему: подбери такую комбинацию tool + режим автономии, при которой политика возвращает `DENY` — посмотри соседние тесты `tests/test_policy.py` и существующие deny-сценарии в `tests/test_loop.py` и используй их конфигурацию, а не выдумывай свою. Если в `_loop(...)` нет параметра режима автономии, задай режим тем же способом, что и соседние тесты.

- [ ] **Step 8: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py::test_policy_deny_result_carries_boundary_kind -v`
Expected: FAIL — `assert None is BoundaryKind.POLICY_DENY`

- [ ] **Step 9: Проставить класс в ветке DENY и в отказе approval**

В `src/svarog_harness/runtime/loop.py` добавить импорт:

```python
from svarog_harness.tools.guidance import BoundaryKind
```

В ветке `PolicyAction.DENY` (около строки 962) заменить построение результата:

```python
            result = ToolResult.failure(
                f"запрещено политикой: {decision.reason}",
                boundary=BoundaryKind.POLICY_DENY,
            )
```

В `_consume_approval` (около строки 700) заменить:

```python
        result = ToolResult.failure(
            f"approval {verb}: {reason}", boundary=BoundaryKind.APPROVAL_DENIED
        )
```

- [ ] **Step 10: Запустить тесты**

Run: `uv run pytest tests/test_loop.py tests/test_file_tools.py tests/test_approval_flow.py -v`
Expected: PASS

- [ ] **Step 11: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/tools/base.py src/svarog_harness/tools/file_tools.py src/svarog_harness/runtime/loop.py tests/test_file_tools.py tests/test_loop.py
git commit -m "feat(tools): classify hard-boundary refusals"
```

---

## Task 3: Подстановка подсказки при рендере

**Files:**
- Modify: `src/svarog_harness/runtime/loop.py` (`_render_tool_result`, строка 994)
- Test: `tests/test_loop.py`, `tests/test_approval_flow.py`

**Interfaces:**
- Consumes: `ToolResult.boundary`, `note_for` из Task 1 и 2.
- Produces: ничего нового для последующих задач.

- [ ] **Step 1: Написать падающий тест**

В `tests/test_loop.py` добавить:

```python
async def test_boundary_note_reaches_model_but_not_trace(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Подсказка уходит модели, в trace остаётся чистая причина (блок E §3)."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1", name="read_file", arguments_json='{"path": "../снаружи.txt"}'
                )
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    # Модель видит причину и подсказку.
    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert tool_messages
    assert "выходит за пределы" in tool_messages[0].content
    assert "ask_user" in tool_messages[0].content

    # В trace — только причина, без текста-инструкции.
    calls = (await db.scalars(select(ToolCall).where(ToolCall.run_id == outcome.run_id))).all()
    assert len(calls) == 1
    assert calls[0].error is not None
    assert "выходит за пределы" in calls[0].error
    assert "ask_user" not in calls[0].error


async def test_ordinary_failure_gets_no_note(db: AsyncSession, tmp_path: Path) -> None:
    """Обычная ошибка tool не получает подсказки: там повтор осмыслен."""
    provider = ScriptedProvider(
        [
            _tool_turn(
                ToolCallRequest(
                    id="c1", name="read_file", arguments_json='{"path": "нет-такого.txt"}'
                )
            ),
            _final("готово"),
        ]
    )
    outcome = await _loop(provider, db, tmp_path).run("читай", AutonomyMode.YOLO)
    assert outcome.state is RunState.COMPLETED

    tool_messages = [m for m in provider.seen_messages[-1] if m.role == "tool"]
    assert tool_messages
    assert "ask_user" not in tool_messages[0].content
    assert "жёсткая граница" not in tool_messages[0].content
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `uv run pytest tests/test_loop.py -k boundary_note -v`
Expected: FAIL — `assert "ask_user" in ...`

- [ ] **Step 3: Подставить подсказку в рендере**

В `src/svarog_harness/runtime/loop.py` добавить импорт `note_for` к уже добавленному в Task 2 импорту `BoundaryKind`:

```python
from svarog_harness.tools.guidance import BoundaryKind, note_for
```

В `_render_tool_result` заменить формирование текста для ветвей ошибки:

```python
        if result.ok:
            text = result.output or "(успех, пустой вывод)"
        elif result.output:
            text = f"ошибка: {result.error}\n{result.output}"
        else:
            text = f"ошибка: {result.error}"
        if result.boundary is not None:
            # Подсказка — надстройка над enforcement (ADR-0002): объясняет
            # уже принятое решение, ничего не разрешая. Повторяется на каждом
            # отказе: она нужна ровно в момент, когда модель собирается
            # повторить бесполезное действие.
            text = f"{text}\n{note_for(result.boundary)}"
        text = redact(text, self._secret_values)
```

Остальной метод (redaction → персистенция → усечение) не меняется. Отказы этих четырёх классов приходят с пустым `output` и коротким `error`, поэтому до ветки усечения не доходят.

- [ ] **Step 4: Запустить тест**

Run: `uv run pytest tests/test_loop.py -k "boundary_note or ordinary_failure" -v`
Expected: PASS

- [ ] **Step 5: Написать тест на подсказку при отказе approval**

В `tests/test_approval_flow.py` добавить:

```python
async def test_denied_call_explains_boundary_to_model(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Отказ в approval объясняет, что повторять тот же запрос не нужно (блок E §3).

    В trace остаётся чистая причина отказа — текст-инструкция туда не уходит.
    """
    deploy = DeployTool()
    provider = ScriptedProvider(
        [
            _tool_turn(ToolCallRequest(id="c1", name="deploy_preview", arguments_json="{}")),
            _final("понял, не деплою"),
        ]
    )
    loop = _loop(provider, db, tmp_path, [deploy])
    outcome = await loop.run("задеплой", AutonomyMode.SUPERVISED)
    assert outcome.state is RunState.WAITING_APPROVAL

    recorder = TraceRecorder(db)
    approval = (await db.execute(select(Approval))).scalar_one()
    await recorder.decide_approval(approval, approved=False, decided_by="test", reason="не сейчас")

    resumed = await _resume(loop, db, outcome.run_id)
    assert resumed.state is RunState.COMPLETED  # type: ignore[attr-defined]

    # Модель получила и причину, и подсказку.
    model_text = provider.seen_messages[-1][-1].content
    assert "не сейчас" in model_text
    assert "повтор" in model_text.lower()

    # В trace — только причина.
    call = (await db.execute(select(ToolCall))).scalar_one()
    assert call.error is not None
    assert "не сейчас" in call.error
    assert "повтор" not in call.error.lower()
```

Реализующему: тест построен по образцу существующего `test_denied_call_reports_reason_to_model` в этом же файле и использует те же фикстуры и хелперы (`DeployTool`, `ScriptedProvider`, `_loop`, `_resume`). Новых фикстур заводить не нужно.

- [ ] **Step 6: Запустить тест**

Run: `uv run pytest tests/test_approval_flow.py -v`
Expected: PASS

- [ ] **Step 7: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add src/svarog_harness/runtime/loop.py tests/test_loop.py tests/test_approval_flow.py
git commit -m "feat(runtime): explain hard boundaries to the model"
```

---

## Task 4: Регрессионный тест на mounts

**Files:**
- Modify: `tests/test_sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `DockerEnvironment.run_args()` (существует).
- Produces: ничего.

- [ ] **Step 1: Написать тест**

В `tests/test_sandbox.py` рядом с `test_docker_run_args_without_skills` добавить:

```python
def test_docker_run_args_never_mount_agent_home(tmp_path: Path) -> None:
    """agent-home не монтируется в sandbox ни при каком режиме (блок E §4).

    Инвариант, из-за которого не понадобилась tmpfs-маскировка: контейнер
    просто не видит каталог с конфигом, секретами и БД. Держим его тестом,
    а не соглашением.
    """
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = agent_home / "skills"
    skills.mkdir()
    state_dir = agent_home / ".svarog" / "agent-state" / "claude-code"
    state_dir.mkdir(parents=True)

    env = DockerEnvironment(
        workspace,
        SandboxConfig(),
        skills_dir=skills,
        extra_mounts=[(state_dir, "/home/agent/.claude", False)],
    )
    args = env.run_args()

    mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
    for mount in mounts:
        host_path = Path(mount.split(":", 1)[0])
        assert host_path != agent_home
        assert agent_home not in host_path.parents or host_path in (skills, state_dir)
```

Реализующему: последняя проверка сформулирована так намеренно — `skills` и каталог состояния внешнего агента лежат **внутри** agent-home и монтируются легально; запрещено монтировать сам agent-home или любой другой путь внутри него. Если сигнатура `DockerEnvironment.__init__` отличается от использованной (проверь `src/svarog_harness/sandbox/docker.py:66`), приведи вызов к фактической, не меняя смысла теста.

- [ ] **Step 2: Запустить тест**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: PASS — тест фиксирует уже существующее поведение, падать он не должен. Если падает, это находка: разберись, какой путь монтируется, и сообщи, не «подгоняя» тест.

- [ ] **Step 3: Проверки и коммит**

```bash
uv run ruff check && uv run ruff format && uv run mypy && uv run pytest
git add tests/test_sandbox.py
git commit -m "test(sandbox): lock invariant that agent-home is never mounted"
```

---

## Task 5: Документация

**Files:**
- Modify: `docs/adr/0002-security-enforcement-over-classification.md`
- Modify: `docs/reference-analysis.md`

**Interfaces:**
- Consumes: результаты задач 1-4.
- Produces: ничего исполняемого.

- [ ] **Step 1: Дополнить ADR-0002**

Добавить раздел про подсказки для жёстких границ в стиле существующих разделов документа. Зафиксировать:

- подсказка — надстройка над enforcement, а не его часть: она объясняет уже принятое решение и не влияет на исход;
- область — четыре класса отказов, повтор которых заведомо бесполезен (выход за workspace, запись в управляющий каталог, запрет политикой, отказ в approval); обычные ошибки tool сюда не входят;
- класс определяется типизированно в момент отказа, а не распознаванием текста ошибки при рендере — распознавание было бы ровно той классификацией, против которой стоит этот ADR;
- подсказка добавляется при рендере результата для модели и не попадает в trace: аудит хранит чистую причину отказа;
- файлы: `tools/guidance.py`, `tools/base.py`, `tools/file_tools.py`, `runtime/loop.py`; тесты-репродьюсеры: `tests/test_guidance.py`, `tests/test_loop.py`, `tests/test_file_tools.py`, `tests/test_approval_flow.py`.

- [ ] **Step 2: Обновить раздел про nanobot в reference-analysis**

В `docs/reference-analysis.md`, в разделе про HKUDS/nanobot, перевести два пункта из «отложено» в «отвергнуто» с причинами:

- **tmpfs-маскировка agent-home** — у nanobot нужна, потому что их bwrap-песочница ro-биндит корень хоста и каталог с ключами виден изнутри. Docker-sandbox Svarog монтирует только workspace, skills и каталог состояния внешнего агента; agent-home не монтируется вообще — прятать нечего. Остаточный случай `local-trusted` — осознанный trade-off режима (ADR-0015 §0.3), закрывается только новым sandbox-бэкендом с namespaces. Вместо механизма инвариант закреплён тестом `tests/test_sandbox.py`.
- **Runtime Context как удаляемый блок метаданных** — у nanobot защищает от утечки метаданных, дописываемых к сообщению пользователя. В Svarog к `user`-сообщению не дописывается ничего (`runtime/context_builder.py`), утечки нет, механизм без потребителя не заводится.

Там же отметить, что перенесённым оказался только третий пункт блока — подсказки для жёстких границ.

- [ ] **Step 3: Финальная проверка и коммит**

```bash
uv run ruff check && uv run pytest
git add docs/adr/0002-security-enforcement-over-classification.md docs/reference-analysis.md
git commit -m "docs: record boundary guidance in ADR-0002"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие спека:** §1 словарь → Task 1; §2 классификация → Task 2; §3 подстановка при рендере → Task 3; §4 тест на mounts → Task 4; §5 тестирование → распределено по задачам 1-4 (четыре класса покрыты: workspace и control-dir в Task 2 и 3, policy в Task 2, approval в Task 3 Step 5; отказ без классификации — Task 3 Step 1); §6 документация → Task 5. Пропусков нет.

**Согласованность типов:** `BoundaryKind` и `note_for` объявлены в Task 1 и используются в Task 2 и 3 под теми же именами. `ToolError(message, *, kind=...)` и `ToolResult.failure(error, *, boundary=...)` объявлены в Task 2 Step 3 и вызываются в Task 2 Step 4/9 и Task 3 Step 3 с теми же сигнатурами.

**Известные места, требующие сверки с кодом при исполнении** (помечены как «Реализующему»): фактическая точка перехвата `ToolError` (Task 2 Step 6); конфигурация deny-сценария политики (Task 2 Step 7); сценарий отказа approval (Task 3 Step 5); фактическая сигнатура `DockerEnvironment.__init__` (Task 4 Step 1).

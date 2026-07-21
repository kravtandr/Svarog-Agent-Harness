# Фиксы блокеров симуляции 21.07 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть блокеры симуляционной кампании 21.07.2026: эскалация вопросов агента пользователю (MCP-first), Flow C перестаёт проглатывать `svarog.yaml`, гейты до Flow C, честная память opencode через MCP, смена дефолтной модели, doctor-чистка сирот, валидатор base_url.

**Architecture:** Переиспользуем существующую механику моста (`ask_user`/`remember` уже реализованы в `bridge_control.py` с grace→suspend→resume): подключаем OpenCode к мосту через remote-MCP в managed `opencode.jsonc` и инструктируем оба адаптера звать `ask_user` вместо завершения run'а вопросом. Остальные фиксы — точечные (git-exclude, порядок гейтов, pydantic-валидатор, doctor-шаг).

**Tech Stack:** Python 3.12, uv, pytest, pydantic v2, typer, docker.

**Spec:** `docs/superpowers/specs/2026-07-21-sim-blockers-fix-design.md`

## Global Constraints

- Все команды из корня репо: `uv run pytest -q tests/<file>` (полный прогон — `uv run pytest -q`, ~2.5 мин).
- Комментарии и сообщения об ошибках — по-русски, в стиле окружающего кода (ссылки на ADR где уместно).
- Fail-closed: недопустимая конфигурация — ошибка ДО запуска ресурсов, не тихая деградация.
- Live-прогоны (Task 1, 9, 10) — ТОЛЬКО во временных каталогах (`mktemp -d`/scratchpad), реальный LLM платный, песочницу в репо не коммитить (`simulation/README.md` §6).
- Секреты: `~/.svarog/secrets.json` содержит `PROVIDER_API_KEY` (OpenRouter) и `CLAUDE_CODE_OAUTH_TOKEN`; значения не печатать и не коммитить.
- Стиль коммитов: `fix(scope): …` / `feat(scope): …` / `docs: …`, в конце тела:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Docker-образы уже собраны: `svarog/agent-opencode:latest`, `svarog/agent-claude:latest`.

---

### Task 1: Спайк — MCP-клиент OpenCode совместим с мостом Svarog (go/no-go)

Мост отдаёт MCP как streamable HTTP (`agent_infra.py:172`: `{"type": "http", "url": ".../svarog/mcp", "headers": {Authorization: Bearer …}}`). Гипотеза: OpenCode подключит его секцией `mcp.<name> = {type: "remote", url, headers, enabled}` в `~/.config/opencode/opencode.jsonc`. Комментарий «не совместим» в `opencode.py:41-42` — проверяемая гипотеза, не факт.

**Files:**
- Modify (ВРЕМЕННО, для спайка; откат после): `src/svarog_harness/runtime/agents/opencode.py`

**Interfaces:**
- Produces: вердикт go/no-go для Task 2–4. NO-GO → СТОП, доложить пользователю, предложить вариант B из спеки (эвристика `waiting_input`). Дальше по плану идти нельзя.

- [ ] **Step 1: Временный провод MCP в managed-конфиг**

В `OpencodeAdapter.provider_files` (opencode.py:74) спайково добавить в `config` перед `return` (url/token в спайке прочитать нечем — временно протащить через переменные окружения хоста, которые выставит шаг 2 из значений, напечатанных bridge'ом; проще: временно захардкодить чтение из файла `/tmp/svarog-spike-mcp.json`):

```python
        # SPIKE: не коммитить. Читаем url/token моста из файла, который
        # положил прогон-спайк (см. план Task 1 Step 2).
        import pathlib
        spike = pathlib.Path("/tmp/svarog-spike-mcp.json")
        if spike.exists():
            data = json.loads(spike.read_text())
            config["mcp"] = {
                "svarog": {
                    "type": "remote",
                    "url": data["url"],
                    "headers": {"Authorization": f"Bearer {data['token']}"},
                    "enabled": True,
                }
            }
```

И в `agent_infra.py::prepare_launch` (после построения `mcp_config`, строка ~185) спайково дописать сброс url/token в тот же файл — НО проще не трогать agent_infra: у opencode `capabilities().mcp == False`, ветка не выполняется. Вместо этого в спайке временно поменять `capabilities()` opencode на `mcp=True` (это заставит `prepare_launch` построить mcp.json — opencode его игнорирует в `command()`, безвредно) и добавить в `prepare_launch` сразу после `if self._adapter.capabilities().mcp:`:

```python
            # SPIKE: не коммитить.
            Path("/tmp/svarog-spike-mcp.json").write_text(json.dumps({
                "url": f"{self.agent_base_url()}/svarog/mcp",
                "token": self.bridge.token if self.bridge is not None else "",
            }))
```

ВНИМАНИЕ порядок: `provider_files` вызывается в `prepare_launch` РАНЬШЕ блока mcp — перенести спайковый сброс файла в начало `prepare_launch` (до `state_files = …`).

- [ ] **Step 2: Live-прогон спайка**

Развернуть среду по `simulation/README.md` §2 + конфиг группы cloud-executor (`simulation/scenarios.md`, секция «Cloud-executor»), adapter opencode. Прогнать:

```bash
cd "$SIM/ws" && HOME="$HOME" uv run --project "$REPO" svarog run --yolo \
  "Перечисли доступные тебе MCP-инструменты сервера svarog: только их имена списком."
```

Expected (GO): в финальном ответе/трейсе — имена tools моста (`remember`, `read_memory`, `read_skill`, `create_skill_proposal`, `ask_user`, `request_approval`). Затем второй прогон: `"Запомни: тестовый факт спайка"` → `git -C "$SIM/memory" log --oneline` показывает новый коммит `memory: …`.

Expected (NO-GO): агент не видит сервер / ошибка подключения в стриме OpenCode. Задокументировать точную ошибку (версия OpenCode, формат ответа) в спеке, СТОП.

- [ ] **Step 3: Откатить спайковые правки**

```bash
git checkout -- src/svarog_harness/runtime/agents/opencode.py src/svarog_harness/runtime/agent_infra.py
rm -f /tmp/svarog-spike-mcp.json
```

- [ ] **Step 4: Записать вердикт**

В `docs/superpowers/specs/2026-07-21-sim-blockers-fix-design.md` §1a дописать строку «Спайк <дата>: GO/NO-GO, <детали>». Commit: `docs: вердикт спайка opencode-mcp`.

---

### Task 2: Контракт адаптера `mcp_client_config` + провод MCP в opencode

**Files:**
- Modify: `src/svarog_harness/runtime/executor.py` (Protocol `AgentAdapter`, ~строка 104)
- Modify: `src/svarog_harness/runtime/agents/opencode.py` (capabilities:40, +метод)
- Modify: `src/svarog_harness/runtime/agents/claude_code.py` (+метод-заглушка)
- Modify: `src/svarog_harness/runtime/agents/codex.py` (+метод-заглушка)
- Modify: `src/svarog_harness/runtime/agent_infra.py` (`prepare_launch`, строки 156–195)
- Test: `tests/test_agent_adapters.py`

**Interfaces:**
- Consumes: `deep_merge(base, override)` из `svarog_harness.config.loader` (loader.py:25); `self.agent_base_url()` и `self.bridge.token` в `ExternalAgentInfra`.
- Produces: `AgentAdapter.mcp_client_config(url: str, token: str) -> dict[str, dict[str, Any]]` — JSON-патчи state-файлов (ключ — относительный путь в state volume); `OpencodeAdapter.capabilities()` → `AdapterCapabilities(hooks=False, resume=True, mcp=True)`.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_agent_adapters.py` добавить (импорты адаптеров там уже есть):

```python
def test_opencode_mcp_client_config_remote_section() -> None:
    adapter = OpencodeAdapter()
    patches = adapter.mcp_client_config("http://bridge:8080/svarog/mcp", "tok-1")
    section = patches[".config/opencode/opencode.jsonc"]["mcp"]["svarog"]
    assert section["type"] == "remote"
    assert section["url"] == "http://bridge:8080/svarog/mcp"
    assert section["headers"]["Authorization"] == "Bearer tok-1"
    assert section["enabled"] is True


def test_claude_and_codex_mcp_client_config_empty() -> None:
    # claude-code получает мост через --mcp-config, codex MCP не поддерживает.
    assert ClaudeCodeAdapter().mcp_client_config("http://x", "t") == {}
    assert CodexAdapter().mcp_client_config("http://x", "t") == {}
```

В `test_capability_matrix` (test_agent_adapters.py:239) поменять ожидание opencode на `mcp=True`.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/test_agent_adapters.py -k "mcp_client or capability"`
Expected: FAIL (`AttributeError: … has no attribute 'mcp_client_config'`).

- [ ] **Step 3: Реализация в адаптерах и Protocol**

`executor.py`, в Protocol `AgentAdapter` (рядом с `managed_policy`, строка ~162):

```python
    def mcp_client_config(self, url: str, token: str) -> dict[str, dict[str, Any]]:
        """JSON-патчи state-файлов для подключения MCP-клиента агента к мосту.

        Ключ — относительный путь state-файла, значение deep-merge'ится в его
        JSON. Пусто — адаптер берёт мост иначе (claude-code: --mcp-config)
        или MCP не поддерживает.
        """
        ...
```

`opencode.py`: `capabilities()` → `AdapterCapabilities(hooks=False, resume=True, mcp=True)`; комментарий на строках 41–42 заменить на «hooks: permission-хуков нет (supervised → fail-closed); MCP — remote-сервер моста в managed opencode.jsonc (спайк 2026-07-21)». Добавить метод:

```python
    def mcp_client_config(self, url: str, token: str) -> dict[str, dict[str, Any]]:
        """Мост Svarog как remote-MCP в managed-конфиге OpenCode."""
        return {
            ".config/opencode/opencode.jsonc": {
                "mcp": {
                    "svarog": {
                        "type": "remote",
                        "url": url,
                        "headers": {"Authorization": f"Bearer {token}"},
                        "enabled": True,
                    }
                }
            }
        }
```

`claude_code.py` и `codex.py` — одинаковая заглушка:

```python
    def mcp_client_config(self, url: str, token: str) -> dict[str, dict[str, Any]]:
        return {}
```

- [ ] **Step 4: Merge патчей в `prepare_launch`**

`agent_infra.py::prepare_launch`: после `state_files.update(self._adapter.provider_files(...))` (строка ~166) и ДО цикла записи файлов вставить:

```python
        if self._adapter.capabilities().mcp:
            mcp_url = f"{self.agent_base_url()}/svarog/mcp"
            mcp_token = self.bridge.token if self.bridge is not None else ""
            for rel, patch in self._adapter.mcp_client_config(mcp_url, mcp_token).items():
                base = json.loads(state_files[rel]) if rel in state_files else {}
                state_files[rel] = (
                    json.dumps(deep_merge(base, patch), ensure_ascii=False, indent=2) + "\n"
                )
```

Импорт: `from svarog_harness.config.loader import deep_merge`. Существующий блок построения `mcp.json` (строки 171–185) не трогать — opencode игнорирует `launch.mcp_config` в `command()`, для claude-code поведение прежнее.

- [ ] **Step 5: Прогнать тесты**

Run: `uv run pytest -q tests/test_agent_adapters.py tests/test_external_executor.py`
Expected: PASS (в т.ч. `test_opencode_provider_files_pin_chat_completions_provider` — provider-часть конфига не изменилась).

- [ ] **Step 6: Commit**

```bash
git add src/svarog_harness/runtime/executor.py src/svarog_harness/runtime/agents/ src/svarog_harness/runtime/agent_infra.py tests/test_agent_adapters.py
git commit -m "feat(executor): мост Svarog как remote-MCP для opencode"
```

---

### Task 3: Инструкции агентам — вопросы через ask_user, память только Svarog

**Files:**
- Modify: `src/svarog_harness/runtime/agents/opencode.py` (`context_files`, строки 63–72)
- Modify: `src/svarog_harness/runtime/agents/claude_code.py` (`context_files`, строки 101–120)
- Test: `tests/test_agent_adapters.py`

**Interfaces:**
- Consumes: `mcp=True` у opencode (Task 2) — иначе инструкция про `remember` лжива.
- Produces: тексты AGENTS.md/CLAUDE.md, которые проверяет live-перепрогон (Task 10).

- [ ] **Step 1: Падающие тесты**

```python
def test_opencode_context_steers_memory_and_questions_to_mcp() -> None:
    files = OpencodeAdapter().context_files("факт: кофе", "")
    text = files[".config/opencode/AGENTS.md"]
    assert "mcp" in text and "remember" in text
    assert "ask_user" in text
    assert "не завершай" in text.lower() or "никогда не завершай" in text.lower()


def test_claude_context_steers_questions_to_ask_user() -> None:
    files = ClaudeCodeAdapter().context_files("факт", "")
    assert "ask_user" in files["CLAUDE.md"]
```

Run: `uv run pytest -q tests/test_agent_adapters.py -k "context"` — Expected: два новых FAIL (существующий `test_claude_context_steers_memory_to_mcp` остаётся зелёным).

- [ ] **Step 2: Общий текст-константа и правка обоих `context_files`**

Чтобы не расходились формулировки (DRY), в `executor.py` рядом с `AgentLaunch` добавить:

```python
# Инструкция человеческих гейтов (§6.5): агент в headless не имеет stdin —
# вопрос обязан идти через мост, иначе run завершится вопросом в пустоту.
ASK_USER_GUIDE = (
    "# Вопросы человеку\n\n"
    "Нужен ответ или решение человека — вызывай MCP-tool `ask_user` и жди "
    "результата. НИКОГДА не завершай работу текстом-вопросом: в headless-режиме "
    "на него некому ответить, и задача будет засчитана проваленной. Для "
    "творческих задач предпочитай разумные дефолты; `ask_user` — только когда "
    "выбор реально блокирует работу."
)
```

`claude_code.py::context_files`: `sections.append(ASK_USER_GUIDE)` после блока «Память» (импортировать константу из `svarog_harness.runtime.executor`).

`opencode.py::context_files` переписать по образцу claude-code (блок «Память» — всегда, не только при непустом `memory`):

```python
    def context_files(self, memory: str, skill_cards: str) -> dict[str, str]:
        """Глобальные правила OpenCode: ~/.config/opencode/AGENTS.md."""
        sections: list[str] = []
        sections.append(
            "# Память\n\n"
            "Единственный источник истины по памяти — Svarog. Чтобы что-то "
            "запомнить между запусками, вызывай MCP-tool `mcp__svarog__remember` "
            "(прочитать — `mcp__svarog__read_memory`); НЕ пиши факты в файлы "
            "workspace и НЕ веди свою локальную память в ~/.local/share/opencode."
            + (f"\n\nТекущая память Svarog:\n\n{memory}" if memory else "")
        )
        sections.append(ASK_USER_GUIDE)
        if skill_cards:
            sections.append(f"# Скиллы Svarog\n\n{skill_cards}")
        return {".config/opencode/AGENTS.md": "\n\n".join(sections) + "\n"}
```

Примечание: имена MCP-tools глазами OpenCode проверить в Task 1 Step 2 (списком из спайка); если OpenCode показывает их как `svarog_remember`/`svarog.remember` — подставить фактические имена в текст.

- [ ] **Step 3: Прогнать тесты**

Run: `uv run pytest -q tests/test_agent_adapters.py`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/svarog_harness/runtime/executor.py src/svarog_harness/runtime/agents/ tests/test_agent_adapters.py
git commit -m "feat(agents): гайд ask_user и память Svarog в контексте обоих адаптеров"
```

---

### Task 4: CLI `svarog approvals answer <id> "<текст>"`

**Files:**
- Modify: `src/svarog_harness/cli/main.py` (рядом с командами `approvals approve/deny`; `approvals_app` объявлен на строке 147)
- Test: `tests/test_approval_flow.py` (паттерны CLI-тестов approvals уже там)

**Interfaces:**
- Consumes: `record_gate_answer(cfg, approval_id, answer, answered_by=...)` из `svarog_harness.cli.chat_engine` (chat_engine.py:67; префикс id резолвит сам через `find_approval_by_prefix`).
- Produces: команда `svarog approvals answer` для headless-ответа на `ask_user`.

- [ ] **Step 1: Падающий тест**

Найти в `tests/test_approval_flow.py` существующий CLI-тест approve/deny (`CliRunner`), скопировать его схему подготовки pending approval с `action_type="user.question"` и добавить:

```python
def test_cli_approvals_answer_records_answer(...) -> None:  # fixtures как у соседей
    result = runner.invoke(app, ["approvals", "answer", approval.id[:8], "жар-птица"])
    assert result.exit_code == 0
    # Ответ записан той же механикой, что проверяет TraceRecorder.answer_question:
    # перечитать approval из БД fetch'ем соседнего deny-теста и убедиться, что
    # состояние = answered, а текст ответа сохранён в payload/answer-поле
    # (точное имя поля — см. модель Approval в storage/models.py).
    stored = fetch_approval(approval.id)  # хелпер/паттерн соседнего deny-теста
    assert "жар-птица" in json.dumps(stored.payload, ensure_ascii=False)
```

(Точные fixtures/fetch взять из соседнего теста deny в том же файле — структура записи ответа видна в `TraceRecorder.answer_question`.)

Run: `uv run pytest -q tests/test_approval_flow.py -k answer` — Expected: FAIL (`No such command 'answer'`).

- [ ] **Step 2: Команда**

В `main.py` после команды deny:

```python
@approvals_app.command("answer")
def approvals_answer(
    approval_id: str = typer.Argument(..., help="ID вопроса (префикс достаточен)"),
    text: str = typer.Argument(..., help="Ответ человека"),
    workspace: Path = typer.Option(Path("."), "--workspace", "-w"),
) -> None:
    """Ответить на ask_user-вопрос агента (headless-путь §6.5)."""
    cfg = _load_cfg(workspace)  # тот же хелпер, что у approve/deny
    record_gate_answer(cfg, approval_id, text, answered_by="cli")
    console.print(f"ответ записан: {approval_id}; продолжить — svarog resume <run>")
```

(Сигнатуру опций `--workspace` и способ загрузки cfg скопировать 1:1 у соседней команды approve — не выдумывать свой.)

- [ ] **Step 3: Прогнать тесты**

Run: `uv run pytest -q tests/test_approval_flow.py`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/svarog_harness/cli/main.py tests/test_approval_flow.py
git commit -m "feat(cli): svarog approvals answer — headless-ответ на ask_user"
```

---

### Task 5: Flow C не коммитит `svarog.yaml`

**Files:**
- Modify: `src/svarog_harness/gitflow/workspace.py` (`commit_step`, строки 85–101)
- Test: `tests/test_workspace_flow.py` (если файла нет — тесты Flow C лежат в `tests/test_gitflow.py`; найти: `grep -rln commit_step tests/`)

**Interfaces:**
- Consumes: `GitRepo.ensure_excluded(pattern)` (repo.py:167) — пишет в `info/exclude`, НЕ трогает уже отслеживаемые файлы (если пользователь сам закоммитил свой svarog.yaml — поведение не меняется).
- Produces: авто-коммиты Flow C без untracked `svarog.yaml`.

- [ ] **Step 1: Падающий тест**

В файл с тестами Flow C (найден grep'ом) добавить async-тест по образцу соседей:

```python
async def test_commit_step_excludes_project_config(tmp_path: Path) -> None:
    # workspace: git-репо с коммитом + untracked svarog.yaml
    ws = tmp_path / "ws"
    ...  # init + первый коммит как в соседних тестах
    (ws / "svarog.yaml").write_text("models: {}\n")
    (ws / "result.md").write_text("работа\n")
    flow = WorkspaceFlow(GitRepo(ws), GitFlowConfig())
    await flow.start("задача")
    sha = await flow.commit_step("svarog: задача")
    assert sha is not None
    files = (await GitRepo(ws)._git("show", "--name-only", "--format=", sha))[1]
    assert "result.md" in files
    assert "svarog.yaml" not in files
    # рабочее дерево конфиг не потеряло
    assert (ws / "svarog.yaml").exists()
```

Run: `uv run pytest -q <файл> -k excludes_project_config` — Expected: FAIL (`svarog.yaml` в files).

- [ ] **Step 2: Фикс**

`workspace.py::commit_step` — рядом с существующим exclude:

```python
        await self._repo.ensure_excluded(".svarog/")
        # Project-конфиг (имена секретов!) не принадлежит диффу run'а: попав в
        # task-ветку, он к тому же исчезает из рабочего дерева при checkout
        # master (кампания 21.07.2026, S11 Watch(6)).
        await self._repo.ensure_excluded("svarog.yaml")
```

Импортировать `PROJECT_CONFIG_NAME` из `svarog_harness.config.loader` и использовать его вместо литерала.

- [ ] **Step 3: Прогнать тесты**

Run: `uv run pytest -q <файл тестов Flow C>` — Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/svarog_harness/gitflow/workspace.py tests/
git commit -m "fix(gitflow): не коммитить svarog.yaml в task-ветку (S11 Watch 6)"
```

---

### Task 6: Гейты внешнего агента ДО Flow C (S15a)

**Files:**
- Modify: `src/svarog_harness/runtime/orchestrator.py` (`run_once`, строки ~837–885)
- Test: `tests/test_external_executor.py` (fail-closed тесты уже там)

**Interfaces:**
- Consumes: `self.assert_external_autonomy_supported(autonomy)` (orchestrator.py:544) и `self.assert_sandbox_available()` — оба идемпотентны.
- Produces: при отказе гейта task-ветка `svarog/*` не создаётся, дерево остаётся на исходной ветке.

- [ ] **Step 1: Падающий тест**

В `tests/test_external_executor.py` (по образцу существующего supervised-fail-closed теста, там же взять фикстуры cfg/runner):

```python
async def test_autonomy_gate_fires_before_task_branch(db, tmp_path) -> None:
    ...  # cfg: executor external/opencode enforcement=containment, workspace-репо с коммитом
    with pytest.raises(SandboxError):
        await runner.run_once("задача", AutonomyMode.SUPERVISED, hooks=RunHooks())
    code, out, _ = await GitRepo(ws)._git("branch", "--list", "svarog/*")
    assert out.strip() == ""  # мусорной ветки нет
```

Run: `uv run pytest -q tests/test_external_executor.py -k before_task_branch` — Expected: FAIL (ветка создана).

- [ ] **Step 2: Фикс порядка в `run_once`**

В `run_once` сразу после `self._warn_layout_tradeoff(hooks)` (до `flow = WorkspaceFlow(...)`, строка ~865) вставить:

```python
        # Fail-closed гейты внешнего агента — ДО Flow C: отказ конфигурации не
        # должен оставлять мусорную task-ветку (S15a, кампания 21.07.2026).
        if self._cfg.executor.type == "external":
            self.assert_external_autonomy_supported(autonomy)
```

Поздние вызовы гейта (строки 883, 898) оставить — идемпотентны и прикрывают путь с `resources`.

- [ ] **Step 3: Прогнать тесты**

Run: `uv run pytest -q tests/test_external_executor.py tests/test_external_docker.py`
Expected: PASS (docker-тест требует docker; при недоступности — skip допустим).

- [ ] **Step 4: Commit**

```bash
git add src/svarog_harness/runtime/orchestrator.py tests/test_external_executor.py
git commit -m "fix(runtime): гейт автономии внешнего агента до Flow C (S15a)"
```

---

### Task 7: Валидатор base_url внешнего executor'а

**Files:**
- Modify: `src/svarog_harness/config/schema.py` (`ExternalExecutorConfig._check_auth`, строки ~383–397 — расширить или добавить второй validator)
- Test: `tests/test_external_executor.py` (рядом с `test_subscription_only_claude_code`, строка 155)

**Interfaces:**
- Produces: `ValidationError` на (а) wire=openai адаптер с anthropic-дефолтом base_url; (б) base_url с суффиксом `/v1` у external.

- [ ] **Step 1: Падающие тесты**

```python
def test_openai_wire_adapter_rejects_default_anthropic_base_url() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        ExternalExecutorConfig(adapter="opencode", image="img", api_key_ref="K")


def test_external_base_url_rejects_v1_suffix() -> None:
    with pytest.raises(ValidationError, match="/v1"):
        ExternalExecutorConfig(
            adapter="opencode", image="img", api_key_ref="K",
            base_url="https://openrouter.ai/api/v1", model="m",
        )


def test_claude_code_default_base_url_still_valid() -> None:
    ExternalExecutorConfig(adapter="claude-code", image="img", api_key_ref="K")
```

Run: `uv run pytest -q tests/test_external_executor.py -k base_url` — Expected: 2 FAIL, 1 PASS.

- [ ] **Step 2: Валидатор**

В `ExternalExecutorConfig` добавить (после `_check_auth`):

```python
    # Адаптеры с openai-совместимым LLM-трафиком (wire=openai): дефолтный
    # anthropic-endpoint для них — гарантированная ошибка в рантайме
    # («Invalid Anthropic API Key», найдено прогоном 21.07.2026).
    _OPENAI_WIRE_ADAPTERS = frozenset({"opencode", "codex"})

    @model_validator(mode="after")
    def _check_base_url(self) -> Self:
        if self.adapter in self._OPENAI_WIRE_ADAPTERS:
            if self.base_url == "https://api.anthropic.com":
                raise ValueError(
                    f"adapter='{self.adapter}' шлёт openai-трафик: задайте "
                    "executor.external.base_url вашего OpenAI-совместимого "
                    "провайдера (например https://openrouter.ai/api — БЕЗ /v1)"
                )
            if self.base_url.rstrip("/").endswith("/v1"):
                raise ValueError(
                    "executor.external.base_url задаётся БЕЗ суффикса /v1 — "
                    "адаптер добавляет его сам (в отличие от "
                    "models.providers.*.base_url, где /v1 нужен)"
                )
        return self
```

(pydantic: приватный атрибут класса объявить как `ClassVar[frozenset[str]]`, иначе pydantic посчитает его полем.)

- [ ] **Step 3: Прогнать тесты**

Run: `uv run pytest -q tests/test_external_executor.py tests/test_config.py tests/test_init_executor.py`
Expected: PASS (если генератор `svarog init` создаёт конфиг с дефолтным base_url для opencode — поправить генератор в `cli/init_executor.py` на явный base_url, тест покажет).

- [ ] **Step 4: Commit**

```bash
git add src/svarog_harness/config/schema.py tests/test_external_executor.py
git commit -m "feat(config): валидатор base_url для wire=openai адаптеров"
```

---

### Task 8: `svarog doctor` — чистка legacy-сирот docker

**Files:**
- Modify: `src/svarog_harness/cli/doctor.py` (+`_check_agent_orphans`, регистрация в `collect_checks`:38)
- Modify: `src/svarog_harness/cli/main.py` (`doctor`, строка 178: опция `--clean-orphans`)
- Test: `tests/test_cli_doctor.py`

**Interfaces:**
- Produces: `find_agent_orphans(run=subprocess.run) -> list[str]` и `remove_agent_orphans(names, networks, run=...) -> None` в `doctor.py`; сироты = ресурсы с label `svarog-agent=1`, у которых label `svarog-owner-pid` отсутствует ЛИБО указывает на мёртвый pid.

- [ ] **Step 1: Падающий тест**

В `tests/test_cli_doctor.py` (следовать паттерну соседних тестов; docker мокается инъекцией runner-callable):

```python
def test_find_agent_orphans_filters_dead_and_unlabeled() -> None:
    outputs = {
        # docker ps: имя\townerpid
        ("ps",): "svarog-old\t\nsvarog-live\t99999999\nsvarog-mine\t" + str(os.getpid()),
        ("network",): "svarog-net-old\t",
    }
    def fake_run(argv, **kw):
        key = ("network",) if "network" in argv else ("ps",)
        return subprocess.CompletedProcess(argv, 0, stdout=outputs[key], stderr="")
    containers, networks = find_agent_orphans(run=fake_run)
    assert containers == ["svarog-old", "svarog-live"]  # без label и мёртвый pid
    assert "svarog-mine" not in containers               # живой владелец
    assert networks == ["svarog-net-old"]
```

Run: `uv run pytest -q tests/test_cli_doctor.py -k orphans` — Expected: FAIL (нет функции).

- [ ] **Step 2: Реализация**

В `doctor.py`:

```python
def _pid_alive(pid: str) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return False
    return True


def find_agent_orphans(run=subprocess.run) -> tuple[list[str], list[str]]:
    """Ресурсы svarog-agent=1 без живого владельца (svarog-owner-pid).

    Ресурсы, созданные до появления reaper'а, метки owner не имеют и не
    подметаются никогда (кампания 21.07.2026: 4 legacy-сироты роняли
    test_external_docker) — их находит этот шаг.
    """
    fmt = "{{.Names}}\t{{.Label \"svarog-owner-pid\"}}"
    out = run(
        ["docker", "ps", "-a", "--filter", "label=svarog-agent=1", "--format", fmt],
        capture_output=True, text=True,
    ).stdout
    containers = [
        name for line in out.splitlines() if (name := line.split("\t")[0])
        and not _pid_alive(line.split("\t")[1] if "\t" in line else "")
    ]
    nfmt = "{{.Name}}\t{{index .Labels \"svarog-owner-pid\"}}"
    nout = run(
        ["docker", "network", "ls", "--filter", "label=svarog-agent=1", "--format", nfmt],
        capture_output=True, text=True,
    ).stdout
    networks = [
        name for line in nout.splitlines() if (name := line.split("\t")[0])
        and not _pid_alive(line.split("\t")[1] if "\t" in line else "")
    ]
    return containers, networks


def remove_agent_orphans(containers: list[str], networks: list[str], run=subprocess.run) -> None:
    if containers:
        run(["docker", "rm", "-f", *containers], capture_output=True, text=True)
    if networks:
        run(["docker", "network", "rm", *networks], capture_output=True, text=True)
```

В `collect_checks` добавить `DoctorCheck` со списком найденных сирот (status warn при непустом). В `main.py::doctor` добавить `clean_orphans: bool = typer.Option(False, "--clean-orphans")`: при флаге — `remove_agent_orphans(*find_agent_orphans())` с печатью удалённого; без флага — только показ и подсказка про флаг. Не звать docker, если `_check_sandbox` уже показал его отсутствие.

- [ ] **Step 3: Прогнать тесты**

Run: `uv run pytest -q tests/test_cli_doctor.py` — Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/svarog_harness/cli/doctor.py src/svarog_harness/cli/main.py tests/test_cli_doctor.py
git commit -m "feat(doctor): поиск и чистка legacy-сирот svarog-agent"
```

---

### Task 9: Bake-off дефолтной модели (S19-драйвер, live)

Дефект gpt-oss-120b (tool-call в reasoning-канал после refuel) — модельный; решение пользователя: сменить дефолт. `scaffold.py::DEFAULT_MODEL="qwen3-coder"` (localhost vLLM) НЕ трогаем — он про локальный серверинг; меняем рекомендованную OpenRouter-модель в симуляционных доках и, при желании пользователя, в собственном `~/.svarog`-конфиге.

**Files:**
- Modify: `simulation/README.md` (§2, `model: openai/gpt-oss-120b`)
- Modify: `simulation/scenarios.md` (секция «Cloud-executor», `model:` в примере executor)

**Interfaces:**
- Consumes: native-среда по `simulation/README.md` §2 с `runtime: {max_iterations: 6, refuel_after_iterations: 5, max_refuel_rounds: 4}`.
- Produces: имя модели-победителя для Task 10 (S19-перепрогон) и для доков.

- [ ] **Step 1: Кандидаты и цены**

Кандидаты (проверить доступность на OpenRouter на момент прогона; при недоступности заменить на ближайший аналог того же семейства): `qwen/qwen3-coder`, `deepseek/deepseek-chat`, `z-ai/glm-4.7`. Критерий отбора: openai-совместимый chat-completions, нормальный native tool-calling, цена сопоставима с gpt-oss-120b (< ~$1/Mtok out).

- [ ] **Step 2: Прогоны**

Для каждого кандидата: свежая native-среда (рецепт §2, `sandbox: local-trusted`), в `svarog.yaml` подставить кандидата, драйвер S19:

```bash
cd "$SIM/ws" && HOME="$HOME" uv run --project "$REPO" svarog run --yolo \
  "Создай четыре файла-раздела документации: intro.md, install.md, usage.md, faq.md — каждый с осмысленным содержимым про вымышленный CLI-инструмент taskman. Затем сведи их в index.md со ссылками на все четыре."
```

2 прогона на кандидата (между прогонами — сброс ws: checkout master, удалить task-ветки и созданные файлы; `svarog.yaml` после Task 5 больше не проглатывается).

Assert победителя: `completed` с непустым финальным ответом 2/2; все 5 файлов созданы; `Run.meta["refuel_rounds"] >= 1` хотя бы в одном прогоне (refuel реально прошли). Если ни один кандидат не дал 2/2 — взять лучшего и честно записать долю.

- [ ] **Step 3: Обновить доки**

В `simulation/README.md` §2 и `simulation/scenarios.md` (пример конфига executor) заменить `openai/gpt-oss-120b` на победителя; в scenarios.md в статус S19 дописать строку «Bake-off <дата>: победитель <model> (N/2), gpt-oss-120b оставлен поддержанным, не дефолтом».

- [ ] **Step 4: Commit**

```bash
git add simulation/README.md simulation/scenarios.md
git commit -m "docs(simulation): смена рекомендованной модели по bake-off S19"
```

---

### Task 10: Live-перепрогон сценариев до зелёного

**Files:**
- Modify: `simulation/scenarios.md` (статусы S11, S12, S13, S15, S19)

**Interfaces:**
- Consumes: все Task 1–9 влиты; образы docker; секреты; рецепт сред — `simulation/README.md` §2 + секция «Cloud-executor» scenarios.md (модель — победитель Task 9).

- [ ] **Step 1: Полный юнит-прогон**

Run: `uv run pytest -q`
Expected: 0 failed (перед стартом live-части; при сиротах docker — `svarog doctor --clean-orphans`).

- [ ] **Step 2: S11 — деливерабл с эскалацией (оба адаптера, P1+P5, по 2 прогона)**

Драйверы из кампании 21.07 (P1: «Разработай ТЗ… сохрани файлом…», P5: «Мне нужен документ… сохрани куда-нибудь»). PASS-критерий (новый, из спеки §1): либо `completed` + `.md`-файл в ws; либо `waiting_approval` с вопросом → `svarog approvals answer <id> "<ответ по личности>"` → `svarog resume <run>` → `completed` + файл. «completed без файла» = FAIL. Проверить также: в task-ветке НЕТ `svarog.yaml` (Task 5).

- [ ] **Step 3: S12 — память opencode через MCP (2 прогона chat)**

Драйвер кампании («Запомни: … / Что ты про меня знаешь?»). PASS: `git -C "$SIM/memory" log` содержит новый `memory:`-коммит; `svarog memory curate` без находок; ответ хода 1 не врёт (говорит, что сохранил, — и это теперь ПРАВДА). Watch: суррогаты в workspace = FAIL.

- [ ] **Step 4: S13 — кодовое слово opencode (2 прогона chat)**

Драйвер кампании («Кодовое слово сессии: жар-птица…»). PASS: a.md создан (ход 1 — сам или через эскалацию вопроса), слово названо, дописано в a.md, один `ses_…` на оба хода.

- [ ] **Step 5: S15 a/b/c — гейты (по 1 прогону)**

Как в кампании. Новый assert для (a): после отказа `git branch --list 'svarog/*'` ПУСТ (Task 6), `svarog.yaml` на месте.

- [ ] **Step 6: S19 — 2 прогона на модели-победителе**

Драйвер и настройки refuel как в Task 9. PASS: `completed` с финальным ответом 2/2, файлы на месте, refuel_rounds >= 1.

- [ ] **Step 7: Дымовые S14 и S16 (по 1 прогону)**

Как в кампании (S14 — через ChatEngine-скрипт, S16 — spawn_child). PASS: прежние assert'ы; цель — capabilities `mcp=True` у opencode ничего не сломала (bridge, budget, child-runs).

- [ ] **Step 8: Обновить статусы и добить регрессии**

В `simulation/scenarios.md` обновить блоки «Статус:» S11/S12/S13/S15/S19 датой и результатами. Каждый НОВЫЙ найденный баг → регрессионный сценарий (правило README §7) и, если фикс тривиален, — фикс + перепрогон до зелёного; если нет — зафиксировать в статусе и доложить.

- [ ] **Step 9: Commit**

```bash
git add simulation/scenarios.md
git commit -m "docs(simulation): статусы после перепрогона — блокеры закрыты"
```

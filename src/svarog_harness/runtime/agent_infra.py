"""Инфраструктура run'а внешнего агента (ADR-0016 §2-§6).

Собирает вокруг одного run: bridge-сервер (LLM-прокси + control-endpoints),
internal-сеть с relay-sidecar'ом (в docker-режиме), persistent agent-state
volume, файлы контекста и launch-файлы (MCP-конфиг, hook-скрипт,
managed-настройки — read-only mounts, агент их переписать не может).
TaskRunner создаёт инфраструктуру до старта sandbox-контейнера и разбирает
после.
"""

import asyncio
import json
import shutil
import uuid
from pathlib import Path

from svarog_harness.config.schema import ExternalExecutorConfig, RuntimeConfig
from svarog_harness.llm.openai_compatible import ApiKeyError
from svarog_harness.runtime.agents import CLIENT_GATE_TIMEOUT_MARGIN_SEC
from svarog_harness.runtime.bridge import (
    BridgeBudget,
    ControlHandler,
    RunBridge,
    UpstreamConfig,
)
from svarog_harness.runtime.executor import AgentAdapter, AgentAuth
from svarog_harness.sandbox.docker import find_docker
from svarog_harness.sandbox.reaper import reap_orphaned_agents
from svarog_harness.sandbox.relay import AgentNetwork
from svarog_harness.secrets import SecretStore

# Путь hook-скрипта в контейнере (ro-mount): managed-настройки агента
# ссылаются на него как на PreToolUse-команду (ADR-0016 §6).
_HOOK_CONTAINER_PATH = "/run/svarog/hook.py"
_MCP_CONTAINER_PATH = "/run/svarog/mcp.json"

# PreToolUse-мост: stdin (JSON вызова) → bridge /svarog/hook → решение.
# Fail-closed: bridge недоступен — deny. Требование к образу агента: python3.
_HOOK_SCRIPT = """\
import json, os, sys, urllib.request

payload = sys.stdin.read().encode()
url = os.environ["SVAROG_BRIDGE_URL"] + "/svarog/hook"
timeout = float(os.environ.get("SVAROG_HOOK_TIMEOUT", "180"))
request = urllib.request.Request(
    url,
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["SVAROG_BRIDGE_TOKEN"],
    },
)
try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        decision = json.load(response)
except Exception as exc:  # fail-closed: недоступный bridge = deny
    decision = {"decision": "deny", "reason": f"svarog bridge недоступен: {exc}"}
allowed = decision.get("decision") == "allow"
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allowed else "deny",
        "permissionDecisionReason": decision.get("reason", ""),
    }
}))
"""


class ExternalAgentInfra:
    """Жизненный цикл bridge + сеть + state volume одного run."""

    def __init__(
        self,
        external_cfg: ExternalExecutorConfig,
        runtime_cfg: RuntimeConfig,
        adapter: AgentAdapter,
        host_store: SecretStore,
        *,
        state_root: Path,
        docker_mode: bool,
        control_handlers: dict[str, ControlHandler] | None = None,
    ) -> None:
        self._external_cfg = external_cfg
        self._runtime_cfg = runtime_cfg
        self._adapter = adapter
        self._host_store = host_store
        # Agent-state volume (ADR-0016 §5): переживает контейнер, лежит в
        # control-plane (per-tenant автоматически), в git не попадает.
        self._state_dir = state_root / "agent-state" / adapter.name
        self._docker_mode = docker_mode
        self._control_handlers = control_handlers or {}
        self.bridge: RunBridge | None = None
        self._network: AgentNetwork | None = None
        # (host, container, ro) — дополнительные mounts контейнера агента.
        self._extra_mounts: list[tuple[Path, str, bool]] = []
        # Одноразовые launch-файлы run'а (MCP-конфиг, hook, managed) — вне
        # state volume, чтобы агент не мог их переписать (ro-mounts).
        self._launch_dir = state_root / "launch" / uuid.uuid4().hex[:8]
        # Пути launch-файлов с точки зрения агента (контейнер или хост).
        self.mcp_config_path: str | None = None
        self.settings_path: str | None = None
        # subscription: OAuth-токен подписки, уходит агенту (§3).
        self._subscription_token: str | None = None

    async def start(self) -> None:
        cfg = self._external_cfg
        api_key: str | None = None
        expected_bearer: str | None = None
        if cfg.auth == "subscription":
            # pass-through: агент аутентифицируется своим OAuth-токеном; прокси
            # его не инжектит, но авторизует LLM-путь сверкой с ним (§3).
            assert cfg.oauth_token_ref is not None  # гарантирует валидатор
            self._subscription_token = self._require_secret(
                cfg.oauth_token_ref, "executor.external.oauth_token_ref"
            )
            expected_bearer = self._subscription_token
        elif cfg.api_key_ref is not None:
            api_key = self._require_secret(cfg.api_key_ref, "executor.external.api_key_ref")
        self.bridge = RunBridge(
            upstream=UpstreamConfig(
                base_url=cfg.base_url,
                api_key=api_key,
                wire_format=self._adapter.wire_format,
                expected_bearer=expected_bearer,
            ),
            budget=BridgeBudget(
                max_tokens=self._runtime_cfg.max_tokens_per_run,
                max_cost_usd=self._runtime_cfg.max_cost_usd_per_run,
                input_usd_per_mtok=cfg.input_usd_per_mtok,
                output_usd_per_mtok=cfg.output_usd_per_mtok,
            ),
            loop=asyncio.get_running_loop(),
            control_handlers=self._control_handlers,
        )
        self.bridge.start()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._extra_mounts = [(self._state_dir, str(self._adapter.state_dir()), False)]
        if self._docker_mode:
            # Подмести ресурсы прошлых run'ов, чей родитель умер без teardown
            # (SIGKILL/OOM): контейнер+relay+сеть переживают процесс (ADR-0016 §2).
            docker = find_docker()
            if docker is not None:
                await reap_orphaned_agents(docker)
            self._network = AgentNetwork(relay_image=cfg.relay_image, bridge_port=self.bridge.port)
            await self._network.start()

    def _require_secret(self, ref: str, field: str) -> str:
        value = self._host_store.get(ref)
        if value is None:
            raise ApiKeyError(
                f"секрет '{ref}' ({field}) не найден в SecretStore/окружении: "
                "задайте `svarog secrets set` или env"
            )
        return value

    def prepare_launch(self, memory: str, skill_cards: str, *, cooperative: bool) -> None:
        """Файлы запуска агента (ADR-0016 §4/§6) — до старта контейнера.

        Контекст (CLAUDE.md) — в state volume (не в workspace: git-flow и
        verifier его не видят). MCP-конфиг, hook-скрипт и managed-настройки —
        одноразовые ro-mounts вне state volume: агент не может их переписать.
        """
        for rel, content in self._adapter.context_files(memory, skill_cards).items():
            target = self._state_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self._adapter.capabilities().mcp:
            mcp_config = {
                "mcpServers": {
                    "svarog": {
                        "type": "http",
                        "url": f"{self.agent_base_url()}/svarog/mcp",
                        "headers": {"Authorization": f"Bearer {self.bridge.token}"}
                        if self.bridge is not None
                        else {},
                    }
                }
            }
            self.mcp_config_path = self._add_launch_file(
                "mcp.json", json.dumps(mcp_config, ensure_ascii=False), _MCP_CONTAINER_PATH
            )
        if cooperative:
            hook_path = self._add_launch_file("hook.py", _HOOK_SCRIPT, _HOOK_CONTAINER_PATH)
            managed = self._adapter.managed_policy(self.mcp_config_path, f"python3 {hook_path}")
            managed_path = self._adapter.managed_policy_path()
            if managed is not None and managed_path is not None:
                self.settings_path = self._add_launch_file(
                    "managed-settings.json", managed, str(managed_path)
                )

    def _add_launch_file(self, name: str, content: str, container_path: str) -> str:
        """Записать launch-файл и вернуть его путь глазами агента."""
        self._launch_dir.mkdir(parents=True, exist_ok=True)
        host_path = self._launch_dir / name
        host_path.write_text(content, encoding="utf-8")
        if not self._docker_mode:
            # local-trusted (только тесты: боевой гейт требует docker) — агент
            # на хосте, пути хостовые.
            return str(host_path)
        self._extra_mounts.append((host_path, container_path, True))
        return container_path

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def network_name(self) -> str | None:
        return self._network.network if self._network is not None else None

    @property
    def extra_mounts(self) -> list[tuple[Path, str, bool]]:
        return list(self._extra_mounts)

    def agent_base_url(self) -> str:
        """URL bridge с точки зрения агента: relay в docker, loopback локально."""
        assert self.bridge is not None, "инфраструктура не запущена"
        if self._network is not None:
            return self._network.agent_base_url()
        return self.bridge.local_url()

    def agent_env(self) -> dict[str, str]:
        assert self.bridge is not None, "инфраструктура не запущена"
        auth = AgentAuth(
            base_url=self.agent_base_url(),
            proxy_token=self.bridge.token,
            mode=self._external_cfg.auth,
            credential=self._subscription_token or "",
        )
        env = self._adapter.base_url_env(auth)
        # Токен и URL bridge отдельными переменными — их читает hook-скрипт
        # (§6) и любой адаптер без специфичных env.
        env.setdefault("SVAROG_BRIDGE_URL", self.agent_base_url())
        env.setdefault("SVAROG_BRIDGE_TOKEN", self.bridge.token)
        # Клиентские таймауты человеческих гейтов (§7) — дольше grace, иначе
        # клиент агента бросает вызов до suspend и run завершается completed.
        gate_sec = self._external_cfg.approval_grace_sec + CLIENT_GATE_TIMEOUT_MARGIN_SEC
        env.setdefault("SVAROG_HOOK_TIMEOUT", str(gate_sec))
        env.setdefault("MCP_TOOL_TIMEOUT", str(gate_sec * 1000))  # Claude Code ждёт мс
        return env

    @property
    def adapter(self) -> AgentAdapter:
        return self._adapter

    async def stop(self) -> None:
        if self._network is not None:
            await self._network.stop()
            self._network = None
        if self.bridge is not None:
            self.bridge.stop()
        shutil.rmtree(self._launch_dir, ignore_errors=True)

"""Self-документация Svarog в sandbox внешнего агента.

Спека: docs/superpowers/specs/2026-07-22-self-docs-design.md
"""

from pathlib import Path

import pytest

from svarog_harness.config.schema import ExternalExecutorConfig, RuntimeConfig
from svarog_harness.runtime import agent_infra as agent_infra_mod
from svarog_harness.runtime import self_docs
from svarog_harness.runtime.agent_infra import ExternalAgentInfra
from svarog_harness.runtime.agents.claude_code import ClaudeCodeAdapter
from svarog_harness.runtime.agents.codex import CodexAdapter
from svarog_harness.runtime.agents.opencode import OpencodeAdapter
from svarog_harness.runtime.self_docs import (
    resolve_docs_root,
    self_docs_hint,
    stage_self_docs,
)
from svarog_harness.secrets import EnvSecretStore


def _fake_root(tmp_path: Path, *, with_agents: bool = True) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "README.md").write_text("# Svarog\n\nкоманды\n", encoding="utf-8")
    if with_agents:
        (root / "AGENTS.md").write_text("# правила\n", encoding="utf-8")
    (root / "docs" / "adr" / "0001-first.md").write_text(
        "\n# ADR-0001. Первое решение\n\ntext\n", encoding="utf-8"
    )
    (root / "docs" / "adr" / "0016-exec.md").write_text(
        "# ADR-0016. External Agent Executor\n", encoding="utf-8"
    )
    return root


def test_resolve_docs_root_finds_repo() -> None:
    root = resolve_docs_root()
    assert root is not None
    assert (root / "README.md").is_file()
    assert (root / "docs" / "adr").is_dir()


def test_stage_copies_docs_and_builds_index(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    dest = tmp_path / "staged"
    result = stage_self_docs(dest, root=root)
    assert result == dest
    assert (dest / "README.md").is_file()
    assert (dest / "AGENTS.md").is_file()
    assert (dest / "adr" / "0001-first.md").is_file()
    index = (dest / "INDEX.md").read_text(encoding="utf-8")
    assert "README.md" in index
    assert "AGENTS.md" in index
    # Заголовок ADR берётся из первой заголовочной строки.
    assert "ADR-0016. External Agent Executor" in index
    assert "adr/0016-exec.md" in index


def test_stage_without_agents_omits_it(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, with_agents=False)
    dest = tmp_path / "staged"
    stage_self_docs(dest, root=root)
    assert not (dest / "AGENTS.md").exists()
    assert "AGENTS.md" not in (dest / "INDEX.md").read_text(encoding="utf-8")


def test_stage_returns_none_when_no_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(self_docs, "resolve_docs_root", lambda: None)
    assert stage_self_docs(tmp_path / "staged") is None


def test_hint_mentions_index_and_path() -> None:
    hint = self_docs_hint("/opt/svarog-docs")
    assert "/opt/svarog-docs/INDEX.md" in hint
    assert "Svarog" in hint


# --- указатель в контекст-файлах адаптеров -----------------------------------

_ADAPTERS = [
    (ClaudeCodeAdapter, "CLAUDE.md"),
    (OpencodeAdapter, ".config/opencode/AGENTS.md"),
    (CodexAdapter, "AGENTS.md"),
]


@pytest.mark.parametrize("adapter_cls, ctx_file", _ADAPTERS)
def test_context_files_include_hint_when_path_given(adapter_cls, ctx_file) -> None:
    files = adapter_cls().context_files("mem", "", "/opt/svarog-docs")
    assert "/opt/svarog-docs/INDEX.md" in files[ctx_file]


@pytest.mark.parametrize("adapter_cls, ctx_file", _ADAPTERS)
def test_context_files_omit_hint_when_none(adapter_cls, ctx_file) -> None:
    files = adapter_cls().context_files("mem", "")
    joined = files.get(ctx_file, "")
    assert "svarog-docs" not in joined


# --- монтирование доков в prepare_launch -------------------------------------


def _codex_infra(tmp_path: Path, *, self_docs_on: bool = True) -> ExternalAgentInfra:
    # codex шлёт openai-трафик — валидатор требует явный base_url провайдера.
    cfg = ExternalExecutorConfig(
        image="img:1",
        adapter="codex",
        base_url="https://openrouter.ai/api",
        self_docs=self_docs_on,
    )
    return ExternalAgentInfra(
        cfg,
        RuntimeConfig(),
        CodexAdapter(),
        EnvSecretStore(),
        state_root=tmp_path / ".svarog",
        docker_mode=True,
    )


def test_config_self_docs_defaults_on() -> None:
    assert ExternalExecutorConfig(image="img:1").self_docs is True


def test_prepare_launch_mounts_self_docs(tmp_path: Path) -> None:
    infra = _codex_infra(tmp_path)
    infra.prepare_launch("mem", "", cooperative=False)
    mounts = {container: (host, ro) for host, container, ro in infra.extra_mounts}
    assert "/opt/svarog-docs" in mounts
    host, ro = mounts["/opt/svarog-docs"]
    assert ro is True
    assert (host / "INDEX.md").is_file()
    agents_md = infra.state_dir / "AGENTS.md"
    assert "/opt/svarog-docs/INDEX.md" in agents_md.read_text(encoding="utf-8")


def test_prepare_launch_self_docs_disabled(tmp_path: Path) -> None:
    infra = _codex_infra(tmp_path, self_docs_on=False)
    infra.prepare_launch("mem", "", cooperative=False)
    assert "/opt/svarog-docs" not in {c for _, c, _ in infra.extra_mounts}
    assert "svarog-docs" not in (infra.state_dir / "AGENTS.md").read_text(encoding="utf-8")


def test_prepare_launch_degrades_without_docs_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_infra_mod, "stage_self_docs", lambda dest: None)
    infra = _codex_infra(tmp_path)
    infra.prepare_launch("mem", "", cooperative=False)  # не должно падать
    assert "/opt/svarog-docs" not in {c for _, c, _ in infra.extra_mounts}
    assert "svarog-docs" not in (infra.state_dir / "AGENTS.md").read_text(encoding="utf-8")

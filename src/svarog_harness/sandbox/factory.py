"""Выбор sandbox-backend'а по конфигурации (§6.9)."""

from pathlib import Path

from svarog_harness.config.schema import SandboxConfig
from svarog_harness.sandbox.base import ExecutionEnvironment
from svarog_harness.sandbox.docker import DockerEnvironment
from svarog_harness.sandbox.local import LocalEnvironment


def create_environment(
    cfg: SandboxConfig,
    workspace: Path,
    *,
    skills_dir: Path | None = None,
    env: dict[str, str] | None = None,
    network: str | None = None,
    extra_mounts: list[tuple[Path, str, bool]] | None = None,
) -> ExecutionEnvironment:
    # network/extra_mounts — периметр внешнего агента (ADR-0016 §2/§5):
    # internal-сеть с relay и agent-state volume; для local-trusted не
    # применимы (процесс и так на хосте).
    if cfg.type == "local-trusted":
        return LocalEnvironment(workspace, env=env)
    return DockerEnvironment(
        workspace, cfg, skills_dir=skills_dir, env=env, network=network, extra_mounts=extra_mounts
    )

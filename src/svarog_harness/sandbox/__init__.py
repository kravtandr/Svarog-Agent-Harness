"""Sandbox: ExecutionEnvironment, docker и local-trusted backends (ADR-0002)."""

from svarog_harness.sandbox.base import ExecResult, ExecutionEnvironment, SandboxError
from svarog_harness.sandbox.docker import DockerEnvironment, find_docker
from svarog_harness.sandbox.factory import create_environment
from svarog_harness.sandbox.local import LocalEnvironment

__all__ = [
    "DockerEnvironment",
    "ExecResult",
    "ExecutionEnvironment",
    "LocalEnvironment",
    "SandboxError",
    "create_environment",
    "find_docker",
]

"""Git flows: память (A), скиллы (B), пользовательский код (C) (ADR-0003)."""

from svarog_harness.gitflow.commit_gate import (
    SecretScanBlockedError,
    commit_guarded,
    scan_ref,
    scan_staged,
)
from svarog_harness.gitflow.repo import GitError, GitRepo, separate_gitdir_for
from svarog_harness.gitflow.workspace import (
    WorkspaceFlow,
    WorkspacePrep,
    task_branch_name,
)

__all__ = [
    "GitError",
    "GitRepo",
    "SecretScanBlockedError",
    "WorkspaceFlow",
    "WorkspacePrep",
    "commit_guarded",
    "scan_ref",
    "scan_staged",
    "separate_gitdir_for",
    "task_branch_name",
]

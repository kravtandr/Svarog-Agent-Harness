"""`svarog install` — прописывает env + alias в shell rc и symlink на user-конфиг.

Чтобы `svarog chat`/`run` работали из любой папки (как `claude`/`codex`), нужен
alias на `uv --project <repo> run svarog`, env-переменные, прибивающие memory/
skills/db к agent-home, и user-level конфиг `~/.svarog/svarog.yaml` → agent-home.
Эта команда делает всё одной строкой вместо ручного heredoc'а в README.

Пишет только в rc-файл (bash/zsh) и в `~/.svarog/` — больше нигде.
"""

import os
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console

from svarog_harness.config.loader import USER_CONFIG_PATH

console = Console()

# Маркеры блока (как у pyenv/conda) — idempotent: повторный install замещает
# старый блок по этим тегам, не дублируя.
_BLOCK_BEGIN = "# >>> svarog >>>"
_BLOCK_END = "# <<< svarog <<<"

Shell = Literal["bash", "zsh"]


def render_rc_block(repo: Path, agent_home: Path) -> str:
    """Сгенерировать rc-блок с env-переменными и alias.

    Пути memory/skills/db — абсолютные (resolved на момент install); внутри
    alias оставлен `"$SVAROG_REPO"` — раскрывается самим shell при вызове, как
    и в ручной инструкции из README.
    """
    memory = (agent_home / "memory").as_posix()
    skills = (agent_home / "skills").as_posix()
    db = (agent_home / ".svarog" / "svarog.db").as_posix()
    repo_posix = repo.as_posix()
    home_posix = agent_home.as_posix()
    # SKILLS__PATHS — JSON-список (Pydantic BaseSettings парсит через env_nested_delimiter).
    skills_value = f'["{skills}"]'
    lines = [
        _BLOCK_BEGIN,
        f'export SVAROG_REPO="{repo_posix}"',
        f'export SVAROG_AGENT_HOME="{home_posix}"',
        f'export SVAROG_MEMORY__PATH="{memory}"',
        f'export SVAROG_SKILLS__PATHS="{skills_value}"',
        f'export SVAROG_STORAGE__DB_PATH="{db}"',
        "alias svarog='uv --project \"$SVAROG_REPO\" run svarog'",
        _BLOCK_END,
    ]
    return "\n".join(lines) + "\n"


def install_to_rc(rc_path: Path, block: str) -> bool:
    """Вписать/заместить svarog-блок в rc-файле. Вернуть True, если файл изменился.

    Idempotent: блок вырезается по маркерам `_BLOCK_BEGIN`/`_BLOCK_END` и
    заменяется на новый; если rc не содержал блока — дописывается в конец.
    """
    existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    new_content = _replace_block(existing, block)
    if new_content != existing:
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        rc_path.write_text(new_content, encoding="utf-8")
        return True
    return False


def _replace_block(content: str, block: str) -> str:
    """Вырезать старый svarog-блок (если есть) и приклеить новый в тот же конец."""
    begin_idx = content.find(_BLOCK_BEGIN)
    end_idx = content.find(_BLOCK_END)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        # вырезаем вместе с завершающим `\n` после маркера `_BLOCK_END`
        cut_end = end_idx + len(_BLOCK_END)
        if cut_end < len(content) and content[cut_end] == "\n":
            cut_end += 1
        before = content[:begin_idx]
        after = content[cut_end:]
        return (before + block + after).rstrip("\n") + "\n" if after else before + block
    # блока не было — дописать
    prefix = content
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix:
        prefix += "\n"
    return prefix + block


def link_user_config(agent_home: Path) -> tuple[bool, str]:
    """Создать symlink `~/.svarog/svarog.yaml` → agent-home/svarog.yaml.

    Возвращает (changed, reason):
    * (True, "linked") — symlink создан;
    * (False, "already-linked") — уже указывает туда же;
    * (False, "exists-regular") — по пути стоит regular file (например, после
      `svarog login`); symlink НЕ ставится, чтобы не затереть чужой конфиг.
    """
    target = USER_CONFIG_PATH.expanduser()
    source = agent_home / "svarog.yaml"
    if target.is_symlink() and str(target.readlink()) == str(source):
        return False, "already-linked"
    if target.exists() and not target.is_symlink():
        return False, "exists-regular"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()  # битый/иной symlink — перезаписываем
    target.symlink_to(source)
    return True, "linked"


def _detect_shell() -> Shell | None:
    """Угадать shell по $SHELL: */zsh → zsh, */bash → bash. None — не определилось."""
    shell = os.environ.get("SHELL", "")
    if shell.endswith("/zsh"):
        return "zsh"
    if shell.endswith("/bash"):
        return "bash"
    return None


def _rc_path(shell: Shell) -> Path:
    return Path("~/.zshrc" if shell == "zsh" else "~/.bashrc").expanduser()


def _detect_repo(agent_home: Path) -> Path | None:
    """Подняться от agent_home наверх, пока не найдём pyproject.toml репозитория."""
    current = agent_home.parent
    for _ in range(10):
        candidate = current / "pyproject.toml"
        if candidate.is_file():
            return current
        if current == current.parent:
            break
        current = current.parent
    return None


def install(
    agent_home: Annotated[
        Path | None,
        typer.Option("--agent-home", help="Каталог agent-home (по умолчанию ./agent-home)"),
    ] = None,
    repo: Annotated[
        Path | None,
        typer.Option(
            "--repo",
            help="Checkout Svarog (с pyproject.toml); по умолчанию — родитель agent-home",
        ),
    ] = None,
    shell: Annotated[
        str | None,
        typer.Option("--shell", help="Целевой rc: bash | zsh (по умолчанию авто по $SHELL)"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Перезаписать блок, даже если он не изменился"),
    ] = False,
    no_symlink: Annotated[
        bool, typer.Option("--no-symlink", help="Не создавать symlink на ~/.svarog/svarog.yaml")
    ] = False,
) -> None:
    """Прописать env + alias в shell rc и symlink на ~/.svarog/svarog.yaml.

    Делает runnable из любой папки: `svarog chat`/`run` работают как `claude`,
    без `cd agent-home`. Идемпотентно — повторный вызов обновляет блок.
    """
    home = (agent_home or Path.cwd() / "agent-home").expanduser().resolve()
    if not (home / "svarog.yaml").is_file():
        console.print(f"[red]{home}/svarog.yaml не найден — сначала `svarog init {home}`[/red]")
        raise typer.Exit(code=1)

    repo_resolved = repo.expanduser().resolve() if repo else _detect_repo(home)
    if repo_resolved is None or not (repo_resolved / "pyproject.toml").is_file():
        console.print(
            "[red]не удалось определить checkout Svarog (нет pyproject.toml над agent-home); "
            "укажите --repo[/red]"
        )
        raise typer.Exit(code=1)

    # 1) shell rc + alias
    if shell is not None:
        if shell not in ("bash", "zsh"):
            console.print(f"[red]--shell: ожидается bash|zsh, получено {shell!r}[/red]")
            raise typer.Exit(code=1)
        chosen: Shell = shell  # type: ignore[assignment]
    else:
        detected = _detect_shell()
        if detected is None:
            console.print(
                "[red]не удалось определить shell по $SHELL — укажите --shell bash|zsh[/red]"
            )
            raise typer.Exit(code=1)
        chosen = detected

    rc = _rc_path(chosen)
    block = render_rc_block(repo_resolved, home)
    changed = install_to_rc(rc, block)
    if changed:
        console.print(f"[green]+[/green] блок svarog записан в [dim]{rc}[/dim]")
    elif force:
        rc.write_text(_replace_block(rc.read_text(encoding="utf-8"), block), encoding="utf-8")
        console.print(f"[green]~[/green] блок svarog переписан в [dim]{rc}[/dim] (--force)")
    else:
        console.print(
            f"[dim]блок в {rc} уже актуален (используйте --force, чтобы переписать)[/dim]"
        )

    # 2) symlink на user-конфиг
    if not no_symlink:
        _, reason = link_user_config(home)
        if reason == "linked":
            console.print(
                f"[green]+[/green] symlink [dim]{USER_CONFIG_PATH}[/dim] → "
                f"[dim]{home / 'svarog.yaml'}[/dim]"
            )
        elif reason == "already-linked":
            console.print(f"[dim]symlink {USER_CONFIG_PATH} уже указывает на agent-home[/dim]")
        elif reason == "exists-regular":
            console.print(
                f"[yellow]! {USER_CONFIG_PATH} уже существует как файл (например, после "
                "`svarog login`). Symlink не создан. Перенесите его содержимое в "
                f"{home / 'svarog.yaml'} и удалите, либо --no-symlink[/yellow]"
            )

    console.print(
        f"\n[green]готово[/green]. Перезагрузите shell (`exec {chosen}` или новый терминал), "
        "затем `svarog chat` из любой папки."
    )

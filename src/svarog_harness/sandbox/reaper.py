"""Garbage collection осиротевших sandbox-ресурсов внешнего агента (ADR-0016 §2).

Контейнер агента (`sleep infinity`), relay-sidecar (`serve_forever`) и
internal-сеть переживают контейнер по построению — их жизнь завершает только
явный `stop()`. Если родительский процесс `svarog` умирает без finally
(SIGKILL, OOM, `kill -9`), стоп не вызывается и ресурсы остаются жить —
«осиротевший контейнер». Гарантировать teardown на SIGKILL нельзя в принципе,
поэтому ресурсы помечаются PID владельца и **подметаются** следующим внешним
run'ом: dead owner → reap.

Критерий безопасен: reap'ается только ресурс, чей PID-владелец мёртв. Живой
конкурентный run (его PID жив) не трогается; PID-reuse ведёт максимум к
пропуску подметания (лишний orphan подождёт), но никогда — к сносу живого.
"""

import asyncio
import json
import os
from pathlib import Path

_LABEL = "svarog-agent=1"
_PID_LABEL = "svarog-owner-pid"
_BOOT_LABEL = "svarog-owner-boot"


def _boot_token() -> str:
    """Идентификатор загрузки хоста: ресурс из прошлой загрузки — гарантированно
    orphan (его PID-пространство больше не существует). Linux — boot_id; на macOS
    недоступно → пустой токен, полагаемся на проверку живости PID."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def owner_label_args() -> list[str]:
    """`--label` аргументы владельца для `docker run` / `network create`."""
    return [
        "--label",
        _LABEL,
        "--label",
        f"{_PID_LABEL}={os.getpid()}",
        "--label",
        f"{_BOOT_LABEL}={_boot_token()}",
    ]


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с данным PID (в текущем PID-пространстве хоста)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс есть, но не наш — считаем живым (не подметаем).
        return True
    return True


def _is_orphan(labels: dict[str, str]) -> bool:
    """Ресурс осиротел: другая загрузка хоста, либо PID-владелец мёртв."""
    pid_raw = labels.get(_PID_LABEL)
    if pid_raw is None:
        # Нет метки владельца — не наш формат/не можем судить: не трогаем.
        return False
    boot = labels.get(_BOOT_LABEL, "")
    current_boot = _boot_token()
    if boot and current_boot and boot != current_boot:
        return True
    try:
        pid = int(pid_raw)
    except ValueError:
        return False
    return not _pid_alive(pid)


async def _run(docker: str, argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        docker, *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def _labels_of(docker: str, argv: list[str]) -> dict[str, str]:
    """Прочитать labels ресурса; `argv` — inspect-команда с `{{json …Labels}}`."""
    code, out = await _run(docker, argv)
    text = out.strip()
    if code != 0 or not text or text == "null":
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def reap_orphaned_agents(docker: str) -> int:
    """Снести sandbox-ресурсы (`svarog-agent=1`) с мёртвым PID-владельцем.

    Порядок: сначала контейнеры (агент + relay), затем сети (сеть нельзя удалить,
    пока к ней подключён контейнер). Возвращает число снятых ресурсов.
    """
    reaped = 0
    code, out = await _run(docker, ["ps", "-aq", "--filter", f"label={_LABEL}"])
    if code == 0:
        for cid in filter(None, (line.strip() for line in out.splitlines())):
            labels = await _labels_of(
                docker, ["inspect", "--format", "{{json .Config.Labels}}", cid]
            )
            if _is_orphan(labels):
                rc, _ = await _run(docker, ["rm", "-f", cid])
                reaped += rc == 0
    code, out = await _run(docker, ["network", "ls", "-q", "--filter", f"label={_LABEL}"])
    if code == 0:
        for nid in filter(None, (line.strip() for line in out.splitlines())):
            labels = await _labels_of(
                docker, ["network", "inspect", "--format", "{{json .Labels}}", nid]
            )
            if _is_orphan(labels):
                rc, _ = await _run(docker, ["network", "rm", nid])
                reaped += rc == 0
    return reaped

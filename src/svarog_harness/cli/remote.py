"""Thin CLI cloud-режима (ADR-0017 §3): `svarog remote …` и `svarog login`.

Тонкий маппинг 1:1 на REST/NDJSON gateway. Инвариант remote purity: команды
не открывают локальную БД, не пишут в локальную память и не запускают
локальный TaskRunner — httpx + user-конфиг + user-level SecretStore, больше
ничего. Профиль подключения живёт в `~/.svarog/svarog.yaml` (секция
`remote:`), токен — в user-level SecretStore под именем `remote.token_ref`.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
import yaml
from rich.console import Console
from rich.table import Table

from svarog_harness.config.loader import USER_CONFIG_PATH
from svarog_harness.secrets import default_secret_store
from svarog_harness.secrets.store import FileSecretStore

console = Console()

remote_app = typer.Typer(help="Cloud-режим: команды исполняет удалённый svarog serve (ADR-0017).")

_DEFAULT_TOKEN_REF = "svarog_remote_token"
_USER_SECRETS_PATH = Path("~/.svarog/secrets.json")


class RemoteError(Exception):
    """Ошибка remote-вызова: сетевая или HTTP с detail сервера."""


@dataclass
class RemoteClient:
    """HTTP-клиент gateway; client_factory подменяется в тестах (TestClient)."""

    base_url: str
    token: str | None = None
    client_factory: Callable[[], httpx.Client] | None = None

    def _client(self, *, timeout: float | None = 30.0) -> httpx.Client:
        if self.client_factory is not None:
            return self.client_factory()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            with self._client() as client:
                resp = client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RemoteError(f"gateway недоступен ({self.base_url}): {exc}") from None
        if resp.status_code >= 400:
            raise RemoteError(_detail(resp))
        if not resp.content:
            return None  # 204 и пустые ответы (заголовок json может стоять и там)
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.content

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._request(method, path, **kwargs))

    def _json_list(self, method: str, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self._request(method, path, **kwargs))

    # --- runs ---
    def create_run(
        self,
        task: str,
        *,
        autonomy: str | None = None,
        repo_url: str | None = None,
        ref: str | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"task": task}
        if autonomy:
            payload["autonomy"] = autonomy
        if repo_url:
            payload["repo"] = {"url": repo_url, **({"ref": ref} if ref else {})}
        if workspace:
            payload["workspace"] = workspace
        return self._json("POST", "/runs", json=payload)

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._json("GET", f"/runs/{run_id}")

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._json_list("GET", "/runs", params={"limit": limit})

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._json("POST", f"/runs/{run_id}/resume")

    def cancel(self, run_id: str) -> dict[str, Any]:
        return self._json("POST", f"/runs/{run_id}/cancel")

    def diff(self, run_id: str) -> dict[str, Any]:
        return self._json("GET", f"/runs/{run_id}/diff")

    def stream_events(self, run_id: str) -> Iterator[dict[str, Any]]:
        """Живые события run'а (NDJSON); завершается на run_finished."""
        try:
            with (
                self._client(timeout=None) as client,
                client.stream("GET", f"/runs/{run_id}/events/stream") as resp,
            ):
                if resp.status_code >= 400:
                    resp.read()
                    raise RemoteError(_detail(resp))
                for line in resp.iter_lines():
                    if line.strip():
                        yield json.loads(line)
        except httpx.HTTPError as exc:
            raise RemoteError(f"стрим оборвался: {exc}") from None

    # --- approvals ---
    def approvals(self) -> list[dict[str, Any]]:
        return self._json_list("GET", "/approvals")

    def decide(self, approval_id: str, *, approved: bool, reason: str | None) -> dict[str, Any]:
        return self._json(
            "POST", f"/approvals/{approval_id}", json={"approved": approved, "reason": reason}
        )

    def answer(self, approval_id: str, text: str) -> dict[str, Any]:
        return self._json("POST", f"/approvals/{approval_id}/answer", json={"answer": text})

    # --- разное ---
    def skills(self) -> list[dict[str, Any]]:
        return self._json_list("GET", "/skills")

    def whoami(self) -> dict[str, Any]:
        return self._json("GET", "/whoami")

    # --- workspaces ---
    def workspaces(self) -> list[dict[str, Any]]:
        return self._json_list("GET", "/workspaces")

    def workspace_create(self, name: str) -> dict[str, Any]:
        return self._json("POST", "/workspaces", json={"name": name})

    def workspace_rm(self, name: str) -> None:
        self._request("DELETE", f"/workspaces/{name}")

    def workspace_files(self, name: str, path: str = ".") -> Any:
        return self._request("GET", f"/workspaces/{name}/files", params={"path": path})

    def workspace_archive(self, name: str) -> bytes:
        data = self._request("GET", f"/workspaces/{name}/archive")
        assert isinstance(data, bytes)
        return data

    # --- сессии ---
    def session_create(
        self, *, title: str = "", repo_url: str | None = None, workspace: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title}
        if repo_url:
            payload["repo"] = {"url": repo_url}
        if workspace:
            payload["workspace"] = workspace
        return self._json("POST", "/sessions", json=payload)

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        return self._json("POST", f"/sessions/{session_id}/messages", json={"text": text})


def _detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail")
    except (json.JSONDecodeError, ValueError):
        detail = None
    return f"HTTP {resp.status_code}: {detail or resp.text[:200]}"


# --- профиль подключения ---------------------------------------------------


def load_remote_client() -> RemoteClient:
    """Клиент из user-профиля (`remote:` в ~/.svarog/svarog.yaml).

    Читаем raw-yaml, а не load_config: remote-клиенту не нужны models/секции
    локального агента (remote purity) — и профиль должен работать на машине
    без какого-либо локального agent-home.
    """
    path = USER_CONFIG_PATH.expanduser()
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    remote = data.get("remote") or {}
    url = remote.get("url")
    if not url:
        raise RemoteError(
            f"remote-профиль не настроен: выполните `svarog login <url>` "
            f"(секция remote в {USER_CONFIG_PATH})"
        )
    token_ref = remote.get("token_ref", _DEFAULT_TOKEN_REF)
    token = default_secret_store(_USER_SECRETS_PATH).get(token_ref)
    return RemoteClient(base_url=url, token=token)


def save_remote_profile(
    url: str, token: str | None, *, token_ref: str = _DEFAULT_TOKEN_REF
) -> None:
    """Записать секцию remote в user-конфиг; токен — в user-level SecretStore."""
    path = USER_CONFIG_PATH.expanduser()
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["remote"] = {"url": url.rstrip("/"), "token_ref": token_ref}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if token:
        # Пишущий store — файловый (0600); default_secret_store слоёный и read-only.
        FileSecretStore(_USER_SECRETS_PATH.expanduser()).set(token_ref, token)


# --- рендеринг -------------------------------------------------------------


def _attach(client: RemoteClient, run_id: str) -> str:
    """Стримить события run'а в консоль; вернуть финальное состояние."""
    state = "unknown"
    for event in client.stream_events(run_id):
        kind = event.get("type")
        if kind == "text":
            console.print(event.get("delta", ""), end="", markup=False, highlight=False)
        elif kind == "tool_call":
            console.print(f"\n[dim]⚙ {event.get('tool')}[/dim]")
        elif kind == "notify":
            console.print(f"\n[yellow]⚠ {event.get('tool')}: {event.get('reason')}[/yellow]")
        elif kind == "check":
            console.print(f"[dim]✓ {event.get('name')}: {event.get('status')}[/dim]")
        elif kind == "commit":
            console.print(f"[dim]⎇ commit {event.get('sha')} ({event.get('branch')})[/dim]")
        elif kind == "run_finished":
            state = str(event.get("state"))
            console.print()
    return state


def _finish_summary(client: RemoteClient, run_id: str, state: str) -> None:
    color = {"completed": "green", "failed": "red", "cancelled": "yellow"}.get(state, "yellow")
    console.print(f"[{color}]run {run_id[:8]} → {state}[/{color}]")
    if state == "waiting_approval":
        for a in client.approvals():
            if a["run_id"] == run_id:
                console.print(
                    f"[yellow]approval {a['approval_id'][:8]}[/yellow] "
                    f"{a['action_type']}: {a.get('payload')}\n"
                    f"[dim]svarog remote approve {a['approval_id'][:8]} | "
                    f"deny {a['approval_id'][:8]}[/dim]"
                )


def _print_runs(runs: list[dict[str, Any]]) -> None:
    table = Table(header_style="bold")
    for col in ("run", "state", "task", "iter", "cost $"):
        table.add_column(col)
    for r in runs:
        table.add_row(
            r["run_id"][:8],
            r["state"],
            (r["task"][:60] + "…") if len(r["task"]) > 60 else r["task"],
            str(r["iterations"]),
            f"{r['cost_usd']:.4f}",
        )
    console.print(table)


def _fail(exc: RemoteError) -> None:
    console.print(f"[red]{exc}[/red]")
    raise typer.Exit(code=1)


# --- команды ---------------------------------------------------------------


def login(
    url: Annotated[str, typer.Argument(help="URL gateway, например https://svarog.team.example")],
    token: Annotated[
        str | None, typer.Option("--token", help="Bearer/JWT; без флага — скрытый ввод")
    ] = None,
) -> None:
    """Сохранить remote-профиль (~/.svarog/svarog.yaml) и токен (SecretStore)."""
    if token is None:
        token = typer.prompt("Токен (пусто — без auth)", default="", hide_input=True) or None
    save_remote_profile(url, token)
    client = RemoteClient(base_url=url.rstrip("/"), token=token)
    try:
        who = client.whoami()
        console.print(
            f"[green]подключено[/green] {url} — тенант "
            f"[bold]{who['tenant_id']}[/bold] ({who['role']})"
        )
    except RemoteError as exc:
        console.print(f"[yellow]профиль сохранён, но проверка не прошла:[/yellow] {exc}")


@remote_app.command("whoami")
def remote_whoami() -> None:
    """Идентичность и usage тенанта на сервере."""
    try:
        who = load_remote_client().whoami()
    except RemoteError as exc:
        _fail(exc)
        return
    console.print(
        f"тенант [bold]{who['tenant_id']}[/bold] ({who['role']}) — "
        f"активных runs: {who['active_runs']}, cost ${who['total_cost_usd']:.4f}, "
        f"tokens {who['total_tokens']}"
    )


@remote_app.command("run")
def remote_run(
    task: Annotated[str, typer.Argument(help="Задача для агента")],
    repo: Annotated[str | None, typer.Option("--repo", help="Git URL (клон на сервере)")] = None,
    ref: Annotated[str | None, typer.Option("--ref", help="Ветка/тег клона")] = None,
    workspace: Annotated[
        str | None, typer.Option("--workspace", "-w", help="Имя named workspace")
    ] = None,
    autonomy: Annotated[str | None, typer.Option("--autonomy", help="yolo|auto|supervised")] = None,
    attach: Annotated[bool, typer.Option("--attach/--no-attach", help="Стримить события")] = True,
) -> None:
    """Запустить run на сервере: git-клон, named workspace или workspace сервиса."""
    try:
        client = load_remote_client()
        ref_view = client.create_run(
            task, autonomy=autonomy, repo_url=repo, ref=ref, workspace=workspace
        )
        run_id = ref_view["run_id"]
        console.print(f"[dim]run {run_id[:8]} запущен[/dim]")
        state = _attach(client, run_id) if attach else ref_view["state"]
        if attach:
            _finish_summary(client, run_id, state)
    except RemoteError as exc:
        _fail(exc)


@remote_app.command("resume")
def remote_resume(
    run_id: Annotated[str, typer.Argument(help="ID/префикс run")],
    attach: Annotated[bool, typer.Option("--attach/--no-attach")] = True,
) -> None:
    """Возобновить suspended-run на сервере."""
    try:
        client = load_remote_client()
        client.resume(run_id)
        if attach:
            _finish_summary(client, run_id, _attach(client, run_id))
    except RemoteError as exc:
        _fail(exc)


@remote_app.command("cancel")
def remote_cancel(run_id: Annotated[str, typer.Argument(help="ID/префикс run")]) -> None:
    """Отменить run (cooperative: живая нога завершится на границе итерации)."""
    try:
        view = load_remote_client().cancel(run_id)
    except RemoteError as exc:
        _fail(exc)
        return
    console.print(f"run {view['run_id'][:8]} → {view['state']}")


@remote_app.command("runs")
def remote_runs(limit: Annotated[int, typer.Option("--limit")] = 20) -> None:
    """Runs тенанта на сервере (свежие сверху)."""
    try:
        _print_runs(load_remote_client().list_runs(limit))
    except RemoteError as exc:
        _fail(exc)


@remote_app.command("show")
def remote_show(
    run_id: Annotated[str, typer.Argument(help="ID/префикс run")],
    diff: Annotated[bool, typer.Option("--diff", help="Показать diff run'а")] = False,
) -> None:
    """Детали run'а; --diff — патч его коммитов и незакоммиченного."""
    try:
        client = load_remote_client()
        detail = client.get_run(run_id)
        console.print_json(json.dumps(detail, ensure_ascii=False))
        if diff:
            d = client.diff(run_id)
            for label, patch in (("committed", d["committed"]), ("uncommitted", d["uncommitted"])):
                if patch:
                    console.rule(label)
                    console.print(patch, markup=False, highlight=False)
    except RemoteError as exc:
        _fail(exc)


@remote_app.command("approvals")
def remote_approvals() -> None:
    """Pending approvals тенанта."""
    try:
        pending = load_remote_client().approvals()
    except RemoteError as exc:
        _fail(exc)
        return
    if not pending:
        console.print("[dim]pending approvals нет[/dim]")
        return
    for a in pending:
        console.print(
            f"[yellow]{a['approval_id'][:8]}[/yellow] run={a['run_id'][:8]} "
            f"{a['action_type']}: {a.get('payload')}"
        )


@remote_app.command("approve")
def remote_approve(
    approval_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Одобрить approval; run возобновится на сервере."""
    _decide(approval_id, approved=True, reason=reason)


@remote_app.command("deny")
def remote_deny(
    approval_id: Annotated[str, typer.Argument()],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Отклонить approval; run возобновится с отказом."""
    _decide(approval_id, approved=False, reason=reason)


def _decide(approval_id: str, *, approved: bool, reason: str | None) -> None:
    try:
        ref = load_remote_client().decide(approval_id, approved=approved, reason=reason)
    except RemoteError as exc:
        _fail(exc)
        return
    verdict = "одобрен" if approved else "отклонён"
    console.print(f"approval {verdict}, run {ref['run_id'][:8]} возобновляется")


@remote_app.command("skills")
def remote_skills() -> None:
    """Скиллы тенанта на сервере."""
    try:
        cards = load_remote_client().skills()
    except RemoteError as exc:
        _fail(exc)
        return
    for card in cards:
        console.print(f"[bold]{card['name']}[/bold] v{card['version']} — {card['description']}")


workspace_app = typer.Typer(help="Named workspaces на сервере (ADR-0017 §1).")
remote_app.add_typer(workspace_app, name="workspace")


@workspace_app.command("create")
def ws_create(name: Annotated[str, typer.Argument(help="Слаг [a-z0-9-]")]) -> None:
    """Создать постоянный named workspace."""
    try:
        load_remote_client().workspace_create(name)
    except RemoteError as exc:
        _fail(exc)
        return
    console.print(f"[green]workspace '{name}' создан[/green]")


@workspace_app.command("list")
def ws_list() -> None:
    """Named workspaces тенанта."""
    try:
        items = load_remote_client().workspaces()
    except RemoteError as exc:
        _fail(exc)
        return
    if not items:
        console.print("[dim]named workspaces нет[/dim]")
        return
    table = Table(header_style="bold")
    for col in ("name", "size", "modified", "busy"):
        table.add_column(col)
    for w in items:
        table.add_row(
            w["name"],
            _human_size(w["size_bytes"]),
            w["modified_at"],
            "●" if w["busy"] else "",
        )
    console.print(table)


@workspace_app.command("rm")
def ws_rm(name: Annotated[str, typer.Argument()]) -> None:
    """Удалить named workspace (отказ, если занят run'ом)."""
    try:
        load_remote_client().workspace_rm(name)
    except RemoteError as exc:
        _fail(exc)
        return
    console.print(f"workspace '{name}' удалён")


@workspace_app.command("pull")
def ws_pull(
    name: Annotated[str, typer.Argument()],
    path: Annotated[
        str | None, typer.Argument(help="Файл внутри workspace; без него — tar.gz архив")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Куда сохранить")] = None,
) -> None:
    """Забрать результаты: файл или архив workspace."""
    try:
        client = load_remote_client()
        if path is None:
            data = client.workspace_archive(name)
            target = out or Path(f"{name}.tar.gz")
        else:
            data = client.workspace_files(name, path)
            if not isinstance(data, bytes):
                console.print_json(json.dumps(data, ensure_ascii=False))  # листинг каталога
                return
            target = out or Path(Path(path).name)
        target.write_bytes(data)
        console.print(f"[green]сохранено[/green] {target} ({_human_size(len(data))})")
    except RemoteError as exc:
        _fail(exc)


@remote_app.command("chat")
def remote_chat(
    workspace: Annotated[
        str | None, typer.Option("--workspace", "-w", help="Named workspace сессии")
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Git URL (клон на сессию)")] = None,
) -> None:
    """Интерактивный чат: каждое сообщение — run на сервере в workspace сессии."""
    try:
        client = load_remote_client()
        session = client.session_create(title="remote-chat", repo_url=repo, workspace=workspace)
    except RemoteError as exc:
        _fail(exc)
        return
    sid = session["session_id"]
    console.print(
        f"[dim]сессия {sid[:8]} | workspace: {session.get('workspace')} | /quit — выход[/dim]"
    )
    while True:
        try:
            text = typer.prompt("›", prompt_suffix=" ").strip()
        except (EOFError, KeyboardInterrupt, typer.Abort):
            break
        if not text or text in {"/quit", "/exit"}:
            break
        try:
            run_id = client.send_message(sid, text)["run_id"]
            state = _attach(client, run_id)
            _finish_summary(client, run_id, state)
        except RemoteError as exc:
            console.print(f"[red]{exc}[/red]")


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


__all__ = [
    "RemoteClient",
    "RemoteError",
    "load_remote_client",
    "login",
    "remote_app",
    "save_remote_profile",
]

"""Bridge-сервер run'а (ADR-0016 §3/§4): LLM-прокси + control-endpoints.

Один host-side HTTP-сервер на run внешнего агента, три роли:

* **LLM-прокси** — агент ходит на bridge вместо endpoint'а провайдера;
  ключ провайдера подставляется здесь (host-side) и в sandbox не попадает
  никогда; usage считается по телам ответов провайдера (единственный
  источник истины бюджетов) — превышение отвечает 429, run уходит в
  suspended (§3).
* **MCP-сервер** (`/svarog/mcp`, фаза 2) — «обратные» инструменты Svarog
  (remember, read_memory, read_skill, create_skill_proposal, ask_user,
  request_approval) под тем же governance, что у нативного loop.
* **Hook-endpoint** (`/svarog/hook`, фаза 3) — PreToolUse-мост к Policy
  Engine: allow / deny / approval c grace period → suspend.

Аутентификация: per-run bearer-токен. Агент получает его как «свой API-ключ»
(x-api-key / Authorization) — прокси меняет его на настоящий upstream-ключ.
Сервер — stdlib ThreadingHTTPServer в фоновом потоке; асинхронные операции
Svarog (память, approvals, policy) исполняются в event loop процесса через
run_coroutine_threadsafe.
"""

import asyncio
import contextlib
import json
import secrets
import socketserver
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx


class QuietHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer без reverse-DNS при bind.

    Стандартный HTTPServer.server_bind зовёт socket.getfqdn() — на хостах с
    кривым reverse-DNS (типичный macOS) это блокирует запуск на десятки
    секунд; имя сервера bridge'у не нужно.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


# Заголовки hop-by-hop и авторизация не форвардятся upstream как есть.
_STRIP_REQUEST_HEADERS = frozenset(
    {"host", "authorization", "x-api-key", "content-length", "connection", "accept-encoding"}
)
_STRIP_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "content-encoding"}
)

_BUDGET_MESSAGE = (
    "бюджет run исчерпан (enforcement на LLM-прокси, ADR-0016 §3): "
    "run будет приостановлен; поднимите лимит и выполните svarog resume"
)


@dataclass
class BridgeUsage:
    """Счётчики прокси — источник истины бюджетов (ADR-0016 §3)."""

    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    budget_exceeded: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class BridgeBudget:
    """Лимиты run'а + цены для пересчёта в стоимость (0 у цены — не считаем)."""

    max_tokens: int
    max_cost_usd: float
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0

    def cost_usd(self, usage: BridgeUsage) -> float:
        return (
            usage.input_tokens * self.input_usd_per_mtok
            + usage.output_tokens * self.output_usd_per_mtok
        ) / 1_000_000


# Control-обработчики (фазы 2-3): (path-суффикс, JSON-тело) → JSON-ответ.
# Исполняются в event loop процесса (не в потоке HTTP-сервера).
ControlHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]


@dataclass
class UpstreamConfig:
    """Куда и с каким ключом форвардить LLM-трафик."""

    base_url: str
    api_key: str | None
    # Формат метеринга: заголовки авторизации и парсер usage.
    wire_format: str = "anthropic"  # anthropic | openai
    timeout_sec: float = 600.0


@dataclass
class RunBridge:
    """Жизненный цикл bridge-сервера одного run."""

    upstream: UpstreamConfig
    budget: BridgeBudget
    loop: asyncio.AbstractEventLoop
    control_handlers: dict[str, ControlHandler] = field(default_factory=dict)
    token: str = field(default_factory=lambda: f"svarog-run-{secrets.token_urlsafe(24)}")
    usage: BridgeUsage = field(default_factory=BridgeUsage)

    _server: QuietHTTPServer | None = None
    _thread: threading.Thread | None = None
    _client: httpx.Client | None = None

    def start(self) -> None:
        """Поднять сервер на 0.0.0.0:<случайный порт> (токен обязателен)."""
        self._client = httpx.Client(timeout=self.upstream.timeout_sec)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: object) -> None:
                pass  # не шумим в stderr процесса

            def do_GET(self) -> None:
                self._dispatch()

            def do_POST(self) -> None:
                self._dispatch()

            def _dispatch(self) -> None:
                # BrokenPipe: клиент ушёл — не ошибка.
                with contextlib.suppress(BrokenPipeError):
                    bridge._handle(self)

        # 0.0.0.0: из контейнера хост достижим только через relay/gateway,
        # каждый запрос требует per-run токен (ADR-0016 §4).
        self._server = QuietHTTPServer(("0.0.0.0", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="svarog-bridge", daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        assert self._server is not None, "bridge не запущен"
        return self._server.server_address[1]

    def local_url(self) -> str:
        """URL для процессов на хосте (local-trusted тесты, сам executor)."""
        return f"http://127.0.0.1:{self.port}"

    def cost_usd(self) -> float:
        return self.budget.cost_usd(self.usage)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._client is not None:
            self._client.close()
            self._client = None

    # --- обработка запросов (в потоке HTTP-сервера) ---

    def _handle(self, req: BaseHTTPRequestHandler) -> None:
        if not self._authorized(req):
            _respond_json(req, 401, {"error": "bridge: неверный или отсутствующий run-токен"})
            return
        if req.path.startswith("/svarog/"):
            self._handle_control(req)
            return
        self._handle_proxy(req)

    def _authorized(self, req: BaseHTTPRequestHandler) -> bool:
        header = req.headers.get("x-api-key") or ""
        bearer = req.headers.get("authorization") or ""
        if bearer.lower().startswith("bearer "):
            bearer = bearer[7:]
        return secrets.compare_digest(header, self.token) or secrets.compare_digest(
            bearer, self.token
        )

    def _handle_control(self, req: BaseHTTPRequestHandler) -> None:
        name = req.path.removeprefix("/svarog/").split("?", 1)[0]
        handler = self.control_handlers.get(name)
        if handler is None:
            _respond_json(req, 404, {"error": f"неизвестный bridge-endpoint: {name}"})
            return
        payload = _read_json_body(req)
        future: Future[dict[str, Any]] = asyncio.run_coroutine_threadsafe(
            handler(payload), self.loop
        )
        try:
            result = future.result(timeout=self.upstream.timeout_sec)
        except Exception as exc:  # ошибки control-слоя — JSON, не 500-трейс
            _respond_json(req, 500, {"error": str(exc)})
            return
        _respond_json(req, int(result.pop("_status", 200)), result)

    def _handle_proxy(self, req: BaseHTTPRequestHandler) -> None:
        assert self._client is not None
        if self._over_budget():
            self.usage.budget_exceeded = True
            _respond_json(req, 429, {"type": "error", "error": {"message": _BUDGET_MESSAGE}})
            return
        body = _read_body(req)
        headers = {k: v for k, v in req.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS}
        if self.upstream.api_key is not None:
            # Инжекция ключа host-side (ADR-0016 §3): в sandbox его нет.
            if self.upstream.wire_format == "anthropic":
                headers["x-api-key"] = self.upstream.api_key
            else:
                headers["Authorization"] = f"Bearer {self.upstream.api_key}"
        url = self.upstream.base_url.rstrip("/") + req.path
        try:
            upstream_req = self._client.build_request(
                req.command, url, headers=headers, content=body
            )
            upstream = self._client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            _respond_json(req, 502, {"error": f"upstream недоступен: {exc}"})
            return
        try:
            self._relay_response(req, upstream)
        finally:
            upstream.close()
        self.usage.requests += 1

    def _relay_response(self, req: BaseHTTPRequestHandler, upstream: httpx.Response) -> None:
        meter = _UsageMeter(self.upstream.wire_format)
        content_type = upstream.headers.get("content-type", "")
        req.send_response(upstream.status_code)
        for key, value in upstream.headers.items():
            if key.lower() not in _STRIP_RESPONSE_HEADERS:
                req.send_header(key, value)
        streaming = "text/event-stream" in content_type
        if streaming:
            req.send_header("Transfer-Encoding", "chunked")
            req.end_headers()
            for chunk in upstream.iter_bytes():
                meter.feed(chunk)
                req.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                req.wfile.flush()
            req.wfile.write(b"0\r\n\r\n")
        else:
            payload = upstream.read()
            meter.feed(payload)
            req.send_header("Content-Length", str(len(payload)))
            req.end_headers()
            req.wfile.write(payload)
        meter.finish()
        self.usage.input_tokens += meter.input_tokens
        self.usage.output_tokens += meter.output_tokens
        if self._over_budget():
            self.usage.budget_exceeded = True

    def _over_budget(self) -> bool:
        if self.usage.total_tokens > self.budget.max_tokens:
            return True
        return self.budget.cost_usd(self.usage) > self.budget.max_cost_usd


class _UsageMeter:
    """Парсер usage из тел ответов провайдера (anthropic / openai).

    SSE-стрим: скармливаются чанки, разбираются `data: {...}`-события.
    Обычный JSON: одно тело целиком. Незнакомые формы игнорируются —
    метеринг консервативен (недосчитать хуже, чем упасть: бюджет добьёт
    следующий запрос).
    """

    def __init__(self, wire_format: str) -> None:
        self._format = wire_format
        self._buffer = b""
        self._pending_output = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk

    def finish(self) -> None:
        text = self._buffer.decode(errors="replace")
        if "data:" in text:
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    self._consume(json.loads(data))
                except json.JSONDecodeError:
                    continue
        else:
            try:
                self._consume(json.loads(text))
            except json.JSONDecodeError:
                return
        self.output_tokens += self._pending_output
        self._pending_output = 0

    def _consume(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        if self._format == "anthropic":
            self._consume_anthropic(event)
        else:
            self._consume_openai(event)

    def _consume_anthropic(self, event: dict[str, Any]) -> None:
        match event.get("type"):
            case "message":  # не-стриминговый ответ Messages API
                self._add(event.get("usage"), "input_tokens", "output_tokens")
            case "message_start":
                message = event.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if isinstance(usage, dict):
                    self.input_tokens += _as_int(usage.get("input_tokens"))
                    self._pending_output = _as_int(usage.get("output_tokens"))
            case "message_delta":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    # output_tokens в delta — кумулятивный по сообщению.
                    self._pending_output = max(
                        self._pending_output, _as_int(usage.get("output_tokens"))
                    )
            case "message_stop":
                self.output_tokens += self._pending_output
                self._pending_output = 0
            case _:
                pass

    def _consume_openai(self, event: dict[str, Any]) -> None:
        usage = event.get("usage")
        if isinstance(usage, dict):
            if "prompt_tokens" in usage:  # chat.completions
                self._add(usage, "prompt_tokens", "completion_tokens", nested=False)
            elif "input_tokens" in usage:  # responses API
                self._add(usage, "input_tokens", "output_tokens", nested=False)
        response = event.get("response")  # responses API: response.completed
        if isinstance(response, dict):
            self._add(response.get("usage"), "input_tokens", "output_tokens")

    def _add(self, usage: object, in_key: str, out_key: str, *, nested: bool = True) -> None:
        if not isinstance(usage, dict):
            return
        self.input_tokens += _as_int(usage.get(in_key))
        self.output_tokens += _as_int(usage.get(out_key))


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _read_body(req: BaseHTTPRequestHandler) -> bytes:
    length = int(req.headers.get("content-length") or 0)
    return req.rfile.read(length) if length > 0 else b""


def _read_json_body(req: BaseHTTPRequestHandler) -> dict[str, Any]:
    body = _read_body(req)
    if not body:
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _respond_json(req: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req.send_response(status)
    req.send_header("Content-Type", "application/json")
    req.send_header("Content-Length", str(len(body)))
    req.end_headers()
    req.wfile.write(body)

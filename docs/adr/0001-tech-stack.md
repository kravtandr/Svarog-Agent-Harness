# ADR-0001: Язык и технологический стек

## Статус

Принято (стек Python подтвержден пользователем 2026-07-08)

## Контекст

ТЗ (TASK.md) не фиксирует язык и стек, при этом от выбора зависит всё: структура репозитория, экосистема интеграций, целевая аудитория контрибьюторов. Реальные кандидаты — Python и TypeScript.

Аргументы за Python:

* примеры в самом ТЗ Python-центричны: FastAPI, скрипты скиллов (`plan_deploy.py`, `test_skill.py`), сценарии для ML/DevOps-команд;
* официальный MCP Python SDK;
* зрелая экосистема LLM-клиентов (openai, litellm) и Docker SDK;
* целевая аудитория (ML/DevOps/corporate) преимущественно пишет на Python.

Аргументы за TypeScript: лучший streaming/async DX, единый язык с будущим Web UI. Отклонено: Web UI — поздняя фаза и в любом случае общается с backend через REST/WS, единый язык не обязателен.

## Решение

**Python 3.12+.** Конкретный стек:

| Область | Выбор |
|---|---|
| Пакетный менеджер / сборка | uv + hatchling |
| CLI | Typer + Rich |
| Модели данных, валидация, config | Pydantic v2 + pydantic-settings |
| Async | asyncio, httpx |
| LLM-клиент | openai SDK против OpenAI-compatible endpoints (покрывает LiteLLM, vLLM, Ollama, OpenRouter); интерфейс `ModelProvider` для остальных |
| БД | SQLite через SQLAlchemy 2.0 (async) + Alembic; тот же код работает на Postgres |
| Sandbox | docker (Docker SDK for Python) |
| Git | тонкая subprocess-обертка над системным `git` (не GitPython: надежность, полнота поведения) |
| MCP (пост-MVP) | официальный `mcp` SDK |
| Gateway (пост-MVP) | FastAPI + uvicorn, WebSocket |
| Качество | pytest, ruff (lint + format), mypy strict |

## Последствия

* Один язык во всем репозитории до появления Web UI.
* Web UI (поздняя фаза) — отдельное frontend-приложение поверх REST/WebSocket API.
* Скрипты скиллов остаются свободными по языку (bash, python) — они исполняются в sandbox, а не импортируются.

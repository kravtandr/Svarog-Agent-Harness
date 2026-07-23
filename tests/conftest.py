"""Общие тестовые хелперы и изоляция окружения."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_svarog_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Убрать SVAROG_* из окружения на время каждого теста.

    Конфигурация — pydantic BaseSettings, то есть читает переменные окружения.
    После `svarog install` в шелле разработчика живут SVAROG_AGENT_HOME,
    SVAROG_SKILLS__PATHS, SVAROG_STORAGE__DB_PATH и SVAROG_MEMORY__PATH, и
    `load_config` внутри теста возвращал их вместо tmp-путей: тест думал, что
    работает в песочнице, а на деле указывал на реальные каталоги и БД
    разработчика. На CI этих переменных нет, поэтому расхождение видно только
    локально — и именно через него каталог скиллов однажды указал внутрь
    рабочего репозитория, где proposal смёл незакоммиченные правки.

    Тест, которому переменная нужна, ставит её сам через monkeypatch — autouse
    отрабатывает до тела теста и его setenv не отменяет.
    """
    for name in [name for name in os.environ if name.startswith("SVAROG_")]:
        monkeypatch.delenv(name, raising=False)


def tmp_workspace() -> Path:
    """Одноразовый workspace для file-tools в тестах реестра.

    Конструкторам file-tools (ReadFileTool, ListDirTool, ...) достаточно
    валидного Path — они не трогают диск, пока не вызван execute(), поэтому
    фикстура pytest не нужна.
    """
    return Path("workspace")

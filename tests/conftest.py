"""Общие тестовые хелперы для нескольких файлов тестов."""

from pathlib import Path


def tmp_workspace() -> Path:
    """Одноразовый workspace для file-tools в тестах реестра.

    Конструкторам file-tools (ReadFileTool, ListDirTool, ...) достаточно
    валидного Path — они не трогают диск, пока не вызван execute(), поэтому
    фикстура pytest не нужна.
    """
    return Path("workspace")

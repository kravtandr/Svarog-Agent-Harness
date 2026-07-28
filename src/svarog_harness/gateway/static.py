"""Поиск собранного клиента и его раздача из gateway.

Бандл едет внутри пакета (собирается в CI), но при разработке лежит в
`web/dist` рядом с исходниками — ищем оба места, чтобы `svarog serve`
поднимал интерфейс и из чекаута, и из установленного колеса.
"""

import os
from pathlib import Path


def web_dist_dir() -> Path | None:
    """Каталог собранного клиента или None, если бандла нет."""
    override = os.environ.get("SVAROG_WEB_DIST")
    if override:
        candidate = Path(override)
        return candidate if (candidate / "index.html").is_file() else None

    packaged = Path(__file__).resolve().parent / "web"
    if (packaged / "index.html").is_file():
        return packaged

    # Чекаут репозитория: src/svarog_harness/gateway → корень → web/dist
    checkout = Path(__file__).resolve().parents[3] / "web" / "dist"
    return checkout if (checkout / "index.html").is_file() else None

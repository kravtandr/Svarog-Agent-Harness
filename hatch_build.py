"""Сборочный хук hatchling: бандл клиента внутрь колеса — если он собран.

Статический `force-include` в pyproject не подходит: hatchling считает его
жёстким требованием и падает с FileNotFoundError, когда источника нет. А
`web/dist` не в git и появляется только после `npm --prefix web run build`,
которого нет ни на чистом чекауте, ни в CI до шага сборки клиента. Поэтому
включение объявляем здесь — динамически и только при наличии бандла.
"""

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# Куда бандл едет внутри пакета; этот же путь ищет gateway/static.py.
PACKAGED_WEB = "svarog_harness/gateway/web"


def bundle_force_include(root: Path, version: str) -> dict[str, str]:
    """Карта force-include для собранного клиента (пустая, если включать нечего).

    В editable-установке возвращаем пустую карту: снимок бандла в site-packages
    перекрывал бы живой `web/dist` в чекауте (см. порядок поиска в
    gateway/static.py) и устаревал бы после каждой пересборки клиента.
    """
    if version == "editable":
        return {}
    dist = Path(root) / "web" / "dist"
    if not (dist / "index.html").is_file():
        return {}
    return {str(dist): PACKAGED_WEB}


class CustomBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    """Подмешивает бандл в build_data колеса."""

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        build_data["force_include"].update(bundle_force_include(Path(self.root), version))

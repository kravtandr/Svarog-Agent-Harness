"""Тесты сборочного хука: бандл клиента едет в колесо, только если он собран."""

from pathlib import Path

from hatch_build import PACKAGED_WEB, bundle_force_include


def test_bundle_skipped_when_not_built(tmp_path: Path) -> None:
    """Нет web/dist (чистый чекаут, CI до `npm run build`) — сборка не падает."""
    assert bundle_force_include(tmp_path, "standard") == {}


def test_bundle_skipped_when_dist_has_no_index(tmp_path: Path) -> None:
    """Пустой/недособранный web/dist не считается бандлом — как и в static.py."""
    (tmp_path / "web" / "dist" / "assets").mkdir(parents=True)

    assert bundle_force_include(tmp_path, "standard") == {}


def test_bundle_mapped_into_package_when_built(tmp_path: Path) -> None:
    dist = _build_bundle(tmp_path)

    assert bundle_force_include(tmp_path, "standard") == {str(dist): PACKAGED_WEB}


def test_bundle_not_baked_into_editable_install(tmp_path: Path) -> None:
    """В editable-установке бандл не копируем: gateway/static.py сам найдёт
    живой `web/dist` в чекауте, а снимок внутри пакета перекрывал бы его и
    устаревал после каждой пересборки клиента."""
    _build_bundle(tmp_path)

    assert bundle_force_include(tmp_path, "editable") == {}


def _build_bundle(root: Path) -> Path:
    dist = root / "web" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return dist

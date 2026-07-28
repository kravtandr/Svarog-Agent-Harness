"""Вложения к сообщению: запись, лимиты, исключение из git (план 2026-07-28)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import create_app
from svarog_harness.gateway.attachments import (
    ALLOWED_SUFFIXES,
    MAX_UPLOAD_BYTES,
    AttachmentTooLarge,
    AttachmentTypeError,
    ensure_git_excluded,
    safe_name,
    store_attachment,
)
from svarog_harness.gitflow import GitRepo


def _write_config(ws: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "state" / "svarog.db"
    (ws / "svarog.yaml").write_text(
        "models:\n"
        "  default: local\n"
        "  providers:\n"
        "    local:\n"
        "      base_url: http://localhost:9/v1\n"
        "      model: fake-model\n"
        "sandbox:\n  type: local-trusted\n"
        f"storage:\n  db_path: {db_path}\n",
        encoding="utf-8",
    )


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GatewayService:
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_config(ws, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return GatewayService(load_config(project_dir=ws), ws)


@pytest.mark.asyncio
async def test_exclude_lands_in_separate_git_dir(tmp_path: Path) -> None:
    """При separate_git_dir исключение пишется в настоящий git-каталог,
    а не в workspace/.git (там просто файл-указатель)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    gitdir = tmp_path / "gitdir"
    repo = GitRepo(ws)
    await repo.init(separate_git_dir=gitdir)

    await ensure_git_excluded(ws, ".attachments/")

    exclude = gitdir / "info" / "exclude"
    assert exclude.is_file()
    assert ".attachments/" in exclude.read_text(encoding="utf-8").splitlines()


@pytest.mark.asyncio
async def test_exclude_is_written_once(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()

    await ensure_git_excluded(ws, ".attachments/")
    await ensure_git_excluded(ws, ".attachments/")

    exclude = ws / ".git" / "info" / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines.count(".attachments/") == 1, "повторный вызов не дублирует строку"


@pytest.mark.asyncio
async def test_exclude_is_noop_without_git(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    await ensure_git_excluded(ws, ".attachments/")  # не падает


@pytest.mark.asyncio
async def test_exclude_not_fooled_by_substring_match(tmp_path: Path) -> None:
    """`.attachments/` — подстрока `.attachments-old/`, но не та же строка;
    должна быть дописана отдельной строкой, а не принята за уже имеющуюся."""
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()
    exclude = ws / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".attachments-old/\n", encoding="utf-8")

    await ensure_git_excluded(ws, ".attachments/")

    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert ".attachments-old/" in lines
    assert ".attachments/" in lines


# --- приём вложения (задача 7) ----------------------------------------------


def test_allowed_suffixes_are_built_from_tools_not_copied() -> None:
    from svarog_harness.tools.document_tools import _DOCUMENT_SUFFIXES, _IMAGE_MIME

    assert set(_IMAGE_MIME) <= ALLOWED_SUFFIXES
    assert set(_DOCUMENT_SUFFIXES) <= ALLOWED_SUFFIXES
    assert ".txt" in ALLOWED_SUFFIXES and ".md" in ALLOWED_SUFFIXES


@pytest.mark.parametrize(
    "raw",
    ["../../etc/passwd", "foo/../../bar.png", "/абсолютный.png", "..\\win.png"],
)
def test_safe_name_strips_directories(raw: str) -> None:
    assert "/" not in safe_name(raw) and "\\" not in safe_name(raw)
    assert not safe_name(raw).startswith("..")


@pytest.mark.parametrize("raw", ["", ".", "..", "...", "..png", "   "])
def test_safe_name_never_empty_or_bare_dots(raw: str) -> None:
    """Пустая строка, одни точки или пробелы не должны давать пустое имя —
    иначе итоговый файл в .attachments/ называется как хеш с подчёркиванием
    без ничего осмысленного после."""
    name = safe_name(raw)
    assert name
    assert "/" not in name and "\\" not in name


def test_safe_name_is_idempotent_on_a_clean_name() -> None:
    assert safe_name("скрин бага.png") == "скрин бага.png"


@pytest.mark.asyncio
async def test_store_puts_file_in_attachments_and_keeps_original_name(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # b"\x89PNG данные" из брифа не компилируется: байтовый литерал не может
    # содержать не-ASCII символы исходника напрямую (SyntaxError). Смысл тот
    # же — не-ASCII содержимое в бинарных данных — записан валидным способом.
    content = b"\x89PNG" + "данные".encode()
    stored = await store_attachment(ws, "скрин бага.png", content)

    assert stored.name == "скрин бага.png"
    assert stored.path.startswith(".attachments/")
    assert (ws / stored.path).read_bytes() == content
    assert (ws / stored.path).resolve().is_relative_to((ws / ".attachments").resolve())


@pytest.mark.asyncio
async def test_same_name_twice_gives_two_files(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    first = await store_attachment(ws, "скрин.png", b"1")
    second = await store_attachment(ws, "скрин.png", b"2")

    assert first.path != second.path, "коллизий нет по построению, отказывать не нужно"


@pytest.mark.asyncio
async def test_unknown_suffix_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AttachmentTypeError, match=r".png"):
        await store_attachment(ws, "вирус.exe", b"MZ")


@pytest.mark.asyncio
async def test_suffix_check_is_case_insensitive(tmp_path: Path) -> None:
    """`.PNG` — тот же формат, что `.png`; отказ по регистру был бы сюрпризом
    для человека, который просто скопировал файл со смартфона."""
    ws = tmp_path / "ws"
    ws.mkdir()
    stored = await store_attachment(ws, "скрин.PNG", b"\x89PNG")
    assert stored.mime == "image/png"


@pytest.mark.asyncio
async def test_too_large_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AttachmentTooLarge):
        await store_attachment(ws, "большой.png", b"x" * (MAX_UPLOAD_BYTES + 1))


@pytest.mark.asyncio
async def test_image_over_vision_limit_is_flagged_not_refused(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    stored = await store_attachment(ws, "огромный.png", b"x" * (6 * 1024 * 1024))
    assert stored.too_large_for_vision is True


@pytest.mark.asyncio
async def test_store_hides_attachments_dir_from_git(tmp_path: Path) -> None:
    """store_attachment сам прячет .attachments/ от git — не нужно вызывать
    ensure_git_excluded отдельно на стороне вызывающего кода."""
    ws = tmp_path / "ws"
    ws.mkdir()
    repo = GitRepo(ws)
    await repo.init()

    await store_attachment(ws, "скрин.png", b"1")

    exclude = ws / ".git" / "info" / "exclude"
    assert ".attachments/" in exclude.read_text(encoding="utf-8").splitlines()


@pytest.mark.asyncio
async def test_upload_endpoint(service: GatewayService) -> None:
    session = await service.create_session(title="вложение")
    client = TestClient(create_app(service=service))

    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": ("скрин.png", b"\x89PNG", "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "скрин.png"
    assert body["path"].startswith(".attachments/")
    assert (service.workspace / body["path"]).is_file()


@pytest.mark.asyncio
async def test_upload_rejects_bad_suffix_with_415(service: GatewayService) -> None:
    session = await service.create_session(title="плохое")
    client = TestClient(create_app(service=service))
    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": ("вирус.exe", b"MZ", "application/octet-stream")},
    )
    assert response.status_code == 415


@pytest.mark.asyncio
async def test_upload_unknown_session_gives_404(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    response = client.post(
        "/sessions/does-not-exist/attachments",
        files={"file": ("скрин.png", b"\x89PNG", "image/png")},
    )
    assert response.status_code == 404

"""Вложения к сообщению: запись, лимиты, исключение из git (план 2026-07-28)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from svarog_harness.config.loader import load_config
from svarog_harness.gateway import GatewayService
from svarog_harness.gateway.api import _read_capped, create_app
from svarog_harness.gateway.attachments import (
    ALLOWED_SUFFIXES,
    MAX_UPLOAD_BYTES,
    AttachmentPathError,
    AttachmentTooLarge,
    AttachmentTypeError,
    ensure_git_excluded,
    safe_name,
    store_attachment,
)
from svarog_harness.gitflow import GitRepo
from svarog_harness.trace.lookup import find_run_by_prefix


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
async def test_nul_byte_in_name_rejected_not_500(tmp_path: Path) -> None:
    """NUL-байт в имени валит `Path.resolve()`/`write_bytes` в `ValueError` —
    это должно стать чистым `AttachmentTypeError` (наружу 415), а не утечкой
    сырого ValueError/OSError, которая на HTTP-слое превратилась бы в 500."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AttachmentTypeError):
        await store_attachment(ws, "скрин\x00.png", b"1")


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
async def test_read_capped_stops_before_consuming_everything() -> None:
    """Тело больше потолка не должно оседать в памяти целиком — иначе
    MAX_UPLOAD_BYTES защищал бы только после того, как вред уже нанесён
    (ADR-0014: тенантов в процессе может быть несколько)."""
    read_calls = 0

    class _FakeUpload:
        async def read(self, size: int) -> bytes:
            nonlocal read_calls
            read_calls += 1
            # Как будто источник готов отдавать сколько угодно — единственное,
            # что должно остановить чтение, — сама проверка потолка.
            return b"x" * size

    with pytest.raises(AttachmentTooLarge):
        await _read_capped(_FakeUpload(), limit=100)

    # Один чанк по 64 КиБ уже больше потолка в 100 байт — обрыв на первом же
    # чтении, а не после того, как гигабайты осели бы в памяти.
    assert read_calls == 1


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
async def test_upload_rejects_over_long_name_with_415(service: GatewayService) -> None:
    """Слишком длинное имя валит `write_bytes` в `OSError` (ENAMETOOLONG) —
    проверяем, что это доходит до HTTP-слоя как чистый 415, а не утекает
    как 500.

    NUL-байт в имени (см. test_nul_byte_in_name_rejected_not_500 — тест на
    чистой функции) был бы более прямой демонстрацией того же класса бага
    через реальный эндпоинт, но `TestClient`/`httpx` percent-кодирует NUL в
    значении `Content-Disposition` ещё на клиенте: сервер получает буквальный
    текст `"%00"`, а не настоящий нулевой байт (проверено эмпирически —
    `file.filename` на сервере оказывается `"...%00.png"`), так что через
    этот путь ветку с NUL не воспроизвести без обращения к internals в
    обход HTTP. Слишком длинное имя — тот же класс исключения (`OSError`,
    не `ValueError`) и реально проходит через `TestClient` до сервера
    (multipart-парсер не режет имя вплоть до нескольких тысяч символов, а
    ENAMETOOLONG на файловой системе срабатывает уже на паре сотен байт),
    так что именно оно годится как end-to-end представитель этого класса.
    """
    session = await service.create_session(title="длинное имя")
    client = TestClient(create_app(service=service))

    long_name = "a" * 300 + ".png"
    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": (long_name, b"\x89PNG", "image/png")},
    )

    assert response.status_code == 415


@pytest.mark.asyncio
async def test_upload_refuses_while_run_is_live(service: GatewayService) -> None:
    """Прикреплять файл к сессии с идущим запуском нельзя — как и удалять
    её историю (ср. test_delete_session_refuses_while_run_is_live)."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from svarog_harness.storage.models import Run, RunState

    session = await service.create_session(title="занятая")

    async def seed(db: AsyncSession) -> None:
        db.add(
            Run(
                id="run-live",
                session_id=session.session_id,
                task="идёт",
                state=RunState.RUNNING,
                autonomy="supervised",
            )
        )
        await db.commit()

    await service._read(seed)

    client = TestClient(create_app(service=service))
    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": ("скрин.png", b"\x89PNG", "image/png")},
    )

    assert response.status_code == 409
    attachments_dir = service.workspace / ".attachments"
    assert not attachments_dir.exists() or not any(attachments_dir.iterdir()), (
        "под живой запуск файл не должен попасть в .attachments/"
    )


@pytest.mark.asyncio
async def test_upload_unknown_session_gives_404(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))
    response = client.post(
        "/sessions/does-not-exist/attachments",
        files={"file": ("скрин.png", b"\x89PNG", "image/png")},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_attachment_paths_are_appended_to_task_text(service) -> None:
    session = await service.create_session(title="с вложением")
    stored = await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")

    run_id = await service.send_message(
        session.session_id, "посмотри баг", None, attachments=[stored.path]
    )

    async def read(db):
        run = await find_run_by_prefix(db, run_id)
        return run.task

    task = await service._read(read)
    assert "посмотри баг" in task
    assert stored.path in task
    assert "read_image" in task, "агенту сказано, чем это читать"


@pytest.mark.asyncio
async def test_path_outside_attachments_is_rejected(service) -> None:
    session = await service.create_session(title="чужое")
    with pytest.raises(AttachmentPathError):
        await service.send_message(
            session.session_id, "текст", None, attachments=["../../etc/passwd"]
        )


@pytest.mark.asyncio
async def test_missing_attachment_is_rejected(service) -> None:
    session = await service.create_session(title="нет файла")
    with pytest.raises(AttachmentPathError):
        await service.send_message(
            session.session_id, "текст", None, attachments=[".attachments/нет.png"]
        )

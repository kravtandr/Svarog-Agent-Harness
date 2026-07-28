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
    verify_attachment,
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
async def test_store_rejects_symlinked_attachments_dir(tmp_path: Path) -> None:
    """Финальное ревью, находка 2: если `.attachments` сам — симлинк наружу,
    старый код резолвил `root` на цель симлинка ДО всех проверок — и любой
    путь внутри неё проходил `is_relative_to(root)`. ПОС ревьюера писал так
    за пределы workspace и читал файл обратно через GET .../attachments/{name}.
    Под `sandbox.type: docker` создать такой симлинк может сам агент — значит
    это реальный побег из bind-mount, не гипотетика."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / ".attachments").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AttachmentTypeError):
        await store_attachment(ws, "скрин.png", b"1")

    assert not any(outside.iterdir()), "запись не должна была уйти за пределы workspace"


@pytest.mark.asyncio
async def test_verify_rejects_symlinked_attachments_dir(tmp_path: Path) -> None:
    """Тот же побег, но на пути раздачи (`verify_attachment`, GET-эндпоинт):
    файл, заранее лежащий вне workspace, не должен становиться читаемым
    только потому, что `.attachments` — симлинк на его каталог."""
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "секрет.txt").write_text("не для этой сессии", encoding="utf-8")
    (ws / ".attachments").symlink_to(outside, target_is_directory=True)

    with pytest.raises(AttachmentPathError):
        verify_attachment(ws, ".attachments/секрет.txt")


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
async def test_upload_endpoint_rejects_too_large_with_413(
    service: GatewayService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Находка 9 финального ревью: 413 (`AttachmentTooLarge`) был протестирован
    только на уровне функции (`test_too_large_rejected`), не через реальный
    эндпоинт — в отличие от 415/409/404 у upload'а. Настоящие 25 МБ не гоняем —
    понижаем потолок в модуле `api`, тот же приём, что и в `test_read_capped_
    stops_before_consuming_everything` для внутренней функции."""
    import svarog_harness.gateway.api as api_module

    monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 4)
    session = await service.create_session(title="слишком большое")
    client = TestClient(create_app(service=service))

    response = client.post(
        f"/sessions/{session.session_id}/attachments",
        files={"file": ("скрин.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_send_message_endpoint_rejects_bad_attachment_path_with_400(
    service: GatewayService,
) -> None:
    """Находка 9 финального ревью: 400 (`AttachmentPathError` из `send_message`)
    был протестирован только как `pytest.raises` на функции сервиса
    (`test_path_outside_attachments_is_rejected`), не через реальный HTTP-эндпоинт."""
    session = await service.create_session(title="плохой путь")
    client = TestClient(create_app(service=service))

    response = client.post(
        f"/sessions/{session.session_id}/messages",
        json={"text": "текст", "attachments": ["../../etc/passwd"]},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_attachments_use_session_workspace_not_service_workspace(
    service: GatewayService,
) -> None:
    """Находка 9 финального ревью: `store_attachment`, `attachment_path` и
    `send_message` резолвят `meta['workspace'] or self.workspace` — но во всех
    тестах выше workspace сессии и workspace сервиса совпадают, так что баг,
    захардкодивший `self.workspace`, был бы невидим (файл ушёл бы не в тот
    тенант). По образцу `test_file_suggestions_use_named_workspace_not_
    service_workspace` (tests/test_gateway_completion.py) — именованный
    workspace (ADR-0017), заведомо другой каталог."""
    await service.create_workspace("proj")
    session = await service.create_session(title="именованный", workspace_name="proj")
    assert session.workspace is not None
    session_ws = Path(session.workspace)
    assert session_ws != service.workspace.expanduser().resolve()

    stored = await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")

    assert (session_ws / stored.path).is_file(), "файл обязан лечь в workspace сессии"
    assert not (service.workspace / stored.path).exists(), (
        "и не должен появиться в workspace сервиса"
    )

    resolved = await service.attachment_path(session.session_id, Path(stored.path).name)
    assert resolved == (session_ws / stored.path).resolve()

    run_id = await service.send_message(
        session.session_id, "посмотри", None, attachments=[stored.path]
    )
    assert run_id


@pytest.mark.asyncio
async def test_attachment_from_another_session_is_rejected_with_400(
    service: GatewayService,
) -> None:
    """Спецификация (2026-07-28-composer-completion-and-uploads-design.md):
    «вложение из другой сессии → 400». Прежде эта строка спецификации
    прикрывалась `test_path_outside_attachments_is_rejected`, которая гоняет
    `../../etc/passwd` — это обход пути, а не вложение реально другой сессии
    (находка 9 финального ревью). Здесь вложение — настоящий файл, сохранённый
    в именованном workspace другой сессии."""
    await service.create_workspace("a")
    await service.create_workspace("b")
    session_a = await service.create_session(title="A", workspace_name="a")
    session_b = await service.create_session(title="B", workspace_name="b")

    stored_in_b = await service.store_attachment(session_b.session_id, "скрин.png", b"\x89PNG")

    client = TestClient(create_app(service=service))
    response = client.post(
        f"/sessions/{session_a.session_id}/messages",
        json={"text": "смотри", "attachments": [stored_in_b.path]},
    )

    assert response.status_code == 400


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


# --- раздача вложения обратно (задача 15) -----------------------------------


@pytest.mark.asyncio
async def test_read_attachment_serves_stored_bytes(service: GatewayService) -> None:
    session = await service.create_session(title="раздача")
    stored = await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")
    client = TestClient(create_app(service=service))
    name = stored.path.removeprefix(".attachments/")

    response = client.get(f"/sessions/{session.session_id}/attachments/{name}")

    assert response.status_code == 200
    assert response.content == b"\x89PNG"


@pytest.mark.asyncio
async def test_read_attachment_image_comes_back_inline_with_explicit_mime(
    service: GatewayService,
) -> None:
    """Картинка — единственное, для чего есть готовый потребитель (<img> в
    ChatScreen); content-type задаётся явно из белого списка изображений,
    а не угадывается голым FileResponse по суффиксу."""
    session = await service.create_session(title="картинка")
    stored = await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")
    client = TestClient(create_app(service=service))
    name = stored.path.removeprefix(".attachments/")

    response = client.get(f"/sessions/{session.session_id}/attachments/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "attachment" not in response.headers.get("content-disposition", "")


@pytest.mark.asyncio
async def test_read_attachment_html_is_forced_download_not_inline(
    service: GatewayService,
) -> None:
    """`.html` проходит белый список загрузки (`ALLOWED_SUFFIXES`), а SPA
    раздаётся с того же origin, где в sessionStorage лежит bearer-токен.
    Открытая по прямой ссылке .html-страница не должна исполниться в этом
    origin — раздача обязана форсировать скачивание (Content-Disposition:
    attachment), а не угадывать content-type по суффиксу и отдавать голый
    FileResponse, который браузер отрисует inline."""
    session = await service.create_session(title="html вложение")
    stored = await service.store_attachment(
        session.session_id, "страница.html", b"<script>alert(1)</script>"
    )
    client = TestClient(create_app(service=service))
    name = stored.path.removeprefix(".attachments/")

    response = client.get(f"/sessions/{session.session_id}/attachments/{name}")

    assert response.status_code == 200
    assert response.headers.get("content-disposition", "").startswith("attachment")


@pytest.mark.asyncio
async def test_read_attachment_unknown_session_gives_404(service: GatewayService) -> None:
    client = TestClient(create_app(service=service))

    response = client.get("/sessions/does-not-exist/attachments/скрин.png")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_read_attachment_missing_file_gives_404(service: GatewayService) -> None:
    session = await service.create_session(title="нет файла")
    client = TestClient(create_app(service=service))

    response = client.get(f"/sessions/{session.session_id}/attachments/нет.png")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_attachment_path_rejects_traversal_not_string_concat(
    service: GatewayService,
) -> None:
    """`attachment_path` обязан резолвить через `verify_attachment` (тот же
    fail-closed путь, что при приёме), а не собирать путь конкатенацией."""
    session = await service.create_session(title="чужое")

    with pytest.raises(AttachmentPathError):
        await service.attachment_path(session.session_id, "../svarog.yaml")


@pytest.mark.asyncio
async def test_attachment_leaves_working_tree_clean(service) -> None:
    """Скриншот не должен уехать в историю task-ветки автокоммитом (Flow C)."""
    repo = GitRepo(service.workspace)
    await repo.init()
    await repo.ensure_identity(name="тест", email="тест@example.com")

    session = await service.create_session(title="чистое дерево")
    await service.store_attachment(session.session_id, "скрин.png", b"\x89PNG")

    dirty = await repo.status_porcelain()

    assert not any(".attachments" in line for line in dirty), f"вложение видно git: {dirty!r}"

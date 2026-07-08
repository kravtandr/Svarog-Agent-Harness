"""Engine, сессии и инициализация SQLite (ADR-0007).

Приложение работает через async engine (aiosqlite); миграции Alembic
выполняются синхронным engine — это два URL на один файл БД.
"""

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.ext.asyncio import AsyncSession as SQLAlchemyAsyncSession

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def async_db_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


def sync_db_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_engine(db_path: Path) -> AsyncEngine:
    """Async engine к SQLite с включенными foreign keys (в SQLite они опт-ин)."""
    engine = create_async_engine(async_db_url(db_path))
    event.listen(engine.sync_engine, "connect", _enable_foreign_keys)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[SQLAlchemyAsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", sync_db_url(db_path))
    return cfg


def init_db(db_path: Path) -> None:
    """Создать файл БД (вместе с директориями) и применить миграции до head.

    Идемпотентно: на актуальной БД ничего не делает. Вызывается из
    `svarog init` и при старте runtime.
    """
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), "head")

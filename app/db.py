"""Moteur SQLAlchemy et session partagee."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

_connect_args: dict[str, object] = {}
if _settings.is_sqlite:
    _connect_args["check_same_thread"] = False
    # Le serveur web et le planificateur ecrivent tous deux : sans ce delai,
    # SQLite renvoie aussitot "database is locked" au second ecrivain.
    _connect_args["timeout"] = 30.0

engine: Engine = create_engine(
    _settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

if _settings.is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Session transactionnelle : commit si tout va bien, rollback sinon."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """Dependance FastAPI."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db(retries: int = 5, delay: float = 1.5) -> None:
    """Cree les tables manquantes.

    Les services web et scheduler demarrent ensemble et appellent tous deux
    cette fonction : deux CREATE TABLE simultanes peuvent entrer en conflit sur
    PostgreSQL. On reessaie donc quelques fois, le temps que le premier termine.
    """
    import logging
    import time

    from sqlalchemy.exc import OperationalError, ProgrammingError

    from app import models  # noqa: F401  (enregistre les mappings)

    logger = logging.getLogger(__name__)
    for tentative in range(1, retries + 1):
        try:
            models.Base.metadata.create_all(bind=engine)
            return
        except (OperationalError, ProgrammingError) as exc:
            if tentative == retries:
                raise
            logger.warning(
                "Creation du schema en conflit ou base pas encore prete "
                "(tentative %d/%d) : %s",
                tentative, retries, str(exc).splitlines()[0][:120],
            )
            time.sleep(delay * tentative)

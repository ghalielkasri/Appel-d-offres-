"""Fixtures de test : base temporaire isolee pour chaque test."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    """Session SQLAlchemy sur une base SQLite neuve, propre a chaque test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    # La configuration et le moteur sont mis en cache au niveau module : il faut
    # les recharger pour que la base temporaire soit prise en compte.
    import app.config
    import app.db

    app.config.get_settings.cache_clear()
    import importlib

    importlib.reload(app.db)
    app.db.init_db()

    session = app.db.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        app.config.get_settings.cache_clear()


@pytest.fixture
def seeded_session(db_session):
    from app.seeds import seed_profiles

    seed_profiles(db_session)
    db_session.commit()
    return db_session


@pytest.fixture
def fixture_html():
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _read

"""Controle de pre-vol : chaque verification doit detecter sa panne."""

from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.doctor import (
    FAIL,
    OK,
    WARN,
    Report,
    check_postgres_password,
    check_timezone,
    check_writable_paths,
    run_diagnostics,
)


def statut(report: Report, nom: str) -> str:
    return next(c.status for c in report.checks if c.name == nom)


def detail(report: Report, nom: str) -> str:
    return next(c.detail for c in report.checks if c.name == nom)


@pytest.mark.parametrize("caractere", ["@", ":", "/", "?", "#", "[", "]"])
def test_url_breaking_password_is_a_failure(monkeypatch, caractere):
    """docker-compose insere ce mot de passe dans une URL : ces caracteres la cassent."""
    monkeypatch.setenv("POSTGRES_PASSWORD", f"mot{caractere}de{caractere}passe")
    report = Report()
    check_postgres_password(report, Settings(_env_file=None))
    assert statut(report, "Mot de passe Postgres") == FAIL


def test_short_password_is_a_warning(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "court")
    report = Report()
    check_postgres_password(report, Settings(_env_file=None))
    assert statut(report, "Mot de passe Postgres") == WARN


def test_good_password_passes(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "Xk7mQp2rLt9vNw4z")
    report = Report()
    check_postgres_password(report, Settings(_env_file=None))
    assert statut(report, "Mot de passe Postgres") == OK


def test_missing_password_on_postgres_is_a_warning(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    report = Report()
    check_postgres_password(
        report,
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@h/db"),
    )
    assert statut(report, "Mot de passe Postgres") == WARN


def test_missing_password_on_sqlite_is_fine(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    report = Report()
    check_postgres_password(report, Settings(_env_file=None, database_url="sqlite:///x.db"))
    assert statut(report, "Mot de passe Postgres") == OK


@pytest.mark.parametrize("fuseau,attendu", [("Africa/Casablanca", OK), ("Mars/Olympus", FAIL)])
def test_timezone(fuseau, attendu):
    report = Report()
    check_timezone(report, Settings(_env_file=None, timezone=fuseau))
    assert statut(report, "Fuseau horaire") == attendu


def test_writable_directory(tmp_path):
    report = Report()
    check_writable_paths(
        report, Settings(_env_file=None, documents_dir=str(tmp_path / "dce"))
    )
    assert statut(report, "Dossier des pièces") == OK


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignore les bits de permission ; le conteneur tourne en uid 10001",
)
def test_unwritable_directory_is_a_failure(tmp_path):
    bloque = tmp_path / "interdit"
    bloque.mkdir()
    bloque.chmod(0o500)
    report = Report()
    check_writable_paths(
        report, Settings(_env_file=None, documents_dir=str(bloque / "dce"))
    )
    try:
        assert statut(report, "Dossier des pièces") == FAIL
    finally:
        bloque.chmod(0o700)


def test_unreachable_database_is_a_failure(monkeypatch):
    """Une base injoignable doit bloquer, pas passer inapercue."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:59999/x")
    import importlib

    import app.config
    import app.db

    app.config.get_settings.cache_clear()
    importlib.reload(app.db)
    try:
        report = run_diagnostics(app.config.get_settings(), skip_network=True)
        assert statut(report, "Base de données") == FAIL
        assert report.ready is False
    finally:
        app.config.get_settings.cache_clear()


def test_missing_columns_are_detected(db_session, monkeypatch):
    """create_all n'ajoute pas les colonnes manquantes : il faut le signaler."""
    from sqlalchemy import text

    from app.db import engine

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE tenders DROP COLUMN families"))

    from app.config import get_settings

    report = run_diagnostics(get_settings(), skip_network=True)
    assert statut(report, "Schéma") == FAIL
    assert "families" in detail(report, "Schéma")


def test_no_active_profile_is_a_failure(db_session):
    """Sans profil actif, rien ne serait jamais notifie."""
    from app.config import get_settings

    report = run_diagnostics(get_settings(), skip_network=True)
    assert statut(report, "Profils de veille") == FAIL


def test_seeded_profile_passes(seeded_session):
    from app.config import get_settings

    seeded_session.commit()
    report = run_diagnostics(get_settings(), skip_network=True)
    assert statut(report, "Profils de veille") == OK
    assert "Cuisine professionnelle" in detail(report, "Profils de veille")


def test_a_broken_check_does_not_hide_the_others(monkeypatch):
    """Un controle qui leve ne doit pas interrompre le diagnostic."""
    import app.doctor as doctor

    def explose(report, settings):
        raise RuntimeError("panne du controle")

    monkeypatch.setattr(doctor, "CHECKS", (explose, doctor.check_timezone))
    report = doctor.run_diagnostics(Settings(_env_file=None), skip_network=True)
    assert len(report.checks) == 2
    assert any("panne du controle" in c.detail for c in report.checks)

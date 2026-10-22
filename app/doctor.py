"""Controle de pre-vol : tout ce qui doit etre vrai avant la premiere collecte.

Chaque verification renvoie un statut et, en cas de probleme, l'action precise a
mener. L'objectif est qu'un deploiement rate se diagnostique en une commande
plutot qu'en lisant des journaux.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import inspect, text

from app.config import Settings, get_settings

OK = "ok"
WARN = "avertissement"
FAIL = "echec"

# Caracteres qui, dans un mot de passe, cassent l'URL de connexion construite
# par docker-compose (postgresql+psycopg://radar:MOTDEPASSE@db:5432/radar).
URL_BREAKING = set("@:/?#[]")


@dataclass(slots=True)
class Check:
    name: str
    status: str
    detail: str = ""
    action: str = ""


@dataclass(slots=True)
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", action: str = "") -> None:
        self.checks.append(Check(name, status, detail, action))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ready(self) -> bool:
        return not self.failures


def check_database(report: Report, settings: Settings) -> None:
    """Connexion a la base, et schema complet."""
    from app.db import engine
    from app.models import Base

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        report.add(
            "Base de données",
            FAIL,
            f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}",
            "Vérifiez DATABASE_URL. Avec docker compose, attendez que le "
            "service `db` soit sain : docker compose ps",
        )
        return

    report.add("Base de données", OK, settings.database_url.split("@")[-1])

    # create_all n'ajoute pas les colonnes manquantes a une table existante :
    # une montee de version sans migration passerait inapercue jusqu'a l'erreur.
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    expected = set(Base.metadata.tables)
    missing_tables = sorted(expected - existing)
    if missing_tables:
        report.add(
            "Schéma",
            WARN,
            f"{len(missing_tables)} table(s) absente(s) : {', '.join(missing_tables)}",
            "Lancez `radar init-db` (les tables sont créées au démarrage du service web).",
        )
        return

    missing_columns: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing:
            continue
        present = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in present:
                missing_columns.append(f"{table_name}.{column.name}")

    if missing_columns:
        report.add(
            "Schéma",
            FAIL,
            f"{len(missing_columns)} colonne(s) manquante(s) : "
            f"{', '.join(missing_columns[:6])}"
            + (" ..." if len(missing_columns) > 6 else ""),
            "La base date d'une version antérieure. `init-db` ne modifie pas les "
            "tables existantes : ajoutez les colonnes avec Alembic, ou repartez "
            "d'une base vide si l'historique est encore court.",
        )
    else:
        report.add("Schéma", OK, f"{len(expected)} tables, toutes les colonnes présentes")


def check_postgres_password(report: Report, settings: Settings) -> None:
    """Un mot de passe mal choisi casse l'URL construite par docker-compose."""
    password = os.environ.get("POSTGRES_PASSWORD", "")
    if not password:
        if settings.is_sqlite:
            report.add("Mot de passe Postgres", OK, "non requis (base SQLite)")
        else:
            report.add(
                "Mot de passe Postgres",
                WARN,
                "POSTGRES_PASSWORD absent de l'environnement",
                "Requis par docker-compose.yml : renseignez-le dans .env.",
            )
        return

    offending = sorted(URL_BREAKING & set(password))
    if offending:
        report.add(
            "Mot de passe Postgres",
            FAIL,
            "contient "
            + ", ".join(f"« {c} »" for c in offending)
            + " : ces caractères cassent l'URL de connexion",
            "docker-compose insère ce mot de passe dans "
            "postgresql+psycopg://radar:MOTDEPASSE@db:5432/radar. "
            "Choisissez un mot de passe composé de lettres, chiffres, - et _.",
        )
    elif len(password) < 12:
        report.add(
            "Mot de passe Postgres",
            WARN,
            f"{len(password)} caractères seulement",
            "Visez 16 caractères ou plus : openssl rand -base64 24 | tr -d '/+=@:'",
        )
    else:
        report.add("Mot de passe Postgres", OK, f"{len(password)} caractères, sans caractère gênant")


def check_writable_paths(report: Report, settings: Settings) -> None:
    """Le dossier des pieces doit etre inscriptible par l'utilisateur du conteneur."""
    directory = Path(settings.documents_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".ecriture-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        report.add(
            "Dossier des pièces",
            FAIL,
            f"{directory} : {type(exc).__name__}",
            "Dans docker compose, /app/data doit être monté sur le volume "
            "`appdata`. Un volume nommé hérite des droits de l'image ; "
            "un bind mount exige un `chown -R 10001:10001` côté hôte.",
        )
        return
    report.add("Dossier des pièces", OK, str(directory))


def check_apify(report: Report, settings: Settings) -> None:
    if not settings.apify_token:
        report.add(
            "Source Apify",
            WARN,
            "APIFY_TOKEN absent : aucune collecte d'avis",
            "Créez un compte sur apify.com, copiez le token dans .env, "
            "puis `docker compose restart web scheduler`.",
        )
        return

    import httpx

    try:
        response = httpx.get(
            "https://api.apify.com/v2/users/me",
            headers={"Authorization": f"Bearer {settings.apify_token}"},
            timeout=20,
        )
    except Exception as exc:
        report.add(
            "Source Apify",
            WARN,
            f"API injoignable : {type(exc).__name__}",
            "Vérifiez la sortie réseau du serveur vers api.apify.com.",
        )
        return

    if response.status_code == 401:
        report.add(
            "Source Apify",
            FAIL,
            "token refusé (HTTP 401)",
            "Regénérez le token : console.apify.com > Settings > API & Integrations.",
        )
    elif response.status_code >= 400:
        report.add("Source Apify", WARN, f"HTTP {response.status_code}", "")
    else:
        data = response.json().get("data", {})
        report.add(
            "Source Apify",
            OK,
            f"token valide (compte {data.get('username') or 'inconnu'}), "
            f"actor {settings.apify_actor}",
        )


def check_portal(report: Report, settings: Settings) -> None:
    """Le portail est-il joignable, et la connexion configuree ?"""
    import httpx

    if not settings.portal_enabled:
        report.add("Portail officiel", WARN, "PORTAL_ENABLED=false", "")
        return

    url = settings.portal_base_url.rstrip("/") + "/"
    try:
        response = httpx.get(
            url,
            timeout=settings.portal_timeout_secs,
            follow_redirects=True,
            headers={"User-Agent": settings.portal_user_agent},
        )
        reachable = response.status_code < 500
        code = response.status_code
    except Exception as exc:
        report.add(
            "Portail officiel",
            FAIL,
            f"{settings.portal_base_url} injoignable ({type(exc).__name__})",
            "Le portail filtre les adresses IP hors Maroc. Sans accès, ni les "
            "résultats d'attribution ni les pièces du DCE ne peuvent être "
            "récupérés. Testez depuis le serveur : "
            f"curl -I {settings.portal_base_url}",
        )
        return

    report.add(
        "Portail officiel",
        OK if reachable else WARN,
        f"{settings.portal_base_url} répond (HTTP {code})",
    )

    if settings.portal_credentials_set:
        report.add(
            "Compte entreprise",
            OK,
            f"identifiants présents pour {settings.portal_login}",
            "Vérifiez la connexion avec `radar portal-login-test`.",
        )
    else:
        report.add(
            "Compte entreprise",
            WARN,
            "PORTAL_LOGIN / PORTAL_PASSWORD absents",
            "Sans eux, les CPS et bordereaux resteront au statut « compte requis ».",
        )


def check_notifications(report: Report, settings: Settings) -> None:
    from app.notifiers import all_notifiers

    actifs = [n.name for n in all_notifiers(settings) if n.enabled]
    if not actifs:
        report.add(
            "Notifications",
            WARN,
            "aucun canal configuré",
            "Renseignez SMTP_* (email), WHATSAPP_* ou WEBHOOK_URL dans .env, "
            "puis vérifiez avec `radar test-notify`.",
        )
    else:
        report.add("Notifications", OK, "canaux actifs : " + ", ".join(actifs))


def check_dashboard_auth(report: Report, settings: Settings) -> None:
    if settings.dashboard_user and settings.dashboard_password:
        report.add("Accès au tableau de bord", OK, f"protégé ({settings.dashboard_user})")
    else:
        report.add(
            "Accès au tableau de bord",
            WARN,
            "aucun mot de passe",
            "Acceptable derrière un reverse proxy qui authentifie, ou si le port "
            "n'écoute que sur 127.0.0.1. Sinon renseignez DASHBOARD_USER et "
            "DASHBOARD_PASSWORD.",
        )


def check_watch_profiles(report: Report, settings: Settings) -> None:
    from app.db import SessionLocal
    from app.models import WatchProfile

    session = SessionLocal()
    try:
        actifs = session.query(WatchProfile).filter(WatchProfile.active.is_(True)).all()
    except Exception as exc:
        report.add("Profils de veille", WARN, f"illisibles : {type(exc).__name__}", "")
        return
    finally:
        session.close()

    if not actifs:
        report.add(
            "Profils de veille",
            FAIL,
            "aucun profil actif : rien ne sera notifié",
            "Lancez `radar init-db` pour créer le profil « Cuisine professionnelle », "
            "ou activez-en un avec `radar profile-edit <nom> --active`.",
        )
    else:
        detail = ", ".join(f"{p.name} ({len(p.keywords)} mots-clés)" for p in actifs)
        report.add("Profils de veille", OK, detail)


def check_timezone(report: Report, settings: Settings) -> None:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(settings.timezone)
    except Exception:
        report.add(
            "Fuseau horaire",
            FAIL,
            f"« {settings.timezone} » inconnu",
            "Utilisez Africa/Casablanca. Dans une image sans tzdata, "
            "le planificateur refuserait de démarrer.",
        )
    else:
        report.add("Fuseau horaire", OK, settings.timezone)


CHECKS: tuple[Callable[[Report, Settings], None], ...] = (
    check_database,
    check_postgres_password,
    check_writable_paths,
    check_timezone,
    check_watch_profiles,
    check_apify,
    check_portal,
    check_notifications,
    check_dashboard_auth,
)


def run_diagnostics(settings: Settings | None = None, skip_network: bool = False) -> Report:
    settings = settings or get_settings()
    report = Report()
    reseau = {check_apify, check_portal}
    for check in CHECKS:
        if skip_network and check in reseau:
            continue
        try:
            check(report, settings)
        except Exception as exc:  # un controle casse ne doit pas masquer les autres
            report.add(
                check.__name__.replace("check_", "").replace("_", " ").capitalize(),
                WARN,
                f"contrôle interrompu : {type(exc).__name__}: {exc}",
            )
    return report

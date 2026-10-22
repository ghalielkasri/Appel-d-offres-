"""Configuration centralisee, lue depuis l'environnement ou le fichier .env."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


# NoDecode empeche pydantic-settings de tenter un json.loads sur la valeur
# d'environnement avant notre validateur. Sans cela, EMAIL_TO=a@x.ma,b@y.ma
# — le format documente dans .env.example — fait echouer le demarrage.
ListFromEnv = Annotated[list[str], NoDecode]


def _split_list(value: str | list[str] | None) -> list[str]:
    """Accepte "a@x.ma, b@y.ma", une vraie liste, ou une liste JSON collee."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]

    text = str(value).strip()
    if not text:
        return []
    # Tolere le format JSON, qu'on peut coller par reflexe dans un .env.
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    app_name: str = "Radar Marches Publics"
    timezone: str = "Africa/Casablanca"
    log_level: str = "INFO"
    base_url: str = "http://localhost:8000"

    # --- Base de donnees ---
    # SQLite par defaut pour un essai local, Postgres en production (docker compose).
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'radar.db'}"

    # --- Source Apify : avis de consultation ---
    apify_token: str = ""
    apify_actor: str = "publicmoney~marchespublics-morocco-scraper"
    apify_max_items: int = 500
    apify_max_pages: int = 20
    apify_timeout_secs: int = 300
    # Second actor, schema plus riche (lots, montants, attributaire quand il sera publie).
    apify_detail_actor: str = "jungle_synthesizer~marchespublics-morocco-public-tenders-scraper"
    apify_detail_enabled: bool = False
    apify_detail_max_items: int = 200

    # --- Source portail : resultats d'attribution ---
    portal_enabled: bool = True
    portal_base_url: str = "https://www.marchespublics.gov.ma"
    portal_timeout_secs: int = 60
    portal_max_pages: int = 20
    portal_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # --- Documents du DCE (CPS, bordereau des prix, reglement...) ---
    # Le portail n'ouvre le telechargement du DCE qu'aux comptes entreprise
    # (Espace fournisseur). Sans identifiants, les references sont enregistrees
    # mais les fichiers restent au statut "auth_required".
    portal_login: str = ""
    portal_password: str = ""
    # Formulaire de connexion : a confirmer sur le portail depuis le serveur,
    # avec `radar portal-login-probe`.
    portal_login_path: str = "/index.php?page=entreprise.EntrepriseHome"
    portal_login_user_field: str = "login"
    portal_login_password_field: str = "password"
    documents_dir: str = str(BASE_DIR / "data" / "documents")
    documents_max_mb: int = 60

    # --- Planification ---
    collect_interval_minutes: int = 30
    awards_interval_hours: int = 12
    scheduler_enabled: bool = True

    # --- Notifications : email ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""
    email_to: ListFromEnv = Field(default_factory=list)

    # --- Notifications : WhatsApp ---
    # "twilio", "meta" ou "" pour desactiver.
    whatsapp_provider: str = ""
    whatsapp_to: ListFromEnv = Field(default_factory=list)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    meta_phone_number_id: str = ""
    meta_access_token: str = ""
    meta_api_version: str = "v21.0"
    meta_template_name: str = ""
    meta_template_lang: str = "fr"

    # --- Notifications : webhook generique (Slack, Teams, n8n, Zapier...) ---
    webhook_url: str = ""

    # --- Dashboard ---
    dashboard_user: str = ""
    dashboard_password: str = ""

    @field_validator("email_to", "whatsapp_to", mode="before")
    @classmethod
    def _parse_lists(cls, value: object) -> list[str]:
        return _split_list(value)  # type: ignore[arg-type]

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.email_from and self.email_to)

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.whatsapp_provider and self.whatsapp_to)

    @property
    def webhook_enabled(self) -> bool:
        return bool(self.webhook_url)

    @property
    def portal_credentials_set(self) -> bool:
        return bool(self.portal_login and self.portal_password)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()

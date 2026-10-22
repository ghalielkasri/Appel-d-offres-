"""Lecture de la configuration depuis l'environnement."""

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("achats@arcade.ma", ["achats@arcade.ma"]),
        ("a@x.ma,b@y.ma", ["a@x.ma", "b@y.ma"]),
        ("a@x.ma, b@y.ma", ["a@x.ma", "b@y.ma"]),
        ("  a@x.ma ,  b@y.ma  ,", ["a@x.ma", "b@y.ma"]),
        ('["a@x.ma","b@y.ma"]', ["a@x.ma", "b@y.ma"]),
        ("", []),
    ],
)
def test_email_to_accepts_env_formats(monkeypatch, raw, expected):
    """Regression : une liste separee par des virgules faisait echouer le demarrage.

    pydantic-settings tente un json.loads sur les champs de type liste avant
    tout validateur ; sans l'annotation NoDecode, le format documente dans
    .env.example levait une SettingsError au lancement de l'application.
    """
    monkeypatch.setenv("EMAIL_TO", raw)
    assert Settings(_env_file=None).email_to == expected


def test_whatsapp_to_accepts_comma_separated(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TO", "+212600000001, +212600000002")
    assert Settings(_env_file=None).whatsapp_to == ["+212600000001", "+212600000002"]


def test_email_enabled_requires_host_from_and_recipients(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.ma")
    monkeypatch.setenv("EMAIL_FROM", "radar@example.ma")
    monkeypatch.setenv("EMAIL_TO", "achats@example.ma")
    assert Settings(_env_file=None).email_enabled is True

    monkeypatch.setenv("EMAIL_TO", "")
    assert Settings(_env_file=None).email_enabled is False


def test_whatsapp_enabled_requires_provider_credentials(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TO", "+212600000001")
    monkeypatch.setenv("WHATSAPP_PROVIDER", "twilio")
    assert Settings(_env_file=None).whatsapp_enabled is True
    # Le provider seul ne suffit pas : le notificateur exige les identifiants.
    from app.notifiers import WhatsAppNotifier

    assert WhatsAppNotifier(Settings(_env_file=None)).enabled is False

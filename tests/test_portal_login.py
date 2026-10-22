"""Connexion au portail et telechargement du DCE.

Ces tests tournent contre un faux portail qui reproduit les contraintes d'une
application PRADO, celle du vrai site : cookie de session, jeton d'etat a
renvoyer a l'identique, noms de champs generes, et page de confirmation avant
que le fichier ne soit servi.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.documents import (
    PortalDocumentFetcher,
    discover_login_form,
    login_signal,
    looks_logged_in,
)
from app.models import DocumentKind, DocumentStatus, TenderDocument
from tests.fake_portal import (
    AVEC_CAPTCHA,
    BON_LOGIN,
    BON_PASSE,
    PASS_FIELD,
    USER_FIELD,
    demarrer,
)

PORT = 8231
BASE = f"http://127.0.0.1:{PORT}"
LOGIN_PATH = "/index.php?page=entreprise.EntrepriseHome"


@pytest.fixture(scope="module")
def portal():
    server = demarrer(PORT)
    yield BASE
    server.shutdown()


@pytest.fixture(autouse=True)
def no_captcha():
    AVEC_CAPTCHA["actif"] = False
    yield
    AVEC_CAPTCHA["actif"] = False


def settings_for(tmp_path, login="", password="", user_field="login", pass_field="password"):
    return Settings(
        _env_file=None,
        portal_base_url=BASE,
        portal_login_path=LOGIN_PATH,
        portal_login=login,
        portal_password=password,
        portal_login_user_field=user_field,
        portal_login_password_field=pass_field,
        documents_dir=str(tmp_path / "dce"),
    )


def a_document():
    return TenderDocument(
        id=1, kind=DocumentKind.CPS, name="CPS.pdf", url=f"{BASE}/dce/8842/cps.pdf"
    )


def test_discovers_generated_field_names(portal):
    """Les noms de champs de PRADO ne peuvent pas etre codes en dur."""
    html = httpx.get(BASE + LOGIN_PATH).text
    form = discover_login_form(html, BASE + "/index.php", "login", "password")
    assert form is not None
    assert form.user_field == USER_FIELD
    assert form.password_field == PASS_FIELD


def test_preserves_hidden_state_fields(portal):
    """Sans le jeton d'etat, le portail rejette la soumission."""
    html = httpx.get(BASE + LOGIN_PATH).text
    form = discover_login_form(html, BASE + "/index.php", "login", "password")
    assert "PRADO_PAGESTATE" in form.fields
    assert "PRADO_POSTBACK_TARGET" in form.fields
    # Le bouton de validation compte aussi : PRADO s'en sert comme declencheur.
    assert any(name.endswith("valider") for name in form.fields)


def test_login_without_credentials(portal, tmp_path):
    with PortalDocumentFetcher(settings_for(tmp_path)) as fetcher:
        assert fetcher.login() is False
        assert "PORTAL_LOGIN" in fetcher.login_report["reason"]


def test_document_without_credentials_is_auth_required(portal, tmp_path):
    with PortalDocumentFetcher(settings_for(tmp_path)) as fetcher:
        document = fetcher.download(a_document())
    assert document.status is DocumentStatus.AUTH_REQUIRED
    assert "compte entreprise" in document.error


def test_wrong_password_is_rejected(portal, tmp_path):
    """Regression : la page de connexion s'intitule « Espace fournisseur ».

    Ce libelle figurait parmi les marqueurs de session ouverte, ce qui faisait
    passer un mot de passe refuse pour une connexion reussie.
    """
    with PortalDocumentFetcher(settings_for(tmp_path, BON_LOGIN, "mauvais")) as fetcher:
        assert fetcher.login() is False
        reason = fetcher.login_report["reason"]
    assert "incorrect" in reason.lower(), "le message du portail doit etre remonte"


def test_successful_login_and_download(portal, tmp_path):
    with PortalDocumentFetcher(settings_for(tmp_path, BON_LOGIN, BON_PASSE)) as fetcher:
        assert fetcher.login() is True, fetcher.login_report.get("reason")
        document = fetcher.download(a_document())

    assert document.status is DocumentStatus.DOWNLOADED
    assert document.content_type.startswith("application/pdf")
    assert document.size_bytes > 0
    assert document.sha256
    # La page de confirmation a bien ete suivie : c'est un PDF, pas du HTML.
    assert Path(document.local_path).read_bytes().startswith(b"%PDF")


def test_captcha_is_reported_not_silently_failed(portal, tmp_path):
    AVEC_CAPTCHA["actif"] = True
    with PortalDocumentFetcher(settings_for(tmp_path, BON_LOGIN, BON_PASSE)) as fetcher:
        assert fetcher.login() is False
        assert fetcher.login_report["form"]["captcha"] is True
        assert "captcha" in fetcher.login_report["reason"].lower()


def test_misconfigured_field_names_self_correct(portal, tmp_path):
    """La decouverte prime sur la configuration : une erreur de .env ne bloque pas."""
    settings = settings_for(tmp_path, BON_LOGIN, BON_PASSE, "inexistant", "autre")
    with PortalDocumentFetcher(settings) as fetcher:
        assert fetcher.login() is True
        assert fetcher.login_report["form"]["user_field"] == USER_FIELD


def test_wrong_login_path_is_reported(portal, tmp_path):
    settings = settings_for(tmp_path, BON_LOGIN, BON_PASSE)
    settings.portal_login_path = "/page-qui-nexiste-pas"
    with PortalDocumentFetcher(settings) as fetcher:
        assert fetcher.login() is False
        assert "PORTAL_LOGIN_PATH" in fetcher.login_report["reason"]


def test_size_limit_is_enforced(portal, tmp_path):
    settings = settings_for(tmp_path, BON_LOGIN, BON_PASSE)
    settings.documents_max_mb = 0
    with PortalDocumentFetcher(settings) as fetcher:
        document = fetcher.download(a_document())
    assert document.status is DocumentStatus.FAILED
    assert "DOCUMENTS_MAX_MB" in document.error


def test_reference_without_url_is_not_downloadable(tmp_path):
    document = TenderDocument(
        id=2, kind=DocumentKind.BORDEREAU, name="BPU.xlsx", url="https://x#BPU.xlsx"
    )
    document.url = "pas-une-url"
    with PortalDocumentFetcher(settings_for(tmp_path)) as fetcher:
        result = fetcher.download(document)
    assert result.status is DocumentStatus.FAILED


@pytest.mark.parametrize(
    "html,attendu",
    [
        ('<input type="password" name="p">', False),
        ("<input type='password'>", False),
        ('<input TYPE = "PASSWORD">', False),
        ("<a>Déconnexion</a>", True),
        ("<p>Mon compte</p>", True),
        ("<h1>Espace fournisseur</h1><input type=password>", False),
    ],
)
def test_login_signal(html, attendu):
    assert looks_logged_in(html) is attendu


def test_login_signal_explains_itself():
    ok, indice = login_signal("<a>Déconnexion</a>")
    assert ok and "deconnexion" in indice
    ok, indice = login_signal('<input type="password">')
    assert not ok and "mot de passe" in indice

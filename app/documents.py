"""Pieces du dossier de consultation : CPS, bordereau des prix, reglement, plans.

Ce que le portail autorise, verifie avant d'ecrire ce module :

* Les **avis** sont publics et lisibles sans compte.
* Le **telechargement du DCE** (donc du CPS et du bordereau des prix) est
  reserve aux comptes entreprise, cree depuis l'Espace fournisseur.
* La **soumission** electronique exige en plus un certificat de signature.

Consequence sur la conception : une piece peut etre *connue* sans etre
*recuperable*. Le statut distingue les deux (``LISTED`` contre ``DOWNLOADED``),
et ``AUTH_REQUIRED`` indique explicitement qu'il manque des identifiants plutot
que de faire passer cela pour une erreur technique.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import DocumentKind, DocumentStatus, Tender, TenderDocument
from app.normalize import clean_whitespace, normalize_text

logger = logging.getLogger(__name__)

# Reconnaissance de la nature d'une piece d'apres son nom de fichier ou son URL.
# L'ordre compte : les motifs les plus specifiques passent devant.
KIND_PATTERNS: tuple[tuple[DocumentKind, tuple[str, ...]], ...] = (
    (
        DocumentKind.BORDEREAU,
        (
            "bordereau", "bpu", "b p u", "dqe", "detail estimatif",
            "detail quantitatif", "devis estimatif", "bordereau des prix",
            "prix unitaires", "cadre du bordereau",
        ),
    ),
    (
        DocumentKind.CPS,
        ("cps", "c p s", "cahier des prescriptions speciales", "prescriptions speciales"),
    ),
    (
        DocumentKind.CPT,
        ("cpt", "prescriptions techniques", "specifications techniques", "cctp"),
    ),
    (
        DocumentKind.REGLEMENT,
        ("reglement de consultation", "reglement consultation", "rc", "r c"),
    ),
    (DocumentKind.PLAN, ("plan", "plans", "dwg", "coupe", "schema")),
    (DocumentKind.AVIS, ("avis d appel", "avis appel offres", "avis de publicite", "aao")),
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def classify_document(name: str | None, url: str | None = None) -> DocumentKind:
    """Nature d'une piece, deduite de son nom puis de son URL."""
    haystack = normalize_text(f"{name or ''} {unquote(url or '')}")
    haystack = re.sub(r"[^a-z0-9]+", " ", haystack)
    if not haystack.strip():
        return DocumentKind.AUTRE
    words = set(haystack.split())
    for kind, patterns in KIND_PATTERNS:
        for pattern in patterns:
            # Un sigle court ("rc", "cps") doit etre un mot entier, sinon
            # "rc" correspondrait a "marche" ou "bordereau".
            if len(pattern) <= 3:
                if pattern in words:
                    return kind
            elif pattern in haystack:
                return kind
    return DocumentKind.AUTRE


def extract_document_refs(raw: Any, base_url: str = "") -> list[dict[str, str]]:
    """Lit les references de pieces d'une charge brute d'actor.

    Le champ ``dce_documents`` est documente comme « attachment references, when
    available », sans structure precisee et vide dans l'exemple publie. Ce
    lecteur accepte donc toutes les formes plausibles : liste d'URL, liste
    d'objets {name, url}, ou objet unique.
    """
    if not raw:
        return []

    candidates: list[Any] = []
    if isinstance(raw, dict):
        for key in ("dce_documents", "dceDocuments", "documents", "attachments", "pieces"):
            value = raw.get(key)
            if value:
                candidates = value if isinstance(value, list) else [value]
                break
    elif isinstance(raw, list):
        candidates = raw

    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in candidates:
        name: str | None = None
        url: str | None = None
        if isinstance(item, str):
            url = item if "/" in item or item.startswith("http") else None
            name = None if url else item
        elif isinstance(item, dict):
            for key in ("url", "href", "link", "downloadUrl", "download_url"):
                if item.get(key):
                    url = str(item[key])
                    break
            for key in ("name", "filename", "fileName", "title", "label", "libelle"):
                if item.get(key):
                    name = str(item[key])
                    break
        if not url and not name:
            continue
        if url and base_url:
            url = urljoin(base_url, url)
        key = url or name or ""
        if key in seen:
            continue
        seen.add(key)
        refs.append({"name": clean_whitespace(name) or "", "url": url or ""})
    return refs


def sync_tender_documents(
    session: Session, tender: Tender, base_url: str = ""
) -> list[TenderDocument]:
    """Enregistre les references de pieces portees par la charge brute de l'avis."""
    refs = extract_document_refs(tender.raw, base_url or tender.consultation_url or "")
    existing = {doc.url: doc for doc in tender.documents}
    created: list[TenderDocument] = []
    for ref in refs:
        url = ref["url"] or f"{tender.consultation_url or ''}#{ref['name']}"
        if url in existing:
            continue
        document = TenderDocument(
            tender_id=tender.id,
            kind=classify_document(ref["name"], ref["url"]),
            name=ref["name"] or Path(urlparse(ref["url"]).path).name or None,
            url=url,
            status=DocumentStatus.LISTED,
            raw=ref,
        )
        session.add(document)
        created.append(document)
    if created:
        session.flush()
    return created


# Classes CSS et balises ou les applications web deposent leurs messages d'erreur.
_ERROR_SELECTORS = (
    {"class": re.compile("err|alert|danger|invalid|message", re.I)},
    {"id": re.compile("err|alert|message", re.I)},
)


def _extract_error_message(html: str) -> str:
    """Releve le message d'erreur affiche par le portail, s'il y en a un."""
    soup = BeautifulSoup(html, "lxml")
    for attrs in _ERROR_SELECTORS:
        for element in soup.find_all(attrs=attrs):
            texte = clean_whitespace(element.get_text(" ", strip=True))
            if texte and 6 <= len(texte) <= 200:
                return texte
    return ""


def safe_filename(document: TenderDocument) -> str:
    """Nom de fichier sur disque, sans caractere pouvant sortir du dossier."""
    base = document.name or Path(urlparse(document.url).path).name or f"piece-{document.id}"
    base = _SAFE_NAME.sub("_", base).strip("._") or f"piece-{document.id}"
    if "." not in base:
        base += ".pdf"
    return f"{document.id:06d}-{document.kind.value}-{base}"[:180]


@dataclass(slots=True)
class LoginForm:
    """Formulaire de connexion decouvert sur une page du portail."""

    action: str
    method: str
    fields: dict[str, str]
    user_field: str
    password_field: str
    has_captcha: bool = False

    @property
    def hidden_count(self) -> int:
        return len(self.fields) - 2


# Marqueurs d'une session ouverte, testes apres normalisation (sans accents).
# "espace fournisseur" en a volontairement ete retire : c'est aussi le titre de
# la page de connexion, et sa presence faisait passer un mot de passe refuse
# pour une connexion reussie.
LOGGED_IN_MARKERS: tuple[str, ...] = (
    "deconnexion", "se deconnecter", "mon compte", "mon espace",
    "tableau de bord entreprise", "mes consultations",
)
_PASSWORD_INPUT = re.compile(r"""type\s*=\s*["']?password""", re.IGNORECASE)
CAPTCHA_MARKERS: tuple[str, ...] = ("captcha", "recaptcha", "code de securite", "g-recaptcha")


def discover_login_form(
    html: str, page_url: str, user_hint: str = "", password_hint: str = ""
) -> LoginForm | None:
    """Trouve le formulaire de connexion et releve TOUS ses champs.

    Indispensable sur une application PRADO comme le portail : le POST doit
    renvoyer les champs caches de la page (jeton d'etat, cible de postback), sans
    quoi le serveur rejette la soumission. Envoyer seulement l'identifiant et le
    mot de passe ne fonctionne pas.
    """
    soup = BeautifulSoup(html, "lxml")
    for form in soup.find_all("form"):
        password_inputs = form.find_all(
            "input", attrs={"type": lambda v: v and v.lower() == "password"}
        )
        if not password_inputs:
            continue

        fields: dict[str, str] = {}
        text_names: list[str] = []
        for element in form.find_all(["input", "select", "textarea"]):
            name = element.get("name")
            if not name:
                continue
            if element.name == "input":
                input_type = str(element.get("type") or "text").lower()
                if input_type in ("submit", "button", "image"):
                    # Un bouton nomme doit etre transmis : PRADO s'en sert pour
                    # savoir quelle action declencher.
                    fields[name] = str(element.get("value") or "")
                    continue
                if input_type in ("checkbox", "radio"):
                    if element.get("checked") is not None:
                        fields[name] = str(element.get("value") or "on")
                    continue
                fields[name] = str(element.get("value") or "")
                if input_type in ("text", "email", "tel"):
                    text_names.append(name)
            elif element.name == "select":
                selected = element.find("option", selected=True) or element.find("option")
                fields[name] = str(selected.get("value") or "") if selected else ""
            else:
                fields[name] = element.get_text() or ""

        password_field = next(
            (str(i.get("name")) for i in password_inputs if i.get("name")), ""
        )
        if not password_field:
            continue
        if password_hint and password_hint in fields:
            password_field = password_hint

        if user_hint and user_hint in fields:
            user_field = user_hint
        else:
            # A defaut d'indication, le champ texte qui precede le mot de passe.
            user_field = text_names[0] if text_names else ""
        if not user_field:
            continue

        fields.setdefault(user_field, "")
        action = str(form.get("action") or "")
        return LoginForm(
            action=urljoin(page_url, action) if action else page_url,
            method=str(form.get("method") or "post").lower(),
            fields=fields,
            user_field=user_field,
            password_field=password_field,
            has_captcha=any(
                marker in normalize_text(str(form))
                for marker in CAPTCHA_MARKERS
            ),
        )
    return None


def login_signal(html: str) -> tuple[bool, str]:
    """Dit si la page temoigne d'une session ouverte, et sur quel indice.

    L'ordre des tests importe. Un champ mot de passe encore present est le
    signal negatif le plus fiable et doit etre examine en premier : la page de
    connexion du portail porte des titres ("Espace fournisseur") que l'on
    retrouve aussi apres connexion, et s'y fier d'abord ferait passer un refus
    d'identifiants pour un succes.
    """
    if _PASSWORD_INPUT.search(html):
        return False, "un champ mot de passe est toujours présent sur la page"
    text = normalize_text(html[:200_000])
    for marker in LOGGED_IN_MARKERS:
        if marker in text:
            return True, f"marqueur « {marker} » trouvé"
    return True, "le formulaire de connexion a disparu (indice faible)"


def looks_logged_in(html: str) -> bool:
    return login_signal(html)[0]


class PortalDocumentFetcher:
    """Telecharge les pieces du DCE en ouvrant une session sur le portail.

    Le portail reserve le DCE aux comptes entreprise. La connexion suit le
    chemin complet d'un formulaire web : lecture de la page, recuperation des
    champs caches, POST avec le cookie de session.

    Ce chemin n'a pas pu etre essaye contre le vrai site, qui refuse les
    connexions hors Maroc. Il est volontairement generique (aucun nom de champ
    code en dur) et `radar portal-login-test` rend compte precisement de ce qui
    se passe depuis votre serveur.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.portal_base_url.rstrip("/")
        self._client: httpx.Client | None = None
        self._logged_in = False
        #: Compte rendu de la derniere tentative, pour le diagnostic.
        self.login_report: dict[str, Any] = {}

    def __enter__(self) -> PortalDocumentFetcher:
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.settings.portal_timeout_secs),
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.portal_user_agent,
                "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._client:
            self._client.close()
        self._client = None
        self._logged_in = False

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError(
                "Utilisez PortalDocumentFetcher comme gestionnaire de contexte "
                "(`with PortalDocumentFetcher() as f:`)."
            )
        return self._client

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def login(self) -> bool:
        """Ouvre une session. Renvoie False et renseigne ``login_report`` sinon."""
        if self._logged_in:
            return True
        self.login_report = {"attempted": False, "ok": False, "reason": "", "steps": []}

        if not self.settings.portal_credentials_set:
            self.login_report["reason"] = (
                "PORTAL_LOGIN et PORTAL_PASSWORD ne sont pas renseignes dans .env."
            )
            return False

        self.login_report["attempted"] = True
        login_url = self.base_url + self.settings.portal_login_path

        # 1. Lecture de la page : elle pose le cookie de session et porte le formulaire.
        try:
            page = self.client.get(login_url)
        except Exception as exc:
            self.login_report["reason"] = (
                f"Page de connexion injoignable ({type(exc).__name__}: {exc}). "
                "Le portail filtre les adresses IP hors Maroc."
            )
            return False
        self.login_report["steps"].append(
            f"GET {login_url} -> HTTP {page.status_code}, "
            f"{len(self.client.cookies)} cookie(s)"
        )
        if page.status_code >= 400:
            self.login_report["reason"] = (
                f"La page de connexion renvoie HTTP {page.status_code}. "
                "Verifiez PORTAL_LOGIN_PATH."
            )
            return False

        # 2. Decouverte du formulaire et de ses champs caches.
        form = discover_login_form(
            page.text,
            str(page.url),
            self.settings.portal_login_user_field,
            self.settings.portal_login_password_field,
        )
        if form is None:
            self.login_report["reason"] = (
                "Aucun formulaire de connexion sur cette page. Trouvez l'URL de "
                "l'Espace fournisseur dans un navigateur et reportez-la dans "
                "PORTAL_LOGIN_PATH, puis relancez `radar portal-login-test`."
            )
            return False

        self.login_report["form"] = {
            "action": form.action,
            "method": form.method,
            "user_field": form.user_field,
            "password_field": form.password_field,
            "hidden_fields": form.hidden_count,
            "captcha": form.has_captcha,
        }
        self.login_report["steps"].append(
            f"formulaire trouve : {form.hidden_count} champ(s) conserve(s), "
            f"identifiant={form.user_field}, mot de passe={form.password_field}"
        )

        if form.has_captcha:
            self.login_report["reason"] = (
                "Le formulaire de connexion comporte un captcha : la connexion "
                "automatique est impossible. Telechargez les DCE manuellement "
                "depuis le portail, ou demandez a la TGR un acces sans captcha."
            )
            return False

        # 3. Soumission : tous les champs du formulaire, credentials compris.
        payload = dict(form.fields)
        payload[form.user_field] = self.settings.portal_login
        payload[form.password_field] = self.settings.portal_password

        try:
            if form.method == "get":
                response = self.client.get(form.action, params=payload)
            else:
                response = self.client.post(
                    form.action,
                    data=payload,
                    headers={"Referer": str(page.url)},
                )
        except Exception as exc:
            self.login_report["reason"] = f"Envoi du formulaire echoue : {type(exc).__name__}: {exc}"
            return False

        self.login_report["steps"].append(
            f"{form.method.upper()} {form.action} -> HTTP {response.status_code}, "
            f"{len(self.client.cookies)} cookie(s)"
        )
        if response.status_code >= 400:
            self.login_report["reason"] = (
                f"Le portail refuse la soumission (HTTP {response.status_code})."
            )
            return False

        connected, indice = login_signal(response.text)
        self.login_report["steps"].append(f"verification : {indice}")
        if not connected:
            # Le portail reaffiche souvent son message d'erreur : on le remonte.
            message = _extract_error_message(response.text)
            self.login_report["reason"] = (
                "Le portail affiche toujours un formulaire de connexion. "
                + (f"Message du portail : « {message} ». " if message else "")
                + "Identifiants refuses, ou champ supplementaire attendu. "
                "Comparez avec les champs listes par `radar portal-login-probe`."
            )
            return False

        self._logged_in = True
        self.login_report["ok"] = True
        logger.info("Session portail ouverte pour %s", self.settings.portal_login)
        return True

    def _follow_download_form(self, html: str, page_url: str) -> httpx.Response | None:
        """Soumet le formulaire d'une page intermediaire de telechargement.

        Le portail intercale souvent une page de confirmation avant de servir le
        DCE. Sans cette etape, on recupere la page HTML au lieu du fichier.
        """
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if form is None:
            return None
        payload: dict[str, str] = {}
        for element in form.find_all(["input", "select", "textarea"]):
            name = element.get("name")
            if not name:
                continue
            if element.name == "input":
                input_type = str(element.get("type") or "text").lower()
                if input_type in ("checkbox", "radio") and element.get("checked") is None:
                    continue
                payload[name] = str(element.get("value") or "")
            else:
                payload[name] = ""
        action = urljoin(page_url, str(form.get("action") or ""))
        method = str(form.get("method") or "post").lower()
        try:
            if method == "get":
                return self.client.get(action, params=payload)
            return self.client.post(action, data=payload, headers={"Referer": page_url})
        except Exception as exc:
            logger.debug("Formulaire de telechargement non suivi: %s", exc)
            return None

    def download(self, document: TenderDocument) -> TenderDocument:
        """Recupere une piece et met son statut a jour. Ne leve jamais."""
        if not document.url.startswith("http"):
            document.status = DocumentStatus.FAILED
            document.error = (
                "Référence sans URL téléchargeable : ouvrez l'avis sur le portail."
            )
            return document

        self.login()
        destination = Path(self.settings.documents_dir)
        destination.mkdir(parents=True, exist_ok=True)
        limit = self.settings.documents_max_mb * 1024 * 1024

        try:
            response = self.client.get(document.url)

            if response.status_code in (401, 403):
                document.status = DocumentStatus.AUTH_REQUIRED
                document.error = self._auth_message(response.status_code)
                return document
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            # Une page HTML en reponse a une demande de fichier signale soit une
            # page de confirmation a soumettre, soit une redirection vers la
            # connexion. On tente la premiere hypothese avant de conclure.
            if "text/html" in content_type:
                followed = self._follow_download_form(response.text, str(response.url))
                if followed is not None and "text/html" not in followed.headers.get(
                    "content-type", ""
                ):
                    response = followed
                    content_type = response.headers.get("content-type", "")
                else:
                    document.status = DocumentStatus.AUTH_REQUIRED
                    document.error = (
                        "Le portail a renvoyé une page HTML au lieu du fichier. "
                        + (
                            "La session est ouverte, mais cette pièce passe par une "
                            "étape supplémentaire : ouvrez l'avis sur le portail."
                            if self._logged_in
                            else self._auth_message(response.status_code)
                        )
                    )
                    return document

            payload = response.content
            if len(payload) > limit:
                document.status = DocumentStatus.FAILED
                document.error = (
                    f"Fichier de {len(payload) / 1048576:.1f} Mo, au-delà de la limite "
                    f"de {self.settings.documents_max_mb} Mo (DOCUMENTS_MAX_MB)."
                )
                return document

            path = destination / safe_filename(document)
            path.write_bytes(payload)

            document.status = DocumentStatus.DOWNLOADED
            document.local_path = str(path)
            document.content_type = content_type or None
            document.size_bytes = len(payload)
            document.sha256 = hashlib.sha256(payload).hexdigest()
            document.error = None
            document.downloaded_at = datetime.now(timezone.utc)
            logger.info("Piece telechargee: %s (%d octets)", path.name, len(payload))
        except Exception as exc:
            document.status = DocumentStatus.FAILED
            document.error = f"{type(exc).__name__}: {exc}"
            logger.warning("Telechargement de %s echoue: %s", document.url, exc)
        return document

    def _auth_message(self, status_code: int) -> str:
        if self._logged_in:
            return (
                f"Accès refusé malgré la session ouverte (HTTP {status_code}). "
                "Ce compte n'a peut-être pas les droits sur cette consultation, "
                "ou il faut s'y être inscrit depuis le portail."
            )
        reason = self.login_report.get("reason") or "aucune tentative de connexion."
        return (
            f"Le portail exige un compte entreprise (HTTP {status_code}). "
            f"Connexion non établie : {reason}"
        )


def download_pending(
    session: Session, settings: Settings | None = None, limit: int = 50
) -> dict[str, int]:
    """Telecharge les pieces encore non recuperees. Renvoie le decompte par statut."""
    settings = settings or get_settings()
    pending = list(
        session.scalars(
            select(TenderDocument)
            .where(TenderDocument.status == DocumentStatus.LISTED)
            .limit(limit)
        ).all()
    )
    counts: dict[str, int] = {}
    if not pending:
        return counts
    with PortalDocumentFetcher(settings) as fetcher:
        for document in pending:
            fetcher.download(document)
            counts[document.status.value] = counts.get(document.status.value, 0) + 1
    session.flush()
    return counts

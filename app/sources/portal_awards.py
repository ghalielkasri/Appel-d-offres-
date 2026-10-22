"""Collecte des resultats d'attribution sur le portail marchespublics.gov.ma.

Pourquoi un parseur a part : aucun actor Apify ne publie encore les avis
d'attribution (attributaire + montant adjuge). Or c'est la rubrique "Resultats"
du portail qui porte, pour chaque consultation decidee, le titulaire, le montant
de son offre et le nombre de concurrents.

Le portail est une application PRADO dont le balisage change au fil des versions.
Le parseur ne s'appuie donc pas sur des chemins CSS figes mais sur deux lectures
complementaires :

1. **Tableaux** : les en-tetes de colonnes sont reconnus par mots-cles
   (``attributaire``, ``montant``, ``reference``...), dans n'importe quel ordre.
2. **Fiches** : a defaut de tableau, les paires "libelle : valeur" sont lues
   directement dans le texte.

Consequence pratique : une refonte du portail qui conserve les mêmes libelles ne
casse pas la collecte. Si les libelles changent, il suffit d'etendre
``COLUMN_PATTERNS`` et ``LABEL_PATTERNS`` ci-dessous.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import Settings, get_settings
from app.normalize import (
    clean_whitespace,
    detect_tax_inclusion,
    normalize_text,
    parse_amount,
    parse_date,
    parse_int,
)
from app.sources.apify_tenders import map_category
from app.sources.base import AwardRecord, Source

logger = logging.getLogger(__name__)

# Chemins candidats de la rubrique "Resultats" du portail. Le premier qui repond
# en HTTP 200 avec un tableau exploitable est retenu, et le choix est journalise.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/index.php?page=entreprise.EntrepriseAdvancedSearch&searchAnnonce&keyWord=&typeAvis=4",
    "/index.php?page=entreprise.ResultatsRecherche&typeAvis=attribution",
    "/index.php?page=entreprise.EntrepriseResultatAttribution",
    "/index.php?page=entreprise.EntrepriseDecisionResultat",
    "/portail.php?page=entreprise.EntrepriseResultatAttribution",
)

# Reconnaissance des colonnes : champ interne -> motifs cherches dans l'en-tete.
COLUMN_PATTERNS: dict[str, tuple[str, ...]] = {
    "reference": ("reference", "numero de consultation", "numero", "num", "consultation"),
    "object": ("objet", "intitule", "designation", "description"),
    "buyer_raw": ("acheteur", "organisme", "maitre d", "entite", "administration"),
    "supplier_raw": (
        "attributaire", "titulaire", "adjudicataire", "soumissionnaire retenu",
        "entreprise retenue", "beneficiaire",
    ),
    "awarded_amount": (
        "montant de l offre", "montant adjuge", "montant attribue",
        "montant du marche", "montant retenu", "montant",
    ),
    "estimated_amount": ("estimation", "montant estime", "budget"),
    "bids_count": (
        "nombre de concurrents", "nombre de devis", "nombre d offres",
        "concurrents", "offres recues", "plis",
    ),
    "award_date": (
        "date d attribution", "date du resultat", "date de la decision",
        "date de publication", "date",
    ),
    "category": ("categorie", "nature", "type de prestation"),
    "procedure_type": ("procedure", "type d avis", "mode de passation"),
    "lot_number": ("lot", "n lot", "numero de lot"),
}

# Reconnaissance des paires "libelle : valeur" dans les fiches sans tableau.
LABEL_PATTERNS: dict[str, tuple[str, ...]] = {
    "supplier_raw": ("attributaire", "titulaire", "adjudicataire", "entreprise retenue"),
    "awarded_amount": ("montant de l offre", "montant adjuge", "montant attribue", "montant"),
    "bids_count": ("nombre de concurrents", "nombre de devis", "nombre d offres"),
    "reference": ("reference", "numero de consultation", "numero"),
    "object": ("objet", "intitule"),
    "buyer_raw": ("acheteur", "organisme", "maitre d ouvrage"),
    "award_date": ("date d attribution", "date du resultat"),
    "lot_number": ("lot",),
}

_LABEL_VALUE_RE = re.compile(
    r"(?P<label>[A-Za-zÀ-ÿ'’\s\.\-]{3,60})\s*:\s*(?P<value>[^\n\r]{1,300})"
)
# Les montants nuls ou absents sont ecrits de bien des manieres sur le portail.
_EMPTY_VALUES = {"", "-", "--", "neant", "n a", "na", "non renseigne", "sans objet", "0"}


def normalize_label(value: str | None) -> str:
    """Forme de comparaison d'un libelle : la ponctuation devient de l'espace.

    Indispensable car le portail ecrit "Montant de l'offre" ou "Date d'attribution" :
    sans cela, les apostrophes empechent les motifs de correspondre.
    """
    text = normalize_text(value)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _header_field(header: str) -> str | None:
    """Associe un en-tete de colonne a un champ interne, le motif le plus long d'abord."""
    text = normalize_label(header)
    if not text:
        return None
    best: tuple[int, str] | None = None
    for field, patterns in COLUMN_PATTERNS.items():
        for pattern in patterns:
            if pattern in text and (best is None or len(pattern) > best[0]):
                best = (len(pattern), field)
    return best[1] if best else None


def _label_field(label: str) -> str | None:
    text = normalize_label(label)
    if not text:
        return None
    best: tuple[int, str] | None = None
    for field, patterns in LABEL_PATTERNS.items():
        for pattern in patterns:
            if pattern in text and (best is None or len(pattern) > best[0]):
                best = (len(pattern), field)
    return best[1] if best else None


def _is_empty(value: str | None) -> bool:
    return value is None or normalize_label(value) in _EMPTY_VALUES


def _cell_text(cell: Tag) -> str:
    return clean_whitespace(cell.get_text(" ", strip=True)) or ""


def _cell_link(cell: Tag, base_url: str) -> str | None:
    anchor = cell.find("a", href=True)
    if not anchor:
        return None
    return urljoin(base_url, str(anchor["href"]))


def _build_award(
    values: dict[str, str], url: str | None, source_name: str, base_url: str
) -> AwardRecord | None:
    """Un enregistrement n'est retenu que s'il porte vraiment une attribution."""
    supplier = clean_whitespace(values.get("supplier_raw"))
    amount_text = values.get("awarded_amount")
    amount = None if _is_empty(amount_text) else parse_amount(amount_text)
    if _is_empty(supplier) and amount is None:
        return None

    reference = clean_whitespace(values.get("reference"))
    source_key_parts = [p for p in (reference, values.get("lot_number"), supplier) if p]
    source_key = " | ".join(source_key_parts) or (url or "")
    if not source_key:
        return None

    return AwardRecord(
        source=source_name,
        source_key=source_key[:256],
        reference=reference,
        object=clean_whitespace(values.get("object")),
        lot_number=clean_whitespace(values.get("lot_number")),
        buyer_raw=clean_whitespace(values.get("buyer_raw")),
        supplier_raw=supplier,
        awarded_amount=amount,
        estimated_amount=parse_amount(values.get("estimated_amount")),
        amount_includes_tax=detect_tax_inclusion(amount_text),
        bids_count=parse_int(values.get("bids_count")),
        award_date=parse_date(values.get("award_date")),
        category=map_category(values.get("category")),
        procedure_type=clean_whitespace(values.get("procedure_type")),
        consultation_url=url or base_url,
        raw={"fields": values, "url": url},
    )


def parse_awards_tables(
    soup: BeautifulSoup, base_url: str, source_name: str
) -> list[AwardRecord]:
    """Lecture 1 : tout tableau dont les en-tetes designent une attribution."""
    awards: list[AwardRecord] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # L'en-tete est la premiere ligne qui contient des <th>, sinon la premiere ligne.
        header_row, header_index = None, 0
        for index, row in enumerate(rows[:3]):
            if row.find("th"):
                header_row, header_index = row, index
                break
        if header_row is None:
            header_row, header_index = rows[0], 0

        headers = [_cell_text(c) for c in header_row.find_all(["th", "td"])]
        mapping = {i: _header_field(h) for i, h in enumerate(headers)}
        fields = {f for f in mapping.values() if f}
        # Sans colonne attributaire ni montant, ce tableau n'est pas un tableau de resultats.
        if not fields & {"supplier_raw", "awarded_amount"}:
            continue

        for row in rows[header_index + 1 :]:
            cells = row.find_all(["td", "th"])
            if not cells or len(cells) < 2:
                continue
            values: dict[str, str] = {}
            url: str | None = None
            for index, cell in enumerate(cells):
                field = mapping.get(index)
                if field and field not in values:
                    values[field] = _cell_text(cell)
                url = url or _cell_link(cell, base_url)
            award = _build_award(values, url, source_name, base_url)
            if award:
                awards.append(award)
    return awards


def parse_awards_labels(
    soup: BeautifulSoup, base_url: str, source_name: str
) -> list[AwardRecord]:
    """Lecture 2 : fiches "libelle : valeur", quand il n'y a pas de tableau."""
    awards: list[AwardRecord] = []
    blocks = soup.find_all(["div", "li", "article", "section", "fieldset"])
    for block in blocks:
        text = block.get_text("\n", strip=True)
        if not text or len(text) > 4000:
            continue
        lowered = normalize_text(text)
        if not any(
            needle in lowered
            for needle in ("attributaire", "titulaire", "adjudicataire")
        ):
            continue
        # On ne garde que le bloc le plus interieur qui contient l'information.
        if block.find(
            lambda t: isinstance(t, Tag)
            and t.name in ("div", "li", "article", "section", "fieldset")
            and any(
                n in normalize_text(t.get_text(" ", strip=True))
                for n in ("attributaire", "titulaire", "adjudicataire")
            )
        ):
            continue

        values: dict[str, str] = {}
        for match in _LABEL_VALUE_RE.finditer(text):
            field = _label_field(match.group("label"))
            if field and field not in values:
                values[field] = match.group("value").strip()
        anchor = block.find("a", href=True)
        url = urljoin(base_url, str(anchor["href"])) if anchor else None
        award = _build_award(values, url, source_name, base_url)
        if award:
            awards.append(award)
    return awards


def parse_awards_html(html: str, base_url: str, source_name: str) -> list[AwardRecord]:
    """Extrait toutes les attributions d'une page, tableaux puis fiches."""
    soup = BeautifulSoup(html, "lxml")
    awards = parse_awards_tables(soup, base_url, source_name)
    if not awards:
        awards = parse_awards_labels(soup, base_url, source_name)
    # Deduplication intra-page, au cas ou un tableau serait imbrique dans un autre.
    unique: dict[str, AwardRecord] = {}
    for award in awards:
        unique.setdefault(award.source_key, award)
    return list(unique.values())


def find_result_links(html: str, base_url: str) -> list[dict[str, str]]:
    """Liste les liens de la page qui mentionnent les resultats ou attributions.

    Utilise par ``radar portal-discover`` pour identifier la bonne URL depuis une
    machine qui, elle, atteint le portail.
    """
    soup = BeautifulSoup(html, "lxml")
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        label = clean_whitespace(anchor.get_text(" ", strip=True)) or ""
        href = str(anchor["href"])
        haystack = normalize_text(f"{label} {href}")
        if not any(
            needle in haystack
            for needle in ("resultat", "attribution", "attributaire", "adjudication")
        ):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append({"label": label, "url": absolute})
    return found


class PortalAwardSource(Source):
    """Resultats d'attribution lus directement sur le portail officiel."""

    name = "portal:marchespublics"
    cost_per_item_usd = 0.0  # acces direct, aucun intermediaire facture

    def __init__(
        self, settings: Settings | None = None, paths: tuple[str, ...] | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.paths = paths or CANDIDATE_PATHS
        self.base_url = self.settings.portal_base_url.rstrip("/")

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.settings.portal_timeout_secs),
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.portal_user_agent,
                "Accept-Language": "fr-FR,fr;q=0.9,ar;q=0.8",
                "Accept": "text/html,application/xhtml+xml",
            },
        )

    def fetch_page(self, url: str) -> str:
        with self._client() as client:
            response = client.get(url)
            response.raise_for_status()
            return response.text

    def discover(self) -> list[dict[str, str]]:
        """Renvoie les liens "resultats" trouves sur la page d'accueil du portail."""
        html = self.fetch_page(self.base_url + "/")
        return find_result_links(html, self.base_url)

    def fetch_awards(self, since: date | None = None) -> list[AwardRecord]:
        """Essaie chaque chemin candidat et garde le premier qui donne des resultats."""
        if not self.settings.portal_enabled:
            logger.info("%s: desactive (PORTAL_ENABLED=false)", self.name)
            return []

        errors: list[str] = []
        for path in self.paths:
            url = self.base_url + path
            try:
                html = self.fetch_page(url)
            except Exception as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            awards = parse_awards_html(html, url, self.name)
            if awards:
                logger.info("%s: %d attributions depuis %s", self.name, len(awards), url)
                if since:
                    awards = [
                        a for a in awards if a.award_date is None or a.award_date >= since
                    ]
                return awards
            errors.append(f"{path}: page lue mais aucune attribution reconnue")

        logger.warning(
            "%s: aucun chemin exploitable.\n  %s\n"
            "Lancez `radar portal-discover` depuis le serveur pour trouver la bonne URL, "
            "puis `radar portal-parse <fichier.html>` pour verifier le parseur.",
            self.name, "\n  ".join(errors),
        )
        return []

    def fetch_awards_from_html(self, html: str, url: str | None = None) -> list[AwardRecord]:
        """Parse un HTML deja telecharge : sert a valider le parseur hors ligne."""
        return parse_awards_html(html, url or self.base_url, self.name)

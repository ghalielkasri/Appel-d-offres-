"""Collecte des avis de consultation via Apify.

Deux actors publient les avis de marchespublics.gov.ma avec des noms de champs
differents (camelCase chez publicmoney, snake_case chez jungle_synthesizer). Le
mapping ci-dessous accepte les deux, et ignore les champs absents : un actor qui
ajoute ou renomme un champ ne casse donc pas la collecte.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings, get_settings
from app.models import Category
from app.normalize import (
    clean_whitespace,
    normalize_text,
    parse_amount,
    parse_date,
    parse_datetime,
    parse_int,
)
from app.sources.apify_client import ApifyClient
from app.sources.base import AwardRecord, Source, TenderRecord

logger = logging.getLogger(__name__)

# Chaque champ interne accepte plusieurs noms possibles, essayes dans l'ordre.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "reference": ("reference", "consultation_ref", "consultationRef", "ref", "numero"),
    "object": ("object", "objet", "objet_consultation", "title", "description"),
    "category": ("category", "categorie", "nature"),
    "procedure_type": ("procedureType", "procedure_type", "procedure", "typeProcedure"),
    "buyer_raw": ("buyer", "buyer_entity", "buyerEntity", "acheteur", "organisme", "entity"),
    "buyer_service": ("buyer_service", "buyerService", "service"),
    "buyer_acronym": ("organisation_acronym", "organisationAcronym", "acronym"),
    "execution_place": ("execution_place", "executionPlace", "lieu", "lieuExecution"),
    "publication_date": ("publicationDate", "publication_date", "datePublication"),
    "submission_deadline": (
        "deadline", "submission_deadline", "submissionDeadline",
        "dateLimite", "date_limite", "closingDate",
    ),
    "opening_session_date": (
        "opening_session_date", "openingSessionDate", "dateOuverture", "openingDate",
    ),
    "estimated_amount": (
        "estimated_amount", "estimatedAmount", "estimatedValue",
        "estimation", "montantEstime",
    ),
    "caution_provisoire": (
        "caution_provisoire", "cautionProvisoire", "caution", "provisionalGuarantee",
    ),
    "consultation_url": ("consultationUrl", "consultation_url", "url", "link"),
    "currency": ("currency", "devise"),
    "lots": ("lots", "lotsCount", "lots_count", "nombreLots"),
    "notice_status": ("notice_status", "noticeStatus", "statut", "status"),
    # Presents dans le schema de jungle_synthesizer, vides tant que le portail
    # n'expose pas les avis d'attribution via cet actor.
    "awarded_supplier": ("awarded_supplier", "awardedSupplier", "attributaire", "titulaire"),
    "awarded_amount": ("awarded_amount", "awardedAmount", "montantAdjuge", "montant_adjuge"),
    "bids_count": ("bids_count", "bidsCount", "nombre_concurrents", "nombreDevis"),
    "award_date": ("award_date", "awardDate", "dateAttribution"),
}

_CATEGORY_MAP = {
    "travaux": Category.TRAVAUX,
    "works": Category.TRAVAUX,
    "fournitures": Category.FOURNITURES,
    "fourniture": Category.FOURNITURES,
    "supplies": Category.FOURNITURES,
    "goods": Category.FOURNITURES,
    "services": Category.SERVICES,
    "service": Category.SERVICES,
    "etudes": Category.SERVICES,
}


def pick(item: dict[str, Any], field: str) -> Any:
    """Premiere valeur non vide parmi les alias connus du champ."""
    for alias in FIELD_ALIASES.get(field, (field,)):
        if alias in item:
            value = item[alias]
            if value not in (None, "", [], {}):
                return value
    return None


def map_category(value: object) -> Category:
    text = normalize_text(str(value) if value is not None else "")
    if not text:
        return Category.AUTRE
    for needle, category in _CATEGORY_MAP.items():
        if needle in text:
            return category
    return Category.AUTRE


def _lots_count(value: object) -> int | None:
    if isinstance(value, list):
        return len(value) or None
    return parse_int(value)


def map_tender(item: dict[str, Any], source_name: str) -> TenderRecord | None:
    """Transforme un enregistrement brut d'Apify en TenderRecord.

    Renvoie None si l'enregistrement n'a ni reference ni URL : sans identifiant
    stable, on ne peut ni dedupliquer ni suivre l'avis dans le temps.
    """
    reference = clean_whitespace(pick(item, "reference"))
    url = clean_whitespace(pick(item, "consultation_url"))
    if not reference and not url:
        logger.debug("Enregistrement ignore (ni reference ni URL): %s", item)
        return None

    return TenderRecord(
        source=source_name,
        source_key=reference or url or "",
        reference=reference,
        object=clean_whitespace(pick(item, "object")),
        category=map_category(pick(item, "category")),
        procedure_type=clean_whitespace(pick(item, "procedure_type")),
        buyer_raw=clean_whitespace(pick(item, "buyer_raw")),
        buyer_service=clean_whitespace(pick(item, "buyer_service")),
        buyer_acronym=clean_whitespace(pick(item, "buyer_acronym")),
        execution_place=clean_whitespace(pick(item, "execution_place")),
        lots_count=_lots_count(pick(item, "lots")),
        publication_date=parse_date(pick(item, "publication_date")),
        submission_deadline=parse_datetime(pick(item, "submission_deadline")),
        opening_session_date=parse_datetime(pick(item, "opening_session_date")),
        estimated_amount=parse_amount(pick(item, "estimated_amount")),
        caution_provisoire=parse_amount(pick(item, "caution_provisoire")),
        currency=clean_whitespace(pick(item, "currency")) or "MAD",
        consultation_url=url,
        raw=item,
    )


def map_award(item: dict[str, Any], source_name: str) -> AwardRecord | None:
    """Extrait une attribution d'un enregistrement d'avis, s'il en porte une."""
    supplier = clean_whitespace(pick(item, "awarded_supplier"))
    amount = parse_amount(pick(item, "awarded_amount"))
    if not supplier and amount is None:
        return None

    reference = clean_whitespace(pick(item, "reference"))
    url = clean_whitespace(pick(item, "consultation_url"))
    return AwardRecord(
        source=source_name,
        source_key=reference or url or "",
        reference=reference,
        object=clean_whitespace(pick(item, "object")),
        buyer_raw=clean_whitespace(pick(item, "buyer_raw")),
        supplier_raw=supplier,
        awarded_amount=amount,
        estimated_amount=parse_amount(pick(item, "estimated_amount")),
        currency=clean_whitespace(pick(item, "currency")) or "MAD",
        bids_count=parse_int(pick(item, "bids_count")),
        award_date=parse_date(pick(item, "award_date")),
        category=map_category(pick(item, "category")),
        procedure_type=clean_whitespace(pick(item, "procedure_type")),
        consultation_url=url,
        raw=item,
    )


class ApifyTenderSource(Source):
    """Avis de consultation collectes via un actor Apify."""

    cost_per_item_usd = 0.002  # tarif du palier gratuit, cf. README de l'actor

    def __init__(
        self,
        settings: Settings | None = None,
        actor_id: str | None = None,
        max_items: int | None = None,
        name: str | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.actor_id = actor_id or self.settings.apify_actor
        self.max_items = max_items or self.settings.apify_max_items
        self.name = name or f"apify:{self.actor_id.split('~')[-1]}"
        # Les deux actors n'ont pas la meme entree : on s'adapte a l'actor cible.
        self._supports_keyword = "publicmoney" in self.actor_id
        self._last_raw_items: list[dict[str, Any]] = []

    def build_input(self, keyword: str | None = None) -> dict[str, Any]:
        if self._supports_keyword:
            payload: dict[str, Any] = {
                "maxItems": self.max_items,
                "maxPages": self.settings.apify_max_pages,
                "schemaOrg": False,
            }
            if keyword:
                payload["keyword"] = keyword
            return payload
        return {"maxItems": self.max_items}

    def _collect(self, keyword: str | None) -> list[dict[str, Any]]:
        with ApifyClient(
            self.settings.apify_token, timeout_secs=self.settings.apify_timeout_secs
        ) as client:
            items, run = client.run_and_collect(self.actor_id, self.build_input(keyword))
        logger.info(
            "%s: %d avis pour le mot-cle %r (run %s)",
            self.name, len(items), keyword or "<tous>", run.get("id"),
        )
        return items

    def fetch_tenders(self, keywords: list[str] | None = None) -> list[TenderRecord]:
        """Un run par mot-cle (filtrage cote portail, donc moins de volume facture).

        Sans mot-cle, un seul run ramene tout ce que l'actor peut voir.
        """
        queries: list[str | None] = list(dict.fromkeys(keywords or [])) or [None]
        if not self._supports_keyword:
            queries = [None]  # cet actor ne sait pas filtrer a la source

        raw_items: list[dict[str, Any]] = []
        seen: set[str] = set()
        records: list[TenderRecord] = []

        for keyword in queries:
            try:
                items = self._collect(keyword)
            except Exception as exc:  # un mot-cle en echec ne doit pas tout arreter
                logger.warning("%s: echec sur le mot-cle %r: %s", self.name, keyword, exc)
                continue
            raw_items.extend(items)
            for item in items:
                record = map_tender(item, self.name)
                if record is None or record.source_key in seen:
                    continue
                seen.add(record.source_key)
                records.append(record)

        self._last_raw_items = raw_items
        return records

    def fetch_awards(self, since: object = None) -> list[AwardRecord]:
        """Recupere les attributions portees par les avis deja collectes.

        Aucun des deux actors ne publie encore les avis d'attribution ; cette
        methode renvoie donc une liste vide en pratique, mais prendra le relais
        automatiquement le jour ou les champs seront remplis.
        """
        awards: list[AwardRecord] = []
        seen: set[str] = set()
        for item in self._last_raw_items:
            award = map_award(item, self.name)
            if award and award.source_key not in seen:
                seen.add(award.source_key)
                awards.append(award)
        return awards

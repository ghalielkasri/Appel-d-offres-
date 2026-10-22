"""Contrat commun a toutes les sources de donnees."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from datetime import date, datetime

from app.models import Category


@dataclass(slots=True)
class TenderRecord:
    """Un avis de consultation, deja normalise, pret a etre enregistre."""

    source: str
    source_key: str
    reference: str | None = None
    object: str | None = None
    category: Category = Category.AUTRE
    procedure_type: str | None = None
    buyer_raw: str | None = None
    buyer_service: str | None = None
    buyer_acronym: str | None = None
    execution_place: str | None = None
    lots_count: int | None = None
    publication_date: date | None = None
    submission_deadline: datetime | None = None
    opening_session_date: datetime | None = None
    estimated_amount: float | None = None
    caution_provisoire: float | None = None
    currency: str = "MAD"
    consultation_url: str | None = None
    raw: dict = field(default_factory=dict)

    def searchable_text(self) -> str:
        """Le texte sur lequel le moteur de filtrage travaille."""
        parts = [
            self.object,
            self.buyer_raw,
            self.buyer_service,
            self.procedure_type,
            self.execution_place,
            self.reference,
        ]
        return " ".join(part for part in parts if part)


@dataclass(slots=True)
class AwardRecord:
    """Un resultat d'attribution : qui a gagne et pour quel montant."""

    source: str
    source_key: str
    reference: str | None = None
    object: str | None = None
    lot_number: str | None = None
    buyer_raw: str | None = None
    supplier_raw: str | None = None
    awarded_amount: float | None = None
    estimated_amount: float | None = None
    currency: str = "MAD"
    amount_includes_tax: bool | None = None
    bids_count: int | None = None
    award_date: date | None = None
    category: Category = Category.AUTRE
    procedure_type: str | None = None
    consultation_url: str | None = None
    raw: dict = field(default_factory=dict)


class Source(ABC):
    """Une source sait collecter des avis, des attributions, ou les deux."""

    name: str = "source"
    #: Cout indicatif par enregistrement, en USD, pour estimer la facture.
    cost_per_item_usd: float = 0.0

    def fetch_tenders(self, keywords: list[str] | None = None) -> list[TenderRecord]:
        return []

    def fetch_awards(self, since: date | None = None) -> list[AwardRecord]:
        return []

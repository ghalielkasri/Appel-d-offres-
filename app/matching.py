"""Moteur de filtrage : decide si un avis releve de votre domaine, et a quel point.

Le score sert a trier : un avis dont l'objet contient plusieurs de vos mots-cles
passe devant un avis ou le terme apparait seulement dans le nom de l'acheteur.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models import WatchProfile
from app.normalize import normalize_text
from app.sources.base import TenderRecord

# Poids par zone : l'objet du marche est le signal le plus fiable.
WEIGHT_OBJECT = 3.0
WEIGHT_PROCEDURE = 1.0
WEIGHT_BUYER = 0.5
WEIGHT_PLACE = 0.5


@dataclass(slots=True)
class MatchResult:
    profile_id: int | None
    profile_name: str
    score: float
    keywords: list[str]

    @property
    def matched(self) -> bool:
        return self.score > 0


def _contains_term(haystack: str, term: str) -> bool:
    """Cherche un terme en respectant les frontieres de mots.

    "pompe" ne doit pas correspondre a "pompier", mais "materiel" doit
    correspondre a "materiels". Un terme de plusieurs mots est cherche tel quel.
    """
    if not term or not haystack:
        return False
    pattern = r"\b" + re.escape(term) + r"\w{0,3}\b"
    return re.search(pattern, haystack) is not None


def score_tender(record: TenderRecord, profile: WatchProfile) -> MatchResult:
    """Score d'un avis pour un profil de veille donne."""
    result = MatchResult(
        profile_id=profile.id, profile_name=profile.name, score=0.0, keywords=[]
    )

    object_text = normalize_text(record.object)
    buyer_text = normalize_text(f"{record.buyer_raw or ''} {record.buyer_service or ''}")
    procedure_text = normalize_text(record.procedure_type)
    place_text = normalize_text(record.execution_place)
    full_text = " ".join([object_text, buyer_text, procedure_text, place_text])

    # Un mot-cle d'exclusion disqualifie l'avis, quel que soit le reste.
    for excluded in profile.excluded_keywords or []:
        if _contains_term(full_text, normalize_text(excluded)):
            return result

    # Filtre de categorie.
    if profile.categories and record.category.value not in profile.categories:
        return result

    # Filtre d'acheteur.
    if profile.buyers:
        wanted = [normalize_text(b) for b in profile.buyers if b]
        if not any(w and w in buyer_text for w in wanted):
            return result

    # Filtres de montant : appliques seulement si l'avis annonce une estimation.
    amount = record.estimated_amount
    if amount is not None:
        if profile.min_amount is not None and amount < profile.min_amount:
            return result
        if profile.max_amount is not None and amount > profile.max_amount:
            return result

    score = 0.0
    matched: list[str] = []
    for keyword in profile.keywords or []:
        term = normalize_text(keyword)
        if not term:
            continue
        hit = False
        if _contains_term(object_text, term):
            score += WEIGHT_OBJECT
            hit = True
        if _contains_term(procedure_text, term):
            score += WEIGHT_PROCEDURE
            hit = True
        if _contains_term(buyer_text, term):
            score += WEIGHT_BUYER
            hit = True
        if _contains_term(place_text, term):
            score += WEIGHT_PLACE
            hit = True
        if hit:
            matched.append(keyword)

    # Un profil sans mot-cle agit comme un filtre pur (categorie, acheteur, montant).
    if not (profile.keywords or []):
        score = max(score, profile.min_score or 1.0)

    result.score = round(score, 2)
    result.keywords = matched
    return result


def match_profiles(
    record: TenderRecord, profiles: list[WatchProfile]
) -> list[MatchResult]:
    """Profils declenches par cet avis, du plus pertinent au moins pertinent."""
    results = []
    for profile in profiles:
        if not profile.active:
            continue
        result = score_tender(record, profile)
        if result.matched and result.score >= (profile.min_score or 0.0):
            results.append(result)
    results.sort(key=lambda r: r.score, reverse=True)
    return results

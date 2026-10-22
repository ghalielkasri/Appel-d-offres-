"""Rapprochement de marches : trouver les precedents comparables a un avis en cours.

Question a laquelle ce module repond : « cet appel d'offres ressemble a quoi ?
qui a gagne les marches comparables, et a quel prix ? »

Trois signaux sont combines :

1. **Le donneur d'ordre.** Le meme acheteur reconduit souvent des besoins
   voisins, avec les memes exigences et souvent les memes candidats.
2. **La famille d'equipement** (``app/taxonomy.py``) : froid, cuisson, laverie...
   Deux marches qui partagent une famille portent sur le meme type de materiel.
3. **La proximite textuelle** des objets, mesuree par ``rapidfuzz`` sur les
   mots significatifs, ce qui tolere les formulations differentes.

Un precedent n'est retenu que s'il presente au moins un signal fort : meme
acheteur, famille commune, ou objets vraiment proches. Sans cette regle, le
simple partage du mot « acquisition » suffirait a rapprocher n'importe quoi.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median

from rapidfuzz import fuzz
from sqlalchemy import Text, cast, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Award, Tender, TenderStatus
from app.normalize import normalize_text
from app.taxonomy import family_label

# Mots trop courants dans les intitules de marches pour porter du sens.
# "materiel" et "equipement" en font partie : presents dans presque tous les
# avis, ils rapprochaient a tort un marche informatique d'un marche de cuisine.
# Ils restent indispensables au profil de veille, mais pas a la discrimination.
STOPWORDS: frozenset[str] = frozenset(
    """
    acquisition fourniture fournitures achat achats livraison installation
    marche marches lot lots travaux prestation prestations service services
    appel offres offre consultation reglement divers diverses
    materiel materiels equipement equipements materielle
    complet complete complets completes neuf neufs
    pour avec dans sans sous entre chez vers par
    des les une aux aux duxx
    profit compte cadre besoin besoins
    annee annees exercice budget
    ville commune province region prefecture royaume maroc marocaine
    ministere direction delegation agence office etablissement centre
    relatif relative concernant objet
    """.split()
)

# Ponderation des trois signaux. La somme fait 1.
WEIGHT_TEXT = 0.45
WEIGHT_FAMILY = 0.30
WEIGHT_BUYER = 0.25

#: En dessous, le rapprochement n'est pas montre.
MIN_SCORE = 0.30
#: Proximite textuelle suffisant a elle seule a qualifier un precedent.
STRONG_TEXT = 0.62
#: Proximite textuelle minimale pour admettre un lien thematique.
TOPICAL_TEXT = 0.50


def significant_tokens(*texts: str | None) -> list[str]:
    """Mots porteurs de sens d'un intitule, sans accents ni mots vides."""
    haystack = normalize_text(" ".join(t for t in texts if t))
    tokens = re.split(r"[^a-z0-9]+", haystack)
    seen: dict[str, None] = {}
    for token in tokens:
        if len(token) >= 4 and token not in STOPWORDS and not token.isdigit():
            seen.setdefault(token, None)
    return list(seen)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def text_similarity(left: str | None, right: str | None) -> float:
    """Proximite de deux intitules, entre 0 et 1.

    ``token_set_ratio`` compare des ensembles de mots : il reste eleve quand les
    memes termes apparaissent dans un ordre different ou noyes dans des
    formulations plus longues, ce qui est le cas courant d'un avis a l'autre.
    """
    a = " ".join(significant_tokens(left))
    b = " ".join(significant_tokens(right))
    if not a or not b:
        return 0.0
    return fuzz.token_set_ratio(a, b) / 100.0


@dataclass(slots=True)
class Comparable:
    """Un precedent rapproche d'un avis, avec la raison du rapprochement."""

    award: Award
    score: float
    same_buyer: bool
    shared_families: list[str] = field(default_factory=list)
    text_score: float = 0.0
    #: Nombre de mots significatifs communs aux deux objets.
    shared_tokens: int = 0

    @property
    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.same_buyer:
            out.append("même donneur d'ordre")
        if self.shared_families:
            labels = ", ".join(family_label(f) for f in self.shared_families)
            out.append(f"même type d'équipement ({labels})")
        if self.text_score >= 0.5:
            out.append(f"objet proche à {self.text_score * 100:.0f} %")
        return out or ["rapprochement faible"]

    @property
    def confidence(self) -> str:
        if self.score >= 0.70:
            return "forte"
        if self.score >= 0.50:
            return "moyenne"
        return "indicative"


def score_pair(
    tender: Tender, award: Award, tender_tokens: set[str] | None = None
) -> Comparable:
    """Note le rapprochement entre un avis et une attribution passee."""
    same_buyer = bool(
        tender.buyer_id and award.buyer_id and tender.buyer_id == award.buyer_id
    )
    tender_families = set(tender.families or [])
    award_families = set(award.families or [])
    shared = sorted(tender_families & award_families)
    family_score = _jaccard(tender_families, award_families)
    text_score = text_similarity(tender.object, award.object)
    shared_tokens = len(
        set(significant_tokens(tender.object)) & set(significant_tokens(award.object))
    )

    score = (
        WEIGHT_TEXT * text_score
        + WEIGHT_FAMILY * family_score
        + WEIGHT_BUYER * (1.0 if same_buyer else 0.0)
    )
    return Comparable(
        award=award,
        score=round(score, 3),
        same_buyer=same_buyer,
        shared_families=shared,
        text_score=round(text_score, 3),
        shared_tokens=shared_tokens,
    )


def _has_topical_link(comparable: Comparable) -> bool:
    """Le rapprochement porte-t-il sur le meme sujet ?

    Le meme acheteur ne suffit pas : sans cette regle, un marche informatique
    de la meme commune serait presente comme comparable a un marche de cuisine.
    Il faut une famille d'equipement commune, ou des objets reellement proches.
    L'acheteur commun ne fait que remonter le precedent dans le classement.
    """
    if comparable.shared_families:
        return True
    # Un lien purement textuel exige au moins deux mots significatifs communs :
    # un seul terme partage produit des rapprochements fortuits.
    return comparable.text_score >= TOPICAL_TEXT and comparable.shared_tokens >= 2


def candidate_awards(
    session: Session, tender: Tender, max_candidates: int = 600
) -> list[Award]:
    """Presélection SQL, pour ne pas noter toute la base a chaque affichage."""
    clauses = []
    if tender.buyer_id:
        clauses.append(Award.buyer_id == tender.buyer_id)
    for token in significant_tokens(tender.object)[:8]:
        clauses.append(Award.search_text.ilike(f"%{token}%"))
    # Les familles sont stockees en JSON : une comparaison textuelle suffit ici,
    # le calcul exact etant refait en Python sur les candidats retenus.
    for family in (tender.families or [])[:6]:
        clauses.append(cast(Award.families, Text).ilike(f'%"{family}"%'))

    if not clauses:
        return []
    stmt = (
        select(Award)
        .options(selectinload(Award.supplier))
        .where(or_(*clauses))
        .order_by(Award.award_date.desc().nullslast())
        .limit(max_candidates)
    )
    return list(session.scalars(stmt).all())


def comparable_awards(
    session: Session, tender: Tender, limit: int = 12, min_score: float = MIN_SCORE
) -> list[Comparable]:
    """Marches passes comparables a cet avis, du plus proche au moins proche."""
    results: list[Comparable] = []
    for award in candidate_awards(session, tender):
        # Un avis ne se compare pas a sa propre attribution.
        if award.tender_id == tender.id:
            continue
        comparable = score_pair(tender, award)
        if comparable.score >= min_score and _has_topical_link(comparable):
            results.append(comparable)
    results.sort(key=lambda c: c.score, reverse=True)
    return results[:limit]


def comparable_tenders(
    session: Session, tender: Tender, limit: int = 8, min_score: float = MIN_SCORE
) -> list[tuple[Tender, float, list[str]]]:
    """Autres consultations en cours qui ressemblent a celle-ci."""
    clauses = []
    if tender.buyer_id:
        clauses.append(Tender.buyer_id == tender.buyer_id)
    for token in significant_tokens(tender.object)[:8]:
        clauses.append(Tender.search_text.ilike(f"%{token}%"))
    if not clauses:
        return []

    candidates = session.scalars(
        select(Tender)
        .where(Tender.id != tender.id, Tender.status == TenderStatus.OPEN, or_(*clauses))
        .limit(400)
    ).all()

    tender_families = set(tender.families or [])
    out: list[tuple[Tender, float, list[str]]] = []
    for other in candidates:
        same_buyer = bool(
            tender.buyer_id and other.buyer_id and tender.buyer_id == other.buyer_id
        )
        other_families = set(other.families or [])
        shared = sorted(tender_families & other_families)
        text_score = text_similarity(tender.object, other.object)
        score = (
            WEIGHT_TEXT * text_score
            + WEIGHT_FAMILY * _jaccard(tender_families, other_families)
            + WEIGHT_BUYER * (1.0 if same_buyer else 0.0)
        )
        if score < min_score:
            continue
        # Meme regle que pour les attributions : un lien thematique est exige.
        shared_tokens = len(
            set(significant_tokens(tender.object)) & set(significant_tokens(other.object))
        )
        if not (shared or (text_score >= TOPICAL_TEXT and shared_tokens >= 2)):
            continue
        reasons = []
        if same_buyer:
            reasons.append("même donneur d'ordre")
        if shared:
            reasons.append("même type d'équipement (" + ", ".join(family_label(f) for f in shared) + ")")
        if text_score >= 0.5:
            reasons.append(f"objet proche à {text_score * 100:.0f} %")
        out.append((other, round(score, 3), reasons))

    out.sort(key=lambda item: item[1], reverse=True)
    return out[:limit]


@dataclass(slots=True)
class PriceBenchmark:
    """Reperes de prix tires des marches comparables."""

    count: int = 0
    minimum: float | None = None
    median_amount: float | None = None
    maximum: float | None = None
    avg_bids: float | None = None
    estimate: float | None = None

    @property
    def gap_to_estimate(self) -> float | None:
        """Ecart en pourcentage entre le median adjuge et l'estimation de l'avis."""
        if self.median_amount is None or not self.estimate:
            return None
        return 100.0 * (self.median_amount - self.estimate) / self.estimate


def price_benchmark(
    comparables: list[Comparable], estimate: float | None = None
) -> PriceBenchmark:
    """Fourchette des montants adjuges sur les marches comparables.

    Le median est prefere a la moyenne : un seul tres gros marche dans la liste
    deplacerait la moyenne et donnerait un repere trompeur.
    """
    amounts = [
        c.award.awarded_amount for c in comparables if c.award.awarded_amount is not None
    ]
    bids = [c.award.bids_count for c in comparables if c.award.bids_count is not None]
    if not amounts:
        return PriceBenchmark(count=0, estimate=estimate)
    return PriceBenchmark(
        count=len(amounts),
        minimum=min(amounts),
        median_amount=float(median(amounts)),
        maximum=max(amounts),
        avg_bids=round(sum(bids) / len(bids), 1) if bids else None,
        estimate=estimate,
    )


def likely_competitors(comparables: list[Comparable], limit: int = 8) -> list[dict]:
    """Entreprises qui remportent le plus souvent les marches comparables.

    C'est la reponse pratique a « qui l'a gagne » : non pas un gagnant isole,
    mais les concurrents recurrents sur ce type de marche chez cet acheteur.
    """
    tally: dict[str, dict] = {}
    for comparable in comparables:
        award = comparable.award
        name = award.supplier.name if award.supplier else award.supplier_raw
        if not name:
            continue
        entry = tally.setdefault(
            name,
            {
                "name": name,
                "supplier_id": award.supplier_id,
                "wins": 0,
                "total_amount": 0.0,
                "last_award": None,
                "same_buyer_wins": 0,
            },
        )
        entry["wins"] += 1
        entry["total_amount"] += award.awarded_amount or 0.0
        if comparable.same_buyer:
            entry["same_buyer_wins"] += 1
        if award.award_date and (
            entry["last_award"] is None or award.award_date > entry["last_award"]
        ):
            entry["last_award"] = award.award_date

    ranked = sorted(
        tally.values(),
        key=lambda e: (e["same_buyer_wins"], e["wins"], e["total_amount"]),
        reverse=True,
    )
    return ranked[:limit]

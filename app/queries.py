"""Requetes de consultation et d'agregation : historique, classements, statistiques."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Award, Buyer, SourceRun, Supplier, Tender, TenderStatus
from app.normalize import normalize_text


@dataclass(slots=True)
class TenderFilters:
    search: str | None = None
    category: str | None = None
    status: str | None = None
    buyer: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    published_from: date | None = None
    published_to: date | None = None
    only_open: bool = False
    min_score: float | None = None
    order: str = "recent"


@dataclass(slots=True)
class AwardFilters:
    search: str | None = None
    supplier: str | None = None
    buyer: str | None = None
    category: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    awarded_from: date | None = None
    awarded_to: date | None = None
    order: str = "recent"


@dataclass(slots=True)
class Page:
    items: list[Any]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def _like(value: str) -> str:
    return f"%{value.strip()}%"


def search_terms(query: str | None) -> list[str]:
    """Decoupe la saisie en termes normalises, sans accents ni casse.

    Une recherche de plusieurs mots exige tous les mots (et non l'un d'entre
    eux) : « materiel tetouan » ne ramene que les avis qui portent les deux.
    """
    normalized = normalize_text(query)
    return [term for term in normalized.split(" ") if term]


def _search_clause(column, query: str | None):
    """Clause SQL de recherche sur une colonne deja normalisee."""
    terms = search_terms(query)
    if not terms:
        return None
    # Chaque terme doit apparaitre : on combine les conditions en ET.
    return and_(*[column.ilike(f"%{term}%") for term in terms])


def tender_query(filters: TenderFilters) -> Select:
    stmt = select(Tender)
    if filters.search:
        clause = _search_clause(Tender.search_text, filters.search)
        if clause is not None:
            # Le repli sur les colonnes brutes couvre les lignes enregistrees
            # avant l'ajout de search_text et non encore reindexees.
            needle = _like(filters.search)
            stmt = stmt.where(
                or_(
                    clause,
                    Tender.object.ilike(needle),
                    Tender.reference.ilike(needle),
                    Tender.buyer_raw.ilike(needle),
                )
            )
    if filters.category:
        stmt = stmt.where(Tender.category == filters.category)
    if filters.status:
        stmt = stmt.where(Tender.status == filters.status)
    if filters.buyer:
        clause = _search_clause(Tender.search_text, filters.buyer)
        stmt = stmt.where(
            clause if clause is not None else Tender.buyer_raw.ilike(_like(filters.buyer))
        )
    if filters.min_amount is not None:
        stmt = stmt.where(Tender.estimated_amount >= filters.min_amount)
    if filters.max_amount is not None:
        stmt = stmt.where(Tender.estimated_amount <= filters.max_amount)
    if filters.published_from:
        stmt = stmt.where(Tender.publication_date >= filters.published_from)
    if filters.published_to:
        stmt = stmt.where(Tender.publication_date <= filters.published_to)
    if filters.min_score is not None:
        stmt = stmt.where(Tender.match_score >= filters.min_score)
    if filters.only_open:
        stmt = stmt.where(
            Tender.status == TenderStatus.OPEN,
            or_(
                Tender.submission_deadline.is_(None),
                Tender.submission_deadline >= datetime.now(timezone.utc),
            ),
        )

    orders = {
        "recent": (Tender.publication_date.desc().nullslast(), Tender.id.desc()),
        "deadline": (Tender.submission_deadline.asc().nullslast(),),
        "amount": (Tender.estimated_amount.desc().nullslast(),),
        "score": (Tender.match_score.desc(), Tender.id.desc()),
    }
    return stmt.order_by(*orders.get(filters.order, orders["recent"]))


def paginate(session: Session, stmt: Select, page: int, per_page: int) -> Page:
    page = max(1, page)
    per_page = min(max(1, per_page), 500)
    total = session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ) or 0
    items = list(session.scalars(stmt.limit(per_page).offset((page - 1) * per_page)).all())
    return Page(items=items, total=total, page=page, per_page=per_page)


def list_tenders(
    session: Session, filters: TenderFilters, page: int = 1, per_page: int = 50
) -> Page:
    return paginate(session, tender_query(filters), page, per_page)


def award_query(filters: AwardFilters) -> Select:
    stmt = select(Award).options(selectinload(Award.supplier))
    if filters.search:
        clause = _search_clause(Award.search_text, filters.search)
        if clause is not None:
            needle = _like(filters.search)
            stmt = stmt.where(
                or_(
                    clause,
                    Award.object.ilike(needle),
                    Award.reference.ilike(needle),
                    Award.supplier_raw.ilike(needle),
                )
            )
    if filters.supplier:
        clause = _search_clause(Award.search_text, filters.supplier)
        stmt = stmt.where(
            clause if clause is not None else Award.supplier_raw.ilike(_like(filters.supplier))
        )
    if filters.buyer:
        clause = _search_clause(Award.search_text, filters.buyer)
        stmt = stmt.where(
            clause if clause is not None else Award.buyer_raw.ilike(_like(filters.buyer))
        )
    if filters.category:
        stmt = stmt.where(Award.category == filters.category)
    if filters.min_amount is not None:
        stmt = stmt.where(Award.awarded_amount >= filters.min_amount)
    if filters.max_amount is not None:
        stmt = stmt.where(Award.awarded_amount <= filters.max_amount)
    if filters.awarded_from:
        stmt = stmt.where(Award.award_date >= filters.awarded_from)
    if filters.awarded_to:
        stmt = stmt.where(Award.award_date <= filters.awarded_to)

    orders = {
        "recent": (Award.award_date.desc().nullslast(), Award.id.desc()),
        "amount": (Award.awarded_amount.desc().nullslast(),),
        "supplier": (Award.supplier_raw.asc().nullslast(),),
    }
    return stmt.order_by(*orders.get(filters.order, orders["recent"]))


def list_awards(
    session: Session, filters: AwardFilters, page: int = 1, per_page: int = 50
) -> Page:
    return paginate(session, award_query(filters), page, per_page)


@dataclass(slots=True)
class SupplierStats:
    """Palmares d'un attributaire : combien de marches, pour quel montant."""

    supplier_id: int | None
    name: str
    awards_count: int
    total_amount: float
    avg_amount: float
    max_amount: float
    first_award: date | None
    last_award: date | None
    buyers_count: int
    avg_bids: float | None

    @property
    def win_share_label(self) -> str:
        return f"{self.awards_count} marche" + ("s" if self.awards_count > 1 else "")


def supplier_ranking(
    session: Session,
    limit: int = 50,
    search: str | None = None,
    category: str | None = None,
    awarded_from: date | None = None,
) -> list[SupplierStats]:
    """Classement des attributaires par montant total remporte."""
    stmt = (
        select(
            Award.supplier_id,
            func.coalesce(Supplier.name, Award.supplier_raw).label("name"),
            func.count(Award.id).label("awards_count"),
            func.coalesce(func.sum(Award.awarded_amount), 0.0).label("total_amount"),
            func.avg(Award.awarded_amount).label("avg_amount"),
            func.max(Award.awarded_amount).label("max_amount"),
            func.min(Award.award_date).label("first_award"),
            func.max(Award.award_date).label("last_award"),
            func.count(func.distinct(Award.buyer_id)).label("buyers_count"),
            func.avg(Award.bids_count).label("avg_bids"),
        )
        .outerjoin(Supplier, Award.supplier_id == Supplier.id)
        .where(or_(Award.supplier_id.isnot(None), Award.supplier_raw.isnot(None)))
        .group_by(Award.supplier_id, func.coalesce(Supplier.name, Award.supplier_raw))
        .order_by(func.coalesce(func.sum(Award.awarded_amount), 0.0).desc())
        .limit(limit)
    )
    if search:
        clause = _search_clause(Supplier.normalized_name, search)
        stmt = stmt.where(
            or_(
                clause if clause is not None else Supplier.name.ilike(_like(search)),
                Award.supplier_raw.ilike(_like(search)),
            )
        )
    if category:
        stmt = stmt.where(Award.category == category)
    if awarded_from:
        stmt = stmt.where(Award.award_date >= awarded_from)

    return [
        SupplierStats(
            supplier_id=row.supplier_id,
            name=row.name or "Attributaire non precise",
            awards_count=row.awards_count,
            total_amount=float(row.total_amount or 0.0),
            avg_amount=float(row.avg_amount or 0.0),
            max_amount=float(row.max_amount or 0.0),
            first_award=row.first_award,
            last_award=row.last_award,
            buyers_count=row.buyers_count or 0,
            avg_bids=float(row.avg_bids) if row.avg_bids is not None else None,
        )
        for row in session.execute(stmt).all()
    ]


@dataclass(slots=True)
class BuyerStats:
    buyer_id: int | None
    name: str
    awards_count: int
    total_amount: float
    suppliers_count: int


def buyer_ranking(session: Session, limit: int = 50) -> list[BuyerStats]:
    """Acheteurs qui depensent le plus, et leur nombre de fournisseurs distincts."""
    stmt = (
        select(
            Award.buyer_id,
            func.coalesce(Buyer.name, Award.buyer_raw).label("name"),
            func.count(Award.id).label("awards_count"),
            func.coalesce(func.sum(Award.awarded_amount), 0.0).label("total_amount"),
            func.count(func.distinct(Award.supplier_id)).label("suppliers_count"),
        )
        .outerjoin(Buyer, Award.buyer_id == Buyer.id)
        .group_by(Award.buyer_id, func.coalesce(Buyer.name, Award.buyer_raw))
        .order_by(func.coalesce(func.sum(Award.awarded_amount), 0.0).desc())
        .limit(limit)
    )
    return [
        BuyerStats(
            buyer_id=row.buyer_id,
            name=row.name or "Acheteur non precise",
            awards_count=row.awards_count,
            total_amount=float(row.total_amount or 0.0),
            suppliers_count=row.suppliers_count or 0,
        )
        for row in session.execute(stmt).all()
    ]


def supplier_history(session: Session, supplier_id: int) -> list[Award]:
    """Tous les marches remportes par un attributaire, du plus recent au plus ancien."""
    return list(
        session.scalars(
            select(Award)
            .where(Award.supplier_id == supplier_id)
            .order_by(Award.award_date.desc().nullslast(), Award.id.desc())
        ).all()
    )


def monthly_awards(session: Session, months: int = 18) -> list[dict[str, Any]]:
    """Serie mensuelle des attributions : volume et montant."""
    rows = session.execute(
        select(
            func.strftime("%Y-%m", Award.award_date).label("month")
            if session.bind and session.bind.dialect.name == "sqlite"
            else func.to_char(Award.award_date, "YYYY-MM").label("month"),
            func.count(Award.id).label("count"),
            func.coalesce(func.sum(Award.awarded_amount), 0.0).label("total"),
        )
        .where(Award.award_date.isnot(None))
        .group_by("month")
        .order_by("month")
    ).all()
    return [
        {"month": row.month, "count": row.count, "total": float(row.total or 0.0)}
        for row in rows
    ][-months:]


def category_breakdown(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            Award.category,
            func.count(Award.id).label("count"),
            func.coalesce(func.sum(Award.awarded_amount), 0.0).label("total"),
        ).group_by(Award.category)
    ).all()
    return [
        {
            "category": row.category.value if hasattr(row.category, "value") else str(row.category),
            "count": row.count,
            "total": float(row.total or 0.0),
        }
        for row in rows
    ]


@dataclass(slots=True)
class Summary:
    tenders_total: int = 0
    tenders_open: int = 0
    tenders_matched: int = 0
    tenders_closing_soon: int = 0
    awards_total: int = 0
    awards_amount: float = 0.0
    suppliers_total: int = 0
    buyers_total: int = 0
    last_run: SourceRun | None = None
    runs: list[SourceRun] = field(default_factory=list)


def summary(session: Session) -> Summary:
    """Chiffres d'en-tete du tableau de bord."""
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=7)
    result = Summary()
    result.tenders_total = session.scalar(select(func.count(Tender.id))) or 0
    result.tenders_open = (
        session.scalar(
            select(func.count(Tender.id)).where(
                Tender.status == TenderStatus.OPEN,
                or_(
                    Tender.submission_deadline.is_(None),
                    Tender.submission_deadline >= now,
                ),
            )
        )
        or 0
    )
    result.tenders_matched = (
        session.scalar(select(func.count(Tender.id)).where(Tender.match_score > 0)) or 0
    )
    result.tenders_closing_soon = (
        session.scalar(
            select(func.count(Tender.id)).where(
                Tender.status == TenderStatus.OPEN,
                Tender.submission_deadline.isnot(None),
                Tender.submission_deadline >= now,
                Tender.submission_deadline <= soon,
            )
        )
        or 0
    )
    result.awards_total = session.scalar(select(func.count(Award.id))) or 0
    result.awards_amount = float(
        session.scalar(select(func.coalesce(func.sum(Award.awarded_amount), 0.0))) or 0.0
    )
    result.suppliers_total = session.scalar(select(func.count(Supplier.id))) or 0
    result.buyers_total = session.scalar(select(func.count(Buyer.id))) or 0
    result.runs = list(
        session.scalars(select(SourceRun).order_by(SourceRun.id.desc()).limit(8)).all()
    )
    result.last_run = result.runs[0] if result.runs else None
    return result


@dataclass(slots=True)
class GlobalSearchResult:
    """Resultat d'une recherche unique portant sur tout le contenu."""

    query: str
    tenders: list[Tender] = field(default_factory=list)
    tenders_total: int = 0
    awards: list[Award] = field(default_factory=list)
    awards_total: int = 0
    suppliers: list["SupplierStats"] = field(default_factory=list)
    buyers: list[BuyerStats] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.tenders_total + self.awards_total + len(self.suppliers) + len(self.buyers)

    @property
    def is_empty(self) -> bool:
        return self.total == 0


def global_search(
    session: Session, query: str | None, limit: int = 20
) -> GlobalSearchResult:
    """Cherche le meme terme dans les avis, les attributions et les entreprises.

    Pensee pour la question courante : « qu'est-ce qui existe sur ce sujet, ou
    sur cette entreprise ? », sans avoir a choisir d'abord une rubrique.
    """
    result = GlobalSearchResult(query=query or "")
    if not search_terms(query):
        return result

    # Les avis : tous, pas seulement ceux du domaine surveille, car une
    # recherche explicite doit pouvoir sortir du perimetre de la veille.
    tenders = list_tenders(
        session, TenderFilters(search=query, min_score=None, order="recent"),
        page=1, per_page=limit,
    )
    result.tenders, result.tenders_total = tenders.items, tenders.total

    awards = list_awards(
        session, AwardFilters(search=query, order="amount"), page=1, per_page=limit
    )
    result.awards, result.awards_total = awards.items, awards.total

    result.suppliers = supplier_ranking(session, limit=limit, search=query)

    terms = search_terms(query)
    buyer_clause = and_(*[Buyer.normalized_name.ilike(f"%{t}%") for t in terms])
    matching_buyers = set(session.scalars(select(Buyer.id).where(buyer_clause)).all())
    if matching_buyers:
        result.buyers = [
            stats for stats in buyer_ranking(session, limit=200)
            if stats.buyer_id in matching_buyers
        ][:limit]
    return result


def closing_soon(session: Session, days: int = 7, limit: int = 20) -> list[Tender]:
    """Avis pertinents dont la date limite approche."""
    now = datetime.now(timezone.utc)
    return list(
        session.scalars(
            select(Tender)
            .where(
                Tender.match_score > 0,
                Tender.status == TenderStatus.OPEN,
                Tender.submission_deadline.isnot(None),
                Tender.submission_deadline >= now,
                Tender.submission_deadline <= now + timedelta(days=days),
            )
            .order_by(Tender.submission_deadline.asc())
            .limit(limit)
        ).all()
    )

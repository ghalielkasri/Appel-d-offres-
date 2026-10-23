"""Le marche vu depuis votre entreprise : classement, segments, concurrents, activite.

Tout part des attributions de votre domaine (celles dont l'objet designe une
famille d'equipement). Les calculs se font en Python sur la liste chargee une
fois par page : quelques milliers de lignes, et les familles sont une liste
JSON que SQLite et PostgreSQL ne filtrent pas de la meme facon.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.models import Award, Supplier, Tender, TenderStatus, WatchedSupplier
from app.normalize import normalize_entity_name
from app.taxonomy import EQUIPMENT_FAMILIES, GENERAL_FAMILY, family_label

#: Periodes du classement : cle de l'adresse -> (libelle, jours ; None = tout).
PERIODS: dict[str, tuple[str, int | None]] = {
    "12m": ("12 derniers mois", 365),
    "36m": ("3 ans", 3 * 365),
    "tout": ("Tout l'historique", None),
}
DEFAULT_PERIOD = "12m"

#: Une couleur par segment, prise dans les series et les teintes de contenu
#: (jamais les couleurs d'etat, reservees aux statuts).
FAMILY_COLORS: dict[str, str] = {
    GENERAL_FAMILY: "--ct-s1",
    "froid": "--ct-s3",
    "cuisson": "--ct-s2",
    "laverie": "--ct-s5",
    "preparation": "--ct-s4",
    "distribution": "--ct-mint-ink",
    "mobilier_inox": "--ct-ink-2",
    "ventilation": "--ct-muted",
    "boissons": "--ct-accent-ink",
    "vaisselle": "--ct-faint",
}
FAMILIES = list(EQUIPMENT_FAMILIES) + [GENERAL_FAMILY]

_DEMO_PREFIX = re.compile(r"^\s*demo[\s\-]+", re.IGNORECASE)


def display_name(name: str | None) -> str:
    """Le nom affiche : sans le marqueur « DEMO » des donnees fictives.

    Le marqueur reste dans la base et dans les exports ; a l'ecran, la banniere
    « donnees fictives » le remplace.
    """
    return _DEMO_PREFIX.sub("", name or "").strip() or (name or "")


def family_color(family: str) -> str:
    return f"var({FAMILY_COLORS.get(family, '--ct-faint')})"


# --------------------------------------------------------------------------- #
# Votre entreprise et vos concurrents suivis
# --------------------------------------------------------------------------- #


def own_supplier(session: Session, settings: Settings) -> Supplier | None:
    """Votre entreprise parmi les attributaires, forme juridique ignoree.

    Le jeu de demonstration marque ses entreprises « DEMO » : la meme raison
    sociale precedee du marqueur est donc acceptee aussi.
    """
    key = normalize_entity_name(settings.company_name)
    if not key:
        return None
    for candidate in (key, f"demo {key}"):
        supplier = session.scalar(select(Supplier).where(Supplier.normalized_name == candidate))
        if supplier is not None:
            return supplier
    return None


def watched_ids(session: Session) -> set[int]:
    return set(session.scalars(select(WatchedSupplier.supplier_id)).all())


def toggle_watch(session: Session, supplier_id: int) -> bool:
    """Suit ou ne suit plus un concurrent. Renvoie True s'il est desormais suivi."""
    row = session.scalar(select(WatchedSupplier).where(WatchedSupplier.supplier_id == supplier_id))
    if row is not None:
        session.delete(row)
        session.flush()
        return False
    session.add(WatchedSupplier(supplier_id=supplier_id))
    session.flush()
    return True


def has_demo_data(session: Session) -> bool:
    return bool(
        session.scalar(select(Tender.id).where(Tender.source.like("demo:%")).limit(1))
        or session.scalar(select(Award.id).where(Award.source.like("demo:%")).limit(1))
    )


# --------------------------------------------------------------------------- #
# Chargement et fenetres de temps
# --------------------------------------------------------------------------- #


def domain_awards(session: Session) -> list[Award]:
    """Les attributions de votre domaine : celles qui designent une famille d'equipement."""
    awards = session.scalars(
        select(Award).options(selectinload(Award.supplier)).order_by(Award.award_date.desc().nullslast())
    ).all()
    return [a for a in awards if a.families]


def window(period: str, today: date | None = None) -> tuple[date | None, date | None]:
    """(debut, debut de la periode precedente de meme duree) ; None = sans limite."""
    days = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])[1]
    if days is None:
        return None, None
    today = today or date.today()
    start = today - timedelta(days=days)
    return start, start - timedelta(days=days)


def in_window(award: Award, start: date | None, end: date | None = None) -> bool:
    if start is None and end is None:
        return True
    if award.award_date is None:
        return False
    if start is not None and award.award_date < start:
        return False
    return end is None or award.award_date < end


def of_family(awards: Iterable[Award], family: str | None) -> list[Award]:
    return [a for a in awards if not family or family in (a.families or [])]


def _key(award: Award) -> str:
    return f"s{award.supplier_id}" if award.supplier_id else f"n:{award.supplier_raw or '?'}"


def _name(award: Award) -> str:
    return award.supplier.name if award.supplier else (award.supplier_raw or "Attributaire non précisé")


# --------------------------------------------------------------------------- #
# Classement
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CompanyRow:
    key: str
    supplier_id: int | None
    name: str
    wins: int = 0
    amount: float = 0.0
    buyers: set[str] = field(default_factory=set)
    bids: list[int] = field(default_factory=list)
    last_win: date | None = None
    families: Counter = field(default_factory=Counter)
    rank: int = 0
    prev_rank: int | None = None
    share: float = 0.0
    is_me: bool = False
    watched: bool = False

    @property
    def avg_bids(self) -> float | None:
        return sum(self.bids) / len(self.bids) if self.bids else None

    @property
    def avg_amount(self) -> float:
        return self.amount / self.wins if self.wins else 0.0

    @property
    def buyers_count(self) -> int:
        return len(self.buyers)

    @property
    def delta(self) -> int | None:
        """Places gagnees (positif) ou perdues depuis la periode precedente."""
        return None if self.prev_rank is None else self.prev_rank - self.rank

    @property
    def top_family(self) -> str | None:
        return self.families.most_common(1)[0][0] if self.families else None


def rank_companies(
    awards: Iterable[Award], me_id: int | None = None, watched: set[int] | None = None
) -> list[CompanyRow]:
    """Classement par montant adjuge, puis par nombre de marches."""
    watched = watched or set()
    rows: dict[str, CompanyRow] = {}
    for award in awards:
        key = _key(award)
        row = rows.get(key)
        if row is None:
            row = rows[key] = CompanyRow(
                key=key, supplier_id=award.supplier_id, name=_name(award),
                is_me=bool(me_id) and award.supplier_id == me_id,
                watched=award.supplier_id in watched,
            )
        row.wins += 1
        row.amount += award.awarded_amount or 0.0
        if award.buyer_raw:
            row.buyers.add(award.buyer_raw)
        if award.bids_count is not None:
            row.bids.append(award.bids_count)
        if award.award_date and (row.last_win is None or award.award_date > row.last_win):
            row.last_win = award.award_date
        for family in award.families or []:
            row.families[family] += award.awarded_amount or 0.0

    ranked = sorted(rows.values(), key=lambda r: (r.amount, r.wins), reverse=True)
    total = sum(r.amount for r in ranked) or 1.0
    for index, row in enumerate(ranked, start=1):
        row.rank = index
        row.share = 100.0 * row.amount / total
    return ranked


def leaderboard(
    awards: list[Award],
    period: str = DEFAULT_PERIOD,
    family: str | None = None,
    me_id: int | None = None,
    watched: set[int] | None = None,
) -> list[CompanyRow]:
    """Classement de la periode, avec l'evolution depuis la periode precedente."""
    scoped = of_family(awards, family)
    start, prev_start = window(period)
    current = rank_companies([a for a in scoped if in_window(a, start)], me_id, watched)
    if start is not None:
        previous = {r.key: r.rank for r in rank_companies([a for a in scoped if in_window(a, prev_start, start)])}
        for row in current:
            row.prev_rank = previous.get(row.key)
    return current


def monthly(awards: Iterable[Award], months: int = 12, today: date | None = None) -> list[dict]:
    """Montant et nombre de marches par mois, mois vides compris, du plus ancien au plus recent."""
    today = today or date.today()
    keys: list[tuple[int, int]] = []
    year, month = today.year, today.month
    for _ in range(months):
        keys.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    keys.reverse()
    totals = {k: [0.0, 0] for k in keys}
    for award in awards:
        if award.award_date:
            k = (award.award_date.year, award.award_date.month)
            if k in totals:
                totals[k][0] += award.awarded_amount or 0.0
                totals[k][1] += 1
    labels = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
    return [
        {"label": f"{labels[m - 1]} {y}", "total": totals[(y, m)][0], "count": totals[(y, m)][1]}
        for y, m in keys
    ]


def spark_path(values: list[float], width: int = 88, height: int = 28) -> str:
    """Trace SVG d'une petite courbe ; plate si tout est nul."""
    if not values:
        return ""
    top = max(values) or 1.0
    step = width / max(len(values) - 1, 1)
    points = [
        f"{i * step:.1f},{height - 2 - (v / top) * (height - 4):.1f}" for i, v in enumerate(values)
    ]
    return "M" + " L".join(points)


# --------------------------------------------------------------------------- #
# Segments (familles d'equipement)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Segment:
    family: str
    label: str
    color: str
    amount: float = 0.0
    wins: int = 0
    share: float = 0.0
    companies: list[CompanyRow] = field(default_factory=list)
    open_tenders: int = 0
    me: CompanyRow | None = None

    @property
    def leader(self) -> CompanyRow | None:
        return self.companies[0] if self.companies else None


def segments(
    awards: list[Award],
    open_tenders: list[Tender],
    period: str = DEFAULT_PERIOD,
    me_id: int | None = None,
    watched: set[int] | None = None,
) -> list[Segment]:
    """Le marche decoupe par famille d'equipement, du plus gros au plus petit.

    Un marche qui couvre deux familles compte dans chacune : la part d'un
    segment est donc calculee sur la somme des segments, pas sur le total adjuge.
    """
    start, _ = window(period)
    scoped = [a for a in awards if in_window(a, start)]
    out: list[Segment] = []
    for family in FAMILIES:
        members = of_family(scoped, family)
        if not members:
            continue
        companies = rank_companies(members, me_id, watched)
        out.append(
            Segment(
                family=family,
                label=family_label(family),
                color=family_color(family),
                amount=sum(a.awarded_amount or 0.0 for a in members),
                wins=len(members),
                companies=companies,
                open_tenders=sum(1 for t in open_tenders if family in (t.families or [])),
                me=next((c for c in companies if c.is_me), None),
            )
        )
    total = sum(s.amount for s in out) or 1.0
    for seg in out:
        seg.share = 100.0 * seg.amount / total
    return sorted(out, key=lambda s: s.amount, reverse=True)


def open_domain_tenders(session: Session) -> list[Tender]:
    from datetime import datetime, timezone

    from sqlalchemy import or_

    now = datetime.now(timezone.utc)
    return list(
        session.scalars(
            select(Tender)
            .where(
                Tender.match_score > 0,
                Tender.status == TenderStatus.OPEN,
                or_(Tender.submission_deadline.is_(None), Tender.submission_deadline >= now),
            )
            .order_by(Tender.submission_deadline.asc().nullslast())
        ).all()
    )


# --------------------------------------------------------------------------- #
# Fiche entreprise : la vue « marque »
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CompanyView:
    supplier: Supplier
    wins: list[Award]
    row: CompanyRow | None
    segment: str | None
    segment_rank: list[CompanyRow]
    segment_row: CompanyRow | None
    families: list[tuple[str, float, int]]
    buyers: list[CompanyRow]

    @property
    def total(self) -> float:
        return sum(a.awarded_amount or 0.0 for a in self.wins)


def company_view(
    awards: list[Award],
    supplier: Supplier,
    period: str = DEFAULT_PERIOD,
    segment: str | None = None,
    me_id: int | None = None,
    watched: set[int] | None = None,
) -> CompanyView:
    """Tout ce que la fiche d'une entreprise montre, sur la periode choisie."""
    start, _ = window(period)
    mine_all = [a for a in awards if a.supplier_id == supplier.id]
    mine = [a for a in mine_all if in_window(a, start)]

    tally: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for award in mine:
        for family in award.families or []:
            tally[family][0] += award.awarded_amount or 0.0
            tally[family][1] += 1
    families = sorted(((f, v[0], v[1]) for f, v in tally.items()), key=lambda x: x[1], reverse=True)

    # Le segment de l'entreprise : celui ou elle gagne le plus, sauf choix explicite.
    seg = segment if segment in FAMILIES else (families[0][0] if families else None)
    board = leaderboard(awards, period, None, me_id, watched)
    segment_rank = leaderboard(awards, period, seg, me_id, watched) if seg else []

    buyers: dict[str, CompanyRow] = {}
    for award in mine:
        name = award.buyer_raw or "Acheteur non précisé"
        row = buyers.setdefault(name, CompanyRow(key=name, supplier_id=award.buyer_id, name=name))
        row.wins += 1
        row.amount += award.awarded_amount or 0.0
        if award.award_date and (row.last_win is None or award.award_date > row.last_win):
            row.last_win = award.award_date

    return CompanyView(
        supplier=supplier,
        wins=mine,
        row=next((r for r in board if r.supplier_id == supplier.id), None),
        segment=seg,
        segment_rank=segment_rank,
        segment_row=next((r for r in segment_rank if r.supplier_id == supplier.id), None),
        families=families,
        buyers=sorted(buyers.values(), key=lambda r: r.amount, reverse=True),
    )


def rank_window(rows: list[CompanyRow], supplier_id: int, size: int = 5) -> list[CompanyRow]:
    """Les voisins d'une entreprise dans un classement : deux au-dessus, deux en dessous."""
    index = next((i for i, r in enumerate(rows) if r.supplier_id == supplier_id), 0)
    start = max(0, min(index - size // 2, len(rows) - size))
    return rows[start:start + size]


@dataclass(slots=True)
class HeadToHead:
    """Vous face a un concurrent, acheteur par acheteur."""

    shared: list[dict]
    only_them: list[dict]
    only_me: list[dict]
    shared_families: list[str]


def head_to_head(awards: list[Award], me_id: int, other_id: int, period: str = "36m") -> HeadToHead:
    start, _ = window(period)
    scoped = [a for a in awards if in_window(a, start)]
    per: dict[str, dict] = {}
    fam_me: set[str] = set()
    fam_them: set[str] = set()
    for award in scoped:
        if award.supplier_id not in (me_id, other_id):
            continue
        name = award.buyer_raw or "Acheteur non précisé"
        entry = per.setdefault(
            name, {"name": name, "buyer_id": award.buyer_id, "me": 0, "me_amount": 0.0, "them": 0, "them_amount": 0.0}
        )
        side = "me" if award.supplier_id == me_id else "them"
        entry[side] += 1
        entry[f"{side}_amount"] += award.awarded_amount or 0.0
        (fam_me if side == "me" else fam_them).update(award.families or [])

    def by_amount(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda e: e["me_amount"] + e["them_amount"], reverse=True)

    return HeadToHead(
        shared=by_amount([e for e in per.values() if e["me"] and e["them"]]),
        only_them=by_amount([e for e in per.values() if e["them"] and not e["me"]]),
        only_me=by_amount([e for e in per.values() if e["me"] and not e["them"]]),
        shared_families=sorted(fam_me & fam_them, key=family_label),
    )


# --------------------------------------------------------------------------- #
# Concurrents et activite
# --------------------------------------------------------------------------- #


def suggestions(
    awards: list[Award], me_id: int | None, watched: set[int], limit: int = 4
) -> list[CompanyRow]:
    """Entreprises a surveiller : celles qui gagnent sur vos familles et chez vos acheteurs."""
    start, _ = window("36m")
    scoped = [a for a in awards if in_window(a, start)]
    my_families = {f for a in scoped if a.supplier_id == me_id for f in a.families or []}
    my_buyers = {a.buyer_raw for a in scoped if a.supplier_id == me_id}
    rows = rank_companies(scoped, me_id, watched)
    scored = []
    for row in rows:
        if row.is_me or row.watched or not row.supplier_id:
            continue
        overlap = sum(v for f, v in row.families.items() if f in my_families) + 0.5 * sum(
            1 for b in row.buyers if b in my_buyers
        ) * row.avg_amount
        if overlap > 0:
            scored.append((overlap, row))
    return [row for _, row in sorted(scored, key=lambda x: x[0], reverse=True)[:limit]]


FEED_SCOPES = {"tous": "Tout le marché", "suivis": "Concurrents suivis", "moi": "Mes marchés"}


def feed(
    awards: list[Award],
    scope: str = "tous",
    family: str | None = None,
    me_id: int | None = None,
    watched: set[int] | None = None,
    limit: int = 60,
) -> list[Award]:
    """Les attributions les plus recentes, du point de vue choisi."""
    watched = watched or set()
    items = [a for a in of_family(awards, family) if a.award_date]
    if scope == "suivis":
        items = [a for a in items if a.supplier_id in watched]
    elif scope == "moi":
        items = [a for a in items if me_id and a.supplier_id == me_id]
    return sorted(items, key=lambda a: a.award_date, reverse=True)[:limit]

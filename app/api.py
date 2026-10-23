"""API HTTP et tableau de bord."""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import __version__, exports, market, queries, similarity
from app.config import BASE_DIR, get_settings
from app.db import get_session, init_db, session_scope
from app.messages import format_amount, format_date
from app.normalize import normalize_text
from app.models import (
    BID_PATH,
    Award,
    Bid,
    BidStage,
    Buyer,
    Category,
    DocumentStatus,
    Supplier,
    Tender,
    TenderDocument,
    TenderStatus,
    WatchProfile,
)
from app.pipeline import collect_awards, collect_tenders, rescore_tenders
from app.queries import AwardFilters, TenderFilters
from app.seeds import ensure_default_profiles
from app.taxonomy import family_label

logger = logging.getLogger(__name__)
settings = get_settings()

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Prepare la base au demarrage : tables, puis profil de veille par defaut."""
    init_db()
    with session_scope() as session:
        created = ensure_default_profiles(session)
    if created:
        logger.info(
            "Profils de veille crees : %s", ", ".join(p.name for p in created)
        )
    logger.info("Radar demarre (base: %s)", settings.database_url)
    yield


app = FastAPI(
    title="Radar des marches publics",
    description=(
        "Veille temps reel des appels d'offres marocains et historique des attributions."
    ),
    version=__version__,
    lifespan=lifespan,
)

TEMPLATES_DIR = BASE_DIR / "app" / "web" / "templates"
STATIC_DIR = BASE_DIR / "app" / "web" / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["montant"] = lambda v: format_amount(v)
templates.env.filters["jour"] = format_date
templates.env.filters["famille"] = family_label
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_STOP_INITIALS = {"de", "du", "des", "la", "le", "les", "l", "d", "et", "pour", "sarl", "au", "sa", "s"}
#: Couleurs des logos : la palette des series, puis l'encre de la menthe.
_LOGO_COLORS = ["--ct-s1", "--ct-s3", "--ct-s2", "--ct-s4", "--ct-s5", "--ct-mint-ink", "--ct-ink-2"]


def _initials(name: str | None) -> str:
    """Deux lettres pour un logo : « Commune Urbaine de Tanger » -> « CU »."""
    words = [w for w in re.split(r"[\s\-'’.]+", name or "") if w]
    kept = [w for w in words if w.lower() not in _STOP_INITIALS and w.upper() != "DEMO"] or words
    if len(kept) == 1:
        return kept[0][:2].upper()
    return "".join(w[0] for w in kept[:2]).upper() or "?"


def _logo_color(name: str | None) -> str:
    digest = sum(ord(ch) for ch in normalize_text(name or ""))
    return f"var({_LOGO_COLORS[digest % len(_LOGO_COLORS)]})"


def _compact(value: float | None) -> str:
    """Montant court pour les chiffres d'en-tete : 12,4 M, 860 k."""
    if value is None:
        return "—"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}".replace(".", ",").replace(",0", "") + " M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.0f} k"
    return f"{value:.0f}"


_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def _month_label(month: str) -> str:
    """« 2025-10 » -> « oct. 2025 »."""
    try:
        year, number = month.split("-")
        return f"{_MONTHS[int(number) - 1]} {year}"
    except (ValueError, IndexError):
        return month


def _qs(params: dict, **changes: object) -> str:
    """Adresse de la page courante avec des parametres changes, toujours encodee.

    Une valeur None ou vide retire le parametre ; changer un filtre ramene a la
    premiere page.
    """
    merged = {k: v for k, v in params.items() if k not in ("page", "ok")}
    for key, value in changes.items():
        if value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    query = urlencode(merged)
    return f"?{query}" if query else "?"


def _asset(name: str) -> str:
    """Adresse d'un fichier statique, marquee de sa date : un deploiement n'est
    jamais masque par une ancienne copie en cache dans le navigateur."""
    try:
        stamp = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{name}?v={stamp}"


def _static_or_none(name: str | None) -> str | None:
    """Adresse d'un fichier statique facultatif, ou None s'il n'existe pas."""
    if not name or not (STATIC_DIR / name).is_file():
        return None
    return _asset(name)


templates.env.globals["asset"] = _asset
templates.env.globals["PERIODS"] = market.PERIODS
templates.env.globals["FAMILIES"] = market.FAMILIES
templates.env.filters["nom"] = market.display_name
templates.env.filters["fcolor"] = market.family_color
templates.env.filters["spark"] = market.spark_path
templates.env.filters["initiales"] = _initials
templates.env.filters["couleur"] = _logo_color
templates.env.filters["court"] = _compact
templates.env.globals["qs"] = _qs

_basic = HTTPBasic(auto_error=False)


def require_auth(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)] = None,
) -> None:
    """Authentification HTTP basique, active seulement si DASHBOARD_USER est defini."""
    if not settings.dashboard_user:
        return
    valid = credentials is not None and (
        secrets.compare_digest(credentials.username, settings.dashboard_user)
        and secrets.compare_digest(credentials.password, settings.dashboard_password)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identifiants requis",
            headers={"WWW-Authenticate": "Basic"},
        )


Auth = Annotated[None, Depends(require_auth)]
Db = Annotated[Session, Depends(get_session)]




def _parse_bool(value: str | None, default: bool = False) -> bool:
    """Lit une case a cocher de formulaire HTML.

    Un navigateur omet une case decochee : le gabarit envoie donc un champ cache
    "false" juste avant la case. Starlette retient la derniere valeur, ce qui
    donne bien false quand la case est decochee. Une valeur vide ou inconnue ne
    doit jamais declencher d'erreur 422.
    """
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "on", "yes", "oui")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def tender_filters(
    q: str | None = None,
    categorie: str | None = None,
    statut: str | None = None,
    acheteur: str | None = None,
    min_montant: float | None = None,
    max_montant: float | None = None,
    du: str | None = None,
    au: str | None = None,
    ouverts: str | None = None,
    pertinents: str | None = None,
    tri: str = "recent",
) -> TenderFilters:
    return TenderFilters(
        search=q,
        category=categorie or None,
        status=statut or None,
        buyer=acheteur or None,
        min_amount=min_montant,
        max_amount=max_montant,
        published_from=_parse_date(du),
        published_to=_parse_date(au),
        only_open=_parse_bool(ouverts, default=False),
        # Par defaut on ne montre que le domaine surveille ; la case permet de tout voir.
        min_score=0.01 if _parse_bool(pertinents, default=True) else None,
        order=tri,
    )


def award_filters(
    q: str | None = None,
    attributaire: str | None = None,
    acheteur: str | None = None,
    categorie: str | None = None,
    min_montant: float | None = None,
    max_montant: float | None = None,
    du: str | None = None,
    au: str | None = None,
    tri: str = "recent",
) -> AwardFilters:
    return AwardFilters(
        search=q,
        supplier=attributaire or None,
        buyer=acheteur or None,
        category=categorie or None,
        min_amount=min_montant,
        max_amount=max_montant,
        awarded_from=_parse_date(du),
        awarded_to=_parse_date(au),
        order=tri,
    )


#: Messages affiches apres une action, choisis par une cle dans l'adresse :
#: jamais de texte libre venu de la requete.
FLASH = {
    "collecte": "Collecte lancée en arrière-plan. Le journal des collectes se mettra à jour.",
    "collecte-portail": "Collecte des attributions lancée. Sans APIFY_TOKEN, les avis ne sont pas collectés.",
    "suivi": "Suivi de la candidature enregistré.",
    "suivi-retire": "Avis retiré du suivi des candidatures.",
    "profil": "Profil enregistré. Tous les avis ont été recalculés.",
    "suivi-concurrent": "Concurrent ajouté à votre liste de suivi.",
    "fin-suivi": "Concurrent retiré de votre liste de suivi.",
}


def _context(request: Request, db: Session | None = None, **extra) -> dict:
    params = dict(request.query_params)
    me = market.own_supplier(db, settings) if db is not None else None
    base = {
        "company_name": settings.company_name,
        "company_logo": _static_or_none(settings.company_logo),
        "company_logo_full": _static_or_none(settings.company_logo_full),
        "me": me,
        "me_id": me.id if me else None,
        "watched": market.watched_ids(db) if db is not None else set(),
        "demo": market.has_demo_data(db) if db is not None else False,
        "request": request,
        "app_name": settings.app_name,
        "version": __version__,
        "categories": [c.value for c in Category],
        "statuses": [s.value for s in TenderStatus],
        "params": params,
        "flash": FLASH.get(params.get("ok", "")),
        "nav": (
            {
                "open": queries.open_relevant_count(db),
                "bids": queries.active_bids_count(db),
                "watched": len(market.watched_ids(db)),
            }
            if db is not None
            else {}
        ),
    }
    base.update(extra)
    return base


def _safe_next(target: str | None, fallback: str) -> str:
    """Une redirection ne sort jamais de l'application."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _with_flash(url: str, key: str) -> str:
    """Ajoute la cle du message avant l'ancre : apres un #, le serveur ne la verrait pas."""
    path, hash_mark, fragment = url.partition("#")
    return f"{path}{'&' if '?' in path else '?'}ok={key}{hash_mark}{fragment}"


def _parse_amount(value: str | None) -> float | None:
    """Montant saisi a la main : « 1 250 000 », « 1250000,50 » ou vide."""
    if not value:
        return None
    cleaned = value.replace(" ", "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    return amount if amount > 0 else None


# --------------------------------------------------------------------------- #
# Pages du tableau de bord
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Db, _: Auth = None) -> Response:
    """Vue d'ensemble : votre place dans le marche, vos opportunites, vos concurrents."""
    ctx = _context(request, db)
    me_id, watched = ctx["me_id"], ctx["watched"]
    awards = market.domain_awards(db)
    board = market.leaderboard(awards, "12m", None, me_id, watched)
    my_row = next((r for r in board if r.is_me), None)
    rival = next((r for r in board if not r.is_me), None)

    chart = None
    if my_row and rival:
        chart = {
            "labels": [p["label"] for p in market.monthly([])],
            "series": [
                {"name": market.display_name(my_row.name), "values": [
                    p["total"] for p in market.monthly(a for a in awards if a.supplier_id == me_id)
                ]},
                {"name": market.display_name(rival.name), "values": [
                    p["total"] for p in market.monthly(a for a in awards if market._key(a) == rival.key)
                ]},
            ],
        }

    open_value, open_priced, open_count = queries.open_relevant_value(db)
    grouped = queries.bids_by_stage(db)
    watched_feed = market.feed(awards, "suivis", None, me_id, watched, limit=6)
    ctx.update(
        summary=queries.summary(db),
        board=board,
        my_row=my_row,
        rival=rival,
        rank_window=market.rank_window(board, me_id) if my_row else board[:5],
        chart=chart,
        closing=queries.closing_soon(db, days=14, limit=5),
        feed=watched_feed or market.feed(awards, "tous", None, me_id, watched, limit=6),
        feed_is_watched=bool(watched_feed),
        segments=market.segments(awards, market.open_domain_tenders(db), "12m", me_id, watched)[:6],
        open_value=open_value,
        open_priced=open_priced,
        open_count=open_count,
        bids=queries.bid_summary(db, grouped),
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


# --------------------------------------------------------------------------- #
# Le marche : entreprises, classement, segments, concurrents, activite
# --------------------------------------------------------------------------- #


def _period(value: str | None) -> str:
    return value if value in market.PERIODS else market.DEFAULT_PERIOD


def _company_page(
    request: Request, db: Session, supplier: Supplier, periode: str | None,
    segment: str | None, onglet: str | None,
) -> Response:
    """La fiche d'une entreprise, sur le modele de la fiche marque du brand tracker."""
    ctx = _context(request, db)
    period = _period(periode)
    awards = market.domain_awards(db)
    view = market.company_view(awards, supplier, period, segment, ctx["me_id"], ctx["watched"])
    is_me = supplier.id == ctx["me_id"]
    tabs = ["apercu", "marches", "concurrents", "acheteurs"] + ([] if is_me or not ctx["me_id"] else ["face"])
    tab = onglet if onglet in tabs else "apercu"
    all_wins = [a for a in awards if a.supplier_id == supplier.id]
    ctx.update(
        supplier=supplier,
        view=view,
        is_me=is_me,
        period=period,
        tab=tab,
        tabs=tabs,
        monthly=market.monthly(view.wins if period == "12m" else all_wins, 12 if period == "12m" else 36),
        rank_window=market.rank_window(view.segment_rank, supplier.id),
        segment_choices=[f for f, _amount, _count in view.families],
        mosaic=sorted(view.wins, key=lambda a: a.awarded_amount or 0, reverse=True)[:3],
        h2h=market.head_to_head(awards, ctx["me_id"], supplier.id) if tab == "face" else None,
        board_size=len(market.leaderboard(awards, period, None)),
        bids=queries.bid_summary(db) if is_me else None,
        last_any=max((a.award_date for a in all_wins if a.award_date), default=None),
    )
    return templates.TemplateResponse(request, "company.html", ctx)


@app.get("/profil", response_class=HTMLResponse)
def own_profile(
    request: Request, db: Db, periode: str | None = None, segment: str | None = None,
    onglet: str | None = None, _: Auth = None,
) -> Response:
    """Votre fiche : vos marches, votre rang, vos segments, vos acheteurs."""
    me = market.own_supplier(db, settings)
    if me is None:
        return templates.TemplateResponse(request, "company_missing.html", _context(request, db))
    return _company_page(request, db, me, periode, segment, onglet)


@app.get("/attributaires")
def suppliers_page(q: str | None = None, _: Auth = None) -> RedirectResponse:
    """L'ancien classement des attributaires est devenu le classement du marche."""
    return RedirectResponse(f"/classement{_qs({}, q=q)}" if q else "/classement", status_code=307)


@app.get("/attributaires/{supplier_id}", response_class=HTMLResponse)
def supplier_detail(
    request: Request, supplier_id: int, db: Db, periode: str | None = None,
    segment: str | None = None, onglet: str | None = None, _: Auth = None,
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail="Attributaire inconnu")
    me = market.own_supplier(db, settings)
    if me is not None and me.id == supplier.id:
        return RedirectResponse(f"/profil{_qs(dict(request.query_params))}", status_code=303)
    return _company_page(request, db, supplier, periode, segment, onglet)


@app.post("/attributaires/{supplier_id}/suivre")
def follow_supplier(
    supplier_id: int, db: Db, next: Annotated[str, Form()] = "", _: Auth = None
) -> RedirectResponse:
    """Suit un concurrent, ou cesse de le suivre."""
    if db.get(Supplier, supplier_id) is None:
        raise HTTPException(status_code=404, detail="Attributaire inconnu")
    followed = market.toggle_watch(db, supplier_id)
    db.commit()
    back = _safe_next(next, f"/attributaires/{supplier_id}")
    return RedirectResponse(_with_flash(back, "suivi-concurrent" if followed else "fin-suivi"), status_code=303)


@app.get("/classement", response_class=HTMLResponse)
def leaderboard_page(
    request: Request, db: Db, periode: str | None = None, famille: str | None = None,
    q: str | None = None, _: Auth = None,
) -> Response:
    """Qui gagne le plus, sur la periode et le segment choisis, et qui progresse."""
    ctx = _context(request, db)
    period = _period(periode)
    family = famille if famille in market.FAMILIES else None
    awards = market.domain_awards(db)
    rows = market.leaderboard(awards, period, family, ctx["me_id"], ctx["watched"])
    total = sum(r.amount for r in rows)
    if q:
        needle = normalize_text(q)
        rows = [r for r in rows if needle in normalize_text(market.display_name(r.name))]
    start, _prev = market.window(period)
    scoped = [a for a in market.of_family(awards, family) if market.in_window(a, start)]
    sparks = {
        r.key: [p["total"] for p in market.monthly(a for a in scoped if market._key(a) == r.key)]
        for r in rows
    }
    movers = [r for r in rows if r.delta]
    ctx.update(
        rows=rows,
        total=total,
        period=period,
        family=family,
        q=q or "",
        sparks=sparks,
        leaders={
            "amount": rows[0] if rows else None,
            "wins": max(rows, key=lambda r: r.wins) if rows else None,
            "buyers": max(rows, key=lambda r: r.buyers_count) if rows else None,
            "momentum": max(movers, key=lambda r: r.delta) if movers else None,
        },
        my_row=next((r for r in rows if r.is_me), None),
    )
    return templates.TemplateResponse(request, "leaderboard.html", ctx)


@app.get("/segments", response_class=HTMLResponse)
def segments_page(request: Request, db: Db, periode: str | None = None, _: Auth = None) -> Response:
    """Le marche par famille d'equipement : taille, leader, votre place."""
    ctx = _context(request, db)
    period = _period(periode)
    awards = market.domain_awards(db)
    segs = market.segments(awards, market.open_domain_tenders(db), period, ctx["me_id"], ctx["watched"])
    start, _prev = market.window(period)
    scoped = [a for a in awards if market.in_window(a, start)]
    ctx.update(
        segments=segs,
        period=period,
        totals={
            "amount": sum(a.awarded_amount or 0 for a in scoped),
            "wins": len(scoped),
            "companies": len({market._key(a) for a in scoped}),
            "open": len(market.open_domain_tenders(db)),
        },
    )
    return templates.TemplateResponse(request, "segments.html", ctx)


@app.get("/concurrents", response_class=HTMLResponse)
def competitors_page(request: Request, db: Db, _: Auth = None) -> Response:
    """Les concurrents que vous suivez, et ceux qu'il faudrait suivre."""
    ctx = _context(request, db)
    me_id, watched = ctx["me_id"], ctx["watched"]
    awards = market.domain_awards(db)
    board = {r.supplier_id: r for r in market.leaderboard(awards, "12m", None, me_id, watched)}
    cards = []
    for supplier_id in watched:
        supplier = db.get(Supplier, supplier_id)
        if supplier is None:
            continue
        theirs = [a for a in awards if a.supplier_id == supplier_id]
        cards.append({
            "supplier": supplier,
            "row": board.get(supplier_id),
            "spark": [p["total"] for p in market.monthly(theirs)],
            "recent": theirs[:3],
            "h2h": market.head_to_head(awards, me_id, supplier_id) if me_id else None,
        })
    cards.sort(key=lambda c: c["row"].amount if c["row"] else 0, reverse=True)
    ctx.update(
        cards=cards,
        my_row=board.get(me_id),
        suggestions=market.suggestions(awards, me_id, watched),
        feed=market.feed(awards, "suivis", None, me_id, watched, limit=8),
    )
    return templates.TemplateResponse(request, "competitors.html", ctx)


@app.get("/activite", response_class=HTMLResponse)
def activity_page(
    request: Request, db: Db, vue: str | None = None, famille: str | None = None, _: Auth = None
) -> Response:
    """Qui a gagne quoi, le plus recent d'abord."""
    ctx = _context(request, db)
    scope = vue if vue in market.FEED_SCOPES else "tous"
    family = famille if famille in market.FAMILIES else None
    awards = market.domain_awards(db)
    items = market.feed(awards, scope, family, ctx["me_id"], ctx["watched"], limit=80)
    months: list[tuple[str, list[Award]]] = []
    for award in items:
        label = _month_label(award.award_date.strftime("%Y-%m"))
        if not months or months[-1][0] != label:
            months.append((label, []))
        months[-1][1].append(award)
    ctx.update(scope=scope, scopes=market.FEED_SCOPES, family=family, months=months, count=len(items))
    return templates.TemplateResponse(request, "activity.html", ctx)


@app.get("/recherche", response_class=HTMLResponse)
def search_page(
    request: Request, db: Db, q: str | None = None, _: Auth = None
) -> Response:
    """Un seul champ, qui cherche dans les avis, les attributions et les entreprises."""
    return templates.TemplateResponse(
        request,
        "search.html",
        _context(request, db, results=queries.global_search(db, q), q=q or ""),
    )


@app.get("/api/search")
def api_search(
    db: Db,
    q: str | None = None,
    limit: Annotated[int, Query(le=200)] = 20,
    _: Auth = None,
) -> dict:
    from app.messages import tender_to_dict

    results = queries.global_search(db, q, limit=limit)
    return {
        "query": results.query,
        "total": results.total,
        "tenders": {
            "total": results.tenders_total,
            "items": [tender_to_dict(t) for t in results.tenders],
        },
        "awards": {
            "total": results.awards_total,
            "items": [
                {
                    "reference": a.reference,
                    "object": a.object,
                    "buyer": a.buyer_raw,
                    "supplier": a.supplier.name if a.supplier else a.supplier_raw,
                    "awarded_amount": a.awarded_amount,
                    "bids_count": a.bids_count,
                    "award_date": a.award_date.isoformat() if a.award_date else None,
                    "url": a.consultation_url,
                }
                for a in results.awards
            ],
        },
        "suppliers": [
            {
                "supplier_id": r.supplier_id,
                "name": r.name,
                "awards_count": r.awards_count,
                "total_amount": r.total_amount,
            }
            for r in results.suppliers
        ],
        "buyers": [
            {
                "buyer_id": r.buyer_id,
                "name": r.name,
                "awards_count": r.awards_count,
                "total_amount": r.total_amount,
            }
            for r in results.buyers
        ],
    }


@app.get("/avis", response_class=HTMLResponse)
def tenders_page(
    request: Request,
    db: Db,
    filters: Annotated[TenderFilters, Depends(tender_filters)],
    page: int = 1,
    _: Auth = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "tenders.html",
        _context(
            request, db, page_data=queries.list_tenders(db, filters, page, 30), filters=filters
        ),
    )


@app.get("/avis/{tender_id}", response_class=HTMLResponse)
def tender_detail(request: Request, tender_id: int, db: Db, _: Auth = None) -> Response:
    """Vue complete d'un avis : pieces du DCE, precedents comparables, reperes de prix."""
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="Avis inconnu")

    comparables = similarity.comparable_awards(db, tender, limit=12)
    return templates.TemplateResponse(
        request,
        "tender_detail.html",
        _context(
            request,
            db,
            tender=tender,
            bid=tender.bid,
            bid_path=BID_PATH,
            bid_stages=list(BidStage),
            documents=sorted(tender.documents, key=lambda d: (d.kind.value, d.name or "")),
            comparables=comparables,
            benchmark=similarity.price_benchmark(comparables, tender.estimated_amount),
            competitors=similarity.likely_competitors(comparables),
            similar_open=similarity.comparable_tenders(db, tender, limit=6),
            buyer_history=(
                queries.list_awards(
                    db, AwardFilters(buyer=tender.buyer_raw, order="recent"), per_page=8
                ).items
                if tender.buyer_raw
                else []
            ),
        ),
    )


@app.get("/avis/{tender_id}/piece/{document_id}")
def tender_document(tender_id: int, document_id: int, db: Db, _: Auth = None) -> Response:
    """Sert une piece deja telechargee depuis le disque."""
    document = db.get(TenderDocument, document_id)
    if document is None or document.tender_id != tender_id:
        raise HTTPException(status_code=404, detail="Pièce inconnue")
    if document.status is not DocumentStatus.DOWNLOADED or not document.local_path:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cette pièce n'est pas encore téléchargée. "
                "Lancez `radar documents-fetch` après avoir renseigné "
                "PORTAL_LOGIN et PORTAL_PASSWORD."
            ),
        )
    path = Path(document.local_path)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="Fichier absent du disque.")
    return FileResponse(
        path,
        media_type=document.content_type or "application/octet-stream",
        filename=document.name or path.name,
    )


@app.get("/attributions", response_class=HTMLResponse)
def awards_page(
    request: Request,
    db: Db,
    filters: Annotated[AwardFilters, Depends(award_filters)],
    page: int = 1,
    _: Auth = None,
) -> Response:
    return templates.TemplateResponse(
        request,
        "awards.html",
        _context(
            request, db, page_data=queries.list_awards(db, filters, page, 50), filters=filters
        ),
    )


@app.get("/acheteurs", response_class=HTMLResponse)
def buyers_page(request: Request, db: Db, _: Auth = None) -> Response:
    return templates.TemplateResponse(
        request,
        "buyers.html", _context(request, db, buyers=queries.buyer_ranking(db, limit=200))
    )


@app.get("/acheteurs/{buyer_id}", response_class=HTMLResponse)
def buyer_detail(request: Request, buyer_id: int, db: Db, _: Auth = None) -> Response:
    """Fiche d'un acheteur : ce qu'il depense, ce qu'il publie, qui gagne chez lui."""
    buyer = db.get(Buyer, buyer_id)
    if buyer is None:
        raise HTTPException(status_code=404, detail="Acheteur inconnu")
    history = queries.buyer_awards(db, buyer_id)
    tenders = queries.buyer_tenders(db, buyer_id)
    bids = [a.bids_count for a in history if a.bids_count is not None]
    return templates.TemplateResponse(
        request,
        "buyer_detail.html",
        _context(
            request,
            db,
            buyer=buyer,
            history=history,
            total=sum(a.awarded_amount or 0 for a in history),
            avg_bids=sum(bids) / len(bids) if bids else None,
            tenders=tenders,
            open_tenders=[t for t in tenders if t.status is TenderStatus.OPEN and (t.days_left is None or t.days_left >= 0)],
            yearly=queries.yearly_series(history),
            suppliers=queries.buyer_suppliers(history),
            families=queries.family_mix(history),
        ),
    )


@app.get("/candidatures", response_class=HTMLResponse)
def bids_page(request: Request, db: Db, _: Auth = None) -> Response:
    """Suivi des candidatures : chaque avis retenu, de la decision a l'issue."""
    grouped = queries.bids_by_stage(db)
    return templates.TemplateResponse(
        request,
        "bids.html",
        _context(
            request,
            db,
            grouped=grouped,
            stages=list(BidStage),
            summary=queries.bid_summary(db, grouped),
        ),
    )


@app.post("/avis/{tender_id}/suivi")
def update_bid(
    tender_id: int,
    db: Db,
    stage: Annotated[str, Form()] = "",
    offered_amount: Annotated[str, Form()] = "",
    note: Annotated[str | None, Form()] = None,
    next: Annotated[str, Form()] = "",
    _: Auth = None,
) -> RedirectResponse:
    """Cree, met a jour ou retire le suivi d'une candidature.

    Une etape vide retire l'avis du suivi. Le montant et la note ne sont
    modifies que s'ils sont envoyes : un bouton d'etape seul ne les efface pas.
    """
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="Avis inconnu")
    back = _safe_next(next, f"/avis/{tender_id}")
    # Lu en base et non par la relation : celle-ci peut dater d'avant un autre envoi.
    existing = db.scalar(select(Bid).where(Bid.tender_id == tender.id))

    if stage in ("", "none"):
        if existing is not None:
            db.delete(existing)
            db.commit()
        return RedirectResponse(_with_flash(back, "suivi-retire"), status_code=303)

    try:
        new_stage = BidStage(stage)
    except ValueError:
        raise HTTPException(status_code=422, detail="Étape inconnue") from None

    bid = existing or Bid(tender_id=tender.id)
    bid.stage = new_stage
    if offered_amount != "":
        bid.offered_amount = _parse_amount(offered_amount)
    if note is not None:
        bid.note = note.strip() or None
    db.add(bid)
    db.commit()
    return RedirectResponse(_with_flash(back, "suivi"), status_code=303)


@app.get("/veille", response_class=HTMLResponse)
def profiles_page(request: Request, db: Db, _: Auth = None) -> Response:
    profiles = list(db.scalars(select(WatchProfile).order_by(WatchProfile.id)).all())
    return templates.TemplateResponse(
        request,
        "profiles.html", _context(request, db, profiles=profiles, settings=settings)
    )


def _split_terms(value: str | None) -> list[str]:
    """Un ou plusieurs termes saisis d'un coup, separes par des virgules."""
    return [term.strip() for term in (value or "").split(",") if term.strip()]


@app.post("/veille/{profile_id}")
def update_profile(
    profile_id: int,
    db: Db,
    action: Annotated[str, Form()],
    value: Annotated[str, Form()] = "",
    _: Auth = None,
) -> RedirectResponse:
    """Modifie un profil depuis l'interface, puis recalcule tous les avis."""
    profile = db.get(WatchProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profil inconnu")

    if action == "toggle":
        profile.active = not profile.active
    elif action == "add_keyword":
        profile.keywords = list(dict.fromkeys((profile.keywords or []) + _split_terms(value)))
    elif action == "remove_keyword":
        profile.keywords = [k for k in profile.keywords or [] if k != value]
    elif action == "add_exclusion":
        profile.excluded_keywords = list(
            dict.fromkeys((profile.excluded_keywords or []) + _split_terms(value))
        )
    elif action == "remove_exclusion":
        profile.excluded_keywords = [k for k in profile.excluded_keywords or [] if k != value]
    elif action == "min_score":
        try:
            profile.min_score = max(0.0, float(value.replace(",", ".")))
        except ValueError:
            raise HTTPException(status_code=422, detail="Score invalide") from None
    else:
        raise HTTPException(status_code=422, detail="Action inconnue")

    db.flush()
    rescore_tenders(db)
    db.commit()
    return RedirectResponse(f"/veille?ok=profil#profil-{profile.id}", status_code=303)


@app.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request, db: Db, _: Auth = None) -> Response:
    return templates.TemplateResponse(
        request,
        "exports.html", _context(request, db, summary=queries.summary(db))
    )


# --------------------------------------------------------------------------- #
# API JSON
# --------------------------------------------------------------------------- #


@app.get("/api/health")
def health(db: Db) -> dict:
    summary = queries.summary(db)
    return {
        "status": "ok",
        "version": __version__,
        "tenders": summary.tenders_total,
        "awards": summary.awards_total,
        "last_run": (
            {
                "source": summary.last_run.source,
                "status": summary.last_run.status,
                "started_at": summary.last_run.started_at.isoformat(),
                "items_fetched": summary.last_run.items_fetched,
            }
            if summary.last_run
            else None
        ),
        "channels": {
            "email": settings.email_enabled,
            "whatsapp": settings.whatsapp_enabled,
            "webhook": settings.webhook_enabled,
        },
    }


@app.get("/api/tenders")
def api_tenders(
    db: Db,
    filters: Annotated[TenderFilters, Depends(tender_filters)],
    page: int = 1,
    per_page: Annotated[int, Query(le=500)] = 100,
    _: Auth = None,
) -> dict:
    from app.messages import tender_to_dict

    result = queries.list_tenders(db, filters, page, per_page)
    return {
        "total": result.total,
        "page": result.page,
        "pages": result.pages,
        "items": [tender_to_dict(t) for t in result.items],
    }


@app.get("/api/awards")
def api_awards(
    db: Db,
    filters: Annotated[AwardFilters, Depends(award_filters)],
    page: int = 1,
    per_page: Annotated[int, Query(le=500)] = 100,
    _: Auth = None,
) -> dict:
    result = queries.list_awards(db, filters, page, per_page)
    return {
        "total": result.total,
        "page": result.page,
        "pages": result.pages,
        "items": [
            {
                "id": a.id,
                "reference": a.reference,
                "object": a.object,
                "lot": a.lot_number,
                "buyer": a.buyer_raw,
                "supplier": a.supplier.name if a.supplier else a.supplier_raw,
                "awarded_amount": a.awarded_amount,
                "currency": a.currency,
                "includes_tax": a.amount_includes_tax,
                "estimated_amount": a.estimated_amount,
                "bids_count": a.bids_count,
                "award_date": a.award_date.isoformat() if a.award_date else None,
                "category": a.category.value,
                "url": a.consultation_url,
            }
            for a in result.items
        ],
    }


@app.get("/api/suppliers")
def api_suppliers(
    db: Db,
    q: str | None = None,
    limit: Annotated[int, Query(le=1000)] = 100,
    _: Auth = None,
) -> dict:
    ranking = queries.supplier_ranking(db, limit=limit, search=q)
    return {
        "total": len(ranking),
        "items": [
            {
                "supplier_id": r.supplier_id,
                "name": r.name,
                "awards_count": r.awards_count,
                "total_amount": r.total_amount,
                "avg_amount": round(r.avg_amount, 2),
                "max_amount": r.max_amount,
                "buyers_count": r.buyers_count,
                "avg_bids": round(r.avg_bids, 1) if r.avg_bids else None,
                "first_award": r.first_award.isoformat() if r.first_award else None,
                "last_award": r.last_award.isoformat() if r.last_award else None,
            }
            for r in ranking
        ],
    }


@app.get("/api/tenders/{tender_id}/comparables")
def api_comparables(tender_id: int, db: Db, limit: int = 12, _: Auth = None) -> dict:
    """Precedents comparables, reperes de prix et concurrents probables."""
    tender = db.get(Tender, tender_id)
    if tender is None:
        raise HTTPException(status_code=404, detail="Avis inconnu")

    comparables = similarity.comparable_awards(db, tender, limit=limit)
    benchmark = similarity.price_benchmark(comparables, tender.estimated_amount)
    return {
        "tender": {
            "id": tender.id,
            "reference": tender.reference,
            "object": tender.object,
            "buyer": tender.buyer_raw,
            "estimated_amount": tender.estimated_amount,
            "families": [family_label(f) for f in tender.families or []],
            "days_left": tender.days_left,
        },
        "benchmark": {
            "count": benchmark.count,
            "min": benchmark.minimum,
            "median": benchmark.median_amount,
            "max": benchmark.maximum,
            "avg_bids": benchmark.avg_bids,
            "gap_to_estimate_pct": (
                round(benchmark.gap_to_estimate, 1)
                if benchmark.gap_to_estimate is not None
                else None
            ),
        },
        "comparables": [
            {
                "score": c.score,
                "confidence": c.confidence,
                "reasons": c.reasons,
                "same_buyer": c.same_buyer,
                "shared_families": [family_label(f) for f in c.shared_families],
                "reference": c.award.reference,
                "object": c.award.object,
                "buyer": c.award.buyer_raw,
                "winner": c.award.supplier.name if c.award.supplier else c.award.supplier_raw,
                "awarded_amount": c.award.awarded_amount,
                "bids_count": c.award.bids_count,
                "award_date": c.award.award_date.isoformat() if c.award.award_date else None,
            }
            for c in comparables
        ],
        "likely_competitors": [
            {
                "name": k["name"],
                "wins": k["wins"],
                "same_buyer_wins": k["same_buyer_wins"],
                "total_amount": k["total_amount"],
                "last_award": k["last_award"].isoformat() if k["last_award"] else None,
            }
            for k in similarity.likely_competitors(comparables)
        ],
    }


@app.get("/api/bids")
def api_bids(db: Db, _: Auth = None) -> dict:
    """Suivi des candidatures, etape par etape."""
    grouped = queries.bids_by_stage(db)
    summary = queries.bid_summary(db, grouped)
    return {
        "summary": {
            "active": summary.active,
            "submitted": summary.submitted,
            "won": summary.won,
            "lost": summary.lost,
            "dropped": summary.dropped,
            "win_rate_pct": round(summary.win_rate, 1) if summary.win_rate is not None else None,
            "pipeline_value": summary.pipeline_value,
        },
        "items": [
            {
                "tender_id": bid.tender_id,
                "reference": bid.tender.reference,
                "object": bid.tender.object,
                "buyer": bid.tender.buyer_raw,
                "stage": bid.stage.value,
                "stage_label": bid.stage.label,
                "offered_amount": bid.offered_amount,
                "estimated_amount": bid.tender.estimated_amount,
                "deadline": (
                    bid.tender.submission_deadline.isoformat()
                    if bid.tender.submission_deadline
                    else None
                ),
                "note": bid.note,
                "updated_at": bid.updated_at.isoformat() if bid.updated_at else None,
            }
            for bids in grouped.values()
            for bid in bids
        ],
    }


@app.get("/api/classement")
def api_leaderboard(
    db: Db, periode: str | None = None, famille: str | None = None, _: Auth = None
) -> dict:
    """Classement du marche de votre domaine, avec l'evolution de chaque rang."""
    me = market.own_supplier(db, settings)
    period = _period(periode)
    family = famille if famille in market.FAMILIES else None
    rows = market.leaderboard(
        market.domain_awards(db), period, family, me.id if me else None, market.watched_ids(db)
    )
    return {
        "period": period,
        "family": family,
        "items": [
            {
                "rank": r.rank,
                "rank_change": r.delta,
                "supplier_id": r.supplier_id,
                "name": r.name,
                "wins": r.wins,
                "amount": r.amount,
                "share_pct": round(r.share, 2),
                "buyers": r.buyers_count,
                "avg_bids": round(r.avg_bids, 1) if r.avg_bids else None,
                "last_win": r.last_win.isoformat() if r.last_win else None,
                "is_you": r.is_me,
                "followed": r.watched,
            }
            for r in rows
        ],
    }


@app.get("/api/stats")
def api_stats(db: Db, _: Auth = None) -> dict:
    summary = queries.summary(db)
    return {
        "tenders": {
            "total": summary.tenders_total,
            "open": summary.tenders_open,
            "matched": summary.tenders_matched,
            "closing_soon": summary.tenders_closing_soon,
        },
        "awards": {"total": summary.awards_total, "amount": summary.awards_amount},
        "entities": {
            "suppliers": summary.suppliers_total,
            "buyers": summary.buyers_total,
        },
        "monthly": queries.monthly_awards(db),
        "by_category": queries.category_breakdown(db),
    }


# --------------------------------------------------------------------------- #
# Exports
# --------------------------------------------------------------------------- #


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_CSV = "text/csv; charset=utf-8"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_STAMP = "%Y%m%d"


@app.get("/export/avis.csv")
def export_tenders_csv(
    db: Db, filters: Annotated[TenderFilters, Depends(tender_filters)], _: Auth = None
) -> Response:
    content = exports.to_csv(exports.TENDER_HEADERS, exports.tender_rows(db, filters))
    return _attachment(content, f"avis_{datetime.now():{_STAMP}}.csv", _CSV)


@app.get("/export/attributions.csv")
def export_awards_csv(
    db: Db, filters: Annotated[AwardFilters, Depends(award_filters)], _: Auth = None
) -> Response:
    content = exports.to_csv(exports.AWARD_HEADERS, exports.award_rows(db, filters))
    return _attachment(content, f"attributions_{datetime.now():{_STAMP}}.csv", _CSV)


@app.get("/export/attributaires.csv")
def export_suppliers_csv(db: Db, _: Auth = None) -> Response:
    content = exports.to_csv(exports.SUPPLIER_HEADERS, exports.supplier_rows(db))
    return _attachment(content, f"attributaires_{datetime.now():{_STAMP}}.csv", _CSV)


@app.get("/export/attributions.xlsx")
def export_awards_xlsx(
    db: Db, filters: Annotated[AwardFilters, Depends(award_filters)], _: Auth = None
) -> Response:
    content = exports.to_xlsx(
        [("Attributions", exports.AWARD_HEADERS, exports.award_rows(db, filters))]
    )
    return _attachment(content, f"attributions_{datetime.now():{_STAMP}}.xlsx", _XLSX)


@app.get("/export/complet.xlsx")
def export_full_xlsx(db: Db, _: Auth = None) -> Response:
    content = exports.full_workbook(db)
    return _attachment(content, f"radar_complet_{datetime.now():{_STAMP}}.xlsx", _XLSX)


@app.get("/export/complet.json")
def export_full_json(db: Db, _: Auth = None) -> Response:
    content = exports.full_json(db)
    return _attachment(
        content, f"radar_complet_{datetime.now():{_STAMP}}.json", "application/json"
    )


# --------------------------------------------------------------------------- #
# Declenchement manuel d'une collecte
# --------------------------------------------------------------------------- #


def _run_collect_tenders() -> None:
    with session_scope() as session:
        collect_tenders(session, settings)


def _run_collect_awards() -> None:
    with session_scope() as session:
        collect_awards(session, settings)


@app.post("/api/collect/tenders")
def trigger_tenders(tasks: BackgroundTasks, _: Auth = None) -> JSONResponse:
    if not settings.apify_token:
        return JSONResponse(
            {"status": "error", "detail": "APIFY_TOKEN absent du fichier .env"},
            status_code=400,
        )
    tasks.add_task(_run_collect_tenders)
    return JSONResponse({"status": "started", "source": "apify"}, status_code=202)


@app.post("/api/collect/awards")
def trigger_awards(tasks: BackgroundTasks, _: Auth = None) -> JSONResponse:
    tasks.add_task(_run_collect_awards)
    return JSONResponse({"status": "started", "source": "portal"}, status_code=202)


@app.post("/collect")
def trigger_from_dashboard(tasks: BackgroundTasks, _: Auth = None) -> RedirectResponse:
    """Bouton "Collecter maintenant" du tableau de bord."""
    if settings.apify_token:
        tasks.add_task(_run_collect_tenders)
    tasks.add_task(_run_collect_awards)
    flash = "collecte" if settings.apify_token else "collecte-portail"
    return RedirectResponse(url=f"/?ok={flash}", status_code=303)

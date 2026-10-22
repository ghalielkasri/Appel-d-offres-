"""API HTTP et tableau de bord."""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
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

from app import __version__, exports, queries, similarity
from app.config import BASE_DIR, get_settings
from app.db import get_session, init_db, session_scope
from app.messages import format_amount, format_date
from app.models import (
    Award,
    Category,
    DocumentStatus,
    Supplier,
    Tender,
    TenderDocument,
    TenderStatus,
    WatchProfile,
)
from app.pipeline import collect_awards, collect_tenders
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


def _context(request: Request, **extra) -> dict:
    base = {
        "request": request,
        "app_name": settings.app_name,
        "version": __version__,
        "categories": [c.value for c in Category],
        "statuses": [s.value for s in TenderStatus],
        "params": dict(request.query_params),
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------- #
# Pages du tableau de bord
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Db, _: Auth = None) -> Response:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(
            request,
            summary=queries.summary(db),
            closing=queries.closing_soon(db, days=10, limit=12),
            top_suppliers=queries.supplier_ranking(db, limit=8),
            recent_awards=queries.list_awards(db, AwardFilters(), per_page=8).items,
            monthly=queries.monthly_awards(db),
            categories_breakdown=queries.category_breakdown(db),
        ),
    )


@app.get("/recherche", response_class=HTMLResponse)
def search_page(
    request: Request, db: Db, q: str | None = None, _: Auth = None
) -> Response:
    """Un seul champ, qui cherche dans les avis, les attributions et les entreprises."""
    return templates.TemplateResponse(
        request,
        "search.html",
        _context(request, results=queries.global_search(db, q), q=q or ""),
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
            {"name": r.name, "awards_count": r.awards_count, "total_amount": r.total_amount}
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
        _context(request, page_data=queries.list_tenders(db, filters, page, 50), filters=filters),
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
            tender=tender,
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
        _context(request, page_data=queries.list_awards(db, filters, page, 50), filters=filters),
    )


@app.get("/attributaires", response_class=HTMLResponse)
def suppliers_page(
    request: Request, db: Db, q: str | None = None, _: Auth = None
) -> Response:
    return templates.TemplateResponse(
        request,
        "suppliers.html",
        _context(request, suppliers=queries.supplier_ranking(db, limit=200, search=q), q=q or ""),
    )


@app.get("/attributaires/{supplier_id}", response_class=HTMLResponse)
def supplier_detail(
    request: Request, supplier_id: int, db: Db, _: Auth = None
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail="Attributaire inconnu")
    history = queries.supplier_history(db, supplier_id)
    total = sum(a.awarded_amount or 0 for a in history)
    return templates.TemplateResponse(
        request,
        "supplier_detail.html",
        _context(request, supplier=supplier, history=history, total=total),
    )


@app.get("/acheteurs", response_class=HTMLResponse)
def buyers_page(request: Request, db: Db, _: Auth = None) -> Response:
    return templates.TemplateResponse(
        request,
        "buyers.html", _context(request, buyers=queries.buyer_ranking(db, limit=200))
    )


@app.get("/veille", response_class=HTMLResponse)
def profiles_page(request: Request, db: Db, _: Auth = None) -> Response:
    profiles = list(db.scalars(select(WatchProfile).order_by(WatchProfile.id)).all())
    return templates.TemplateResponse(
        request,
        "profiles.html", _context(request, profiles=profiles, settings=settings)
    )


@app.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request, db: Db, _: Auth = None) -> Response:
    return templates.TemplateResponse(
        request,
        "exports.html", _context(request, summary=queries.summary(db))
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
    return RedirectResponse(url="/", status_code=303)

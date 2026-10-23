"""Routes du tableau de bord, de l'API et des exports."""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(seeded_session, monkeypatch):
    import importlib

    import app.api

    importlib.reload(app.api)
    # Le module API ouvre ses propres sessions : on lui passe celle du test.
    app.api.app.dependency_overrides[app.api.get_session] = lambda: seeded_session
    with TestClient(app.api.app) as test_client:
        yield test_client
    app.api.app.dependency_overrides.clear()


PAGES = [
    "/", "/recherche", "/avis", "/attributions", "/attributaires",
    "/acheteurs", "/veille", "/exports", "/candidatures",
    "/profil", "/classement", "/segments", "/concurrents", "/activite",
]


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize(
    "path", ["/api/health", "/api/tenders", "/api/awards", "/api/suppliers", "/api/stats", "/api/bids", "/api/classement"]
)
def test_json_endpoints(client, path):
    response = client.get(path)
    assert response.status_code == 200
    json.loads(response.text)


@pytest.mark.parametrize(
    "value", ["", "false", "true", "0", "nimportequoi"]
)
def test_checkbox_params_never_error(client, value):
    """Une case a cocher vide ou inattendue ne doit pas provoquer de 422."""
    assert client.get(f"/avis?pertinents={value}&ouverts={value}").status_code == 200


def test_tender_detail_page(seeded_session, client):
    """La fiche d'un avis : vue complete, pieces du DCE et comparables."""
    from app.models import Category, WatchProfile
    from app.pipeline import upsert_tender
    from app.sources.base import TenderRecord
    from sqlalchemy import select

    profiles = list(
        seeded_session.scalars(
            select(WatchProfile).where(WatchProfile.active.is_(True))
        ).all()
    )
    tender, _, _ = upsert_tender(
        seeded_session,
        TenderRecord(
            source="t", source_key="D1", reference="AOO 1/2026",
            object="Fourniture de fours mixtes pour la cuisine centrale",
            buyer_raw="Commune de Tanger", category=Category.FOURNITURES,
            estimated_amount=900000.0,
        ),
        profiles,
    )
    seeded_session.flush()

    response = client.get(f"/avis/{tender.id}")
    assert response.status_code == 200
    page = response.text
    assert "Pièces du dossier de consultation" in page
    assert "Marchés comparables" in page
    assert "Fiche complète de l'avis" in page
    # Sans piece referencee, l'explication sur le compte entreprise doit s'afficher.
    assert "compte" in page.lower()


def test_unknown_tender_detail_returns_404(client):
    assert client.get("/avis/999999").status_code == 404


def test_comparables_api(seeded_session, client):
    from app.models import Category, WatchProfile
    from app.pipeline import upsert_award, upsert_tender
    from app.sources.base import AwardRecord, TenderRecord
    from sqlalchemy import select

    profiles = list(
        seeded_session.scalars(
            select(WatchProfile).where(WatchProfile.active.is_(True))
        ).all()
    )
    tender, _, _ = upsert_tender(
        seeded_session,
        TenderRecord(
            source="t", source_key="D2", reference="AOO 2/2026",
            object="Fourniture de chambres froides",
            buyer_raw="Ministère de la Santé", category=Category.FOURNITURES,
            estimated_amount=2000000.0,
        ),
        profiles,
    )
    upsert_award(
        seeded_session,
        AwardRecord(
            source="p", source_key="W2", reference="AOO 9/2025",
            object="Acquisition de chambres froides pour les hôpitaux",
            buyer_raw="Ministère de la Santé", supplier_raw="FROID ATLAS SARL",
            awarded_amount=1900000.0, bids_count=7, category=Category.FOURNITURES,
        ),
    )
    seeded_session.flush()

    payload = client.get(f"/api/tenders/{tender.id}/comparables").json()
    assert set(payload) == {"tender", "benchmark", "comparables", "likely_competitors"}
    assert payload["comparables"], "le precedent de froid du meme acheteur doit remonter"
    assert payload["comparables"][0]["winner"] == "FROID ATLAS SARL"
    assert payload["benchmark"]["median"] == 1900000.0


def test_document_route_refuses_when_not_downloaded(seeded_session, client):
    from app.models import Category, DocumentKind, TenderDocument, WatchProfile
    from app.pipeline import upsert_tender
    from app.sources.base import TenderRecord
    from sqlalchemy import select

    profiles = list(
        seeded_session.scalars(
            select(WatchProfile).where(WatchProfile.active.is_(True))
        ).all()
    )
    tender, _, _ = upsert_tender(
        seeded_session,
        TenderRecord(
            source="t", source_key="D3", reference="AOO 3/2026",
            object="Equipement de cuisine", category=Category.FOURNITURES,
        ),
        profiles,
    )
    seeded_session.flush()
    document = TenderDocument(
        tender_id=tender.id, kind=DocumentKind.BORDEREAU,
        name="BPU.xlsx", url="https://p.ma/bpu.xlsx",
    )
    seeded_session.add(document)
    seeded_session.flush()

    response = client.get(f"/avis/{tender.id}/piece/{document.id}")
    assert response.status_code == 409
    assert "documents-fetch" in response.json()["detail"]


def test_unknown_supplier_returns_404(client):
    assert client.get("/attributaires/999999").status_code == 404


@pytest.mark.parametrize(
    "path,media",
    [
        ("/export/avis.csv", "text/csv"),
        ("/export/attributions.csv", "text/csv"),
        ("/export/attributaires.csv", "text/csv"),
        ("/export/attributions.xlsx", "spreadsheetml"),
        ("/export/complet.xlsx", "spreadsheetml"),
        ("/export/complet.json", "application/json"),
    ],
)
def test_exports_download(client, path, media):
    response = client.get(path)
    assert response.status_code == 200
    assert media in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    assert len(response.content) > 0


def test_csv_has_bom_for_excel(client):
    assert client.get("/export/attributions.csv").content.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("query", ["materiel", "", "xyzinexistant", "equip maroc"])
def test_search_page_handles_any_query(client, query):
    assert client.get(f"/recherche?q={query}").status_code == 200


def test_search_api_shape(client):
    payload = client.get("/api/search?q=materiel").json()
    assert set(payload) == {"query", "total", "tenders", "awards", "suppliers", "buyers"}
    assert "items" in payload["tenders"] and "total" in payload["tenders"]


def test_collect_without_token_is_refused(client):
    response = client.post("/api/collect/tenders")
    assert response.status_code == 400
    assert "APIFY_TOKEN" in response.json()["detail"]

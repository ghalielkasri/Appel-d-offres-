"""Suivi des candidatures, edition des profils depuis l'interface, fiches acheteur."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


@pytest.fixture
def client(seeded_session):
    import importlib

    import app.api

    importlib.reload(app.api)
    app.api.app.dependency_overrides[app.api.get_session] = lambda: seeded_session
    with TestClient(app.api.app) as test_client:
        yield test_client
    app.api.app.dependency_overrides.clear()


def _tender(session, key="T1", obj="Fourniture de fours mixtes pour la cuisine centrale", **extra):
    from app.models import Category, WatchProfile
    from app.pipeline import upsert_tender
    from app.sources.base import TenderRecord

    profiles = list(session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all())
    tender, _, _ = upsert_tender(
        session,
        TenderRecord(
            source="t", source_key=key, reference=f"AOO {key}/2026", object=obj,
            buyer_raw="Commune de Tanger", category=Category.FOURNITURES,
            estimated_amount=900000.0, **extra,
        ),
        profiles,
    )
    session.flush()
    return tender


def _post_bid(client, tender_id, **data):
    return client.post(f"/avis/{tender_id}/suivi", data=data, follow_redirects=False)


def test_bid_lifecycle(seeded_session, client):
    tender = _tender(seeded_session)

    response = _post_bid(client, tender.id, stage="review")
    assert response.status_code == 303
    seeded_session.refresh(tender)
    assert tender.bid is not None and tender.bid.stage.value == "review"

    _post_bid(client, tender.id, stage="submitted", offered_amount="1 250 000,50", note="  Offre déposée  ")
    seeded_session.refresh(tender.bid)
    assert tender.bid.stage.value == "submitted"
    assert tender.bid.offered_amount == 1250000.5
    assert tender.bid.note == "Offre déposée"

    # Un bouton d'etape seul ne doit effacer ni le montant ni la note.
    _post_bid(client, tender.id, stage="won")
    seeded_session.refresh(tender.bid)
    assert tender.bid.stage.value == "won"
    assert tender.bid.offered_amount == 1250000.5
    assert tender.bid.note == "Offre déposée"

    _post_bid(client, tender.id, stage="")
    seeded_session.expire_all()
    assert seeded_session.get(type(tender), tender.id).bid is None


def test_unknown_stage_is_refused(seeded_session, client):
    tender = _tender(seeded_session)
    assert _post_bid(client, tender.id, stage="gagne-peut-etre").status_code == 422


def test_bid_on_unknown_tender_is_404(client):
    assert _post_bid(client, 999999, stage="review").status_code == 404


@pytest.mark.parametrize(
    "target,expected",
    [
        ("/candidatures#etape-go", "/candidatures?ok=suivi#etape-go"),
        ("https://evil.example/", None),
        ("//evil.example/", None),
    ],
)
def test_redirect_stays_inside_the_app(seeded_session, client, target, expected):
    tender = _tender(seeded_session)
    location = _post_bid(client, tender.id, stage="go", next=target).headers["location"]
    assert location == (expected or f"/avis/{tender.id}?ok=suivi")


def test_win_rate_needs_a_decided_bid(seeded_session, client):
    first = _tender(seeded_session, key="A")
    second = _tender(seeded_session, key="B", obj="Acquisition de chambres froides")

    _post_bid(client, first.id, stage="submitted")
    summary = client.get("/api/bids").json()["summary"]
    assert summary["win_rate_pct"] is None
    assert summary["active"] == 1

    _post_bid(client, first.id, stage="won")
    _post_bid(client, second.id, stage="lost")
    summary = client.get("/api/bids").json()["summary"]
    assert summary["win_rate_pct"] == 50.0
    assert summary["won"] == 1 and summary["lost"] == 1 and summary["active"] == 0


def test_pipeline_page_lists_bids(seeded_session, client):
    tender = _tender(seeded_session)
    assert "Aucune candidature suivie" in client.get("/candidatures").text
    _post_bid(client, tender.id, stage="go", note="Vérifier la puissance électrique")
    page = client.get("/candidatures").text
    assert "Fourniture de fours mixtes" in page
    assert "Vérifier la puissance électrique" in page


def test_editing_a_profile_rescores_stored_tenders(seeded_session, client):
    from app.models import WatchProfile

    tender = _tender(seeded_session, key="IT", obj="Acquisition de serveurs et de licences")
    assert tender.match_score == 0

    profile = seeded_session.scalar(select(WatchProfile).where(WatchProfile.active.is_(True)))
    response = client.post(
        f"/veille/{profile.id}", data={"action": "add_keyword", "value": "serveurs, licences"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    seeded_session.refresh(tender)
    assert tender.match_score > 0, "le nouvel avis doit entrer dans le domaine sans nouvelle collecte"
    seeded_session.refresh(profile)
    assert {"serveurs", "licences"} <= set(profile.keywords)

    client.post(f"/veille/{profile.id}", data={"action": "remove_keyword", "value": "serveurs"})
    client.post(f"/veille/{profile.id}", data={"action": "remove_keyword", "value": "licences"})
    seeded_session.refresh(tender)
    assert tender.match_score == 0


def test_exclusion_and_toggle_from_the_interface(seeded_session, client):
    from app.models import WatchProfile

    tender = _tender(seeded_session)
    assert tender.match_score > 0
    profile = seeded_session.scalar(select(WatchProfile).where(WatchProfile.active.is_(True)))

    client.post(f"/veille/{profile.id}", data={"action": "add_exclusion", "value": "fours mixtes"})
    seeded_session.refresh(tender)
    assert tender.match_score == 0

    client.post(f"/veille/{profile.id}", data={"action": "remove_exclusion", "value": "fours mixtes"})
    seeded_session.refresh(tender)
    assert tender.match_score > 0

    client.post(f"/veille/{profile.id}", data={"action": "toggle"})
    seeded_session.refresh(profile)
    assert profile.active is False


def test_profile_rejects_unknown_action(seeded_session, client):
    from app.models import WatchProfile

    profile = seeded_session.scalar(select(WatchProfile))
    assert client.post(f"/veille/{profile.id}", data={"action": "drop_table"}).status_code == 422
    assert client.post(f"/veille/{profile.id}", data={"action": "min_score", "value": "abc"}).status_code == 422
    assert client.post("/veille/999999", data={"action": "toggle"}).status_code == 404


def test_buyer_detail_page(seeded_session, client):
    from app.models import Category
    from app.pipeline import upsert_award
    from app.sources.base import AwardRecord

    tender = _tender(seeded_session)
    award, _ = upsert_award(
        seeded_session,
        AwardRecord(
            source="p", source_key="W1", reference="AOO 9/2025",
            object="Acquisition de fours de cuisson", buyer_raw="Commune de Tanger",
            supplier_raw="CUISINOX SARL", awarded_amount=850000.0, bids_count=6,
            category=Category.FOURNITURES,
        ),
    )
    seeded_session.flush()
    assert award.buyer_id == tender.buyer_id

    page = client.get(f"/acheteurs/{tender.buyer_id}")
    assert page.status_code == 200
    assert "CUISINOX SARL" in page.text
    assert "Fourniture de fours mixtes" in page.text
    assert client.get("/acheteurs/999999").status_code == 404


def test_page_links_encode_the_query(client):
    import app.api

    link = app.api._qs({"q": "froid & cuisson #2", "page": "3", "tri": "recent"}, page=4)
    assert link == "?q=froid+%26+cuisson+%232&tri=recent&page=4"
    assert app.api._qs({"categorie": "Travaux"}, categorie=None) == "?"


def test_demo_bids_are_cleared_with_the_demo(db_session):
    from app.demo import SUIVIS, clear_demo_data, load_demo_data
    from app.models import Bid
    from app.seeds import seed_profiles

    seed_profiles(db_session)
    load_demo_data(db_session)
    assert len(db_session.scalars(select(Bid)).all()) == len(SUIVIS)
    clear_demo_data(db_session)
    assert db_session.scalars(select(Bid)).all() == []

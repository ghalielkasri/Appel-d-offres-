"""Le marche vu depuis votre entreprise : profil, classement, segments, concurrents."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import market
from app.models import Category


@pytest.fixture
def client(seeded_session):
    import importlib

    import app.api

    importlib.reload(app.api)
    app.api.app.dependency_overrides[app.api.get_session] = lambda: seeded_session
    with TestClient(app.api.app) as test_client:
        yield test_client
    app.api.app.dependency_overrides.clear()


def _award(session, key, supplier, amount, days_ago, obj="Fourniture de fours mixtes", buyer="Commune de Tanger"):
    from app.pipeline import upsert_award
    from app.sources.base import AwardRecord

    award, _ = upsert_award(
        session,
        AwardRecord(
            source="t", source_key=key, reference=f"AOO {key}", object=obj, buyer_raw=buyer,
            supplier_raw=supplier, awarded_amount=amount, bids_count=5,
            award_date=date.today() - timedelta(days=days_ago), category=Category.FOURNITURES,
        ),
    )
    session.flush()
    return award


def _settings(name="Arcade Equipement"):
    from app.config import Settings

    return Settings(company_name=name)


def test_own_company_is_found_whatever_the_legal_form(db_session):
    _award(db_session, "A1", "ARCADE EQUIPEMENT SARL AU", 100.0, 10)
    me = market.own_supplier(db_session, _settings())
    assert me is not None and me.name == "ARCADE EQUIPEMENT SARL AU"
    assert market.own_supplier(db_session, _settings("Autre Société")) is None


def test_demo_marker_is_hidden_on_screen_only():
    assert market.display_name("DEMO FROIDEX SARL AU") == "FROIDEX SARL AU"
    assert market.display_name("Demo-Froidex S.A.R.L.") == "Froidex S.A.R.L."
    assert market.display_name("Démolition Atlas") == "Démolition Atlas"


def test_leaderboard_ranks_by_amount_and_tracks_moves(db_session):
    # Periode precedente : B devant A. Periode courante : A devant B.
    _award(db_session, "P1", "Alpha SARL", 100.0, 500)
    _award(db_session, "P2", "Beta SARL", 900.0, 500)
    _award(db_session, "C1", "Alpha SARL", 800.0, 30)
    _award(db_session, "C2", "Beta SARL", 300.0, 60)
    _award(db_session, "X1", "Info SARL", 9_000_000.0, 20, obj="Acquisition de serveurs informatiques")

    awards = market.domain_awards(db_session)
    rows = market.leaderboard(awards, "12m")
    assert [r.name for r in rows] == ["Alpha SARL", "Beta SARL"], "hors domaine exclu, montant d'abord"
    assert rows[0].delta == 1 and rows[1].delta == -1
    assert round(rows[0].share + rows[1].share) == 100

    everything = market.leaderboard(awards, "tout")
    assert everything[0].name == "Beta SARL" and everything[0].delta is None


def test_segments_split_the_market_and_place_you(db_session):
    me = _award(db_session, "S1", "Arcade Equipement", 600.0, 20, obj="Fourniture de chambres froides")
    _award(db_session, "S2", "Froid Plus", 400.0, 25, obj="Fourniture de chambres froides")
    _award(db_session, "S3", "Inox Co", 1000.0, 25, obj="Fourniture de mobilier inox")

    segs = market.segments(market.domain_awards(db_session), [], "12m", me.supplier_id)
    by_family = {s.family: s for s in segs}
    assert round(sum(s.share for s in segs)) == 100
    assert by_family["froid"].me.rank == 1
    assert by_family["mobilier_inox"].me is None
    assert by_family["mobilier_inox"].leader.name == "Inox Co"


def test_head_to_head_sorts_buyers(db_session):
    mine = _award(db_session, "H1", "Arcade Equipement", 500.0, 40, buyer="Commune de Tanger")
    theirs = _award(db_session, "H2", "Froid Plus", 300.0, 40, buyer="Commune de Tanger")
    _award(db_session, "H3", "Froid Plus", 700.0, 40, buyer="CHU Ibn Rochd")
    _award(db_session, "H4", "Arcade Equipement", 200.0, 40, buyer="OFPPT")

    h2h = market.head_to_head(market.domain_awards(db_session), mine.supplier_id, theirs.supplier_id)
    assert [e["name"] for e in h2h.shared] == ["Commune de Tanger"]
    assert [e["name"] for e in h2h.only_them] == ["CHU Ibn Rochd"]
    assert [e["name"] for e in h2h.only_me] == ["OFPPT"]


def test_profile_and_market_pages(seeded_session, client):
    _award(seeded_session, "M1", "Arcade Equipement SARL", 500.0, 40)
    rival = _award(seeded_session, "M2", "Froid Plus", 900.0, 30, obj="Fourniture de chambres froides")

    page = client.get("/profil")
    assert page.status_code == 200 and "Arcade Equipement SARL" in page.text
    for tab in ("marches", "concurrents", "acheteurs"):
        assert client.get(f"/profil?onglet={tab}").status_code == 200
    assert client.get(f"/attributaires/{rival.supplier_id}?onglet=face").status_code == 200
    for path in ("/classement?periode=36m&famille=froid", "/segments?periode=tout", "/concurrents",
                 "/activite?vue=suivis", "/activite?vue=moi&famille=cuisson"):
        assert client.get(path).status_code == 200, path

    me_id = market.own_supplier(seeded_session, _settings()).id
    redirect = client.get(f"/attributaires/{me_id}", follow_redirects=False)
    assert redirect.status_code == 303 and redirect.headers["location"].startswith("/profil")


def test_profile_without_own_company_explains_why(client):
    page = client.get("/profil")
    assert page.status_code == 200
    assert "COMPANY_NAME" in page.text


def test_old_supplier_list_redirects_to_the_leaderboard(client):
    response = client.get("/attributaires", params={"q": "froid & co"}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/classement?q=froid+%26+co"


def test_following_a_competitor(seeded_session, client):
    from app.models import WatchedSupplier

    rival = _award(seeded_session, "F1", "Froid Plus", 900.0, 30, obj="Fourniture de chambres froides")
    client.post(f"/attributaires/{rival.supplier_id}/suivre", data={"next": "/concurrents"})
    assert seeded_session.scalar(select(WatchedSupplier.supplier_id)) == rival.supplier_id
    assert "Froid Plus" in client.get("/concurrents").text
    assert "Froid Plus" in client.get("/activite?vue=suivis").text

    client.post(f"/attributaires/{rival.supplier_id}/suivre")
    assert seeded_session.scalar(select(WatchedSupplier.id)) is None
    assert client.post("/attributaires/999999/suivre").status_code == 404


def test_leaderboard_api(seeded_session, client):
    _award(seeded_session, "J1", "Arcade Equipement", 500.0, 40)
    _award(seeded_session, "J2", "Froid Plus", 900.0, 30, obj="Fourniture de chambres froides")
    payload = client.get("/api/classement").json()
    assert payload["period"] == "12m"
    assert [i["name"] for i in payload["items"]] == ["Froid Plus", "Arcade Equipement"]
    assert payload["items"][1]["is_you"] is True


def test_demo_names_can_be_replaced_locally(db_session):
    """Des noms de substitution renomment les entreprises, pas les marches, et gardent DEMO."""
    from app.demo import ATTRIBUTIONS, load_demo_data
    from app.models import Award, Supplier
    from app.seeds import ensure_default_profiles

    ensure_default_profiles(db_session)
    _tenders, awards = load_demo_data(db_session, {"DEMO FROIDEX SARL AU": "Froid Réel"})
    assert awards == len(ATTRIBUTIONS)
    names = set(db_session.scalars(select(Supplier.name)).all())
    assert "DEMO Froid Réel" in names
    assert not any("froidex" in n.lower() for n in names), "les variantes sont renommees aussi"
    assert all("DEMO" in (a.supplier_raw or "").upper() for a in db_session.scalars(select(Award)).all())


def test_demo_market_has_your_company_and_competitors(db_session):
    from app.demo import WATCHED, clear_demo_data, load_demo_data
    from app.models import WatchedSupplier
    from app.seeds import ensure_default_profiles

    ensure_default_profiles(db_session)
    load_demo_data(db_session)
    me = market.own_supplier(db_session, _settings())
    assert me is not None, "le jeu de demonstration contient Arcade Equipement"
    assert len(market.watched_ids(db_session)) == len(WATCHED)
    rows = market.leaderboard(market.domain_awards(db_session), "12m", None, me.id)
    assert any(r.is_me for r in rows) and len(rows) >= 10

    clear_demo_data(db_session)
    assert db_session.scalar(select(WatchedSupplier.id)) is None

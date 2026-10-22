"""Recherche : insensibilite aux accents, multi-mots, recherche globale."""

import pytest
from sqlalchemy import select

from app.models import Category, WatchProfile
from app.pipeline import upsert_award, upsert_tender
from app.queries import (
    AwardFilters,
    TenderFilters,
    global_search,
    list_awards,
    list_tenders,
    search_terms,
    supplier_ranking,
)
from app.sources.base import AwardRecord, TenderRecord


@pytest.fixture
def accented_data(seeded_session):
    """Donnees accentuees, telles que le portail les publie."""
    session = seeded_session
    profiles = list(
        session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
    )
    rows = [
        ("A1", "Acquisition de matériel et équipement technique", "Commune Urbaine de Tétouan", "Tétouan"),
        ("A2", "Fourniture d'équipements médicaux", "Ministère de la Santé", "Rabat"),
        ("A3", "Achat de mobilier de bureau", "Université Mohammed V", "Rabat"),
    ]
    for ref, obj, buyer, place in rows:
        upsert_tender(
            session,
            TenderRecord(
                source="t", source_key=ref, reference=ref, object=obj,
                buyer_raw=buyer, execution_place=place, category=Category.FOURNITURES,
            ),
            profiles,
        )
    upsert_award(
        session,
        AwardRecord(
            source="p", source_key="W1", reference="A1",
            object="Acquisition de matériel technique",
            buyer_raw="Commune de Tétouan", supplier_raw="STÉ ÉQUIP MAROC",
            awarded_amount=208576.0,
        ),
    )
    session.flush()
    return session


@pytest.mark.parametrize(
    "query,expected",
    [
        ("matériel", 1), ("materiel", 1), ("MATERIEL", 1), ("Matériel", 1),
        ("équipement", 2), ("equipement", 2), ("ÉQUIPEMENT", 2),
        ("Tétouan", 1), ("tetouan", 1), ("TETOUAN", 1),
        ("santé", 1), ("sante", 1),
        ("médicaux", 1), ("medicaux", 1),
        ("université", 1), ("universite", 1),
    ],
)
def test_tender_search_ignores_accents_and_case(accented_data, query, expected):
    """Le cas qui comptait : on tape sans accent, on trouve quand meme."""
    assert list_tenders(accented_data, TenderFilters(search=query, min_score=None)).total == expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("materiel tetouan", 1),   # les deux termes presents
        ("materiel rabat", 0),     # chacun existe, mais pas ensemble
        ("equipements medicaux", 1),
        ("mobilier universite", 1),
    ],
)
def test_multi_word_search_requires_every_word(accented_data, query, expected):
    assert list_tenders(accented_data, TenderFilters(search=query, min_score=None)).total == expected


@pytest.mark.parametrize("query", ["materiel", "matériel", "equip", "ÉQUIP", "tetouan"])
def test_award_search_ignores_accents(accented_data, query):
    assert list_awards(accented_data, AwardFilters(search=query)).total == 1


def test_buyer_filter_ignores_accents(accented_data):
    assert list_tenders(
        accented_data, TenderFilters(buyer="ministere", min_score=None)
    ).total == 1


def test_supplier_ranking_search_ignores_accents(accented_data):
    assert len(supplier_ranking(accented_data, search="equip maroc")) == 1


def test_search_terms_normalises():
    assert search_terms("  Matériel   Médical ") == ["materiel", "medical"]
    assert search_terms("") == []
    assert search_terms(None) == []


def test_global_search_covers_every_kind(accented_data):
    result = global_search(accented_data, "materiel")
    assert result.tenders_total == 1
    assert result.awards_total == 1
    assert result.total >= 2
    assert not result.is_empty


def test_global_search_finds_a_company(accented_data):
    result = global_search(accented_data, "equip maroc")
    assert len(result.suppliers) == 1
    assert result.awards_total == 1


def test_global_search_finds_a_buyer(accented_data):
    result = global_search(accented_data, "tetouan")
    assert len(result.buyers) == 1


def test_global_search_empty_query_returns_nothing(accented_data):
    for query in ("", None, "   "):
        assert global_search(accented_data, query).is_empty


def test_global_search_ignores_watch_profile_scope(seeded_session):
    """Une recherche explicite doit pouvoir sortir du domaine surveille."""
    profiles = list(
        seeded_session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
    )
    upsert_tender(
        seeded_session,
        TenderRecord(
            source="t", source_key="T9", reference="T9",
            object="Travaux de construction d'une route",
            category=Category.TRAVAUX,
        ),
        profiles,
    )
    seeded_session.flush()
    assert global_search(seeded_session, "route").tenders_total == 1


def test_reindex_makes_old_rows_searchable(accented_data):
    """Les lignes privees de search_text redeviennent trouvables apres reindexation."""
    from app.models import Tender
    from app.pipeline import reindex_search

    for tender in accented_data.scalars(select(Tender)).all():
        tender.search_text = None
    accented_data.flush()

    tenders, awards = reindex_search(accented_data)
    assert tenders == 3 and awards == 1
    assert list_tenders(accented_data, TenderFilters(search="materiel", min_score=None)).total == 1

"""Rapprochement des marches : par donneur d'ordre et type d'equipement."""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import Category, Tender, WatchProfile
from app.pipeline import upsert_award, upsert_tender
from app.similarity import (
    comparable_awards,
    comparable_tenders,
    likely_competitors,
    price_benchmark,
    significant_tokens,
    text_similarity,
)
from app.sources.base import AwardRecord, TenderRecord

NOW = datetime.now(timezone.utc)


def _profiles(session):
    return list(
        session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
    )


def _tender(session, ref, obj, buyer, amount=None, days=20):
    record = TenderRecord(
        source="t", source_key=ref, reference=ref, object=obj, buyer_raw=buyer,
        category=Category.FOURNITURES, estimated_amount=amount,
        submission_deadline=NOW + timedelta(days=days),
    )
    tender, _, _ = upsert_tender(session, record, _profiles(session))
    session.flush()
    return tender


def _award(session, ref, obj, buyer, supplier, amount, bids=6, days_ago=100):
    upsert_award(
        session,
        AwardRecord(
            source="p", source_key=f"{ref}|{supplier}", reference=ref, object=obj,
            buyer_raw=buyer, supplier_raw=supplier, awarded_amount=amount,
            bids_count=bids, category=Category.FOURNITURES,
            award_date=(NOW - timedelta(days=days_ago)).date(),
        ),
    )
    session.flush()


@pytest.fixture
def kitchen_data(seeded_session):
    s = seeded_session
    _award(s, "A1", "Fourniture de chambres froides pour les hôpitaux",
           "Ministère de la Santé", "FROID ATLAS SARL", 2250000.0, 6, 160)
    _award(s, "A2", "Acquisition de matériel de réfrigération et conservation",
           "Ministère de la Santé", "Froid-Atlas S.A.R.L.", 1880000.0, 9, 430)
    _award(s, "A3", "Acquisition de matériel informatique et serveurs",
           "Ministère de la Santé", "INFOTECH MAROC", 760000.0, 5, 150)
    _award(s, "A4", "Fourniture de fours de cuisson pour le restaurant",
           "Université Mohammed V", "CUISIPRO MAROC", 915000.0, 7, 185)
    return s


def test_same_buyer_and_family_is_a_strong_match(kitchen_data):
    tender = _tender(
        kitchen_data, "T1", "Fourniture de chambres froides pour les cantines",
        "Ministère de la Santé", 2400000.0,
    )
    comparables = comparable_awards(kitchen_data, tender)
    assert comparables, "les precedents de froid du meme acheteur doivent remonter"
    assert comparables[0].same_buyer is True
    assert "froid" in comparables[0].shared_families
    assert comparables[0].confidence == "forte"


def test_same_buyer_alone_is_not_enough(kitchen_data):
    """Le defaut corrige : un marche informatique du meme acheteur ne doit pas sortir."""
    tender = _tender(
        kitchen_data, "T2", "Fourniture de chambres froides pour les cantines",
        "Ministère de la Santé", 2400000.0,
    )
    noms = [c.award.supplier_raw for c in comparable_awards(kitchen_data, tender, limit=20)]
    assert not any("INFOTECH" in (n or "") for n in noms)


def test_generic_words_do_not_create_matches(kitchen_data):
    """"materiel" et "equipement" sont trop courants pour rapprocher deux marches."""
    tender = _tender(
        kitchen_data, "T3", "Acquisition de matériel de préparation : trancheuses",
        "Ministère de la Santé", 310000.0,
    )
    noms = [c.award.supplier_raw for c in comparable_awards(kitchen_data, tender, limit=20)]
    assert not any("INFOTECH" in (n or "") for n in noms)


def test_match_across_buyers_on_family(kitchen_data):
    """Un autre acheteur reste pertinent si le type d'equipement correspond."""
    tender = _tender(
        kitchen_data, "T4", "Fourniture de fours mixtes pour la cuisine centrale",
        "Commune de Tanger", 980000.0,
    )
    comparables = comparable_awards(kitchen_data, tender)
    assert comparables
    assert comparables[0].award.supplier_raw == "CUISIPRO MAROC"
    assert comparables[0].same_buyer is False


def test_tender_is_not_compared_to_its_own_award(seeded_session):
    s = seeded_session
    tender = _tender(s, "T5", "Fourniture de chambres froides", "Commune X", 100000.0)
    _award(s, "T5", "Fourniture de chambres froides", "Commune X", "GAGNANT SARL", 95000.0)
    s.flush()
    assert all(c.award.tender_id != tender.id for c in comparable_awards(s, tender))


def test_price_benchmark_uses_median(kitchen_data):
    tender = _tender(
        kitchen_data, "T6", "Fourniture de chambres froides pour les cantines",
        "Ministère de la Santé", 2400000.0,
    )
    comparables = comparable_awards(kitchen_data, tender)
    bench = price_benchmark(comparables, tender.estimated_amount)
    assert bench.count == 2
    assert bench.minimum == 1880000.0
    assert bench.maximum == 2250000.0
    assert bench.median_amount == pytest.approx(2065000.0)
    assert bench.gap_to_estimate == pytest.approx(-13.96, abs=0.1)


def test_price_benchmark_handles_no_comparable():
    bench = price_benchmark([], estimate=500000.0)
    assert bench.count == 0
    assert bench.median_amount is None
    assert bench.gap_to_estimate is None


def test_likely_competitors_ranks_same_buyer_first(kitchen_data):
    tender = _tender(
        kitchen_data, "T7", "Fourniture de chambres froides pour les cantines",
        "Ministère de la Santé", 2400000.0,
    )
    competitors = likely_competitors(comparable_awards(kitchen_data, tender))
    assert competitors
    # Les deux variantes du nom sont regroupees en un seul attributaire.
    assert competitors[0]["wins"] == 2
    assert competitors[0]["same_buyer_wins"] == 2


def test_comparable_open_tenders(kitchen_data):
    first = _tender(
        kitchen_data, "T8", "Fourniture de chambres froides pour les cantines",
        "Commune A", 500000.0,
    )
    _tender(
        kitchen_data, "T9", "Acquisition de chambres froides et congélateurs",
        "Commune B", 600000.0,
    )
    similaires = comparable_tenders(kitchen_data, first)
    assert any(t.reference == "T9" for t, _score, _reasons in similaires)


def test_significant_tokens_drops_boilerplate():
    tokens = significant_tokens("Acquisition de fourniture de matériel de cuisine pour la commune")
    assert "cuisine" in tokens
    for mot in ("acquisition", "fourniture", "materiel", "commune"):
        assert mot not in tokens


def test_text_similarity_is_symmetric_and_bounded():
    a, b = "Fourniture de fours mixtes", "Acquisition de fours mixtes de cuisson"
    assert text_similarity(a, b) == text_similarity(b, a)
    assert 0.0 <= text_similarity(a, b) <= 1.0
    assert text_similarity(None, b) == 0.0

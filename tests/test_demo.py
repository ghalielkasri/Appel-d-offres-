"""Jeu de donnees de demonstration : chargement, retrait, et caractere fictif."""

from __future__ import annotations

from sqlalchemy import func, select

from app.demo import ATTRIBUTIONS, AVIS, clear_demo_data, load_demo_data
from app.models import (
    Award,
    DocumentKind,
    NotificationLog,
    Supplier,
    Tender,
    TenderDocument,
)
from app.seeds import ensure_default_profiles


def test_load_and_clear(db_session):
    ensure_default_profiles(db_session)
    avis, attributions = load_demo_data(db_session)
    assert (avis, attributions) == (len(AVIS), len(ATTRIBUTIONS))

    retires = clear_demo_data(db_session)
    assert retires == (len(AVIS), len(ATTRIBUTIONS))
    assert db_session.scalar(select(func.count(Tender.id))) == 0
    assert db_session.scalar(select(func.count(Award.id))) == 0
    assert db_session.scalar(select(func.count(Supplier.id))) == 0


def test_clear_works_when_documents_reference_a_tender(db_session):
    """Regression : une suppression en masse ne declenche pas les cascades.

    Une piece du DCE rattachee a un avis de demonstration faisait echouer le
    retrait sur la contrainte de cle etrangere.
    """
    ensure_default_profiles(db_session)
    load_demo_data(db_session)

    tender = db_session.scalar(select(Tender).limit(1))
    db_session.add(
        TenderDocument(
            tender_id=tender.id, kind=DocumentKind.CPS,
            name="CPS.pdf", url="https://p.ma/cps.pdf",
        )
    )
    db_session.add(
        NotificationLog(
            tender_id=tender.id, channel="email", target="a@b.ma", status="sent"
        )
    )
    db_session.flush()

    clear_demo_data(db_session)
    assert db_session.scalar(select(func.count(Tender.id))) == 0
    assert db_session.scalar(select(func.count(TenderDocument.id))) == 0
    assert db_session.scalar(select(func.count(NotificationLog.id))) == 0


def test_a_real_award_is_detached_not_deleted(db_session):
    """Une attribution reelle rattachee a un avis de demo doit survivre."""
    ensure_default_profiles(db_session)
    load_demo_data(db_session)
    tender = db_session.scalar(select(Tender).limit(1))

    db_session.add(
        Award(
            source="portal:marchespublics", source_key="reel-1",
            tender_id=tender.id, reference="AOO 1/2026",
            supplier_raw="ENTREPRISE REELLE", awarded_amount=1000.0,
        )
    )
    db_session.flush()

    clear_demo_data(db_session)
    reste = db_session.scalars(
        select(Award).where(Award.source == "portal:marchespublics")
    ).all()
    assert len(reste) == 1
    assert reste[0].tender_id is None, "l'attribution doit etre detachee, pas supprimee"


def test_reloading_does_not_duplicate(db_session):
    ensure_default_profiles(db_session)
    load_demo_data(db_session)
    assert load_demo_data(db_session) == (0, 0)
    assert db_session.scalar(select(func.count(Tender.id))) == len(AVIS)


def test_demo_data_is_unmistakably_fictitious(db_session):
    """Le depot etant public, aucune ligne ne doit pouvoir passer pour reelle."""
    ensure_default_profiles(db_session)
    load_demo_data(db_session)

    for tender in db_session.scalars(select(Tender)).all():
        assert tender.reference.startswith("DEMO "), tender.reference
    for award in db_session.scalars(select(Award)).all():
        assert "DEMO" in (award.supplier_raw or "").upper(), award.supplier_raw
        assert award.reference.startswith("DEMO "), award.reference


def test_supplier_variants_still_group(db_session):
    """Les noms fictifs doivent conserver le regroupement des raisons sociales."""
    ensure_default_profiles(db_session)
    load_demo_data(db_session)

    froidex = db_session.scalars(
        select(Supplier).where(Supplier.normalized_name == "demo froidex")
    ).all()
    assert len(froidex) == 1, "les variantes d'ecriture doivent donner un seul attributaire"

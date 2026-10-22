"""Deduplication, scoring, notifications et historique."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

import app.pipeline as pipeline
from app.models import Award, Category, NotificationLog, Supplier, Tender, TenderStatus, WatchProfile
from app.notifiers.base import DeliveryResult, Notifier
from app.sources.base import AwardRecord, TenderRecord


class RecordingNotifier(Notifier):
    """Canal factice qui note ce qu'il recoit."""

    name = "email"

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def targets(self) -> list[str]:
        return ["achats@example.ma"]

    def send(self, tenders):
        self.batches.append([t.reference for t in tenders])
        return [DeliveryResult(self.name, self.targets[0], True)]


def tender_record(ref: str, obj: str, **kwargs) -> TenderRecord:
    defaults = {
        "source": "apify:test",
        "source_key": ref,
        "reference": ref,
        "object": obj,
        "category": Category.FOURNITURES,
        "submission_deadline": datetime.now(timezone.utc) + timedelta(days=20),
    }
    return TenderRecord(**{**defaults, **kwargs})


def active_profiles(session):
    return list(session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all())


def test_same_tender_is_not_duplicated(seeded_session):
    record = tender_record("AOO 1", "Acquisition de fours de cuisine")
    first, is_new, _ = pipeline.upsert_tender(seeded_session, record, active_profiles(seeded_session))
    second, is_new_again, changed = pipeline.upsert_tender(
        seeded_session, record, active_profiles(seeded_session)
    )
    assert is_new is True and is_new_again is False and changed is False
    assert first.id == second.id
    assert seeded_session.scalar(select(func.count(Tender.id))) == 1


def test_modified_tender_is_flagged_as_changed(seeded_session):
    profiles = active_profiles(seeded_session)
    pipeline.upsert_tender(seeded_session, tender_record("AOO 1", "Fours de cuisine"), profiles)
    _, is_new, changed = pipeline.upsert_tender(
        seeded_session, tender_record("AOO 1", "Fours de cuisine - objet revise"), profiles
    )
    assert is_new is False and changed is True


def test_notification_is_sent_once_per_target(seeded_session, monkeypatch):
    notifier = RecordingNotifier()
    monkeypatch.setattr(pipeline, "enabled_notifiers", lambda *a, **k: [notifier])

    profiles = active_profiles(seeded_session)
    tender, _, _ = pipeline.upsert_tender(
        seeded_session, tender_record("AOO 1", "Acquisition de fours de cuisine"), profiles
    )
    seeded_session.flush()

    assert pipeline.notify_tenders(seeded_session, [tender]) == 1
    # Un second appel ne doit rien renvoyer : le journal fait barrage.
    assert pipeline.notify_tenders(seeded_session, [tender]) == 0
    assert len(notifier.batches) == 1
    assert seeded_session.scalar(select(func.count(NotificationLog.id))) == 1


def test_irrelevant_tender_is_never_notified(seeded_session, monkeypatch):
    """Filet de securite : un avis de score nul ne part jamais en alerte."""
    notifier = RecordingNotifier()
    monkeypatch.setattr(pipeline, "enabled_notifiers", lambda *a, **k: [notifier])

    profiles = active_profiles(seeded_session)
    tender, _, _ = pipeline.upsert_tender(
        seeded_session,
        tender_record("AOO 9", "Travaux de voirie", category=Category.TRAVAUX),
        profiles,
    )
    seeded_session.flush()
    assert tender.match_score == 0
    assert pipeline.notify_tenders(seeded_session, [tender]) == 0
    assert notifier.batches == []


def award_record(ref: str, supplier: str, amount: float) -> AwardRecord:
    return AwardRecord(
        source="portal:test",
        source_key=f"{ref} | {supplier}",
        reference=ref,
        object="Fourniture de materiel de cuisine",
        buyer_raw="Commune de Azla",
        supplier_raw=supplier,
        awarded_amount=amount,
        bids_count=5,
        category=Category.FOURNITURES,
    )


def test_award_variants_group_under_one_supplier(seeded_session):
    """Deux ecritures d'un meme nom doivent pointer sur le meme attributaire."""
    pipeline.upsert_award(seeded_session, award_record("A1", "STE EQUIP MAROC SARL AU", 100.0))
    pipeline.upsert_award(seeded_session, award_record("A2", "Equip-Maroc S.A.R.L.", 200.0))
    seeded_session.flush()

    assert seeded_session.scalar(select(func.count(Supplier.id))) == 1
    supplier = seeded_session.scalars(select(Supplier)).one()
    total = seeded_session.scalar(
        select(func.sum(Award.awarded_amount)).where(Award.supplier_id == supplier.id)
    )
    assert total == 300.0


def test_award_is_not_duplicated(seeded_session):
    record = award_record("A1", "EQUIP MAROC", 100.0)
    _, first = pipeline.upsert_award(seeded_session, record)
    _, second = pipeline.upsert_award(seeded_session, record)
    assert first is True and second is False
    assert seeded_session.scalar(select(func.count(Award.id))) == 1


def test_award_marks_matching_tender_as_awarded(seeded_session):
    profiles = active_profiles(seeded_session)
    tender, _, _ = pipeline.upsert_tender(
        seeded_session, tender_record("AOO 7", "Acquisition de fours de cuisine"), profiles
    )
    seeded_session.flush()
    pipeline.upsert_award(seeded_session, award_record("AOO 7", "EQUIP MAROC", 500.0))
    seeded_session.flush()

    seeded_session.refresh(tender)
    assert tender.status is TenderStatus.AWARDED
    assert len(tender.awards) == 1

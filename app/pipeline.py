"""Chaine de traitement : collecte -> deduplication -> scoring -> notification.

Deux garanties structurent ce module :

* **Pas de doublon** : un avis est identifie par (source, source_key). Une
  seconde collecte met a jour l'avis existant au lieu d'en creer un nouveau.
* **Pas de notification en double** : chaque envoi reussi est journalise dans
  ``notification_logs``, dont la contrainte d'unicite porte sur
  (avis, profil, canal, destinataire).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.matching import match_profiles
from app.models import (
    Award,
    Buyer,
    NotificationLog,
    SourceRun,
    Supplier,
    Tender,
    TenderStatus,
    WatchProfile,
)
from app.normalize import (
    clean_whitespace,
    content_hash,
    normalize_entity_name,
    normalize_text,
)
from app.notifiers import enabled_notifiers
from app.seeds import keywords_for_collection
from app.sources.apify_tenders import ApifyTenderSource
from app.sources.base import AwardRecord, TenderRecord
from app.taxonomy import detect_families
from app.sources.portal_awards import PortalAwardSource

logger = logging.getLogger(__name__)

# Champs dont une modification signale une vraie mise a jour de l'avis.
_TRACKED_FIELDS = (
    "object", "category", "procedure_type", "buyer_raw", "buyer_service",
    "execution_place", "lots_count", "publication_date", "submission_deadline",
    "opening_session_date", "estimated_amount", "caution_provisoire",
    "consultation_url",
)


def upsert_buyer(session: Session, name: str | None, acronym: str | None = None) -> Buyer | None:
    name = clean_whitespace(name)
    if not name:
        return None
    key = normalize_entity_name(name)
    if not key:
        return None
    buyer = session.scalar(select(Buyer).where(Buyer.normalized_name == key))
    if buyer is None:
        buyer = Buyer(name=name, normalized_name=key, acronym=clean_whitespace(acronym))
        session.add(buyer)
        session.flush()
    elif acronym and not buyer.acronym:
        buyer.acronym = clean_whitespace(acronym)
    return buyer


def upsert_supplier(session: Session, name: str | None) -> Supplier | None:
    """Regroupe les variantes d'une meme raison sociale sous un seul attributaire."""
    name = clean_whitespace(name)
    if not name:
        return None
    key = normalize_entity_name(name)
    if not key:
        return None
    supplier = session.scalar(select(Supplier).where(Supplier.normalized_name == key))
    if supplier is None:
        supplier = Supplier(name=name, normalized_name=key)
        session.add(supplier)
        session.flush()
    return supplier


def build_search_text(*parts: str | None) -> str:
    """Assemble la forme de recherche : sans accents, en minuscules.

    C'est contre cette colonne que portent les recherches de l'interface, afin
    que « materiel » trouve « matériel » et « tetouan » trouve « Tétouan ».
    """
    return normalize_text(" ".join(part for part in parts if part))


def _apply_scoring(tender: Tender, record: TenderRecord, profiles: list[WatchProfile]) -> None:
    matches = match_profiles(record, profiles)
    tender.match_score = matches[0].score if matches else 0.0
    keywords: list[str] = []
    for match in matches:
        for keyword in match.keywords:
            if keyword not in keywords:
                keywords.append(keyword)
    tender.matched_keywords = keywords
    tender.matched_profiles = [m.profile_id for m in matches if m.profile_id]


def upsert_tender(
    session: Session, record: TenderRecord, profiles: list[WatchProfile]
) -> tuple[Tender, bool, bool]:
    """Cree ou met a jour un avis. Renvoie (avis, est_nouveau, a_change)."""
    existing = session.scalar(
        select(Tender).where(
            Tender.source == record.source, Tender.source_key == record.source_key
        )
    )
    digest = content_hash(
        {field: getattr(record, field) for field in _TRACKED_FIELDS}
    )
    buyer = upsert_buyer(session, record.buyer_raw, record.buyer_acronym)
    now = datetime.now(timezone.utc)

    if existing is None:
        tender = Tender(
            source=record.source,
            source_key=record.source_key,
            reference=record.reference,
            consultation_url=record.consultation_url,
            object=record.object,
            category=record.category,
            procedure_type=record.procedure_type,
            execution_place=record.execution_place,
            lots_count=record.lots_count,
            buyer_id=buyer.id if buyer else None,
            buyer_raw=record.buyer_raw,
            buyer_service=record.buyer_service,
            publication_date=record.publication_date,
            submission_deadline=record.submission_deadline,
            opening_session_date=record.opening_session_date,
            estimated_amount=record.estimated_amount,
            caution_provisoire=record.caution_provisoire,
            currency=record.currency,
            raw=record.raw,
            content_hash=digest,
            search_text=build_search_text(
                record.object, record.buyer_raw, record.buyer_service,
                record.execution_place, record.reference, record.procedure_type,
            ),
            families=detect_families(record.object, record.procedure_type),
            first_seen_at=now,
            last_seen_at=now,
        )
        _apply_scoring(tender, record, profiles)
        session.add(tender)
        session.flush()
        return tender, True, False

    existing.last_seen_at = now
    changed = existing.content_hash != digest
    if changed:
        for field in _TRACKED_FIELDS:
            setattr(existing, field, getattr(record, field))
        existing.reference = record.reference or existing.reference
        existing.buyer_id = buyer.id if buyer else existing.buyer_id
        existing.currency = record.currency or existing.currency
        existing.raw = record.raw
        existing.content_hash = digest
        existing.search_text = build_search_text(
            record.object, record.buyer_raw, record.buyer_service,
            record.execution_place, record.reference, record.procedure_type,
        )
        existing.families = detect_families(record.object, record.procedure_type)
        _apply_scoring(existing, record, profiles)
    return existing, False, changed


def _refresh_tender_status(session: Session, award: Award) -> None:
    """Un avis attribue passe au statut AWARDED et pointe vers son attribution."""
    if award.tender_id is None and award.reference:
        tender = session.scalar(
            select(Tender).where(Tender.reference == award.reference)
        )
        if tender:
            award.tender_id = tender.id
    if award.tender_id:
        tender = session.get(Tender, award.tender_id)
        if tender and tender.status != TenderStatus.AWARDED:
            tender.status = TenderStatus.AWARDED


def upsert_award(session: Session, record: AwardRecord) -> tuple[Award, bool]:
    """Cree ou met a jour une attribution. Renvoie (attribution, est_nouvelle)."""
    existing = session.scalar(
        select(Award).where(
            Award.source == record.source, Award.source_key == record.source_key
        )
    )
    buyer = upsert_buyer(session, record.buyer_raw)
    supplier = upsert_supplier(session, record.supplier_raw)

    if existing is None:
        award = Award(
            source=record.source,
            source_key=record.source_key,
            reference=record.reference,
            object=record.object,
            lot_number=record.lot_number,
            buyer_id=buyer.id if buyer else None,
            buyer_raw=record.buyer_raw,
            supplier_id=supplier.id if supplier else None,
            supplier_raw=record.supplier_raw,
            awarded_amount=record.awarded_amount,
            estimated_amount=record.estimated_amount,
            currency=record.currency,
            amount_includes_tax=record.amount_includes_tax,
            bids_count=record.bids_count,
            award_date=record.award_date,
            category=record.category,
            procedure_type=record.procedure_type,
            consultation_url=record.consultation_url,
            search_text=build_search_text(
                record.object, record.supplier_raw, record.buyer_raw,
                record.reference, record.procedure_type,
            ),
            families=detect_families(record.object, record.procedure_type),
            raw=record.raw,
        )
        session.add(award)
        session.flush()
        _refresh_tender_status(session, award)
        return award, True

    # Les attributions sont immuables en pratique ; on complete seulement les trous.
    for field in (
        "object", "lot_number", "awarded_amount", "estimated_amount",
        "amount_includes_tax", "bids_count", "award_date", "procedure_type",
        "consultation_url",
    ):
        if getattr(existing, field) in (None, "") and getattr(record, field) is not None:
            setattr(existing, field, getattr(record, field))
    if supplier and existing.supplier_id is None:
        existing.supplier_id = supplier.id
    if buyer and existing.buyer_id is None:
        existing.buyer_id = buyer.id
    existing.search_text = build_search_text(
        existing.object, existing.supplier_raw, existing.buyer_raw,
        existing.reference, existing.procedure_type,
    )
    existing.families = detect_families(existing.object, existing.procedure_type)
    existing.scraped_at = datetime.now(timezone.utc)
    _refresh_tender_status(session, existing)
    return existing, False


def pending_notifications(
    session: Session, tenders: list[Tender], channel: str, target: str
) -> list[Tender]:
    """Avis pas encore notifies sur ce couple canal/destinataire."""
    if not tenders:
        return []
    ids = [t.id for t in tenders]
    already = set(
        session.scalars(
            select(NotificationLog.tender_id).where(
                NotificationLog.tender_id.in_(ids),
                NotificationLog.channel == channel,
                NotificationLog.target == target,
                NotificationLog.status == "sent",
            )
        ).all()
    )
    return [t for t in tenders if t.id not in already]


def notify_tenders(
    session: Session, tenders: list[Tender], settings: Settings | None = None
) -> int:
    """Envoie les alertes et journalise chaque envoi. Renvoie le nombre de messages."""
    settings = settings or get_settings()

    # Filet de securite : un avis de score nul ne releve pas du domaine surveille.
    # Sans ce garde-fou, un appelant qui passerait toute la base declencherait une
    # avalanche d'alertes hors sujet.
    tenders = [t for t in tenders if t.match_score > 0]
    if not tenders:
        return 0

    notifiers = enabled_notifiers(settings)
    if not notifiers:
        logger.info(
            "Aucun canal de notification configure : %d avis enregistres sans alerte. "
            "Renseignez SMTP_* , WHATSAPP_* ou WEBHOOK_URL dans .env.",
            len(tenders),
        )
        return 0

    sent = 0
    for notifier in notifiers:
        for target in notifier.targets:
            batch = pending_notifications(session, tenders, notifier.name, target)
            if not batch:
                continue
            # Les avis les plus pertinents en tete du message.
            batch.sort(key=lambda t: t.match_score, reverse=True)
            for result in notifier.send(batch):
                if result.target != target:
                    continue
                for tender in batch:
                    session.add(
                        NotificationLog(
                            tender_id=tender.id,
                            profile_id=(tender.matched_profiles or [None])[0],
                            channel=notifier.name,
                            target=target,
                            status="sent" if result.ok else "failed",
                            error=result.error,
                        )
                    )
                if result.ok:
                    sent += 1
                    logger.info(
                        "%s -> %s : %d avis notifies", notifier.name, target, len(batch)
                    )
                else:
                    logger.warning(
                        "%s -> %s : echec (%s)", notifier.name, target, result.error
                    )
            session.flush()
    return sent


def collect_tenders(
    session: Session,
    settings: Settings | None = None,
    keywords: list[str] | None = None,
    notify: bool = True,
) -> SourceRun:
    """Collecte les avis chez Apify, les enregistre, puis notifie les nouveautes."""
    settings = settings or get_settings()
    source = ApifyTenderSource(settings)
    run = SourceRun(source=source.name)
    session.add(run)
    session.flush()

    try:
        profiles = list(
            session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
        )
        search_terms = keywords if keywords is not None else keywords_for_collection(session)
        records = source.fetch_tenders(search_terms)

        new_tenders: list[Tender] = []
        updated = 0
        for record in records:
            tender, is_new, changed = upsert_tender(session, record, profiles)
            if is_new and tender.match_score > 0:
                new_tenders.append(tender)
            elif changed:
                updated += 1
        session.flush()

        run.items_fetched = len(records)
        run.items_new = len(new_tenders)
        run.items_updated = updated
        run.estimated_cost_usd = round(len(records) * source.cost_per_item_usd, 4)
        run.details = {
            "keywords": search_terms,
            "actor": source.actor_id,
            "matched": len(new_tenders),
        }

        if notify and new_tenders:
            run.notifications_sent = notify_tenders(session, new_tenders, settings)

        run.status = "succeeded"
    except Exception as exc:
        logger.exception("Collecte des avis en echec")
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = datetime.now(timezone.utc)
        session.flush()
    return run


def collect_awards(
    session: Session, settings: Settings | None = None, since: date | None = None
) -> SourceRun:
    """Collecte les resultats d'attribution sur le portail officiel."""
    settings = settings or get_settings()
    source = PortalAwardSource(settings)
    run = SourceRun(source=source.name)
    session.add(run)
    session.flush()

    try:
        records = source.fetch_awards(since=since)
        new_count = 0
        for record in records:
            _, is_new = upsert_award(session, record)
            new_count += int(is_new)
        session.flush()
        run.items_fetched = len(records)
        run.items_new = new_count
        run.items_updated = len(records) - new_count
        run.status = "succeeded"
        if not records:
            run.error = (
                "Aucune attribution recuperee. Verifiez l'URL de la rubrique Resultats "
                "avec `radar portal-discover` depuis le serveur."
            )
    except Exception as exc:
        logger.exception("Collecte des attributions en echec")
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        run.finished_at = datetime.now(timezone.utc)
        session.flush()
    return run


def _record_from_tender(tender: Tender) -> TenderRecord:
    """Reconstitue l'entree du moteur de filtrage a partir d'un avis enregistre."""
    return TenderRecord(
        source=tender.source,
        source_key=tender.source_key,
        reference=tender.reference,
        object=tender.object,
        category=tender.category,
        procedure_type=tender.procedure_type,
        buyer_raw=tender.buyer_raw,
        buyer_service=tender.buyer_service,
        execution_place=tender.execution_place,
        estimated_amount=tender.estimated_amount,
        currency=tender.currency,
    )


def rescore_tenders(session: Session) -> tuple[int, int]:
    """Recalcule le score de veille de tous les avis enregistres.

    Le score est fige a la collecte : sans ce recalcul, un mot-cle ajoute a un
    profil ne s'appliquerait qu'aux avis collectes ensuite. Renvoie
    (avis recalcules, avis desormais pertinents).
    """
    profiles = list(
        session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
    )
    tenders = list(session.scalars(select(Tender)).all())
    for tender in tenders:
        _apply_scoring(tender, _record_from_tender(tender), profiles)
    session.flush()
    return len(tenders), sum(1 for t in tenders if t.match_score > 0)


def reindex_search(session: Session) -> tuple[int, int]:
    """Recalcule search_text sur toutes les lignes existantes.

    A lancer une fois apres la mise a jour qui introduit la colonne, pour que
    les donnees deja collectees soient trouvables sans accents.
    """
    tenders = list(session.scalars(select(Tender)).all())
    for tender in tenders:
        tender.search_text = build_search_text(
            tender.object, tender.buyer_raw, tender.buyer_service,
            tender.execution_place, tender.reference, tender.procedure_type,
        )
        tender.families = detect_families(tender.object, tender.procedure_type)
    awards = list(session.scalars(select(Award)).all())
    for award in awards:
        award.search_text = build_search_text(
            award.object, award.supplier_raw, award.buyer_raw,
            award.reference, award.procedure_type,
        )
        award.families = detect_families(award.object, award.procedure_type)
    session.flush()
    return len(tenders), len(awards)


def ingest_awards(session: Session, records: list[AwardRecord]) -> tuple[int, int]:
    """Enregistre une liste d'attributions deja constituee (import, fichier HTML)."""
    new_count = 0
    for record in records:
        _, is_new = upsert_award(session, record)
        new_count += int(is_new)
    session.flush()
    return new_count, len(records) - new_count

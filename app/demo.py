"""Jeu de donnees de demonstration, pour explorer l'interface avant la premiere collecte.

AVERTISSEMENT : toutes les donnees de ce fichier sont INVENTEES. Les noms
d'attributaires, les montants et les references sont fictifs et ne
correspondent a aucun marche reel. Chaque reference porte la mention "DEMO"
et chaque attributaire le prefixe "DEMO", afin qu'aucune ligne ne puisse etre
prise pour un vrai resultat d'attribution. Les noms d'acheteurs publics sont
ceux d'institutions reelles, mais les marches qui leur sont attribues ici
n'existent pas.

Les enregistrements portent la source "demo:*", ce qui permet de les retirer
d'un seul coup sans toucher aux donnees reelles.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    Award,
    Category,
    NotificationLog,
    Supplier,
    Tender,
    TenderDocument,
    WatchProfile,
)
from app.pipeline import upsert_award, upsert_tender
from app.sources.base import AwardRecord, TenderRecord

DEMO_SOURCE_TENDER = "demo:avis"
DEMO_SOURCE_AWARD = "demo:attributions"

_NOW = datetime.now(timezone.utc)

# (reference, objet, acheteur, lieu, categorie, estimation, jours avant echeance)
# Consultations EN COURS, centrees sur la cuisine professionnelle.
AVIS: list[tuple] = [
    ("DEMO AOO 12/2026/CUT", "Acquisition et installation d'équipements de cuisine pour la cuisine centrale municipale",
     "Commune Urbaine de Tanger", "Tanger", Category.FOURNITURES, 1850000.0, 12),
    ("DEMO AOO 45/2026/MS", "Fourniture de chambres froides et de matériel de réfrigération pour les cantines hospitalières",
     "Ministère de la Santé", "Rabat", Category.FOURNITURES, 2400000.0, 21),
    ("DEMO AOO 08/2026/UM5", "Fourniture de fours mixtes et de matériel de cuisson pour le restaurant universitaire",
     "Université Mohammed V", "Rabat", Category.FOURNITURES, 980000.0, 5),
    ("DEMO AOO 31/2026/CRT", "Acquisition de matériel de laverie : lave-vaisselle à capot et tunnel de lavage",
     "Conseil Régional de Tétouan", "Tétouan", Category.FOURNITURES, 620000.0, 3),
    ("DEMO AOO 54/2026/MEN", "Équipement des cantines scolaires : mobilier inox, chariots de distribution et vaisselle",
     "Ministère de l'Éducation Nationale", "Meknès", Category.FOURNITURES, 3100000.0, 28),
    ("DEMO AOO 17/2026/ONDA", "Installation de hottes aspirantes et système d'extraction des fumées de cuisine",
     "Office National des Aéroports", "Casablanca", Category.SERVICES, 540000.0, 16),
    ("DEMO AOO 22/2026/CUT", "Fourniture de matériel de préparation : trancheuses, pétrins et robots coupe",
     "Commune Urbaine de Tanger", "Tanger", Category.FOURNITURES, 310000.0, 9),
    ("DEMO AOO 63/2026/OFPPT", "Acquisition de cuisines pédagogiques complètes pour les centres de formation hôtelière",
     "OFPPT", "Marrakech", Category.FOURNITURES, 4200000.0, 34),
    # Hors domaine : doivent etre ecartes de la veille et des rapprochements.
    ("DEMO AOO 99/2026/MET", "Travaux de construction d'une route provinciale",
     "Ministère de l'Équipement et du Transport", "Béni Mellal", Category.TRAVAUX, 42000000.0, 18),
    ("DEMO AOO 77/2026/CUT", "Acquisition de matériel informatique et de licences logicielles",
     "Commune Urbaine de Tanger", "Tanger", Category.FOURNITURES, 890000.0, 14),
]

# (reference, objet, acheteur, attributaire, montant, concurrents, jours depuis attribution, TTC)
# Precedents ATTRIBUES : plusieurs sont comparables aux avis ci-dessus, par
# acheteur et par famille d'equipement, ce qui permet d'eprouver le moteur.
ATTRIBUTIONS: list[tuple] = [
    ("DEMO AOO 10/2025/CUT", "Acquisition d'équipements de cuisine pour la cuisine centrale municipale",
     "Commune Urbaine de Tanger", "DEMO FROIDEX SARL AU", 1720000.0, 8, 210, True),
    ("DEMO AOO 04/2024/CUT", "Fourniture de matériel de cuisson et de froid pour les cantines communales",
     "Commune Urbaine de Tanger", "DEMO CUISITEST SARL", 1395000.0, 11, 520, False),
    ("DEMO AOO 41/2025/MS", "Fourniture de chambres froides pour les hôpitaux régionaux",
     "Ministère de la Santé", "DEMO FROIDEX SARL AU", 2250000.0, 6, 160, False),
    ("DEMO AOO 38/2024/MS", "Acquisition de matériel de réfrigération et de conservation",
     "Ministère de la Santé", "Demo-Froidex S.A.R.L.", 1880000.0, 9, 430, True),
    ("DEMO AOO 06/2025/UM5", "Fourniture de fours de cuisson pour le restaurant universitaire",
     "Université Mohammed V", "DEMO CUISITEST SARL", 915000.0, 7, 185, False),
    ("DEMO AOO 29/2024/CRT", "Acquisition de lave-vaisselle et matériel de plonge",
     "Conseil Régional de Tétouan", "DEMO LAVATEST EQUIPEMENT", 578000.0, 5, 380, True),
    ("DEMO AOO 50/2025/MEN", "Mobilier inox et chariots de distribution pour les cantines scolaires",
     "Ministère de l'Éducation Nationale", "DEMO INOXTEST SARL", 2870000.0, 13, 140, False),
    ("DEMO AOO 48/2024/MEN", "Fourniture de vaisselle et ustensiles pour les internats",
     "Ministère de l'Éducation Nationale", "DEMO INOXTEST SARL", 640000.0, 15, 470, True),
    ("DEMO AOO 14/2025/ONDA", "Installation de hottes et extraction pour les cuisines des aérogares",
     "Office National des Aéroports", "DEMO VENTITEST SARL", 495000.0, 4, 120, True),
    ("DEMO AOO 19/2025/CUT", "Acquisition de trancheuses et matériel de préparation",
     "Commune Urbaine de Tanger", "DEMO CUISITEST SARL", 268000.0, 9, 95, False),
    ("DEMO AOO 60/2024/OFPPT", "Équipement de cuisines pédagogiques pour les centres de formation",
     "OFPPT", "DEMO FROIDEX SARL AU", 3850000.0, 10, 410, False),
    # Precedent hors domaine, chez un acheteur present ci-dessus : il ne doit
    # jamais ressortir comme comparable d'un marche de cuisine.
    ("DEMO AOO 70/2025/CUT", "Acquisition de matériel informatique et serveurs",
     "Commune Urbaine de Tanger", "DEMO INFOTEST", 760000.0, 6, 150, True),
]

def load_demo_data(session: Session) -> tuple[int, int]:
    """Insere le jeu de demonstration. Renvoie (avis, attributions)."""
    profiles = list(
        session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
    )

    tenders = 0
    for ref, obj, buyer, place, category, amount, days in AVIS:
        record = TenderRecord(
            source=DEMO_SOURCE_TENDER,
            source_key=ref,
            reference=ref,
            object=obj,
            buyer_raw=buyer,
            execution_place=place,
            category=category,
            procedure_type="Appel d'offres ouvert" if ref.startswith("AOO") else "Bon de commande",
            estimated_amount=amount,
            publication_date=(_NOW - timedelta(days=30 - days)).date(),
            submission_deadline=_NOW + timedelta(days=days),
            consultation_url=f"https://www.marchespublics.gov.ma/demo/{ref.replace(' ', '-').replace('/', '-')}",
            # Charge brute d'exemple, au format de l'actor publicmoney : la fiche
            # d'un avis montre ainsi la section « Donnees brutes de la source ».
            raw={
                "reference": ref,
                "object": obj,
                "buyer": buyer,
                "category": category.value,
                "deadline": (_NOW + timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
                "consultationUrl": f"https://www.marchespublics.gov.ma/demo/{ref}",
                "_demo": True,
            },
        )
        _, is_new, _ = upsert_tender(session, record, profiles)
        tenders += int(is_new)

    awards = 0
    for ref, obj, buyer, supplier, amount, bids, days_ago, ttc in ATTRIBUTIONS:
        record = AwardRecord(
            source=DEMO_SOURCE_AWARD,
            source_key=f"{ref} | {supplier}",
            reference=ref,
            object=obj,
            buyer_raw=buyer,
            supplier_raw=supplier,
            awarded_amount=amount,
            bids_count=bids,
            amount_includes_tax=ttc,
            award_date=(_NOW - timedelta(days=days_ago)).date(),
            category=Category.FOURNITURES,
            procedure_type="Appel d'offres ouvert",
            consultation_url=f"https://www.marchespublics.gov.ma/demo/{ref.replace(' ', '-').replace('/', '-')}",
        )
        _, is_new = upsert_award(session, record)
        awards += int(is_new)

    session.flush()
    return tenders, awards


def clear_demo_data(session: Session) -> tuple[int, int]:
    """Retire le jeu de demonstration, sans toucher aux donnees reelles.

    L'ordre des suppressions compte. Une suppression en masse ne declenche pas
    les cascades de l'ORM : il faut donc retirer d'abord tout ce qui pointe vers
    les avis de demonstration — pieces du DCE, journaux de notification,
    attributions rattachees — sinon la contrainte de cle etrangere echoue.
    """
    ids = list(
        session.scalars(
            select(Tender.id).where(Tender.source == DEMO_SOURCE_TENDER)
        ).all()
    )

    if ids:
        session.execute(delete(TenderDocument).where(TenderDocument.tender_id.in_(ids)))
        session.execute(delete(NotificationLog).where(NotificationLog.tender_id.in_(ids)))
        # Une attribution reelle peut etre rattachee a un avis de demonstration :
        # on la detache plutot que de la supprimer.
        session.execute(
            update(Award)
            .where(Award.tender_id.in_(ids), Award.source != DEMO_SOURCE_AWARD)
            .values(tender_id=None)
        )

    awards = session.execute(
        delete(Award).where(Award.source == DEMO_SOURCE_AWARD)
    ).rowcount
    tenders = session.execute(
        delete(Tender).where(Tender.source == DEMO_SOURCE_TENDER)
    ).rowcount

    # Les attributaires devenus orphelins sont supprimes a leur tour.
    orphans = session.scalars(
        select(Supplier).where(
            ~Supplier.id.in_(select(Award.supplier_id).where(Award.supplier_id.isnot(None)))
        )
    ).all()
    for supplier in orphans:
        session.delete(supplier)
    session.flush()
    return tenders, awards

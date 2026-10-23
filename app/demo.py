"""Jeu de donnees de demonstration, pour explorer l'interface avant la premiere collecte.

AVERTISSEMENT : toutes les donnees de ce fichier sont INVENTEES. Les noms
d'attributaires, les montants et les references sont fictifs et ne
correspondent a aucun marche reel. Chaque reference porte la mention "DEMO"
et chaque attributaire le prefixe "DEMO", afin qu'aucune ligne ne puisse etre
prise pour un vrai resultat d'attribution. A l'ecran, le prefixe est remplace
par une banniere « donnees fictives » presente sur toutes les pages. Les noms
d'acheteurs publics sont ceux d'institutions reelles, mais les marches qui
leur sont attribues ici n'existent pas.

Le marche est genere, mais toujours le meme (graine fixe) : onze entreprises
avec leurs familles d'equipement, leurs acheteurs et leur trajectoire, dont
« Arcade Equipement », l'entreprise du tableau de bord, en progression.

Les enregistrements portent la source "demo:*", ce qui permet de les retirer
d'un seul coup sans toucher aux donnees reelles.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import (
    Award,
    Bid,
    BidStage,
    Category,
    NotificationLog,
    Supplier,
    Tender,
    TenderDocument,
    WatchedSupplier,
    WatchProfile,
)
from app.normalize import normalize_entity_name
from app.pipeline import upsert_award, upsert_tender
from app.sources.base import AwardRecord, TenderRecord

DEMO_SOURCE_TENDER = "demo:avis"
DEMO_SOURCE_AWARD = "demo:attributions"

_NOW = datetime.now(timezone.utc)
_TODAY = _NOW.date()

# (acheteur, sigle, ville, destination ajoutee a l'objet)
BUYERS: list[tuple[str, str, str, str]] = [
    ("Commune Urbaine de Tanger", "CUT", "Tanger", "pour la cuisine centrale municipale"),
    ("Commune de Casablanca", "CC", "Casablanca", "pour les cantines communales"),
    ("Ministère de la Santé", "MS", "Rabat", "pour les cuisines hospitalières"),
    ("CHU Ibn Rochd de Casablanca", "CHUIR", "Casablanca", "pour la cuisine du CHU"),
    ("Université Mohammed V", "UM5", "Rabat", "pour le restaurant universitaire"),
    ("Université Hassan II de Casablanca", "UH2C", "Casablanca", "pour le restaurant universitaire"),
    ("Université Cadi Ayyad", "UCA", "Marrakech", "pour les restaurants universitaires"),
    ("Office National des Œuvres Universitaires Sociales et Culturelles", "ONOUSC", "Rabat", "pour les cités universitaires"),
    ("Ministère de l'Éducation Nationale", "MEN", "Meknès", "pour les cantines scolaires"),
    ("Délégation Générale à l'Administration Pénitentiaire", "DGAPR", "Rabat", "pour les établissements pénitentiaires"),
    ("OFPPT", "OFPPT", "Marrakech", "pour les centres de formation hôtelière"),
    ("Office National des Aéroports", "ONDA", "Casablanca", "pour les restaurants des aérogares"),
    ("Conseil Régional de Tanger-Tétouan-Al Hoceïma", "CRTTA", "Tétouan", "pour les internats de la région"),
    ("Conseil Régional de Souss-Massa", "CRSM", "Agadir", "pour les internats de la région"),
]
_BUYER = {acr: (name, city, dest) for name, acr, city, dest in BUYERS}

# Objets par famille : chacun designe bien sa famille (voir app/taxonomy.py).
OBJECTS: dict[str, list[str]] = {
    "froid": [
        "Fourniture de chambres froides {dest}",
        "Acquisition de congélateurs et de matériel de réfrigération {dest}",
        "Fourniture et installation d'une chambre froide négative {dest}",
    ],
    "cuisson": [
        "Fourniture de fours mixtes {dest}",
        "Acquisition de matériel de cuisson : fourneaux, marmites et sauteuses {dest}",
        "Fourniture de friteuses et de plaques de cuisson {dest}",
    ],
    "laverie": [
        "Acquisition de lave-vaisselle à capot {dest}",
        "Fourniture d'un tunnel de lavage et de matériel de plonge {dest}",
    ],
    "preparation": [
        "Fourniture de matériel de préparation : trancheuses et pétrins {dest}",
        "Acquisition d'éplucheuses et de coupe-légumes {dest}",
    ],
    "distribution": [
        "Fourniture de bacs gastronorme et de conteneurs isothermes {dest}",
        "Installation d'une ligne self-service {dest}",
    ],
    "mobilier_inox": [
        "Fourniture de mobilier inox : tables et étagères {dest}",
        "Acquisition de matériel inox {dest}",
    ],
    "ventilation": [
        "Installation de hottes aspirantes et extraction des fumées {dest}",
        "Remplacement des hottes de cuisine {dest}",
    ],
    "boissons": [
        "Acquisition d'une machine à café et de percolateurs {dest}",
        "Fourniture de distributeurs de boissons {dest}",
    ],
    "vaisselle": [
        "Fourniture de vaisselle et ustensiles de cuisine {dest}",
        "Acquisition de batterie de cuisine et de couverts {dest}",
    ],
    "cuisine_generale": [
        "Équipement complet de la cuisine centrale {dest}",
        "Acquisition d'équipements de cuisine collective {dest}",
    ],
}
# Montant typique d'un marche par famille, en dirhams.
BASE_AMOUNT = {
    "froid": 1_200_000, "cuisson": 900_000, "laverie": 600_000, "preparation": 350_000,
    "distribution": 700_000, "mobilier_inox": 800_000, "ventilation": 500_000,
    "boissons": 200_000, "vaisselle": 400_000, "cuisine_generale": 2_400_000,
}

# (raisons sociales, marches gagnes, taille, trajectoire, familles ponderees, acheteurs habituels)
# Toutes fictives. La premiere ecriture est la raison sociale de reference ;
# les suivantes sont des variantes, comme le portail en publie.
COMPANIES: list[tuple[list[str], int, float, str, dict[str, int], list[str]]] = [
    (["DEMO Arcade Equipement SARL"], 22, 1.0, "up",
     {"cuisson": 4, "preparation": 3, "froid": 2, "cuisine_generale": 2, "distribution": 1},
     ["CC", "UH2C", "CHUIR", "OFPPT", "UCA"]),
    (["DEMO FROIDEX SARL AU", "Demo-Froidex S.A.R.L."], 26, 1.25, "flat",
     {"froid": 6, "cuisine_generale": 2, "cuisson": 1}, ["MS", "CHUIR", "OFPPT", "CUT"]),
    (["DEMO Atlas Grande Cuisine SA"], 18, 1.6, "flat",
     {"cuisine_generale": 5, "cuisson": 2, "distribution": 2}, ["MEN", "ONOUSC", "DGAPR", "UM5"]),
    (["DEMO Cuisinox SARL"], 17, 0.95, "down",
     {"cuisson": 5, "preparation": 3}, ["UM5", "CUT", "CC", "UCA"]),
    (["DEMO Inoxa Industries"], 14, 1.0, "flat",
     {"mobilier_inox": 5, "distribution": 2}, ["MEN", "DGAPR", "CRTTA"]),
    (["DEMO Lavatec Maroc SARL"], 11, 0.8, "up",
     {"laverie": 6}, ["CRTTA", "CHUIR", "MS", "ONOUSC"]),
    (["DEMO Ventis Extraction"], 9, 0.75, "flat",
     {"ventilation": 6}, ["ONDA", "CC", "OFPPT"]),
    (["DEMO Sahara Froid SARL"], 11, 0.9, "up",
     {"froid": 5, "boissons": 1}, ["CRSM", "UCA", "MS"]),
    (["DEMO Gastronorm Pro"], 8, 0.65, "down",
     {"distribution": 4, "vaisselle": 2}, ["ONOUSC", "MEN"]),
    (["DEMO Chef Equipement Services"], 7, 0.5, "flat",
     {"vaisselle": 3, "boissons": 3}, ["OFPPT", "ONDA", "CUT"]),
    (["DEMO Rif Inox SARL"], 7, 0.6, "up",
     {"mobilier_inox": 3, "preparation": 1}, ["CRTTA", "CUT"]),
]
OWN_COMPANY = COMPANIES[0][0][0]
#: Les concurrents suivis au chargement.
WATCHED = [
    "DEMO FROIDEX SARL AU", "DEMO Atlas Grande Cuisine SA", "DEMO Cuisinox SARL", "DEMO Inoxa Industries",
]


def _days_ago(rng: random.Random, trend: str) -> int:
    """Anciennete d'une victoire : recente pour une entreprise qui monte, ancienne sinon."""
    r = rng.random()
    if trend == "up":
        r = r ** 1.8
    elif trend == "down":
        r = 1 - r ** 1.8
    return 12 + int(1080 * r)


def _amount(rng: random.Random, family: str, size: float) -> float:
    value = BASE_AMOUNT[family] * size * rng.lognormvariate(0, 0.35)
    return float(round(value, -3))


def _url(ref: str) -> str:
    return f"https://www.marchespublics.gov.ma/demo/{ref.replace(' ', '-').replace('/', '-')}"


def _build_awards() -> list[dict]:
    """Les attributions passees, generees une fois pour toutes (graine fixe)."""
    rng = random.Random(2026)
    awards: list[dict] = []
    for names, count, size, trend, families, habitual in COMPANIES:
        fam_keys, fam_weights = zip(*families.items())
        for i in range(count):
            family = rng.choices(fam_keys, fam_weights)[0]
            acr = rng.choice(habitual) if rng.random() < 0.75 else rng.choice(BUYERS)[1]
            buyer, _city, dest = _BUYER[acr]
            when = _TODAY - timedelta(days=_days_ago(rng, trend))
            amount = _amount(rng, family, size)
            awards.append({
                "object": rng.choice(OBJECTS[family]).format(dest=dest),
                "buyer": buyer,
                "acr": acr,
                "supplier": names[i % len(names)] if len(names) > 1 and i % 4 == 3 else names[0],
                "amount": amount,
                "estimate": round(amount * rng.uniform(0.95, 1.2), -3) if rng.random() < 0.7 else None,
                "bids": rng.randint(3, 14),
                "date": when,
                "ttc": rng.random() < 0.5,
                "family": family,
            })
    # Un precedent hors domaine, chez un acheteur present : il ne doit jamais
    # ressortir comme comparable d'un marche de cuisine ni dans le classement.
    awards.append({
        "object": "Acquisition de matériel informatique et serveurs", "buyer": _BUYER["CUT"][0],
        "acr": "CUT", "supplier": "DEMO INFOTEST", "amount": 760_000.0, "estimate": None,
        "bids": 6, "date": _TODAY - timedelta(days=150), "ttc": True, "family": None,
    })
    awards.sort(key=lambda a: a["date"])
    for seq, award in enumerate(awards, start=1):
        award["ref"] = f"DEMO AOO {seq:03d}/{award['date'].year}/{award['acr']}"
    return awards


ATTRIBUTIONS: list[dict] = _build_awards()


def _build_tenders() -> tuple[list[dict], list[tuple]]:
    """Avis en cours, et avis passes sur lesquels Arcade a candidate."""
    rng = random.Random(7)
    tenders: list[dict] = []
    for i in range(22):
        family = rng.choice(list(OBJECTS))
        name, acr, city, dest = rng.choice(BUYERS)
        days = rng.randint(1, 42)
        tenders.append({
            "ref": f"DEMO AOO {200 + i}/{_TODAY.year}/{acr}",
            "object": rng.choice(OBJECTS[family]).format(dest=dest),
            "buyer": name, "city": city, "category": Category.FOURNITURES,
            "amount": round(BASE_AMOUNT[family] * rng.uniform(0.7, 1.5), -3) if rng.random() < 0.85 else None,
            "deadline": _NOW + timedelta(days=days, hours=rng.randint(0, 8)),
            "published": _TODAY - timedelta(days=rng.randint(3, 25)),
        })
    # Hors domaine : doivent etre ecartes de la veille et des rapprochements.
    for i, (obj, acr, amount, cat) in enumerate([
        ("Travaux de construction d'une route provinciale", "CRSM", 42_000_000.0, Category.TRAVAUX),
        ("Acquisition de matériel informatique et de licences logicielles", "CUT", 890_000.0, Category.FOURNITURES),
    ]):
        name, city, _dest = _BUYER[acr]
        tenders.append({
            "ref": f"DEMO AOO {290 + i}/{_TODAY.year}/{acr}", "object": obj, "buyer": name,
            "city": city, "category": cat, "amount": amount,
            "deadline": _NOW + timedelta(days=14 + 4 * i), "published": _TODAY - timedelta(days=10),
        })

    open_refs = [t["ref"] for t in tenders]
    suivis: list[tuple] = [
        (open_refs[0], BidStage.REVIEW, None, None),
        (open_refs[1], BidStage.GO, None, "Visite des lieux prévue la semaine prochaine."),
        (open_refs[2], BidStage.PREPARING, None, "Bordereau en cours de chiffrage avec le fournisseur."),
        (open_refs[3], BidStage.PREPARING, None, "Caution provisoire demandée à la banque."),
        (open_refs[4], BidStage.SUBMITTED, None, "Offre déposée sur le portail."),
        (open_refs[5], BidStage.DROPPED, None, "No-go : délai de livraison incompatible."),
    ]

    # Avis passes : les victoires recentes d'Arcade, et des marches perdus face
    # aux concurrents, sur les familles d'Arcade, avec une offre un peu plus chere.
    own = [a for a in ATTRIBUTIONS if a["supplier"] == OWN_COMPANY]
    arcade_families = set(COMPANIES[0][4])
    lost = [
        a for a in ATTRIBUTIONS
        if a["supplier"] != OWN_COMPANY and a["family"] in arcade_families
        and (_TODAY - a["date"]).days < 540
    ]
    for award, stage in [(a, BidStage.WON) for a in own[-4:]] + [(a, BidStage.LOST) for a in lost[-6:]]:
        deadline = datetime.combine(award["date"] - timedelta(days=25), datetime.min.time(), tzinfo=timezone.utc)
        tenders.append({
            "ref": award["ref"], "object": award["object"], "buyer": award["buyer"],
            "city": _BUYER[award["acr"]][1], "category": Category.FOURNITURES,
            "amount": award["estimate"] or round(award["amount"] * 1.1, -3),
            "deadline": deadline + timedelta(hours=10), "published": (deadline - timedelta(days=30)).date(),
        })
        offered = award["amount"] if stage is BidStage.WON else round(award["amount"] * rng.uniform(1.03, 1.18), -3)
        note = None if stage is BidStage.WON else "Offre jugée plus chère que celle de l'attributaire."
        suivis.append((award["ref"], stage, offered, note))
    return tenders, suivis


AVIS, SUIVIS = _build_tenders()

#: Noms de substitution propres a une machine : {raison sociale fictive: nom affiche}.
#: data/ n'est pas versionne : des noms d'entreprises reelles poses sur des
#: marches inventes ne doivent jamais partir dans le depot public.
NAMES_FILE = BASE_DIR / "data" / "demo_names.json"


def local_names() -> dict[str, str]:
    try:
        names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in names.items() if not str(k).startswith("_")} if isinstance(names, dict) else {}


def _renamer(names: dict[str, str] | None) -> Callable[[str], str]:
    """Renomme une entreprise fictive, variantes comprises ; le marqueur DEMO reste."""
    table: dict[str, str] = {}
    for variants, *_rest in COMPANIES:
        target = (names or {}).get(variants[0])
        if target:
            for variant in variants:
                table[variant] = f"DEMO {target}"
    return lambda name: table.get(name, name)


def load_demo_data(session: Session, names: dict[str, str] | None = None) -> tuple[int, int]:
    """Insere le jeu de demonstration. Renvoie (avis, attributions).

    ``names`` renomme les entreprises fictives (voir NAMES_FILE) ; les montants,
    dates et marches restent ceux du jeu genere.
    """
    rename = _renamer(names)
    profiles = list(
        session.scalars(select(WatchProfile).where(WatchProfile.active.is_(True))).all()
    )

    tenders = 0
    for t in AVIS:
        record = TenderRecord(
            source=DEMO_SOURCE_TENDER,
            source_key=t["ref"],
            reference=t["ref"],
            object=t["object"],
            buyer_raw=t["buyer"],
            execution_place=t["city"],
            category=t["category"],
            procedure_type="Appel d'offres ouvert",
            estimated_amount=t["amount"],
            publication_date=t["published"],
            submission_deadline=t["deadline"],
            consultation_url=_url(t["ref"]),
            # Charge brute d'exemple : la fiche d'un avis montre ainsi la
            # section « Donnees brutes de la source ».
            raw={
                "reference": t["ref"], "object": t["object"], "buyer": t["buyer"],
                "category": t["category"].value,
                "deadline": t["deadline"].strftime("%Y-%m-%d %H:%M"), "_demo": True,
            },
        )
        _, is_new, _ = upsert_tender(session, record, profiles)
        tenders += int(is_new)

    awards = 0
    for a in ATTRIBUTIONS:
        record = AwardRecord(
            source=DEMO_SOURCE_AWARD,
            source_key=f"{a['ref']} | {a['supplier']}",
            reference=a["ref"],
            object=a["object"],
            buyer_raw=a["buyer"],
            supplier_raw=rename(a["supplier"]),
            awarded_amount=a["amount"],
            estimated_amount=a["estimate"],
            bids_count=a["bids"],
            amount_includes_tax=a["ttc"],
            award_date=a["date"],
            category=Category.FOURNITURES,
            procedure_type="Appel d'offres ouvert",
            consultation_url=_url(a["ref"]),
        )
        _, is_new = upsert_award(session, record)
        awards += int(is_new)
    session.flush()

    for ref, stage, offered, note in SUIVIS:
        tender = session.scalar(
            select(Tender).where(Tender.source == DEMO_SOURCE_TENDER, Tender.source_key == ref)
        )
        if tender is not None and tender.bid is None:
            session.add(Bid(tender_id=tender.id, stage=stage, offered_amount=offered, note=note))

    for name in WATCHED:
        supplier = session.scalar(
            select(Supplier).where(Supplier.normalized_name == normalize_entity_name(rename(name)))
        )
        if supplier is not None and not session.scalar(
            select(WatchedSupplier.id).where(WatchedSupplier.supplier_id == supplier.id)
        ):
            session.add(WatchedSupplier(supplier_id=supplier.id))
    session.flush()
    return tenders, awards


def clear_demo_data(session: Session) -> tuple[int, int]:
    """Retire le jeu de demonstration, sans toucher aux donnees reelles.

    L'ordre des suppressions compte. Une suppression en masse ne declenche pas
    les cascades de l'ORM : il faut donc retirer d'abord tout ce qui pointe vers
    les avis de demonstration — suivis, pieces du DCE, journaux de
    notification, attributions rattachees — sinon la contrainte de cle
    etrangere echoue.
    """
    ids = list(
        session.scalars(
            select(Tender.id).where(Tender.source == DEMO_SOURCE_TENDER)
        ).all()
    )

    if ids:
        session.execute(delete(Bid).where(Bid.tender_id.in_(ids)))
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

    # Les attributaires devenus orphelins sont supprimes a leur tour, avec
    # leur suivi eventuel.
    orphans = session.scalars(
        select(Supplier).where(
            ~Supplier.id.in_(select(Award.supplier_id).where(Award.supplier_id.isnot(None)))
        )
    ).all()
    if orphans:
        session.execute(
            delete(WatchedSupplier).where(WatchedSupplier.supplier_id.in_([s.id for s in orphans]))
        )
    for supplier in orphans:
        session.delete(supplier)
    session.flush()
    return tenders, awards


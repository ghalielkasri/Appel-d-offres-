"""Profils de veille livres par defaut, orientes equipement et materiel."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import WatchProfile
from app.taxonomy import all_keywords

EQUIPEMENT_KEYWORDS = [
    # Termes generiques d'achat de biens
    "equipement", "equipements", "materiel", "materiels", "fourniture", "fournitures",
    "acquisition", "achat", "approvisionnement", "livraison",
    # Familles de materiel
    "mobilier", "outillage", "quincaillerie", "machine", "machines", "engin", "engins",
    "groupe electrogene", "climatisation", "chauffage", "froid", "pompe", "pompes",
    "compresseur", "ascenseur", "transformateur", "onduleur", "generateur",
    "materiel informatique", "materiel medical", "materiel technique",
    "materiel de bureau", "materiel roulant", "materiel didactique",
    "equipement sportif", "equipement de laboratoire", "equipement de cuisine",
    "electromenager", "appareillage", "instrumentation", "metrologie",
    # Prestations liees au materiel
    "piece de rechange", "pieces de rechange", "installation", "montage",
    "mise en service", "location de materiel", "maintenance des equipements",
]

EXCLUDED_KEYWORDS = [
    # Prestations intellectuelles ou de main-d'oeuvre, sans fourniture de biens.
    "gardiennage", "nettoyage", "jardinage", "restauration collective",
    "assurance", "audit", "formation", "etude juridique", "transport du personnel",
]

# Exclusions du profil cuisine : volontairement tres peu nombreuses.
# Attention : "restauration collective" ne doit PAS y figurer. Pour un
# fournisseur de cuisine professionnelle, ces marches sont au coeur de la cible,
# alors qu'ils sont a ecarter pour un profil equipement generaliste.
CUISINE_EXCLUSIONS = [
    "gardiennage", "assurance", "audit", "etude juridique",
    "transport du personnel", "formation du personnel",
]

DEFAULT_PROFILES: list[dict] = [
    {
        "name": "Cuisine professionnelle",
        "description": (
            "Avis portant sur l'equipement de cuisine collective et de restauration : "
            "froid, cuisson, laverie, preparation, distribution, mobilier inox, "
            "ventilation, boissons et vaisselle."
        ),
        "keywords": all_keywords(),
        "excluded_keywords": CUISINE_EXCLUSIONS,
        # Les marches de cuisine sortent en Fournitures, en Services (installation
        # et maintenance) et parfois en Travaux (cuisine livree avec le batiment).
        "categories": [],
        "buyers": [],
        "min_score": 3.0,
        "channels": [],
        "active": True,
    },
    {
        "name": "Equipement & materiel",
        "description": (
            "Avis portant sur la fourniture, l'acquisition, l'installation ou la "
            "maintenance d'equipements et de materiels."
        ),
        "keywords": EQUIPEMENT_KEYWORDS,
        "excluded_keywords": EXCLUDED_KEYWORDS,
        "categories": ["Fournitures", "Services"],
        "buyers": [],
        "min_score": 3.0,
        "channels": [],
        # Inactif par defaut : il elargirait la veille bien au-dela de la cuisine.
        # Activez-le avec `radar profile-edit "Equipement & materiel" --active`.
        "active": False,
    },
    {
        "name": "Gros marches equipement (> 1 MDH)",
        "description": (
            "Meme domaine, mais uniquement les avis dont l'estimation depasse "
            "un million de dirhams. Utile pour un canal d'alerte prioritaire."
        ),
        "keywords": EQUIPEMENT_KEYWORDS,
        "excluded_keywords": EXCLUDED_KEYWORDS,
        "categories": [],
        "buyers": [],
        "min_amount": 1_000_000.0,
        "min_score": 3.0,
        "channels": [],
        "active": False,
    },
]


def seed_profiles(session: Session, overwrite: bool = False) -> list[WatchProfile]:
    """Cree les profils par defaut absents. Ne touche pas aux profils modifies."""
    created: list[WatchProfile] = []
    for spec in DEFAULT_PROFILES:
        existing = (
            session.query(WatchProfile).filter(WatchProfile.name == spec["name"]).one_or_none()
        )
        if existing and not overwrite:
            continue
        if existing:
            for key, value in spec.items():
                setattr(existing, key, value)
            created.append(existing)
            continue
        profile = WatchProfile(**spec)
        session.add(profile)
        created.append(profile)
    session.flush()
    return created


def ensure_default_profiles(session: Session) -> list[WatchProfile]:
    """Cree les profils par defaut si, et seulement si, aucun n'existe.

    Appele au demarrage des services. Sans cela, un deploiement neuf cree bien
    les tables mais aucun profil de veille : le planificateur collecterait sans
    jamais rien notifier, et sans erreur apparente.

    La condition « table vide » evite de ressusciter un profil que vous auriez
    volontairement supprime : une fois qu'il existe au moins un profil, cette
    fonction ne touche plus a rien.
    """
    if session.query(WatchProfile).first() is not None:
        return []
    return seed_profiles(session)


def keywords_for_collection(session: Session, limit: int = 12) -> list[str]:
    """Mots-cles envoyes au filtre serveur d'Apify.

    Un run par mot-cle est facture, donc on ne retient que les termes les plus
    larges : ils ramenent l'essentiel du domaine, le filtrage fin etant fait
    ensuite en local par le moteur de scoring.
    """
    # Termes larges, pensees pour le filtre serveur du portail : ils ramenent
    # l'essentiel du domaine en peu de runs, le tri fin etant fait en local.
    broad_terms = [
        "cuisine", "restauration", "chambre froide", "four",
        "laverie", "cantine", "equipement de cuisine", "materiel de cuisine",
    ]
    profiles = session.query(WatchProfile).filter(WatchProfile.active.is_(True)).all()
    active_keywords = {
        keyword.strip().lower()
        for profile in profiles
        for keyword in (profile.keywords or [])
    }
    available = [term for term in broad_terms if term in active_keywords]
    if not available:
        # Aucun profil actif ne reprend ces termes : on interroge quand meme le
        # portail sur le domaine, sinon la collecte ne ramenerait rien.
        available = broad_terms
    return available[:limit]

"""Familles d'equipement de cuisine professionnelle.

Sert deux besoins :

* **Veille** : les mots-cles du profil « Cuisine professionnelle » sont la reunion
  de toutes les familles, ce qui evite d'entretenir deux listes.
* **Similarite** : deux marches qui partagent une famille portent sur le meme
  type d'equipement. C'est le signal qui permet de repondre a « lesquels sont
  similaires par type d'equipement, et qui les a gagnes ».

Les termes sont ecrits sans accent : la comparaison passe par ``normalize_text``.
"""

from __future__ import annotations

import re

from app.normalize import normalize_text

#: Famille -> (libelle affiche, termes qui la designent)
EQUIPMENT_FAMILIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "froid": (
        "Froid & conservation",
        (
            "chambre froide", "chambres froides", "armoire froide", "armoire refrigeree",
            "armoire negative", "armoire positive", "refrigeration", "refrigerateur",
            "congelateur", "congelation", "surgelation", "vitrine refrigeree",
            "table refrigeree", "cellule de refroidissement", "chaine de froid",
            "groupe frigorifique", "machine a glace", "machine a glacons",
            "conservateur", "frigorifique",
        ),
    ),
    "cuisson": (
        "Cuisson",
        (
            "four", "fours", "four mixte", "four a convection", "four combine",
            "fourneau", "fourneaux", "piano de cuisson", "plaque de cuisson",
            "rechaud", "grill", "grillade", "salamandre", "rotissoire",
            "friteuse", "friteuses", "sauteuse", "marmite", "marmites",
            "bain marie", "bain-marie", "braisiere", "cuiseur", "cuiseur a riz",
            "cuiseur vapeur", "bruleur", "crepiere", "gaufrier", "plancha",
        ),
    ),
    "laverie": (
        "Laverie & plonge",
        (
            "laverie", "plonge", "lave vaisselle", "lave-vaisselle",
            "lave batterie", "lave-batterie", "lave verres", "machine a laver la vaisselle",
            "tunnel de lavage", "evier", "eviers", "bac de lavage", "douchette",
            "adoucisseur",
        ),
    ),
    "preparation": (
        "Préparation",
        (
            "trancheuse", "hachoir", "petrin", "petrins", "batteur", "batteur melangeur",
            "melangeur", "mixeur", "blender", "robot coupe", "robot de cuisine",
            "eplucheuse", "eplucheuse a pommes de terre", "coupe legumes",
            "coupe-legumes", "essoreuse", "cutter", "laminoir", "faconneuse",
            "balance de cuisine", "trancheur",
        ),
    ),
    "distribution": (
        "Distribution & service",
        (
            "self service", "self-service", "libre service", "banque de distribution",
            "chariot chauffant", "chariot de service", "chariot bain marie",
            "buffet", "presentoir chauffant", "vitrine chauffante", "rampe de distribution",
            "plateau repas", "chariot a plateaux", "bac gastronorme", "gastronorme",
            "conteneur isotherme", "caisson isotherme",
        ),
    ),
    "mobilier_inox": (
        "Mobilier inox",
        (
            "mobilier inox", "materiel inox", "equipement inox",
            "table de travail inox", "table inox", "etagere inox", "etageres inox",
            "plan de travail inox", "acier inoxydable", "mobilier de cuisine",
            "placard inox", "desserte inox", "billot",
        ),
    ),
    "ventilation": (
        "Ventilation & extraction",
        (
            "hotte", "hottes", "hotte aspirante", "extraction des fumees",
            "ventilation de cuisine", "caisson d extraction", "filtre a graisse",
            "conduit d extraction",
        ),
    ),
    "boissons": (
        "Boissons",
        (
            "machine a cafe", "percolateur", "distributeur de boissons",
            "fontaine a eau", "refroidisseur d eau", "presse agrumes",
            "distributeur automatique",
        ),
    ),
    "vaisselle": (
        "Vaisselle & ustensiles",
        (
            "vaisselle", "couverts", "ustensiles de cuisine", "batterie de cuisine",
            "casserole", "casseroles", "marmite inox", "plat de service",
            "assiettes", "verrerie", "coutellerie",
        ),
    ),
}

#: Contextes qui disqualifient un terme. "lave-vaisselle" designe la laverie,
#: pas la vaisselle : le tiret faisant frontiere de mot, il faut l'ecarter
#: explicitement avant de chercher le terme simple.
NEGATIVE_CONTEXT: dict[str, tuple[str, ...]] = {
    "vaisselle": ("lave vaisselle", "lave-vaisselle", "laverie", "lavage de la vaisselle"),
    "four": ("four a chaux", "four a pain industriel"),
    "balance de cuisine": (),
}

#: Termes generiques du domaine : ils qualifient l'avis sans designer une famille.
GENERIC_TERMS: tuple[str, ...] = (
    "cuisine", "cuisines", "cuisine professionnelle", "cuisine collective",
    "cuisine centrale", "materiel de cuisine", "equipement de cuisine",
    "equipements de cuisine", "restauration", "restauration collective",
    "cantine", "cantines", "refectoire", "economat", "office",
    "materiel de restauration", "equipement de restauration",
    "cuisiniste", "collectivite",
)


#: Famille attribuee a un marche de cuisine dont l'objet ne designe aucune
#: famille precise ("equipement de cuisine", "cuisine centrale"). Sans elle, ces
#: marches — souvent les plus gros, car ils equipent une cuisine entiere — ne
#: pourraient se rapprocher d'aucun autre par le type d'equipement.
GENERAL_FAMILY = "cuisine_generale"
GENERAL_FAMILY_LABEL = "Cuisine (ensemble)"


def all_keywords() -> list[str]:
    """Tous les termes du domaine : generiques puis familles, sans doublon."""
    seen: dict[str, None] = {}
    for term in GENERIC_TERMS:
        seen.setdefault(term, None)
    for _label, terms in EQUIPMENT_FAMILIES.values():
        for term in terms:
            seen.setdefault(term, None)
    return list(seen)


def family_label(family: str) -> str:
    if family == GENERAL_FAMILY:
        return GENERAL_FAMILY_LABEL
    entry = EQUIPMENT_FAMILIES.get(family)
    return entry[0] if entry else family


def detect_families(*texts: str | None) -> list[str]:
    """Familles d'equipement reconnues dans un texte.

    Un avis « Acquisition de fours et de chambres froides » renvoie
    ``["cuisson", "froid"]``.
    """
    haystack = normalize_text(" ".join(t for t in texts if t))
    if not haystack:
        return []
    found: list[str] = []
    for family, (_label, terms) in EQUIPMENT_FAMILIES.items():
        for term in terms:
            # Les contextes disqualifiants sont retires avant la recherche.
            target = haystack
            for phrase in NEGATIVE_CONTEXT.get(term, ()):
                target = target.replace(phrase, " ")
            # Les termes d'un seul mot exigent une frontiere de mot, pour que
            # "four" ne corresponde pas a "fourniture" ni a "fourgon".
            if " " in term:
                if term in target:
                    found.append(family)
                    break
            elif re.search(r"\b" + re.escape(term) + r"(s|x)?\b", target):
                found.append(family)
                break
    if not found and _matches_generic(haystack):
        return [GENERAL_FAMILY]
    return found


def _matches_generic(haystack: str) -> bool:
    return any(term in haystack for term in GENERIC_TERMS)


def is_kitchen_related(*texts: str | None) -> bool:
    """Vrai si le texte releve de la cuisine professionnelle."""
    haystack = normalize_text(" ".join(t for t in texts if t))
    if not haystack:
        return False
    if detect_families(*texts):
        return True
    return _matches_generic(haystack)

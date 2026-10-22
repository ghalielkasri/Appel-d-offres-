"""Familles d'equipement de cuisine professionnelle."""

import pytest

from app.taxonomy import (
    GENERAL_FAMILY,
    all_keywords,
    detect_families,
    family_label,
    is_kitchen_related,
)


@pytest.mark.parametrize(
    "texte,attendues",
    [
        ("Acquisition de fours mixtes", {"cuisson"}),
        ("Fourniture de chambres froides", {"froid"}),
        ("Acquisition de lave-vaisselle et tunnel de lavage", {"laverie"}),
        ("Fourniture de vaisselle et de couverts", {"vaisselle"}),
        ("Tables de travail inox et étagères inox", {"mobilier_inox"}),
        ("Installation de hottes aspirantes", {"ventilation"}),
        ("Trancheuses, pétrins et robots coupe", {"preparation"}),
        ("Chariots chauffants et banque de distribution", {"distribution"}),
        ("Machine à café et percolateur", {"boissons"}),
        ("Fours et chambres froides", {"cuisson", "froid"}),
    ],
)
def test_detect_families(texte, attendues):
    assert set(detect_families(texte)) == attendues


@pytest.mark.parametrize(
    "texte",
    [
        "Fourniture de fournitures de bureau",   # "four" dans "fourniture"
        "Acquisition de fourgons utilitaires",   # "four" dans "fourgon"
        "Travaux de réhabilitation d'un four à chaux",  # contexte disqualifiant
        "Acquisition de matériel informatique",
        "Travaux de construction d'une école",
        "Prestation de gardiennage",
    ],
)
def test_no_false_positive(texte):
    assert detect_families(texte) == []
    assert is_kitchen_related(texte) is False


def test_lave_vaisselle_is_laverie_not_vaisselle():
    """Le tiret fait frontiere de mot : sans garde-fou, "vaisselle" matcherait."""
    assert set(detect_families("Acquisition d'un lave-vaisselle")) == {"laverie"}


def test_both_families_when_both_present():
    familles = set(detect_families("Fourniture d'un lave-vaisselle et de vaisselle"))
    assert familles == {"laverie", "vaisselle"}


@pytest.mark.parametrize(
    "texte",
    [
        "Acquisition d'équipements de cuisine pour la cuisine centrale",
        "Équipement de la cantine scolaire",
        "Matériel de restauration collective",
    ],
)
def test_generic_kitchen_gets_general_family(texte):
    """Un marche de cuisine sans famille precise doit rester rapprochable."""
    assert detect_families(texte) == [GENERAL_FAMILY]
    assert is_kitchen_related(texte) is True


def test_general_family_has_a_label():
    assert family_label(GENERAL_FAMILY) == "Cuisine (ensemble)"
    assert family_label("froid") == "Froid & conservation"


def test_keywords_cover_the_domain():
    mots = all_keywords()
    assert len(mots) == len(set(mots)), "aucun doublon attendu"
    for attendu in ("cuisine", "restauration collective", "chambre froide", "lave-vaisselle"):
        assert attendu in mots

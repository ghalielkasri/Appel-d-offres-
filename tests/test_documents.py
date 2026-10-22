"""Pieces du DCE : classement, extraction des references, ecriture sur disque."""

import pytest

from app.documents import classify_document, extract_document_refs, safe_filename
from app.models import DocumentKind, TenderDocument


@pytest.mark.parametrize(
    "nom,attendu",
    [
        ("CPS.pdf", DocumentKind.CPS),
        ("cps_AOO_12_2026.pdf", DocumentKind.CPS),
        ("Cahier des prescriptions speciales.pdf", DocumentKind.CPS),
        ("Bordereau des prix - BPU.xlsx", DocumentKind.BORDEREAU),
        ("BPU.xls", DocumentKind.BORDEREAU),
        ("DQE.xlsx", DocumentKind.BORDEREAU),
        ("detail estimatif.pdf", DocumentKind.BORDEREAU),
        ("Reglement de consultation.pdf", DocumentKind.REGLEMENT),
        ("RC.pdf", DocumentKind.REGLEMENT),
        ("CPT - specifications techniques.pdf", DocumentKind.CPT),
        ("plans_cuisine.dwg", DocumentKind.PLAN),
        ("annexe3.zip", DocumentKind.AUTRE),
    ],
)
def test_classify_document(nom, attendu):
    assert classify_document(nom) is attendu


def test_short_acronyms_need_a_whole_word():
    """"rc" ne doit pas etre reconnu a l'interieur d'un autre mot."""
    assert classify_document("marche_de_travaux.pdf") is DocumentKind.AUTRE


def test_classify_falls_back_on_url():
    assert classify_document(None, "https://p.ma/dce/8842/cps_signe.pdf") is DocumentKind.CPS


@pytest.mark.parametrize(
    "charge,nombre",
    [
        ({"dce_documents": ["https://p.ma/cps.pdf", "https://p.ma/bpu.xlsx"]}, 2),
        ({"dce_documents": [{"name": "CPS.pdf", "url": "/dce/cps.pdf"}]}, 1),
        ({"documents": {"title": "RC", "downloadUrl": "https://p.ma/rc.pdf"}}, 1),
        ({"dce_documents": []}, 0),
        ({}, 0),
        (None, 0),
    ],
)
def test_extract_document_refs_accepts_every_shape(charge, nombre):
    """Le champ dce_documents n'a pas de structure documentee : on accepte tout."""
    assert len(extract_document_refs(charge, "https://p.ma/")) == nombre


def test_relative_urls_are_made_absolute():
    refs = extract_document_refs(
        {"dce_documents": [{"name": "CPS.pdf", "url": "/dce/8842/cps.pdf"}]},
        "https://www.marchespublics.gov.ma/",
    )
    assert refs[0]["url"].startswith("https://www.marchespublics.gov.ma/")


def test_duplicate_references_are_dropped():
    refs = extract_document_refs(
        {"dce_documents": ["https://p.ma/cps.pdf", "https://p.ma/cps.pdf"]}
    )
    assert len(refs) == 1


@pytest.mark.parametrize(
    "nom",
    ["../../etc/passwd", "..\\\\windows\\\\system32", "/absolute/path.pdf", "nom avec espaces.xlsx"],
)
def test_safe_filename_cannot_escape_the_directory(nom):
    document = TenderDocument(id=7, kind=DocumentKind.CPS, name=nom, url="https://p.ma/f.pdf")
    resultat = safe_filename(document)
    assert "/" not in resultat and "\\\\" not in resultat
    assert ".." not in resultat


def test_safe_filename_always_has_an_extension():
    document = TenderDocument(id=1, kind=DocumentKind.CPS, name="sans_extension", url="https://p.ma/x")
    assert safe_filename(document).endswith(".pdf")

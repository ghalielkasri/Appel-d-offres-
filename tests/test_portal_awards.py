"""Parseur des resultats d'attribution du portail."""

from app.sources.portal_awards import (
    find_result_links,
    normalize_label,
    parse_awards_html,
)

BASE = "https://www.marchespublics.gov.ma/results"


def test_table_layout(fixture_html):
    awards = parse_awards_html(fixture_html("resultats_tableau.html"), BASE, "portal")
    assert len(awards) == 2, "la ligne infructueuse doit etre ecartee"

    first = next(a for a in awards if a.reference == "AOO 12/2026/CT")
    assert first.supplier_raw == "STE EQUIP MAROC SARL AU"
    assert first.awarded_amount == 208576.0
    assert first.amount_includes_tax is True
    assert first.bids_count == 22
    assert first.buyer_raw == "Commune de Azla"
    # Le lien de la ligne est resolu en URL absolue.
    assert first.consultation_url.startswith("https://www.marchespublics.gov.ma/")


def test_navigation_table_is_ignored(fixture_html):
    """Un tableau sans colonne attributaire ni montant n'est pas un resultat."""
    awards = parse_awards_html(fixture_html("resultats_tableau.html"), BASE, "portal")
    assert all(a.reference != "Consultations" for a in awards)


def test_card_layout(fixture_html):
    awards = parse_awards_html(fixture_html("resultats_fiches.html"), BASE, "portal")
    assert len(awards) == 2

    first = next(a for a in awards if a.reference == "BC 331/2026")
    assert first.supplier_raw == "SOCIETE ARCADE EQUIPEMENT"
    assert first.awarded_amount == 745200.5
    assert first.amount_includes_tax is False
    assert first.bids_count == 5
    # Le libelle "Date d'attribution" porte une apostrophe : il doit etre reconnu.
    assert first.award_date is not None


def test_labels_with_apostrophes_are_recognised():
    assert normalize_label("Montant de l'offre") == "montant de l offre"
    assert normalize_label("Date d'attribution") == "date d attribution"


def test_source_keys_are_unique(fixture_html):
    awards = parse_awards_html(fixture_html("resultats_tableau.html"), BASE, "portal")
    keys = [a.source_key for a in awards]
    assert len(keys) == len(set(keys))


def test_empty_page_yields_nothing():
    assert parse_awards_html("<html><body><p>rien</p></body></html>", BASE, "portal") == []


def test_find_result_links():
    html = (
        '<a href="/index.php?page=cons">Consultations</a>'
        '<a href="/index.php?page=res">Resultats des appels d\'offres</a>'
        '<a href="/aide">Aide</a>'
    )
    links = find_result_links(html, "https://www.marchespublics.gov.ma")
    assert len(links) == 1
    assert links[0]["url"].endswith("page=res")

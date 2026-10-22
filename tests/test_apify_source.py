"""Mapping des deux schemas d'actors Apify."""

from datetime import date

from app.config import Settings
from app.models import Category
from app.sources.apify_tenders import ApifyTenderSource, map_award, map_tender

PUBLICMONEY = {
    "reference": "RFP-2026-0184",
    "publicationDate": "2026-09-04",
    "deadline": "2026-10-15 10:00",
    "buyer": "Commune Urbaine de Tanger",
    "consultationUrl": "https://www.marchespublics.gov.ma/notice/8842",
    "procedureType": "Appel d'offres ouvert",
    "category": "Fournitures",
    "object": "Acquisition de materiel technique",
}

JUNGLE = {
    "consultation_ref": "AOO/42/2026",
    "organisation_acronym": "MIN-SANTE",
    "object": "Fourniture d'equipements medicaux",
    "category": "Fournitures",
    "buyer_entity": "Ministere de la Sante",
    "buyer_service": "Direction des equipements",
    "estimated_amount": 4500000,
    "caution_provisoire": "45 000,00",
    "awarded_amount": "4 125 300,00",
    "awarded_supplier": "STE EQUIP MAROC SARL AU",
    "bids_count": "7 offres",
    "award_date": "12/10/2026",
    "submission_deadline": "01/09/2026 09:00",
    "lots": [{"n": 1}, {"n": 2}],
    "consultation_url": "https://x/ao/42",
}


def test_maps_camel_case_schema():
    record = map_tender(PUBLICMONEY, "apify:pm")
    assert record is not None
    assert record.reference == "RFP-2026-0184"
    assert record.category is Category.FOURNITURES
    assert record.publication_date == date(2026, 9, 4)
    assert record.buyer_raw == "Commune Urbaine de Tanger"


def test_maps_snake_case_schema():
    record = map_tender(JUNGLE, "apify:js")
    assert record is not None
    assert record.reference == "AOO/42/2026"
    assert record.estimated_amount == 4500000.0
    assert record.caution_provisoire == 45000.0
    assert record.lots_count == 2
    assert record.buyer_service == "Direction des equipements"


def test_record_without_identifier_is_dropped():
    """Sans reference ni URL, aucune deduplication n'est possible."""
    assert map_tender({"object": "sans identifiant"}, "apify:x") is None


def test_award_extracted_when_fields_present():
    award = map_award(JUNGLE, "apify:js")
    assert award is not None
    assert award.supplier_raw == "STE EQUIP MAROC SARL AU"
    assert award.awarded_amount == 4125300.0
    assert award.bids_count == 7
    assert award.award_date == date(2026, 10, 12)


def test_no_award_when_fields_absent():
    assert map_award(PUBLICMONEY, "apify:pm") is None


def test_keyword_sent_only_to_actor_that_supports_it():
    settings = Settings(apify_token="x")
    pm = ApifyTenderSource(settings, actor_id="publicmoney~marchespublics-morocco-scraper")
    js = ApifyTenderSource(settings, actor_id="jungle_synthesizer~marchespublics-morocco-public-tenders-scraper")
    assert pm.build_input("materiel")["keyword"] == "materiel"
    assert "keyword" not in js.build_input("materiel")


def test_unknown_fields_do_not_break_mapping():
    """Un actor qui ajoute des champs ne doit pas casser la collecte."""
    payload = dict(PUBLICMONEY, champNouveau="valeur", autre={"imbrique": 1})
    assert map_tender(payload, "apify:pm") is not None

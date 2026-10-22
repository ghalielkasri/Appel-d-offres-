"""Normalisation des dates, montants et raisons sociales."""

from datetime import date

import pytest

from app.normalize import (
    content_hash,
    detect_tax_inclusion,
    normalize_entity_name,
    parse_amount,
    parse_date,
    parse_datetime,
    parse_int,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("208 576,00 DH TTC", 208576.0),
        ("1.234.567,89", 1234567.89),
        ("1,234,567.89", 1234567.89),
        ("450000", 450000.0),
        ("12 000 000,00 MAD", 12000000.0),
        ("3.500", 3500.0),       # point separateur de milliers
        ("99.50", 99.5),         # point decimal
        (450000.5, 450000.5),
        ("", None),
        ("neant", None),
        (None, None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-04", date(2026, 9, 4)),
        ("04/09/2026", date(2026, 9, 4)),
        ("04-09-2026 10:30", date(2026, 9, 4)),
        ("Le 04/09/2026 a 09:00", date(2026, 9, 4)),
        ("", None),
        ("n/a", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_parse_datetime_is_timezone_aware():
    parsed = parse_datetime("2026-09-04 10:30")
    assert parsed is not None and parsed.tzinfo is not None


@pytest.mark.parametrize(
    "variants",
    [
        ("STE EQUIP MAROC SARL AU", "Equip-Maroc S.A.R.L.", "SOCIETE EQUIP MAROC"),
        ("ARCADE EQUIPEMENT SARL", "Arcade Equipement", "Ste Arcade Equipement SA"),
    ],
)
def test_entity_name_groups_variants(variants):
    """Les variantes d'une meme raison sociale doivent donner une seule cle."""
    keys = {normalize_entity_name(v) for v in variants}
    assert len(keys) == 1, keys


def test_entity_name_keeps_distinct_companies_apart():
    assert normalize_entity_name("EQUIP MAROC SARL") != normalize_entity_name("FRIGO SERVICE SARL")


def test_entity_name_never_empties_a_name():
    """Un nom reduit a une forme juridique doit rester identifiable."""
    assert normalize_entity_name("SARL") != ""


def test_detect_tax_inclusion():
    assert detect_tax_inclusion("208 576,00 DH TTC") is True
    assert detect_tax_inclusion("45 000 DH HT") is False
    assert detect_tax_inclusion("45 000 DH") is None


def test_parse_int_from_text():
    assert parse_int("22 devis recus") == 22
    assert parse_int(None) is None


def test_content_hash_is_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})

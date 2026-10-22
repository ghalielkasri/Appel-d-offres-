"""Normalisation des donnees brutes du portail : textes, dates, montants, raisons sociales."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, time, timezone

# Formes juridiques et prefixes a retirer pour regrouper les variantes d'un meme nom.
_LEGAL_FORMS = (
    "sarl au", "sarl-au", "sarl a.u", "sarlau", "sarl", "s.a.r.l", "s a r l",
    "ste", "societe", "sté", "snc", "scs", "sca", "s.a", "sa", "eurl",
    "groupement", "gie", "cooperative", "ets", "etablissements", "entreprise",
)

_AMOUNT_RE = re.compile(r"-?\d[\d\s .,]*")
_TAX_INCL_RE = re.compile(r"\bt\.?t\.?c\.?\b", re.IGNORECASE)
_TAX_EXCL_RE = re.compile(r"\bh\.?t\.?\b", re.IGNORECASE)

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%d/%m/%y",
)
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y à %H:%M",
    "%d-%m-%Y %H:%M",
)


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(value: str | None) -> str:
    """Minuscules, sans accents, espaces normalises : la forme utilisee pour comparer."""
    if not value:
        return ""
    cleaned = strip_accents(str(value)).lower()
    cleaned = re.sub(r"[ \s]+", " ", cleaned)
    return cleaned.strip()


def normalize_entity_name(value: str | None) -> str:
    """Cle de regroupement d'un acheteur ou d'un attributaire.

    "STE EQUIP MAROC SARL AU" et "Equip-Maroc S.A.R.L." donnent la meme cle.
    """
    text = normalize_text(value)
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens = text.split(" ")
    # Retire les formes juridiques, qu'elles soient en tete ou en fin de nom.
    for form in sorted(_LEGAL_FORMS, key=len, reverse=True):
        parts = form.split(" ")
        n = len(parts)
        while len(tokens) > n and tokens[:n] == parts:
            tokens = tokens[n:]
        while len(tokens) > n and tokens[-n:] == parts:
            tokens = tokens[:-n]
    result = " ".join(tokens).strip()
    return result or text


def clean_whitespace(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[ \s]+", " ", str(value)).strip()
    return cleaned or None


def parse_amount(value: object) -> float | None:
    """Lit "1 234 567,89 DH", "1.234.567,89", "208576.00" ou un nombre deja type."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(" ", " ").strip()
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
    if "," in number and "." in number:
        # Le dernier separateur rencontre est le separateur decimal.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(",", ".")
    elif number.count(".") > 1:
        number = number.replace(".", "")
    elif "." in number:
        # Un point suivi de 3 chiffres sans decimale est un separateur de milliers.
        head, _, tail = number.rpartition(".")
        if len(tail) == 3 and head:
            number = head.replace(".", "") + tail
    try:
        return float(number)
    except ValueError:
        return None


def detect_tax_inclusion(value: str | None) -> bool | None:
    """True si le montant est TTC, False si HT, None si non precise."""
    if not value:
        return None
    if _TAX_INCL_RE.search(value):
        return True
    if _TAX_EXCL_RE.search(value):
        return False
    return None


def parse_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_whitespace(str(value))
    if not text:
        return None
    parsed = parse_datetime(text)
    if parsed:
        return parsed.date()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    match = re.search(r"(\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4})", text)
    if match and match.group(1) != text:
        return parse_date(match.group(1))
    return None


def parse_datetime(value: object) -> datetime | None:
    """Renvoie un datetime conscient du fuseau (UTC si non precise)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    text = clean_whitespace(str(value))
    if not text:
        return None
    iso_candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    only_date = None
    for fmt in _DATE_FORMATS:
        try:
            only_date = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if only_date:
        return only_date.replace(tzinfo=timezone.utc)
    # Dernier recours : une date suivie d'une heure dans un texte plus long.
    match = re.search(r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})\D+(\d{1,2})[:h](\d{2})", text)
    if match:
        day, hour, minute = match.groups()
        base = parse_date(day)
        if base:
            return datetime.combine(
                base, time(int(hour), int(minute)), tzinfo=timezone.utc
            )
    return None


def parse_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def content_hash(payload: object) -> str:
    """Empreinte stable d'un enregistrement, pour detecter les vraies modifications."""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

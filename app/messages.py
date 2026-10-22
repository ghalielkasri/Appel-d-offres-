"""Mise en forme des alertes : email HTML, texte brut et message WhatsApp court."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from app.models import Tender

MAD = "DH"


def format_amount(amount: float | None, currency: str = "MAD") -> str:
    if amount is None:
        return "non precise"
    symbol = MAD if currency in ("MAD", "", None) else currency
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + f" {symbol}"


def format_date(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y a %Hh%M")
    return value.strftime("%d/%m/%Y") if hasattr(value, "strftime") else str(value)


def days_left(tender: Tender) -> int | None:
    if not tender.submission_deadline:
        return None
    deadline = tender.submission_deadline
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    delta = deadline - datetime.now(timezone.utc)
    return delta.days


def subject_for(tenders: list[Tender]) -> str:
    if len(tenders) == 1:
        obj = (tenders[0].object or tenders[0].reference or "nouvel avis")[:80]
        return f"[Appel d'offres] {obj}"
    return f"[Appels d'offres] {len(tenders)} nouveaux avis dans votre domaine"


def render_text(tenders: list[Tender], base_url: str = "") -> str:
    lines = [
        f"{len(tenders)} nouvel(s) avis correspondant a votre veille.",
        "",
    ]
    for tender in tenders:
        remaining = days_left(tender)
        deadline = format_date(tender.submission_deadline)
        if remaining is not None:
            deadline += f" ({remaining} jour(s) restant(s))"
        lines += [
            f"* {tender.object or 'Objet non precise'}",
            f"  Reference    : {tender.reference or '-'}",
            f"  Acheteur     : {tender.buyer_raw or '-'}",
            f"  Categorie    : {tender.category.value} / {tender.procedure_type or '-'}",
            f"  Estimation   : {format_amount(tender.estimated_amount, tender.currency)}",
            f"  Date limite  : {deadline}",
            f"  Pertinence   : {tender.match_score:.0f}"
            + (f" ({', '.join(tender.matched_keywords[:6])})" if tender.matched_keywords else ""),
            f"  Avis         : {tender.consultation_url or '-'}",
            "",
        ]
    if base_url:
        lines.append(f"Tableau de bord : {base_url}")
    return "\n".join(lines)


def render_html(tenders: list[Tender], base_url: str = "") -> str:
    cards = []
    for tender in tenders:
        remaining = days_left(tender)
        urgency = ""
        if remaining is not None and remaining <= 5:
            urgency = (
                f'<span style="background:#fee2e2;color:#991b1b;padding:2px 8px;'
                f'border-radius:10px;font-size:12px;font-weight:600;">'
                f"J-{max(remaining, 0)}</span>"
            )
        rows = [
            ("Reference", tender.reference or "-"),
            ("Acheteur", tender.buyer_raw or "-"),
            ("Categorie", f"{tender.category.value} / {tender.procedure_type or '-'}"),
            ("Estimation", format_amount(tender.estimated_amount, tender.currency)),
            ("Date limite", format_date(tender.submission_deadline)),
            ("Lieu", tender.execution_place or "-"),
        ]
        body = "".join(
            f'<tr><td style="padding:3px 12px 3px 0;color:#64748b;font-size:13px;'
            f'white-space:nowrap;">{escape(label)}</td>'
            f'<td style="padding:3px 0;font-size:13px;color:#0f172a;">{escape(str(value))}</td></tr>'
            for label, value in rows
        )
        keywords = "".join(
            f'<span style="background:#e0e7ff;color:#3730a3;padding:2px 7px;'
            f'border-radius:10px;font-size:11px;margin-right:4px;">{escape(k)}</span>'
            for k in (tender.matched_keywords or [])[:6]
        )
        link = ""
        if tender.consultation_url:
            link = (
                f'<a href="{escape(tender.consultation_url)}" '
                f'style="display:inline-block;margin-top:10px;background:#1d4ed8;color:#fff;'
                f'padding:8px 14px;border-radius:6px;text-decoration:none;font-size:13px;'
                f'font-weight:600;">Ouvrir l\'avis</a>'
            )
        cards.append(
            f'<div style="border:1px solid #e2e8f0;border-radius:10px;padding:16px;'
            f'margin-bottom:14px;background:#fff;">'
            f'<div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:10px;">'
            f"{escape(tender.object or 'Objet non precise')} {urgency}</div>"
            f"<table style=\"border-collapse:collapse;\">{body}</table>"
            f'<div style="margin-top:10px;">{keywords}</div>{link}</div>'
        )

    dashboard = (
        f'<p style="font-size:13px;color:#64748b;">Tableau de bord complet et '
        f'historique des attributions : <a href="{escape(base_url)}">{escape(base_url)}</a></p>'
        if base_url
        else ""
    )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'background:#f1f5f9;padding:22px;">'
        f'<h2 style="margin:0 0 4px;font-size:19px;color:#0f172a;">'
        f"{len(tenders)} nouvel(s) avis dans votre domaine</h2>"
        f'<p style="margin:0 0 18px;color:#64748b;font-size:13px;">'
        f"Radar des marches publics - {datetime.now().strftime('%d/%m/%Y a %Hh%M')}</p>"
        f"{''.join(cards)}{dashboard}</div>"
    )


def render_whatsapp(tenders: list[Tender], base_url: str = "") -> str:
    """Message court : WhatsApp coupe les textes longs, on vise l'essentiel."""
    if len(tenders) == 1:
        tender = tenders[0]
        remaining = days_left(tender)
        urgency = f" (J-{max(remaining, 0)})" if remaining is not None else ""
        parts = [
            "*Nouvel appel d'offres*",
            f"{tender.object or 'Objet non precise'}",
            f"Acheteur : {tender.buyer_raw or '-'}",
            f"Ref : {tender.reference or '-'}",
            f"Estimation : {format_amount(tender.estimated_amount, tender.currency)}",
            f"Limite : {format_date(tender.submission_deadline)}{urgency}",
        ]
        if tender.consultation_url:
            parts.append(tender.consultation_url)
        return "\n".join(parts)

    lines = [f"*{len(tenders)} nouveaux appels d'offres*", ""]
    for tender in tenders[:8]:
        remaining = days_left(tender)
        urgency = f" J-{max(remaining, 0)}" if remaining is not None else ""
        lines.append(
            f"- {(tender.object or '-')[:70]}\n  {tender.buyer_raw or '-'}"
            f" | {format_amount(tender.estimated_amount, tender.currency)}{urgency}"
        )
    if len(tenders) > 8:
        lines.append(f"...et {len(tenders) - 8} autre(s).")
    if base_url:
        lines += ["", base_url]
    return "\n".join(lines)


def tender_to_dict(tender: Tender) -> dict:
    """Charge utile du webhook : tous les champs utiles, serialisables en JSON."""
    return {
        "id": tender.id,
        "source": tender.source,
        "reference": tender.reference,
        "object": tender.object,
        "category": tender.category.value,
        "procedure_type": tender.procedure_type,
        "buyer": tender.buyer_raw,
        "buyer_service": tender.buyer_service,
        "execution_place": tender.execution_place,
        "publication_date": tender.publication_date.isoformat() if tender.publication_date else None,
        "submission_deadline": (
            tender.submission_deadline.isoformat() if tender.submission_deadline else None
        ),
        "days_left": days_left(tender),
        "estimated_amount": tender.estimated_amount,
        "currency": tender.currency,
        "status": tender.status.value,
        "match_score": tender.match_score,
        "matched_keywords": tender.matched_keywords,
        "consultation_url": tender.consultation_url,
    }

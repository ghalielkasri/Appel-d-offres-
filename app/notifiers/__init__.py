"""Fabrique des canaux de notification actifs."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.notifiers.base import DeliveryResult, Notifier
from app.notifiers.email_notifier import EmailNotifier
from app.notifiers.webhook_notifier import WebhookNotifier
from app.notifiers.whatsapp_notifier import WhatsAppNotifier

__all__ = [
    "DeliveryResult",
    "EmailNotifier",
    "Notifier",
    "WebhookNotifier",
    "WhatsAppNotifier",
    "all_notifiers",
    "enabled_notifiers",
]


def all_notifiers(settings: Settings | None = None) -> list[Notifier]:
    settings = settings or get_settings()
    return [
        EmailNotifier(settings),
        WhatsAppNotifier(settings),
        WebhookNotifier(settings),
    ]


def enabled_notifiers(
    settings: Settings | None = None, channels: list[str] | None = None
) -> list[Notifier]:
    """Canaux configures, eventuellement restreints a ceux d'un profil de veille."""
    notifiers = [n for n in all_notifiers(settings) if n.enabled]
    if channels:
        wanted = {c.strip().lower() for c in channels}
        notifiers = [n for n in notifiers if n.name in wanted]
    return notifiers

"""Webhook JSON generique : Slack, Teams, n8n, Make, ou votre propre endpoint.

C'est le canal a utiliser pour tout systeme non couvert par email et WhatsApp :
le payload contient l'integralite des champs de chaque avis.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings, get_settings
from app.messages import render_text, subject_for, tender_to_dict
from app.models import Tender
from app.notifiers.base import DeliveryResult, Notifier

logger = logging.getLogger(__name__)


class WebhookNotifier(Notifier):
    name = "webhook"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.webhook_enabled

    @property
    def targets(self) -> list[str]:
        return [self.settings.webhook_url] if self.enabled else []

    def send(self, tenders: list[Tender]) -> list[DeliveryResult]:
        if not self.enabled:
            return []
        payload = {
            "event": "new_tenders" if tenders else "test",
            "count": len(tenders),
            # "text" permet a Slack et Teams d'afficher le message tel quel.
            "text": render_text(tenders, self.settings.base_url)
            if tenders
            else "Radar marches publics : test de configuration du webhook.",
            "subject": subject_for(tenders) if tenders else "Test de configuration",
            "dashboard_url": self.settings.base_url,
            "tenders": [tender_to_dict(t) for t in tenders],
        }
        url = self.settings.webhook_url
        try:
            response = httpx.post(url, json=payload, timeout=30)
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            return [DeliveryResult(self.name, url, True)]
        except Exception as exc:
            logger.warning("Webhook %s echoue: %s", url, exc)
            return [DeliveryResult(self.name, url, False, str(exc))]

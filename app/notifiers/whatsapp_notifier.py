"""Alertes WhatsApp, via Twilio ou l'API Cloud de Meta.

Rappel important : hors de la fenetre de 24 h ouverte par un message de
l'utilisateur, WhatsApp n'accepte que des messages bases sur un *modele*
pre-approuve. Pour des alertes a toute heure, renseignez META_TEMPLATE_NAME
(un modele avec une seule variable de corps) ou utilisez Twilio avec un
modele approuve. Sans modele, l'envoi ne part que dans cette fenetre.
"""

from __future__ import annotations

import logging

import httpx

from app.config import Settings, get_settings
from app.messages import render_whatsapp
from app.models import Tender
from app.notifiers.base import DeliveryResult, Notifier

logger = logging.getLogger(__name__)

TEST_MESSAGE = "Radar marches publics : test de configuration WhatsApp reussi."


class WhatsAppNotifier(Notifier):
    name = "whatsapp"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        settings = self.settings
        if not settings.whatsapp_enabled:
            return False
        if settings.whatsapp_provider == "twilio":
            return bool(settings.twilio_account_sid and settings.twilio_auth_token)
        if settings.whatsapp_provider == "meta":
            return bool(settings.meta_phone_number_id and settings.meta_access_token)
        return False

    @property
    def targets(self) -> list[str]:
        return list(self.settings.whatsapp_to)

    def send(self, tenders: list[Tender]) -> list[DeliveryResult]:
        if not self.enabled:
            return []
        body = render_whatsapp(tenders, self.settings.base_url) if tenders else TEST_MESSAGE
        sender = (
            self._send_twilio
            if self.settings.whatsapp_provider == "twilio"
            else self._send_meta
        )
        results: list[DeliveryResult] = []
        for target in self.targets:
            try:
                sender(target, body)
                results.append(DeliveryResult(self.name, target, True))
            except Exception as exc:
                logger.warning("WhatsApp vers %s echoue: %s", target, exc)
                results.append(DeliveryResult(self.name, target, False, str(exc)))
        return results

    @staticmethod
    def _normalize_number(number: str) -> str:
        cleaned = number.strip().replace(" ", "")
        return cleaned.removeprefix("whatsapp:")

    def _send_twilio(self, target: str, body: str) -> None:
        settings = self.settings
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{settings.twilio_account_sid}/Messages.json"
        )
        response = httpx.post(
            url,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token),
            data={
                "From": settings.twilio_whatsapp_from,
                "To": f"whatsapp:{self._normalize_number(target)}",
                "Body": body,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Twilio HTTP {response.status_code}: {response.text[:300]}")

    def _send_meta(self, target: str, body: str) -> None:
        settings = self.settings
        url = (
            f"https://graph.facebook.com/{settings.meta_api_version}/"
            f"{settings.meta_phone_number_id}/messages"
        )
        number = self._normalize_number(target).lstrip("+")
        if settings.meta_template_name:
            # Modele approuve : fonctionne a tout moment, y compris hors fenetre 24 h.
            payload: dict = {
                "messaging_product": "whatsapp",
                "to": number,
                "type": "template",
                "template": {
                    "name": settings.meta_template_name,
                    "language": {"code": settings.meta_template_lang},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [{"type": "text", "text": body[:1024]}],
                        }
                    ],
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": number,
                "type": "text",
                "text": {"preview_url": False, "body": body[:4096]},
            }
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.meta_access_token}"},
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Meta HTTP {response.status_code}: {response.text[:300]}")

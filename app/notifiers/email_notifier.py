"""Alertes par email via SMTP."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import Settings, get_settings
from app.messages import render_html, render_text, subject_for
from app.models import Tender
from app.notifiers.base import DeliveryResult, Notifier

logger = logging.getLogger(__name__)


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.email_enabled

    @property
    def targets(self) -> list[str]:
        return list(self.settings.email_to)

    def _build_message(self, tenders: list[Tender], recipient: str) -> EmailMessage:
        message = EmailMessage()
        if tenders:
            message["Subject"] = subject_for(tenders)
            message.set_content(render_text(tenders, self.settings.base_url))
            message.add_alternative(
                render_html(tenders, self.settings.base_url), subtype="html"
            )
        else:
            message["Subject"] = "[Radar marches publics] Test de configuration"
            message.set_content(
                "Ce message confirme que l'envoi d'emails du radar fonctionne.\n"
                f"Tableau de bord : {self.settings.base_url}\n"
            )
        message["From"] = self.settings.email_from
        message["To"] = recipient
        return message

    def send(self, tenders: list[Tender]) -> list[DeliveryResult]:
        if not self.enabled:
            return []
        results: list[DeliveryResult] = []
        try:
            with self._connect() as server:
                for recipient in self.targets:
                    try:
                        server.send_message(self._build_message(tenders, recipient))
                        results.append(DeliveryResult(self.name, recipient, True))
                    except Exception as exc:
                        logger.warning("Email vers %s echoue: %s", recipient, exc)
                        results.append(
                            DeliveryResult(self.name, recipient, False, str(exc))
                        )
        except Exception as exc:
            # Connexion SMTP impossible : tous les destinataires sont en echec.
            logger.error("Connexion SMTP impossible: %s", exc)
            return [
                DeliveryResult(self.name, recipient, False, f"SMTP: {exc}")
                for recipient in self.targets
            ]
        return results

    def _connect(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        settings = self.settings
        if settings.smtp_port == 465:
            server: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=30
            )
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            if settings.smtp_use_tls:
                server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        return server

"""Contrat commun aux canaux de notification."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import Tender

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DeliveryResult:
    channel: str
    target: str
    ok: bool
    error: str | None = None


class Notifier(ABC):
    """Un canal d'envoi. ``targets`` liste les destinataires configures."""

    name: str = "notifier"

    @property
    @abstractmethod
    def enabled(self) -> bool: ...

    @property
    @abstractmethod
    def targets(self) -> list[str]: ...

    @abstractmethod
    def send(self, tenders: list[Tender]) -> list[DeliveryResult]:
        """Envoie une alerte groupee. Ne leve pas : les echecs sont dans le retour."""

    def send_test(self) -> list[DeliveryResult]:
        """Envoie un message de verification de la configuration."""
        return self.send([])

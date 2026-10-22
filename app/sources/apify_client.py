"""Client minimal de l'API Apify : lancement d'un run, attente, lecture du dataset."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyError(RuntimeError):
    """Erreur renvoyee par l'API Apify ou run termine en echec."""


class ApifyClient:
    def __init__(self, token: str, timeout_secs: int = 300) -> None:
        if not token:
            raise ApifyError(
                "Token Apify manquant : renseignez APIFY_TOKEN dans le fichier .env "
                "(console.apify.com > Settings > Integrations)."
            )
        self._token = token
        self._timeout_secs = timeout_secs
        self._client = httpx.Client(
            base_url=API_BASE,
            timeout=httpx.Timeout(60.0, read=120.0),
            headers={"Authorization": f"Bearer {token}"},
        )

    def __enter__(self) -> ApifyClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- Primitives ---

    def start_run(self, actor_id: str, run_input: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"/acts/{actor_id}/runs", json=run_input)
        if response.status_code >= 400:
            raise ApifyError(
                f"Lancement de l'actor {actor_id} refuse "
                f"(HTTP {response.status_code}): {response.text[:400]}"
            )
        return response.json()["data"]

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/actor-runs/{run_id}")
        if response.status_code >= 400:
            raise ApifyError(f"Lecture du run {run_id} impossible (HTTP {response.status_code})")
        return response.json()["data"]

    def iter_dataset_items(
        self, dataset_id: str, page_size: int = 1000
    ) -> list[dict[str, Any]]:
        """Lit tout le dataset en suivant la pagination."""
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self._client.get(
                f"/datasets/{dataset_id}/items",
                params={"offset": offset, "limit": page_size, "clean": "true"},
            )
            if response.status_code >= 400:
                raise ApifyError(
                    f"Lecture du dataset {dataset_id} impossible "
                    f"(HTTP {response.status_code})"
                )
            page = response.json()
            if not page:
                break
            items.extend(page)
            if len(page) < page_size:
                break
            offset += len(page)
        return items

    # --- Enchainement complet ---

    def run_and_collect(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        poll_interval: float = 5.0,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Lance l'actor, attend la fin, renvoie (enregistrements, metadonnees du run)."""
        run = self.start_run(actor_id, run_input)
        run_id = run["id"]
        dataset_id = run["defaultDatasetId"]
        logger.info("Run Apify %s demarre pour %s", run_id, actor_id)

        deadline = time.monotonic() + self._timeout_secs
        status = run.get("status", "READY")
        while status not in TERMINAL_STATES:
            if time.monotonic() > deadline:
                raise ApifyError(
                    f"Run {run_id} toujours en cours apres {self._timeout_secs}s "
                    "(augmentez APIFY_TIMEOUT_SECS ou baissez APIFY_MAX_ITEMS)."
                )
            time.sleep(poll_interval)
            run = self.get_run(run_id)
            status = run.get("status", "")

        if status != "SUCCEEDED":
            raise ApifyError(
                f"Run {run_id} termine avec le statut {status}: "
                f"{run.get('statusMessage') or 'aucun detail'}"
            )

        items = self.iter_dataset_items(dataset_id)
        logger.info("Run %s: %d enregistrements recuperes", run_id, len(items))
        return items, run

"""Planificateur : collectes periodiques des avis et des attributions."""

from __future__ import annotations

import logging
import signal
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.db import init_db, session_scope
from app.pipeline import collect_awards, collect_tenders
from app.seeds import ensure_default_profiles

logger = logging.getLogger(__name__)


def job_collect_tenders() -> None:
    settings = get_settings()
    if not settings.apify_token:
        logger.warning("APIFY_TOKEN absent : collecte des avis ignoree.")
        return
    with session_scope() as session:
        run = collect_tenders(session, settings)
        logger.info(
            "Avis: %s | %d lus, %d nouveaux, %d alertes",
            run.status, run.items_fetched, run.items_new, run.notifications_sent,
        )


def job_collect_awards() -> None:
    settings = get_settings()
    if not settings.portal_enabled:
        return
    with session_scope() as session:
        run = collect_awards(session, settings)
        logger.info(
            "Attributions: %s | %d lues, %d nouvelles",
            run.status, run.items_fetched, run.items_new,
        )


def build_scheduler() -> BlockingScheduler:
    settings = get_settings()
    scheduler = BlockingScheduler(timezone=settings.timezone)

    scheduler.add_job(
        job_collect_tenders,
        IntervalTrigger(minutes=settings.collect_interval_minutes),
        id="collect_tenders",
        name="Collecte des avis",
        # Une collecte qui depasse l'intervalle ne doit pas en empiler d'autres.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
        next_run_time=datetime.now(),  # une premiere passe des le demarrage
    )
    scheduler.add_job(
        job_collect_awards,
        IntervalTrigger(hours=settings.awards_interval_hours),
        id="collect_awards",
        name="Collecte des attributions",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    return scheduler


def run_forever() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    init_db()
    # Le planificateur peut gagner la course au demarrage : il amorce donc
    # aussi les profils, sinon sa premiere collecte ne notifierait rien.
    with session_scope() as session:
        created = ensure_default_profiles(session)
    if created:
        logger.info("Profils de veille crees : %s", ", ".join(p.name for p in created))

    scheduler = build_scheduler()
    logger.info(
        "Planificateur demarre : avis toutes les %d min, attributions toutes les %d h (%s)",
        settings.collect_interval_minutes, settings.awards_interval_hours, settings.timezone,
    )

    def _stop(*_args: object) -> None:
        logger.info("Arret demande, fermeture du planificateur.")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    scheduler.start()


if __name__ == "__main__":
    run_forever()

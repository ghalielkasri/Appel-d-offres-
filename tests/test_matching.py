"""Moteur de filtrage par domaine."""

from app.matching import match_profiles, score_tender
from app.models import Category, WatchProfile
from app.sources.base import TenderRecord


def record(obj: str, **kwargs) -> TenderRecord:
    defaults = {
        "source": "test",
        "source_key": "k",
        "object": obj,
        "category": Category.FOURNITURES,
    }
    return TenderRecord(**{**defaults, **kwargs})


def profiles(session) -> list[WatchProfile]:
    return session.query(WatchProfile).all()


def test_kitchen_tender_is_matched(seeded_session):
    """Le profil actif par defaut est « Cuisine professionnelle »."""
    result = match_profiles(
        record("Acquisition de fours mixtes pour la cuisine centrale"),
        profiles(seeded_session),
    )
    assert result and result[0].score > 0
    assert result[0].profile_name == "Cuisine professionnelle"
    assert "cuisine" in result[0].keywords


def test_generic_equipment_tender_is_not_matched_by_default(seeded_session):
    """Un marche d'equipement hors cuisine reste hors du domaine surveille.

    Le profil generaliste existe mais est livre inactif : sans cela, il noierait
    la veille cuisine sous les marches de materiel de toute nature.
    """
    result = match_profiles(
        record("Acquisition de materiel informatique"), profiles(seeded_session)
    )
    assert result == []


def test_generalist_profile_works_once_activated(seeded_session):
    for profile in profiles(seeded_session):
        if profile.name == "Equipement & materiel":
            profile.active = True
    seeded_session.flush()
    result = match_profiles(
        record("Acquisition de materiel et equipement technique"), profiles(seeded_session)
    )
    assert result and "materiel" in result[0].keywords


def test_works_tender_is_rejected(seeded_session):
    result = match_profiles(
        record("Travaux de construction d'une ecole", category=Category.TRAVAUX),
        profiles(seeded_session),
    )
    assert result == []


def test_excluded_keyword_disqualifies(seeded_session):
    """Le gardiennage est exclu meme quand l'avis parle de cantine."""
    result = match_profiles(
        record("Gardiennage des locaux de la cantine", category=Category.SERVICES),
        profiles(seeded_session),
    )
    assert result == []


def test_restauration_collective_is_targeted_not_excluded(seeded_session):
    """Correction importante : ces marches sont au coeur de la cible cuisine.

    Le profil generaliste les excluait, ce qui aurait fait manquer l'essentiel
    des marches de restauration collective.
    """
    result = match_profiles(
        record("Equipement pour la restauration collective des internats"),
        profiles(seeded_session),
    )
    assert result and result[0].score > 0


def test_object_weighs_more_than_buyer_name(seeded_session):
    profile = next(
        p for p in profiles(seeded_session) if p.name == "Cuisine professionnelle"
    )
    in_object = score_tender(record("Acquisition de materiel de cuisine"), profile)
    in_buyer = score_tender(
        record("Prestation diverse", buyer_raw="Direction de la cuisine centrale"), profile
    )
    assert in_object.score > in_buyer.score


def test_word_boundaries_avoid_false_positives(seeded_session):
    """"pompe" ne doit pas correspondre a "pompiers"."""
    profile = WatchProfile(
        name="t", keywords=["pompe"], excluded_keywords=[], categories=[],
        buyers=[], min_score=1.0, channels=[], active=True,
    )
    assert score_tender(record("Acquisition de pompes doseuses"), profile).score > 0
    assert score_tender(record("Equipement des sapeurs pompiers"), profile).score == 0


def test_amount_filter_applies_only_when_amount_known(seeded_session):
    profile = WatchProfile(
        name="t", keywords=["cuisine"], excluded_keywords=[], categories=[],
        buyers=[], min_amount=1_000_000.0, min_score=1.0, channels=[], active=True,
    )
    assert score_tender(record("Cuisine centrale", estimated_amount=500_000), profile).score == 0
    assert score_tender(record("Cuisine centrale", estimated_amount=2_000_000), profile).score > 0
    # Sans estimation publiee, l'avis ne doit pas etre ecarte a tort.
    assert score_tender(record("Cuisine centrale"), profile).score > 0


def test_inactive_profile_never_matches(seeded_session):
    for profile in profiles(seeded_session):
        profile.active = False
    assert match_profiles(record("Acquisition de fours de cuisine"), profiles(seeded_session)) == []


def test_default_profiles_are_seeded_on_a_fresh_database(db_session):
    """Premier demarrage : sans profil, le planificateur ne notifierait rien.

    Les services appellent ensure_default_profiles au demarrage ; c'est ce qui
    rend un deploiement neuf immediatement fonctionnel.
    """
    from app.models import WatchProfile
    from app.seeds import ensure_default_profiles

    assert db_session.query(WatchProfile).count() == 0
    crees = ensure_default_profiles(db_session)
    db_session.flush()

    assert crees, "au moins un profil doit etre cree"
    actifs = db_session.query(WatchProfile).filter(WatchProfile.active.is_(True)).all()
    assert [p.name for p in actifs] == ["Cuisine professionnelle"]


def test_seeding_is_idempotent(db_session):
    from app.models import WatchProfile
    from app.seeds import ensure_default_profiles

    ensure_default_profiles(db_session)
    db_session.flush()
    avant = db_session.query(WatchProfile).count()

    assert ensure_default_profiles(db_session) == []
    assert db_session.query(WatchProfile).count() == avant


def test_a_deleted_profile_is_not_resurrected(db_session):
    """Un profil supprime volontairement doit le rester."""
    from app.models import WatchProfile
    from app.seeds import ensure_default_profiles

    ensure_default_profiles(db_session)
    db_session.flush()
    db_session.query(WatchProfile).filter(
        WatchProfile.name == "Cuisine professionnelle"
    ).delete()
    db_session.flush()

    assert ensure_default_profiles(db_session) == []
    noms = [p.name for p in db_session.query(WatchProfile).all()]
    assert "Cuisine professionnelle" not in noms

"""Interface en ligne de commande : `radar <commande>`."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from app import exports
from app.config import get_settings
from app.db import init_db, session_scope
from app.models import WatchProfile
from app.pipeline import (
    collect_awards,
    collect_tenders,
    ingest_awards,
    notify_tenders,
    reindex_search,
)
from app.queries import AwardFilters, TenderFilters, summary, supplier_ranking
from app.seeds import seed_profiles

app = typer.Typer(
    add_completion=False,
    help="Radar des marches publics marocains : veille et historique des attributions.",
)
console = Console()


def _setup_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command("init-db")
def cmd_init_db(
    seed: Annotated[bool, typer.Option(help="Creer aussi les profils de veille par defaut")] = True,
) -> None:
    """Cree les tables et, par defaut, les profils de veille."""
    _setup_logging()
    init_db()
    console.print("[green]Tables creees ou deja presentes.[/green]")
    if seed:
        with session_scope() as session:
            profiles = seed_profiles(session)
        for profile in profiles:
            console.print(f"  profil ajoute : [bold]{profile.name}[/bold]")
        if not profiles:
            console.print("  profils deja presents, rien a faire.")
    # Les lignes anterieures a la colonne search_text deviennent trouvables.
    with session_scope() as session:
        tenders, awards = reindex_search(session)
    if tenders or awards:
        console.print(f"  index de recherche : {tenders} avis, {awards} attribution(s)")


@app.command("demo")
def cmd_demo(
    clear: Annotated[bool, typer.Option("--clear", help="Retirer le jeu de demonstration")] = False,
) -> None:
    """Charge un jeu de donnees de demonstration pour explorer l'interface.

    Les enregistrements portent la source "demo:*" et se retirent avec
    `radar demo --clear`, sans toucher aux donnees reelles.
    """
    _setup_logging()
    init_db()
    from app.demo import clear_demo_data, load_demo_data

    with session_scope() as session:
        seed_profiles(session)
        if clear:
            tenders, awards = clear_demo_data(session)
            console.print(f"[green]Retire : {tenders} avis, {awards} attribution(s).[/green]")
        else:
            tenders, awards = load_demo_data(session)
            console.print(
                f"[green]Charge : {tenders} avis, {awards} attribution(s).[/green]\n"
                "Lancez `radar serve` puis ouvrez http://localhost:8000"
            )


@app.command("reindex")
def cmd_reindex() -> None:
    """Recalcule l'index de recherche sur les donnees deja collectees.

    Necessaire une seule fois, apres la mise a jour qui rend la recherche
    insensible aux accents.
    """
    _setup_logging()
    init_db()
    with session_scope() as session:
        tenders, awards = reindex_search(session)
    console.print(f"[green]{tenders} avis et {awards} attribution(s) reindexes.[/green]")


@app.command("collect")
def cmd_collect(
    keyword: Annotated[
        Optional[list[str]],
        typer.Option("--keyword", "-k", help="Mot-cle a envoyer au portail (repetable)"),
    ] = None,
    notify: Annotated[bool, typer.Option(help="Envoyer les alertes")] = True,
) -> None:
    """Collecte les avis de consultation via Apify, puis notifie les nouveautes."""
    _setup_logging()
    init_db()
    settings = get_settings()
    if not settings.apify_token:
        console.print(
            "[red]APIFY_TOKEN absent.[/red] Creez un compte sur apify.com, copiez le "
            "token (Settings > API & Integrations) dans le fichier .env, puis relancez."
        )
        raise typer.Exit(code=1)

    with session_scope() as session:
        run = collect_tenders(session, settings, keywords=list(keyword) if keyword else None, notify=notify)
        status = "green" if run.status == "succeeded" else "red"
        console.print(
            f"[{status}]{run.status}[/{status}] : {run.items_fetched} avis recuperes, "
            f"{run.items_new} nouveaux pertinents, {run.items_updated} mis a jour, "
            f"{run.notifications_sent} alerte(s) envoyee(s), "
            f"cout estime ${run.estimated_cost_usd or 0:.3f}"
        )
        if run.error:
            console.print(f"[red]{run.error}[/red]")
            raise typer.Exit(code=1)


@app.command("collect-awards")
def cmd_collect_awards(
    since: Annotated[Optional[str], typer.Option(help="Ne garder que les attributions depuis AAAA-MM-JJ")] = None,
) -> None:
    """Collecte les resultats d'attribution sur le portail officiel."""
    _setup_logging()
    init_db()
    since_date: date | None = None
    if since:
        since_date = datetime.strptime(since, "%Y-%m-%d").date()
    with session_scope() as session:
        run = collect_awards(session, get_settings(), since=since_date)
        status = "green" if run.status == "succeeded" else "red"
        console.print(
            f"[{status}]{run.status}[/{status}] : {run.items_fetched} attributions lues, "
            f"{run.items_new} nouvelles, {run.items_updated} mises a jour"
        )
        if run.error:
            console.print(f"[yellow]{run.error}[/yellow]")


@app.command("collect-all")
def cmd_collect_all() -> None:
    """Enchaine la collecte des avis puis celle des attributions."""
    settings = get_settings()
    if settings.apify_token:
        cmd_collect()
    else:
        console.print("[yellow]APIFY_TOKEN absent : collecte des avis ignoree.[/yellow]")
    cmd_collect_awards()


@app.command("portal-discover")
def cmd_portal_discover() -> None:
    """Liste les liens "resultats" de la page d'accueil du portail.

    A lancer depuis le serveur, qui atteint le portail, pour identifier l'URL
    exacte de la rubrique des attributions.
    """
    _setup_logging()
    from app.sources.portal_awards import PortalAwardSource

    source = PortalAwardSource(get_settings())
    try:
        links = source.discover()
    except Exception as exc:
        console.print(f"[red]Portail injoignable :[/red] {type(exc).__name__}: {exc}")
        console.print(
            "Verifiez que le serveur atteint bien "
            f"{source.base_url} (le portail filtre certaines plages d'adresses)."
        )
        raise typer.Exit(code=1)

    if not links:
        console.print("[yellow]Aucun lien 'resultat' trouve sur la page d'accueil.[/yellow]")
        return
    table = Table("Libelle", "URL")
    for link in links:
        table.add_row(link["label"] or "-", link["url"])
    console.print(table)
    console.print(
        "\nAjoutez l'URL retenue en tete de CANDIDATE_PATHS dans "
        "[bold]app/sources/portal_awards.py[/bold], puis relancez `radar collect-awards`."
    )


@app.command("portal-fetch")
def cmd_portal_fetch(
    url: Annotated[str, typer.Argument(help="URL de la page de resultats a telecharger")],
    out: Annotated[Path, typer.Option(help="Fichier de sortie")] = Path("page.html"),
) -> None:
    """Telecharge une page du portail pour inspection ou mise au point du parseur."""
    _setup_logging()
    from app.sources.portal_awards import PortalAwardSource

    source = PortalAwardSource(get_settings())
    html = source.fetch_page(url)
    out.write_text(html, encoding="utf-8")
    console.print(f"[green]{len(html)} caracteres ecrits dans {out}[/green]")


@app.command("portal-parse")
def cmd_portal_parse(
    path: Annotated[Path, typer.Argument(help="Fichier HTML a analyser")],
    save: Annotated[bool, typer.Option(help="Enregistrer les attributions trouvees")] = False,
    url: Annotated[str, typer.Option(help="URL d'origine, pour resoudre les liens relatifs")] = "",
) -> None:
    """Analyse un fichier HTML de resultats : verifie le parseur sans reseau."""
    _setup_logging()
    from app.sources.portal_awards import PortalAwardSource

    html = path.read_text(encoding="utf-8", errors="replace")
    source = PortalAwardSource(get_settings())
    awards = source.fetch_awards_from_html(html, url or None)

    if not awards:
        console.print(
            "[yellow]Aucune attribution reconnue dans ce fichier.[/yellow]\n"
            "Si la page en contient pourtant, relevez les libelles exacts des colonnes "
            "et completez COLUMN_PATTERNS / LABEL_PATTERNS dans "
            "app/sources/portal_awards.py."
        )
        raise typer.Exit(code=1)

    table = Table("Reference", "Attributaire", "Montant", "Concur.", "Date", "Acheteur")
    for award in awards[:40]:
        table.add_row(
            award.reference or "-",
            (award.supplier_raw or "-")[:32],
            f"{award.awarded_amount:,.2f}" if award.awarded_amount else "-",
            str(award.bids_count or "-"),
            award.award_date.isoformat() if award.award_date else "-",
            (award.buyer_raw or "-")[:28],
        )
    console.print(table)
    console.print(f"[green]{len(awards)} attribution(s) reconnue(s).[/green]")

    if save:
        init_db()
        with session_scope() as session:
            new, updated = ingest_awards(session, awards)
        console.print(f"[green]{new} enregistree(s), {updated} mise(s) a jour.[/green]")


@app.command("documents-sync")
def cmd_documents_sync() -> None:
    """Enregistre les references de pieces du DCE portees par les avis collectes."""
    _setup_logging()
    init_db()
    from sqlalchemy import select

    from app.documents import sync_tender_documents
    from app.models import Tender

    settings = get_settings()
    with session_scope() as session:
        tenders = list(session.scalars(select(Tender)).all())
        total = 0
        for tender in tenders:
            total += len(sync_tender_documents(session, tender, settings.portal_base_url))
    console.print(
        f"[green]{total} nouvelle(s) reference(s) sur {len(tenders)} avis.[/green]"
    )
    if total == 0:
        console.print(
            "[yellow]Aucune reference trouvee.[/yellow] Les actors Apify ne remplissent "
            "pas encore le champ dce_documents ; utilisez le lien « Voir sur le portail » "
            "de la fiche d'un avis en attendant."
        )


@app.command("documents-fetch")
def cmd_documents_fetch(
    limit: Annotated[int, typer.Option(help="Nombre de pieces a telecharger")] = 50,
) -> None:
    """Telecharge les pieces du DCE (CPS, bordereau des prix...).

    Le portail reserve ce telechargement aux comptes entreprise : renseignez
    PORTAL_LOGIN et PORTAL_PASSWORD dans le fichier .env.
    """
    _setup_logging()
    init_db()
    from app.documents import download_pending

    settings = get_settings()
    if not settings.portal_credentials_set:
        console.print(
            "[yellow]PORTAL_LOGIN et PORTAL_PASSWORD ne sont pas renseignes.[/yellow]\n"
            "Les pieces publiques seront tentees, les autres marquees « compte requis »."
        )
    with session_scope() as session:
        counts = download_pending(session, settings, limit=limit)
    if not counts:
        console.print("Aucune piece en attente de telechargement.")
        return
    libelles = {
        "downloaded": "[green]telechargees[/green]",
        "auth_required": "[yellow]compte entreprise requis[/yellow]",
        "failed": "[red]en echec[/red]",
        "listed": "non traitees",
    }
    for statut, nombre in counts.items():
        console.print(f"  {nombre:4} {libelles.get(statut, statut)}")


@app.command("portal-login-probe")
def cmd_portal_login_probe() -> None:
    """Releve les champs du formulaire de connexion du portail.

    A lancer depuis le serveur. Le formulaire n'a pas pu etre observe depuis
    l'environnement de developpement : cette commande donne les valeurs a
    mettre dans PORTAL_LOGIN_PATH, PORTAL_LOGIN_USER_FIELD et
    PORTAL_LOGIN_PASSWORD_FIELD.
    """
    _setup_logging()
    from bs4 import BeautifulSoup

    from app.sources.portal_awards import PortalAwardSource

    settings = get_settings()
    source = PortalAwardSource(settings)
    try:
        html = source.fetch_page(source.base_url + "/")
    except Exception as exc:
        console.print(f"[red]Portail injoignable :[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1)

    soup = BeautifulSoup(html, "lxml")
    forms = soup.find_all("form")
    trouve = False
    for index, form in enumerate(forms, start=1):
        champs = form.find_all("input")
        types = {str(c.get("type") or "text").lower() for c in champs}
        if "password" not in types:
            continue
        trouve = True
        console.print(f"\n[bold]Formulaire {index}[/bold] action={form.get('action') or '(page courante)'}")
        table = Table("name", "type", "id")
        for champ in champs:
            table.add_row(
                str(champ.get("name") or "-"),
                str(champ.get("type") or "text"),
                str(champ.get("id") or "-"),
            )
        console.print(table)
        users = [
            c.get("name") for c in champs
            if str(c.get("type") or "text").lower() in ("text", "email") and c.get("name")
        ]
        passwords = [
            c.get("name") for c in champs
            if str(c.get("type") or "").lower() == "password" and c.get("name")
        ]
        console.print("A reporter dans .env :")
        console.print(f"  PORTAL_LOGIN_PATH={form.get('action') or '/'}")
        if users:
            console.print(f"  PORTAL_LOGIN_USER_FIELD={users[0]}")
        if passwords:
            console.print(f"  PORTAL_LOGIN_PASSWORD_FIELD={passwords[0]}")

    if not trouve:
        console.print(
            "[yellow]Aucun formulaire de connexion sur la page d'accueil.[/yellow] "
            "Le portail place souvent l'Espace fournisseur sur une page dediee : "
            "trouvez son URL dans un navigateur, puis relancez "
            "`radar portal-fetch <url> --out /app/data/login.html`."
        )


@app.command("portal-login-test")
def cmd_portal_login_test() -> None:
    """Tente une connexion au portail et rend compte de chaque etape.

    A lancer depuis le serveur, apres avoir renseigne PORTAL_LOGIN et
    PORTAL_PASSWORD. Aucune donnee n'est modifiee : la commande ne fait que
    diagnostiquer.
    """
    _setup_logging()
    from app.documents import PortalDocumentFetcher

    settings = get_settings()
    console.print(f"[bold]Portail[/bold]      {settings.portal_base_url}")
    console.print(f"[bold]Page[/bold]         {settings.portal_login_path}")
    console.print(f"[bold]Identifiant[/bold]  {settings.portal_login or '(vide)'}")
    console.print(
        f"[bold]Mot de passe[/bold] {'defini (' + str(len(settings.portal_password)) + ' caracteres)' if settings.portal_password else '(vide)'}"
    )
    console.print(
        f"[bold]Champs[/bold]       attendus : {settings.portal_login_user_field} / "
        f"{settings.portal_login_password_field}\n"
    )

    if not settings.portal_credentials_set:
        console.print(
            "[red]Identifiants absents.[/red] Renseignez PORTAL_LOGIN et "
            "PORTAL_PASSWORD dans le fichier .env, puis relancez."
        )
        raise typer.Exit(code=1)

    with PortalDocumentFetcher(settings) as fetcher:
        ok = fetcher.login()
        report = fetcher.login_report

        for step in report.get("steps", []):
            console.print(f"  . {step}")

        form = report.get("form")
        if form:
            console.print("\n[bold]Formulaire detecte[/bold]")
            table = Table("Propriete", "Valeur")
            table.add_row("action", str(form["action"]))
            table.add_row("methode", str(form["method"]).upper())
            table.add_row("champ identifiant", str(form["user_field"]))
            table.add_row("champ mot de passe", str(form["password_field"]))
            table.add_row("champs caches conserves", str(form["hidden_fields"]))
            table.add_row("captcha", "OUI" if form["captcha"] else "non")
            console.print(table)

            # Si les champs reels diffferent de la configuration, on le dit.
            if form["user_field"] != settings.portal_login_user_field:
                console.print(
                    f"\n[yellow]A corriger dans .env :[/yellow] "
                    f"PORTAL_LOGIN_USER_FIELD={form['user_field']}"
                )
            if form["password_field"] != settings.portal_login_password_field:
                console.print(
                    f"[yellow]A corriger dans .env :[/yellow] "
                    f"PORTAL_LOGIN_PASSWORD_FIELD={form['password_field']}"
                )

        if ok:
            console.print("\n[green]Connexion reussie.[/green] "
                          "Vous pouvez lancer `radar documents-fetch`.")
        else:
            console.print(f"\n[red]Connexion echouee.[/red] {report.get('reason', '')}")
            raise typer.Exit(code=1)


@app.command("comparables")
def cmd_comparables(
    reference: Annotated[str, typer.Argument(help="Reference de l'avis, ou une partie")],
) -> None:
    """Affiche les marches comparables a un avis, et qui les a remportes."""
    _setup_logging()
    init_db()
    from sqlalchemy import select

    from app.models import Tender
    from app.similarity import comparable_awards, likely_competitors, price_benchmark

    with session_scope() as session:
        tender = session.scalar(
            select(Tender).where(Tender.reference.ilike(f"%{reference}%")).limit(1)
        )
        if tender is None:
            console.print(f"[red]Aucun avis ne correspond a « {reference} ».[/red]")
            raise typer.Exit(code=1)

        console.print(f"\n[bold]{tender.reference}[/bold] — {tender.object}")
        console.print(f"  acheteur   : {tender.buyer_raw or '-'}")
        console.print(f"  estimation : {tender.estimated_amount or 0:,.0f} DH")

        comparables = comparable_awards(session, tender, limit=12)
        if not comparables:
            console.print("\n[yellow]Aucun precedent comparable en base.[/yellow]")
            return

        table = Table("Score", "Attributaire", "Montant", "Concur.", "Date", "Pourquoi")
        for item in comparables:
            award = item.award
            table.add_row(
                f"{item.score:.2f}",
                (award.supplier.name if award.supplier else award.supplier_raw or "-")[:26],
                f"{award.awarded_amount:,.0f}" if award.awarded_amount else "-",
                str(award.bids_count or "-"),
                award.award_date.strftime("%m/%Y") if award.award_date else "-",
                " ; ".join(item.reasons)[:52],
            )
        console.print(table)

        bench = price_benchmark(comparables, tender.estimated_amount)
        console.print(
            f"\n  Fourchette adjugee : {bench.minimum:,.0f} -> {bench.maximum:,.0f} DH"
            f" | median {bench.median_amount:,.0f} DH | {bench.avg_bids} concurrents en moyenne"
        )
        if bench.gap_to_estimate is not None:
            console.print(f"  Median vs estimation de l'avis : {bench.gap_to_estimate:+.1f} %")

        console.print("\n  Concurrents probables :")
        for entry in likely_competitors(comparables):
            console.print(
                f"    {entry['name'][:30]:32} {entry['wins']} comparable(s), "
                f"dont {entry['same_buyer_wins']} chez cet acheteur"
            )


@app.command("test-notify")
def cmd_test_notify() -> None:
    """Envoie un message de test sur chaque canal configure."""
    _setup_logging()
    from app.notifiers import all_notifiers

    notifiers = all_notifiers(get_settings())
    any_enabled = False
    for notifier in notifiers:
        if not notifier.enabled:
            console.print(f"  [dim]{notifier.name:9} inactif[/dim]")
            continue
        any_enabled = True
        for result in notifier.send_test():
            mark = "[green]envoye[/green]" if result.ok else f"[red]echec : {result.error}[/red]"
            console.print(f"  {notifier.name:9} -> {result.target} : {mark}")
    if not any_enabled:
        console.print(
            "[yellow]Aucun canal configure.[/yellow] Renseignez SMTP_*, WHATSAPP_* "
            "ou WEBHOOK_URL dans le fichier .env."
        )


@app.command("doctor")
def cmd_doctor(
    skip_network: Annotated[
        bool, typer.Option("--local", help="Ne pas tester Apify ni le portail")
    ] = False,
) -> None:
    """Controle de pre-vol : tout ce qui doit etre vrai avant la premiere collecte."""
    _setup_logging()
    from app.doctor import FAIL, OK, WARN, run_diagnostics

    settings = get_settings()
    console.print(f"[bold]{settings.app_name}[/bold] — contrôle de pré-vol\n")

    report = run_diagnostics(settings, skip_network=skip_network)
    couleurs = {OK: "green", WARN: "yellow", FAIL: "red"}
    symboles = {OK: "OK  ", WARN: "ATTN", FAIL: "ECHEC"}

    for check in report.checks:
        couleur = couleurs[check.status]
        console.print(
            f"  [{couleur}]{symboles[check.status]:5}[/{couleur}] "
            f"[bold]{check.name}[/bold] — {check.detail}"
        )
        if check.action and check.status != OK:
            console.print(f"        [dim]-> {check.action}[/dim]")

    console.print()
    if report.ready:
        if report.warnings:
            console.print(
                f"[green]Prêt a fonctionner[/green], avec "
                f"{len(report.warnings)} point(s) d'attention ci-dessus."
            )
        else:
            console.print("[green]Tout est en ordre.[/green]")
    else:
        console.print(
            f"[red]{len(report.failures)} blocage(s)[/red] à lever avant la "
            "première collecte."
        )
        raise typer.Exit(code=1)


@app.command("status")
def cmd_status() -> None:
    """Affiche l'etat de la base et des dernieres collectes."""
    _setup_logging()
    init_db()
    settings = get_settings()
    with session_scope() as session:
        info = summary(session)
        console.print(f"[bold]Base[/bold]        {settings.database_url}")
        console.print(
            f"[bold]Avis[/bold]        {info.tenders_total} dont "
            f"{info.tenders_matched} pertinents, {info.tenders_open} ouverts, "
            f"{info.tenders_closing_soon} a cloturer sous 7 jours"
        )
        console.print(
            f"[bold]Attributions[/bold] {info.awards_total} pour "
            f"{info.awards_amount:,.0f} DH, {info.suppliers_total} attributaire(s)"
        )
        console.print(
            f"[bold]Canaux[/bold]      email={settings.email_enabled} "
            f"whatsapp={settings.whatsapp_enabled} webhook={settings.webhook_enabled}"
        )
        if info.runs:
            table = Table("Source", "Debut", "Statut", "Lus", "Nouveaux", "Alertes", "Erreur")
            for run in info.runs:
                table.add_row(
                    run.source,
                    run.started_at.strftime("%d/%m %H:%M"),
                    run.status,
                    str(run.items_fetched),
                    str(run.items_new),
                    str(run.notifications_sent),
                    (run.error or "")[:40],
                )
            console.print(table)


@app.command("top")
def cmd_top(
    limit: Annotated[int, typer.Option(help="Nombre d'attributaires a afficher")] = 15,
) -> None:
    """Affiche le classement des attributaires."""
    _setup_logging()
    init_db()
    with session_scope() as session:
        ranking = supplier_ranking(session, limit=limit)
        if not ranking:
            console.print("[yellow]Aucune attribution en base.[/yellow]")
            return
        table = Table("#", "Attributaire", "Marches", "Montant total", "Moyen", "Acheteurs")
        for index, row in enumerate(ranking, start=1):
            table.add_row(
                str(index), row.name[:40], str(row.awards_count),
                f"{row.total_amount:,.0f}", f"{row.avg_amount:,.0f}", str(row.buyers_count),
            )
        console.print(table)


@app.command("export")
def cmd_export(
    out: Annotated[Path, typer.Argument(help="Fichier de sortie (.xlsx, .json ou .csv)")],
    what: Annotated[str, typer.Option(help="complet, avis, attributions ou attributaires")] = "complet",
) -> None:
    """Exporte les donnees historiques dans un fichier."""
    _setup_logging()
    init_db()
    suffix = out.suffix.lower()
    with session_scope() as session:
        if suffix == ".json":
            content = exports.full_json(session)
        elif suffix == ".xlsx":
            if what == "complet":
                content = exports.full_workbook(session)
            elif what == "avis":
                content = exports.to_xlsx([("Avis", exports.TENDER_HEADERS, exports.tender_rows(session))])
            elif what == "attributions":
                content = exports.to_xlsx([("Attributions", exports.AWARD_HEADERS, exports.award_rows(session))])
            else:
                content = exports.to_xlsx([("Attributaires", exports.SUPPLIER_HEADERS, exports.supplier_rows(session))])
        elif suffix == ".csv":
            if what == "avis":
                content = exports.to_csv(exports.TENDER_HEADERS, exports.tender_rows(session))
            elif what == "attributaires":
                content = exports.to_csv(exports.SUPPLIER_HEADERS, exports.supplier_rows(session))
            else:
                content = exports.to_csv(exports.AWARD_HEADERS, exports.award_rows(session))
        else:
            console.print("[red]Extension non reconnue : utilisez .xlsx, .json ou .csv[/red]")
            raise typer.Exit(code=1)
    out.write_bytes(content)
    console.print(f"[green]{len(content):,} octets ecrits dans {out}[/green]")


@app.command("profile-list")
def cmd_profile_list() -> None:
    """Liste les profils de veille."""
    _setup_logging()
    init_db()
    with session_scope() as session:
        profiles = session.query(WatchProfile).order_by(WatchProfile.id).all()
        for profile in profiles:
            state = "[green]actif[/green]" if profile.active else "[dim]inactif[/dim]"
            console.print(f"\n[bold]{profile.name}[/bold] {state} (score min {profile.min_score})")
            console.print(f"  categories : {', '.join(profile.categories) or 'toutes'}")
            console.print(f"  montant    : {profile.min_amount or '-'} -> {profile.max_amount or '-'}")
            console.print(f"  mots-cles  : {', '.join(profile.keywords)}")
            if profile.excluded_keywords:
                console.print(f"  exclusions : {', '.join(profile.excluded_keywords)}")


@app.command("profile-edit")
def cmd_profile_edit(
    name: Annotated[str, typer.Argument(help="Nom exact du profil")],
    add_keyword: Annotated[Optional[list[str]], typer.Option("--add-keyword", help="Mot-cle a ajouter")] = None,
    remove_keyword: Annotated[Optional[list[str]], typer.Option("--remove-keyword", help="Mot-cle a retirer")] = None,
    add_exclusion: Annotated[Optional[list[str]], typer.Option("--add-exclusion", help="Exclusion a ajouter")] = None,
    categories: Annotated[Optional[str], typer.Option(help='Categories, ex. "Fournitures,Services" ou "" pour toutes')] = None,
    min_amount: Annotated[Optional[float], typer.Option(help="Estimation minimale")] = None,
    max_amount: Annotated[Optional[float], typer.Option(help="Estimation maximale")] = None,
    min_score: Annotated[Optional[float], typer.Option(help="Score minimal pour alerter")] = None,
    active: Annotated[Optional[bool], typer.Option(help="Activer ou desactiver le profil")] = None,
) -> None:
    """Modifie un profil de veille."""
    _setup_logging()
    init_db()
    with session_scope() as session:
        profile = session.query(WatchProfile).filter(WatchProfile.name == name).one_or_none()
        if profile is None:
            console.print(f"[red]Profil introuvable : {name}[/red]")
            console.print("Noms disponibles : " + ", ".join(
                p.name for p in session.query(WatchProfile).all()
            ))
            raise typer.Exit(code=1)

        if add_keyword:
            profile.keywords = list(dict.fromkeys((profile.keywords or []) + list(add_keyword)))
        if remove_keyword:
            drop = {k.lower() for k in remove_keyword}
            profile.keywords = [k for k in (profile.keywords or []) if k.lower() not in drop]
        if add_exclusion:
            profile.excluded_keywords = list(
                dict.fromkeys((profile.excluded_keywords or []) + list(add_exclusion))
            )
        if categories is not None:
            profile.categories = [c.strip() for c in categories.split(",") if c.strip()]
        if min_amount is not None:
            profile.min_amount = min_amount or None
        if max_amount is not None:
            profile.max_amount = max_amount or None
        if min_score is not None:
            profile.min_score = min_score
        if active is not None:
            profile.active = active
        session.flush()
        console.print(f"[green]Profil « {profile.name} » mis a jour.[/green]")
        console.print(f"  {len(profile.keywords)} mot(s)-cle(s), score min {profile.min_score}, actif={profile.active}")


@app.command("renotify")
def cmd_renotify(
    limit: Annotated[int, typer.Option(help="Nombre d'avis a renvoyer")] = 5,
) -> None:
    """Renvoie une alerte pour les avis pertinents les plus recents.

    Utile apres avoir configure un nouveau canal : le journal d'envoi empeche
    les doublons, mais un canal jamais utilise n'a rien dans ce journal.
    """
    _setup_logging()
    init_db()
    from sqlalchemy import select

    from app.models import Tender

    with session_scope() as session:
        tenders = list(
            session.scalars(
                select(Tender)
                .where(Tender.match_score > 0)
                .order_by(Tender.first_seen_at.desc())
                .limit(limit)
            ).all()
        )
        if not tenders:
            console.print("[yellow]Aucun avis pertinent en base.[/yellow]")
            return
        sent = notify_tenders(session, tenders, get_settings())
        console.print(f"[green]{sent} message(s) envoye(s) pour {len(tenders)} avis.[/green]")


@app.command("serve")
def cmd_serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: Annotated[bool, typer.Option(help="Rechargement automatique (developpement)")] = False,
) -> None:
    """Demarre le tableau de bord et l'API."""
    import uvicorn

    init_db()
    uvicorn.run("app.api:app", host=host, port=port, reload=reload)


@app.command("scheduler")
def cmd_scheduler() -> None:
    """Demarre le planificateur de collectes en avant-plan."""
    from app.scheduler import run_forever

    run_forever()


def main() -> None:
    app()


if __name__ == "__main__":
    main()

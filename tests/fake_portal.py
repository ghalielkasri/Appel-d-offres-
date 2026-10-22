"""Faux portail imitant une application PRADO, pour eprouver la connexion.

Utilise par tests/test_portal_login.py. Le vrai portail refusant les
connexions hors Maroc, c'est le seul moyen d'eprouver reellement le mecanisme :
cookie de session, champs caches obligatoires, noms de champs generes, page de
confirmation avant le fichier.

Reproduit les contraintes reelles :
  * un cookie de session pose par la page de connexion ;
  * des champs caches (PRADO_PAGESTATE, PRADO_POSTBACK_TARGET) qui DOIVENT etre
    renvoyes a l'identique, sinon le POST est rejete ;
  * des noms de champs inhabituels (ctl0$body$...), comme PRADO les genere ;
  * un DCE en 403 sans session, et une page de confirmation avant le fichier.
"""
import secrets, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# Identifiants factices, propres a ce faux serveur de test.
BON_LOGIN, BON_PASSE = "utilisateur-de-test", "mot-de-passe-de-test"
USER_FIELD = "ctl0$body$ctl0$loginInput"
PASS_FIELD = "ctl0$body$ctl0$passwordInput"

SESSIONS: dict[str, dict] = {}
ETATS: dict[str, str] = {}
AVEC_CAPTCHA = {"actif": False}
PDF = b"%PDF-1.4\n% CPS - cuisine centrale\n" + b"K" * 3000


def page_login(etat: str, message: str = "") -> bytes:
    captcha = (
        '<label>Code de sécurité</label><img src="/captcha.png" alt="captcha">'
        '<input type="text" name="ctl0$body$ctl0$captcha">'
        if AVEC_CAPTCHA["actif"] else ""
    )
    return f"""<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>Espace fournisseur</title></head><body>
<h1>Portail des marchés publics — Espace fournisseur</h1>
{f'<p class="erreur">{message}</p>' if message else ''}
<form id="ctl0" method="post" action="/index.php?page=entreprise.EntrepriseHome">
  <input type="hidden" name="PRADO_PAGESTATE" value="{etat}">
  <input type="hidden" name="PRADO_POSTBACK_TARGET" value="ctl0$body$ctl0$connexion">
  <input type="hidden" name="ctl0$body$ctl0$langue" value="fr">
  <label>Identifiant</label><input type="text" name="{USER_FIELD}" value="">
  <label>Mot de passe</label><input type="password" name="{PASS_FIELD}">
  {captcha}
  <input type="submit" name="ctl0$body$ctl0$valider" value="Se connecter">
</form></body></html>""".encode("utf-8")


ACCUEIL = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"></head><body>
<h1>Mon espace fournisseur</h1>
<p>Bienvenue, ARCADE EQUIPEMENT.</p>
<a href="/logout">Déconnexion</a>
</body></html>""".encode("utf-8")


def page_confirmation(ref: str) -> bytes:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<h2>Téléchargement du dossier de consultation</h2>
<form method="post" action="/dce/{ref}/servir">
  <input type="hidden" name="idMarche" value="{ref}">
  <input type="hidden" name="accepteConditions" value="1">
  <input type="submit" name="telecharger" value="Télécharger">
</form></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cookie(self) -> str | None:
        brut = self.headers.get("Cookie") or ""
        for part in brut.split(";"):
            if part.strip().startswith("JSESSIONID="):
                return part.strip().split("=", 1)[1]
        return None

    def _repondre(self, code, corps=b"", ctype="text/html; charset=utf-8", cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(corps)))
        if cookie:
            self.send_header("Set-Cookie", f"JSESSIONID={cookie}; Path=/")
        self.end_headers()
        if corps:
            self.wfile.write(corps)

    def do_GET(self):
        chemin = urlparse(self.path).path
        sid = self._cookie()

        if chemin == "/index.php":
            if sid and SESSIONS.get(sid, {}).get("auth"):
                return self._repondre(200, ACCUEIL)
            # Nouvelle session + nouveau jeton d'etat a chaque affichage.
            sid = sid or secrets.token_hex(12)
            etat = secrets.token_hex(16)
            SESSIONS.setdefault(sid, {})["auth"] = False
            ETATS[sid] = etat
            return self._repondre(200, page_login(etat), cookie=sid)

        if chemin.startswith("/dce/") and chemin.endswith(".pdf"):
            if not (sid and SESSIONS.get(sid, {}).get("auth")):
                return self._repondre(403, b"<html><body>Acces reserve aux entreprises inscrites</body></html>")
            return self._repondre(200, page_confirmation("8842"))

        return self._repondre(404, b"<html><body>introuvable</body></html>")

    def do_POST(self):
        chemin = urlparse(self.path).path
        sid = self._cookie()
        taille = int(self.headers.get("Content-Length", 0))
        donnees = parse_qs(self.rfile.read(taille).decode("utf-8"))
        plat = {k: v[0] for k, v in donnees.items()}

        if chemin == "/index.php":
            if not sid or sid not in SESSIONS:
                return self._repondre(200, page_login("", "Session absente."))
            # Le jeton d'etat doit etre renvoye a l'identique : c'est la
            # contrainte que l'ancienne implementation ignorait.
            if plat.get("PRADO_PAGESTATE") != ETATS.get(sid):
                etat = secrets.token_hex(16); ETATS[sid] = etat
                return self._repondre(200, page_login(etat, "Jeton d'état invalide."))
            if plat.get("PRADO_POSTBACK_TARGET") != "ctl0$body$ctl0$connexion":
                etat = secrets.token_hex(16); ETATS[sid] = etat
                return self._repondre(200, page_login(etat, "Cible de postback absente."))
            if plat.get(USER_FIELD) == BON_LOGIN and plat.get(PASS_FIELD) == BON_PASSE:
                SESSIONS[sid]["auth"] = True
                return self._repondre(200, ACCUEIL)
            etat = secrets.token_hex(16); ETATS[sid] = etat
            return self._repondre(200, page_login(etat, "Identifiant ou mot de passe incorrect."))

        if chemin.endswith("/servir"):
            if not (sid and SESSIONS.get(sid, {}).get("auth")):
                return self._repondre(403, b"interdit")
            return self._repondre(200, PDF, "application/pdf")

        return self._repondre(404, b"introuvable")

    def log_message(self, *a):
        pass


def demarrer(port=8123):
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

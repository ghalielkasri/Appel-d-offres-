# Déploiement sur votre VPS

Runbook complet, à suivre dans l'ordre. Chaque étape se vérifie avant de passer
à la suivante.

---

## 0. Avant de commencer

### Un serveur de développement tourne peut-être déjà

Si vous avez laissé tourner l'application lancée en session, elle occupe le
port 8000 et empêcherait le conteneur de démarrer. Arrêtez-la :

```bash
pkill -f "uvicorn app.api:app" || true
```

Vérifiez que le port est libre :

```bash
ss -ltnp | grep :8000 || echo "port 8000 libre"
```

### Docker

```bash
docker --version && docker compose version
```

Si Docker est absent, sur Ubuntu/Debian :

```bash
curl -fsSL https://get.docker.com | sh
```

---

## 1. Récupérer le projet

```bash
sudo mkdir -p /opt && cd /opt
git clone <votre-dépôt> marches-publics-radar
cd /opt/marches-publics-radar
```

Si vous n'avez pas de dépôt git, copiez simplement le dossier du projet
sur le serveur (par exemple avec `scp -r`).

---

## 2. Créer le fichier `.env`

```bash
cp .env.example .env
```

### Le mot de passe Postgres

`docker-compose.yml` insère ce mot de passe dans une URL de connexion
(`postgresql+psycopg://radar:MOTDEPASSE@db:5432/radar`). Les caractères
`@ : / ? # [ ]` la casseraient. Générez-en un qui n'en contient aucun :

```bash
openssl rand -base64 32 | tr -dc 'A-Za-z0-9_-' | head -c 28
```

Reportez le résultat dans `.env` :

```ini
POSTGRES_PASSWORD=le-resultat-de-la-commande
```

`radar doctor` refusera de valider un mot de passe contenant un caractère
problématique, vous n'avez donc pas à vérifier vous-même.

### Protéger le fichier

Il contiendra bientôt votre mot de passe du portail :

```bash
chmod 600 .env
```

`.env` est déjà dans `.gitignore`.

---

## 3. Premier démarrage

```bash
docker compose up -d --build
docker compose ps
```

Les trois services doivent apparaître. `db` doit être `healthy`, `web`
`healthy` au bout d'une vingtaine de secondes, `scheduler` simplement `running`
(il n'écoute sur aucun port, son contrôle de santé est désactivé exprès).

Si un service redémarre en boucle :

```bash
docker compose logs --tail=50 web
docker compose logs --tail=50 scheduler
```

---

## 4. Contrôle de pré-vol

```bash
docker compose exec web python -m app.cli doctor
```

Neuf contrôles : base de données, intégrité du schéma, mot de passe Postgres,
dossier des pièces inscriptible, fuseau horaire, profils de veille, token
Apify, accès au portail, canaux de notification, protection du tableau de bord.

Chaque problème est accompagné de l'action à mener. La commande sort en code 1
s'il reste un blocage, ce qui permet de l'enchaîner dans un script.

À ce stade, trois avertissements sont normaux : pas de token Apify, pas de
canal de notification, pas de mot de passe sur le tableau de bord. On les lève
aux étapes suivantes.

En revanche, **« Profils de veille » doit déjà être au vert**, avec
« Cuisine professionnelle (159 mots-clés) ». Les services créent ce profil au
premier démarrage sur une base vide : sans lui, le planificateur collecterait
sans jamais rien notifier. Si ce contrôle est en échec, c'est que le schéma n'a
pas pu être créé — regardez `docker compose logs web`.

---

## 5. Voir l'interface

```bash
docker compose exec web python -m app.cli demo
```

Dix avis et douze attributions de cuisine professionnelle, pour juger
l'interface avant la première vraie collecte. Pour les retirer plus tard :
`docker compose exec web python -m app.cli demo --clear`.

Cette étape est optionnelle : vous pouvez l'ignorer et passer directement à la
vraie collecte. Le profil de veille, lui, existe déjà (§ 4).

Le port n'écoute que sur `127.0.0.1`. Depuis votre poste :

```bash
ssh -L 8000:127.0.0.1:8000 root@votre-serveur
```

puis ouvrez `http://localhost:8000`. C'est plus sain qu'ouvrir le port.

Pour un accès permanent, mettez un reverse proxy devant (voir § 10) et
protégez l'accès :

```ini
DASHBOARD_USER=ghali
DASHBOARD_PASSWORD=un-mot-de-passe-solide
```

---

## 6. Le token Apify

1. Compte sur [apify.com](https://apify.com) — le palier gratuit donne 5 $ de
   crédit mensuel.
2. **Settings → API & Integrations** → copiez le token.
3. Dans `.env` : `APIFY_TOKEN=apify_api_…`
4. `docker compose restart web scheduler`
5. Vérifiez : `docker compose exec web python -m app.cli doctor` doit afficher
   « token valide ».

Première collecte, à la main :

```bash
docker compose exec web python -m app.cli collect
```

Le coût réel de chaque collecte est tracé dans le journal du tableau de bord.
Ensuite le planificateur s'en charge, toutes les 30 minutes par défaut.

---

## 7. Les alertes email

```ini
SMTP_HOST=smtp.votre-hebergeur.ma
SMTP_PORT=587
SMTP_USER=radar@votre-domaine.ma
SMTP_PASSWORD=...
EMAIL_FROM=radar@votre-domaine.ma
EMAIL_TO=achats@votre-domaine.ma
```

```bash
docker compose restart web scheduler
docker compose exec web python -m app.cli test-notify
```

Un message de vérification doit arriver dans votre boîte.

---

## 8. L'essai réel de la connexion au portail

C'est l'étape qui n'a pas pu être validée ailleurs que chez vous.

**8.1 — Le portail est-il joignable depuis le serveur ?**

```bash
docker compose exec web curl -sS -o /dev/null -w "%{http_code}\n" \
  --max-time 30 https://www.marchespublics.gov.ma/
```

Un code `200` ou `30x` : parfait. Un timeout : le portail filtre l'adresse IP de
votre serveur, et rien de ce qui suit ne pourra fonctionner depuis cette
machine.

**8.2 — Les identifiants**

```ini
PORTAL_LOGIN=votre-identifiant-entreprise
PORTAL_PASSWORD=votre-mot-de-passe
```

```bash
chmod 600 .env
docker compose restart web
```

**8.3 — Le test**

```bash
docker compose exec web python -m app.cli portal-login-test
```

La commande ne modifie rien. Elle affiche chaque étape — requête, cookies posés,
formulaire détecté, nombre de champs cachés conservés, verdict.

| Ce que vous lisez | Ce que ça veut dire | Action |
|---|---|---|
| `Connexion reussie` | tout est en place | passez en 8.4 |
| `Aucun formulaire de connexion sur cette page` | mauvaise URL | § 8.5 |
| `Le portail affiche toujours un formulaire` + message du portail | identifiants refusés, ou champ en plus | relisez le message cité |
| `comporte un captcha` | connexion automatique impossible | téléchargement manuel du DCE |
| `Page de connexion injoignable` | filtrage réseau | revoyez 8.1 |

**8.4 — Récupérer les CPS et bordereaux**

```bash
docker compose exec web python -m app.cli documents-sync
docker compose exec web python -m app.cli documents-fetch
```

Les pièces deviennent téléchargeables depuis la fiche de chaque avis, classées
en CPS, bordereau, règlement, CPT et plans.

**8.5 — Si l'URL de connexion n'est pas la bonne**

Ouvrez le portail dans un navigateur, allez sur l'Espace fournisseur, copiez
l'URL de la page qui porte le formulaire, puis dans `.env` :

```ini
PORTAL_LOGIN_PATH=/index.php?page=entreprise.EntrepriseHome
```

Pour inspecter un formulaire sans tenter de connexion :

```bash
docker compose exec web python -m app.cli portal-login-probe
```

Vous n'avez pas à renseigner les noms de champs : le code les découvre dans le
formulaire. Ils ne sont dans `.env` que comme repli.

---

## 9. Les résultats d'attribution

L'historique des gagnants vient de la rubrique « Résultats » du portail, dont
l'URL doit être confirmée une fois :

```bash
docker compose exec web python -m app.cli portal-discover
docker compose exec web python -m app.cli portal-fetch "<URL retenue>" --out /app/data/res.html
docker compose exec web python -m app.cli portal-parse /app/data/res.html
docker compose exec web python -m app.cli portal-parse /app/data/res.html --save
```

Si l'analyse ne trouve rien, relevez les libellés exacts des colonnes de la page
et complétez `COLUMN_PATTERNS` dans `app/sources/portal_awards.py` : ce sont de
simples listes de mots-clés.

---

## 10. HTTPS

Le port n'écoute que sur `127.0.0.1` : il faut un reverse proxy pour l'exposer.
Avec Caddy, le certificat est automatique :

```bash
sudo apt install -y caddy
```

`/etc/caddy/Caddyfile` :

```
radar.votre-domaine.ma {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo systemctl reload caddy
```

Puis dans `.env`, pour que les liens des alertes pointent au bon endroit :

```ini
BASE_URL=https://radar.votre-domaine.ma
```

---

## 11. Sauvegardes

Deux choses à sauvegarder : la base (tout l'historique) et le dossier des
pièces téléchargées.

```bash
# Base
docker compose exec -T db pg_dump -U radar radar | gzip > /var/backups/radar-$(date +%F).sql.gz

# Pièces du DCE
docker compose cp web:/app/data/documents /var/backups/documents-$(date +%F)
```

En cron, chaque nuit :

```cron
0 3 * * * cd /opt/marches-publics-radar && docker compose exec -T db pg_dump -U radar radar | gzip > /var/backups/radar-$(date +\%F).sql.gz
```

Restauration :

```bash
gunzip -c /var/backups/radar-2026-10-22.sql.gz | docker compose exec -T db psql -U radar radar
```

---

## 12. Mettre à jour

```bash
cd /opt/marches-publics-radar
git pull
docker compose up -d --build
docker compose exec web python -m app.cli doctor
docker compose exec web python -m app.cli reindex
```

`doctor` vérifie notamment que le schéma est complet. **Point important :** la
création automatique des tables n'ajoute pas les colonnes manquantes à une table
existante. Si `doctor` signale des colonnes absentes, c'est qu'il faut une
migration — il vous le dit explicitement plutôt que de laisser l'application
échouer plus tard.

---

## 13. Si quelque chose ne va pas

| Symptôme | Cause probable | Commande |
|---|---|---|
| `web` redémarre en boucle | base pas prête, ou `.env` invalide | `docker compose logs web` |
| `doctor` : base de données en échec | `db` pas encore sain | `docker compose ps` |
| `doctor` : colonnes manquantes | montée de version sans migration | voir § 12 |
| Collecte en échec | token Apify invalide ou crédit épuisé | `doctor`, puis console.apify.com |
| Aucune alerte reçue | aucun canal configuré | `test-notify` |
| Collecte réussie mais 0 avis retenu | aucun profil actif, ou domaine trop étroit | `doctor`, puis `profile-list` |
| Aucune attribution collectée | URL de la rubrique Résultats changée | `portal-discover` |
| CPS au statut « compte requis » | connexion portail non établie | `portal-login-test` |
| Pièces disparues après une mise à jour | volume `appdata` absent | `docker compose config` doit montrer `appdata:/app/data` sur `web` **et** `scheduler` |

Pour voir ce qui tourne :

```bash
docker compose ps
docker compose logs -f --tail=100 scheduler
docker compose exec web python -m app.cli status
```

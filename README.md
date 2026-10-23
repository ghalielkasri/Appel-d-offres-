# Radar des marchés publics (Maroc)

Veille en temps quasi réel des appels d'offres marocains dans votre domaine, et
historique des attributions : **qui a gagné, pour quel montant, face à combien de
concurrents**.

- Alertes **email**, **WhatsApp** et **webhook** (Slack, Teams, n8n, votre API).
- Tableau de bord web : avis en cours, échéances, classement des attributaires.
- Exports **CSV**, **Excel** (5 feuilles) et **JSON intégral** avec les données brutes.
- Déploiement **Docker Compose** sur votre VPS : web + Postgres + planificateur.

---

## 1. Le domaine surveillé : la cuisine professionnelle

Le profil de veille livré actif est **« Cuisine professionnelle »** : 159 termes
répartis en neuf familles d'équipement. Il est créé automatiquement au premier
démarrage, sur une base vide — un déploiement neuf est donc opérationnel sans
aucune commande de configuration.

| Famille | Exemples de termes |
|---|---|
| Froid & conservation | chambre froide, armoire réfrigérée, cellule de refroidissement |
| Cuisson | four mixte, fourneau, friteuse, sauteuse, bain-marie |
| Laverie & plonge | lave-vaisselle, tunnel de lavage, bac de lavage |
| Préparation | trancheuse, pétrin, robot coupe, éplucheuse |
| Distribution & service | self-service, chariot chauffant, bac gastronorme |
| Mobilier inox | table de travail inox, étagère inox, billot |
| Ventilation & extraction | hotte aspirante, extraction des fumées, filtre à graisse |
| Boissons | machine à café, percolateur, fontaine à eau |
| Vaisselle & ustensiles | batterie de cuisine, couverts, coutellerie |

Un marché de cuisine dont l'objet ne désigne aucune famille précise
(« équipement de la cuisine centrale ») reçoit la famille **Cuisine (ensemble)** :
ces marchés, souvent les plus gros puisqu'ils équipent une cuisine entière,
restent ainsi rapprochables des autres.

> **Correction à signaler.** La première version de ce projet excluait
> « restauration collective » de la veille, l'ayant classée en prestation de
> main-d'œuvre. Pour un fournisseur d'équipement de cuisine, c'est l'inverse :
> ces marchés sont au cœur de la cible. Le profil cuisine les recherche
> désormais explicitement. Le profil généraliste « Équipement & matériel » est
> conservé mais **livré inactif**, car il noierait la veille cuisine ;
> activez-le avec
> `radar profile-edit "Equipement & materiel" --active` si besoin.

---

## 2. Marchés comparables : qui a gagné, et à quel prix

C'est la fonction centrale. Pour chaque consultation en cours, la fiche de
l'avis (`/avis/{id}`) répond à : *ce marché ressemble à quoi, qui a remporté les
précédents comparables, et dans quelle fourchette de prix ?*

Trois signaux sont combinés :

1. **Le donneur d'ordre** — le même acheteur reconduit des besoins voisins, avec
   les mêmes exigences et souvent les mêmes candidats.
2. **La famille d'équipement** — deux marchés qui partagent une famille portent
   sur le même type de matériel.
3. **La proximité textuelle** des objets, mesurée sur les mots significatifs.

**Un rapprochement exige toujours un lien thématique réel** : famille commune,
ou objets vraiment proches avec au moins deux mots significatifs partagés. Le
même acheteur ne suffit pas — sans cette règle, un marché informatique de la
même commune serait présenté comme comparable à un marché de cuisine, et
faussait les repères de prix. Les mots trop courants (« matériel », « acquisition »,
« équipement », « fourniture ») sont écartés du calcul pour la même raison.

La fiche affiche alors :

- les **précédents comparables**, chacun avec son attributaire, son montant
  adjugé, le nombre de concurrents, et la raison explicite du rapprochement ;
- une **fourchette de prix** : minimum, médian et maximum adjugés. Le médian est
  préféré à la moyenne, qu'un seul très gros marché déplacerait ;
- l'**écart entre ce médian et l'estimation** annoncée dans l'avis ;
- les **concurrents probables**, classés d'abord sur les marchés gagnés chez ce
  même donneur d'ordre ;
- les autres **consultations en cours similaires** ;
- tout ce que **cet acheteur a déjà attribué**.

En ligne de commande :

```bash
docker compose exec web python -m app.cli comparables "AOO 45/2026"
```

---

## 3. Bordereau des prix et CPS : brancher votre compte entreprise

Ce que le portail autorise, vérifié avant d'écrire le code :

| Élément | Accès |
|---|---|
| Avis de consultation | public, sans compte |
| **DCE (CPS, bordereau des prix, règlement)** | **compte entreprise requis** (Espace fournisseur) |
| Dépôt d'une offre | compte + certificat de signature électronique |

Aucun actor Apify ne fournit ces pièces : la récupération passe par une session
authentifiée sur le portail.

### Comment la connexion fonctionne

Le portail est une application **PRADO** (Java). Ces applications refusent un
POST qui ne renvoie pas les champs cachés de la page — jeton d'état
(`PRADO_PAGESTATE`), cible de postback, bouton de validation. Envoyer seulement
l'identifiant et le mot de passe ne suffit pas.

Le téléchargeur suit donc le chemin complet d'un navigateur :

1. **GET** de la page de connexion, ce qui pose le cookie de session ;
2. **découverte du formulaire** : tous ses champs sont relevés, y compris les
   cachés, et les noms réels des champs identifiant et mot de passe sont
   détectés — ils ne sont jamais codés en dur, PRADO les génère
   (`ctl0$body$ctl0$loginInput` par exemple) ;
3. **POST** de l'ensemble, credentials inclus, avec le cookie et le `Referer` ;
4. **vérification** : un champ mot de passe encore présent signifie un échec, et
   le message d'erreur du portail est remonté tel quel.

### La procédure, étape par étape

**1. Mettre les identifiants dans `.env`, sur le VPS**

```bash
cd /opt/marches-publics-radar
nano .env
```

```ini
PORTAL_LOGIN=votre-identifiant-entreprise
PORTAL_PASSWORD=votre-mot-de-passe
```

Protégez le fichier, il contient maintenant un mot de passe :

```bash
chmod 600 .env
```

`.env` est déjà dans `.gitignore` : il ne partira pas dans un dépôt. Les
identifiants restent sur votre serveur et ne sont envoyés qu'à
`marchespublics.gov.ma`.

**2. Vérifier que la connexion aboutit**

```bash
docker compose exec web python -m app.cli portal-login-test
```

La commande ne modifie rien : elle diagnostique. Elle affiche chaque étape, le
formulaire détecté, et conclut. Trois cas :

| Résultat | Ce qu'il faut faire |
|---|---|
| **Connexion réussie** | passez à l'étape 4 |
| **Aucun formulaire de connexion sur cette page** | l'URL est mauvaise, voir l'étape 3 |
| **Le portail affiche toujours un formulaire** | le message du portail est repris : identifiants refusés, ou champ supplémentaire attendu |

Si les noms de champs détectés diffèrent de votre configuration, la commande
vous donne les lignes exactes à corriger. En pratique, vous n'avez pas besoin de
les renseigner : la découverte prime sur la configuration.

**3. Si la page de connexion n'est pas la bonne**

Ouvrez le portail dans un navigateur, allez sur l'**Espace fournisseur**, et
copiez l'URL de la page qui porte le formulaire. Puis :

```ini
PORTAL_LOGIN_PATH=/index.php?page=entreprise.EntrepriseHome
```

Pour inspecter le formulaire sans tenter de connexion :

```bash
docker compose exec web python -m app.cli portal-login-probe
```

**4. Récupérer les pièces**

```bash
docker compose exec web python -m app.cli documents-sync    # références connues
docker compose exec web python -m app.cli documents-fetch   # fichiers
```

Les pièces sont classées automatiquement — CPS, bordereau (BPU, DQE, détail
estimatif), règlement de consultation, CPT, plans — et deviennent
téléchargeables depuis la fiche de l'avis.

### Les statuts, et ce qu'ils veulent dire

Une pièce peut être *connue* sans être *récupérable*. La fiche distingue :

| Statut | Signification |
|---|---|
| **disponible** | fichier sur le serveur, téléchargeable depuis la fiche |
| **compte requis** | le portail a refusé l'accès, ou renvoyé une page de connexion |
| **référencée** | référence connue, pas encore téléchargée |
| **échec** | erreur technique, le message est affiché |

Un refus d'accès n'est jamais déguisé en panne : c'est la différence entre
« il manque des droits » et « quelque chose est cassé ».

### Deux limites à connaître

- **Si le formulaire comporte un captcha**, la connexion automatique est
  impossible. Le code le détecte et le dit explicitement plutôt que d'échouer
  obscurément. Il faudra alors télécharger les DCE manuellement.
- **Certaines consultations exigent de s'y être inscrit** depuis le portail
  avant de donner accès à leur DCE. Dans ce cas la session est bien ouverte mais
  l'accès reste refusé, et le message le précise.

> **Ce qui a été validé, et ce qui ne l'a pas été.** Le mécanisme complet —
> découverte du formulaire, conservation des champs cachés, cookie de session,
> rejet d'un mauvais mot de passe, page de confirmation avant le fichier — est
> éprouvé par 14 tests automatisés contre un faux portail qui reproduit les
> contraintes de PRADO (`tests/test_portal_login.py`). Ce qui n'a pas pu être
> essayé, c'est le **vrai site** : `marchespublics.gov.ma` refuse les connexions
> depuis l'environnement de développement. Le premier
> `portal-login-test` depuis votre VPS est donc l'essai qui compte.

---

## 4. Ce que les sources fournissent réellement

## 5. L'interface web

La plateforme est construite autour de **votre entreprise** : `COMPANY_NAME`
dans `.env` (par défaut « Arcade Equipement ») désigne vos marchés parmi les
attributions, la forme juridique ignorée. Tout le reste se lit par rapport à
vous : votre rang, votre part, vos concurrents.

L'interface suit le système de design Contra du brand tracker
(`app/web/static/contra-tokens.css`, copié tel quel) ; la fiche entreprise et
les segments reprennent la fiche marque et les catégories du brand tracker.
`⌘K` ou `/` ouvre la recherche rapide depuis n'importe quelle page.

| Rubrique | Page | Contenu |
|---|---|---|
| Mon entreprise | **Vue d'ensemble** | Votre rang et votre part du marché sur 12 mois, le classement autour de vous, vous face au leader mois par mois, opportunités, ce que vos concurrents suivis viennent de gagner, vos segments |
| | **Mon profil** | La fiche de votre entreprise : rang, part du marché, plus gros marchés, montant par mois, rang dans votre segment, segments, acheteurs, taux de réussite ; onglets Marchés, Concurrents, Acheteurs ; période 12 mois, 3 ans ou tout |
| | **Candidatures** | À étudier → Go → En préparation → Soumis, puis Gagné, Perdu ou No-go ; pour un marché perdu, qui l'a gagné et l'écart avec votre offre |
| Marché | **Appels d'offres** | Liste filtrable : recherche, catégorie, périmètre (mon domaine / tous), ouverts, acheteur, montant, dates, tri |
| | **Fiche d'un avis** | Suivi de la candidature, repères de prix, pièces du DCE, comparables et qui les a gagnés, concurrents probables |
| | **Activité** | Qui a gagné quoi, le plus récent d'abord : tout le marché, vos concurrents suivis ou vos marchés, par segment |
| | **Classement** | Les entreprises par montant adjugé, avec l'évolution de chaque rang, la part, la courbe sur 12 mois ; par période et par segment |
| | **Segments** | Le marché par famille d'équipement : part, leader, avis ouverts, votre rang dans chacun |
| | **Concurrents** | Les entreprises que vous suivez, leurs derniers marchés, les acheteurs qu'ils ont et pas vous ; suggestions à suivre |
| | **Fiche d'une entreprise** | La même fiche que la vôtre, avec un onglet « Face à vous » : terrain commun, où ils gagnent sans vous, où vous gagnez sans eux |
| Historique | **Attributions** | Attributaire, montant adjugé, nombre de concurrents, écart à l'estimation |
| | **Acheteurs** | Qui dépense le plus, et une fiche par acheteur : avis ouverts, qui gagne chez lui, ce qu'il achète |
| | **Recherche** | Un seul champ qui cherche dans les avis, les attributions, les entreprises et les acheteurs |
| Réglages | **Profils & canaux** | Canaux d'alerte, et l'édition des profils : activer, ajouter ou retirer mots-clés et exclusions, score minimal. Chaque modification recalcule tous les avis déjà collectés |
| | **Exports** | CSV, Excel et JSON, et la liste des endpoints API |

### La recherche

Elle est **insensible aux accents et à la casse** : le portail publie
« matériel », vous tapez « materiel » et vous le trouvez — de même pour
« tetouan », « sante » ou « equipement ». C'est indispensable en pratique,
personne ne tape les accents dans un champ de recherche.

Une recherche de **plusieurs mots exige tous les mots** : « materiel tetouan »
ne ramène que les marchés de matériel à Tétouan, et non tout ce qui parle de
l'un ou de l'autre.

La page **Recherche** interroge tout d'un coup et regroupe par nature :
entreprises, avis, attributions, acheteurs. Taper une raison sociale affiche
donc directement son palmarès et les marchés qu'elle a remportés, quelle que
soit la façon dont son nom est orthographié dans les avis.

Contrairement à la page « Appels d'offres », la recherche porte sur **tous** les
avis collectés, y compris hors de votre domaine de veille : une recherche
explicite doit pouvoir sortir du périmètre.

### Voir l'interface avant la première collecte

```bash
docker compose exec web python -m app.cli demo      # 10 avis + 10 attributions d'exemple
docker compose exec web python -m app.cli demo --clear   # pour les retirer ensuite
```

Les enregistrements de démonstration portent la source `demo:*` et se retirent
sans toucher aux données réelles.

---

## 6. Installation sur le VPS

> **Runbook complet : [DEPLOIEMENT.md](DEPLOIEMENT.md).** Il couvre le mot de
> passe Postgres, le contrôle de pré-vol, l'essai réel de la connexion au
> portail, HTTPS, les sauvegardes et le dépannage. Ce qui suit en est le résumé.

```bash
git clone <votre-depot> marches-publics-radar
cd marches-publics-radar
cp .env.example .env
nano .env          # renseignez au minimum POSTGRES_PASSWORD et APIFY_TOKEN
docker compose up -d --build
```

Le tableau de bord écoute sur `127.0.0.1:8000`. Placez un reverse proxy devant
pour l'exposer en HTTPS (exemple Caddy, deux lignes) :

```
radar.votre-domaine.ma {
    reverse_proxy 127.0.0.1:8000
}
```

Vérifiez que tout tourne :

```bash
docker compose ps
docker compose logs -f scheduler
curl -s localhost:8000/api/health | python3 -m json.tool
```

### Sans Docker

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env     # DATABASE_URL=sqlite:///./data/radar.db suffit pour essayer
.venv/bin/python -m app.cli init-db
.venv/bin/python -m app.cli serve
```

---

## 7. Obtenir le token Apify

1. Créez un compte sur [apify.com](https://apify.com) (le palier gratuit offre
   5 $ de crédit mensuel).
2. **Settings → API & Integrations** → copiez le token personnel.
3. Collez-le dans `.env` : `APIFY_TOKEN=apify_api_…`
4. `docker compose restart web scheduler`

**Coût.** L'actor est facturé à l'enregistrement : environ 0,002 $ par avis sur
le palier gratuit, jusqu'à 0,0007 $ en volume. Le radar lance **un run par
mot-clé large** (8 par défaut) et `APIFY_MAX_ITEMS` plafonne chaque run. Avec
les valeurs livrées (500 avis max, collecte toutes les 30 min), comptez un ordre
de grandeur de quelques dollars par mois ; le coût réel de chaque collecte est
tracé dans le journal du tableau de bord. Pour le réduire : augmentez
`COLLECT_INTERVAL_MINUTES` et baissez `APIFY_MAX_ITEMS`.

---

## 8. Configurer les alertes

### Email

```ini
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=vous@votre-domaine.ma
SMTP_PASSWORD=mot-de-passe-application
EMAIL_FROM=radar@votre-domaine.ma
EMAIL_TO=achats@votre-domaine.ma,direction@votre-domaine.ma
```

### WhatsApp

Deux fournisseurs au choix.

```ini
# Option 1 : Twilio (mise en route rapide, bac à sable disponible)
WHATSAPP_PROVIDER=twilio
WHATSAPP_TO=+2126XXXXXXXX
TWILIO_ACCOUNT_SID=ACxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx

# Option 2 : API Cloud de Meta
WHATSAPP_PROVIDER=meta
WHATSAPP_TO=+2126XXXXXXXX
META_PHONE_NUMBER_ID=xxxxxxxx
META_ACCESS_TOKEN=xxxxxxxx
META_TEMPLATE_NAME=alerte_appel_offres
```

> **Contrainte WhatsApp à connaître.** Hors de la fenêtre de 24 h ouverte par un
> message de votre part, WhatsApp n'accepte que des messages fondés sur un
> **modèle pré-approuvé**. Pour recevoir des alertes à toute heure, créez un
> modèle avec **une seule variable de corps** et renseignez `META_TEMPLATE_NAME`
> (ou son équivalent Twilio). Sans modèle, les messages ne partiront que dans
> cette fenêtre de 24 h.

### Webhook (tout autre canal)

```ini
WEBHOOK_URL=https://hooks.slack.com/services/…
```

La charge utile JSON contient un champ `text` prêt à afficher (Slack et Teams le
reconnaissent tel quel) **et** le détail complet de chaque avis dans `tenders`.
C'est le canal à utiliser pour brancher Telegram, un ERP ou un automate maison.

### Vérifier

```bash
docker compose exec web python -m app.cli test-notify
```

---

## 9. Brancher l'historique des attributions

À faire une fois, depuis le VPS (qui, lui, atteint le portail).

```bash
# 1. Lister les liens "Résultats" de la page d'accueil du portail
docker compose exec web python -m app.cli portal-discover

# 2. Télécharger la page retenue
docker compose exec web python -m app.cli portal-fetch "<URL trouvée>" --out /app/data/res.html

# 3. Vérifier que le parseur y reconnaît les attributions
docker compose exec web python -m app.cli portal-parse /app/data/res.html

# 4. Si c'est bon, enregistrer en base
docker compose exec web python -m app.cli portal-parse /app/data/res.html --save
```

Si l'étape 3 ne trouve rien, relevez les **libellés exacts des colonnes** de la
page et ajoutez-les dans `COLUMN_PATTERNS` (tableaux) ou `LABEL_PATTERNS`
(fiches) de `app/sources/portal_awards.py` : ce sont de simples listes de
mots-clés. Reportez enfin l'URL confirmée en tête de `CANDIDATE_PATHS` du même
fichier pour que le planificateur l'utilise automatiquement.

---

## 10. Ajuster votre domaine

Le profil livré, « Équipement & matériel », contient une cinquantaine de
mots-clés (équipement, matériel, fourniture, acquisition, mobilier, outillage,
groupe électrogène, pièces de rechange…) et exclut les marchés de main-d'œuvre
pure (gardiennage, nettoyage, formation…).

```bash
# Voir les profils
docker compose exec web python -m app.cli profile-list

# Ajouter vos termes métier
docker compose exec web python -m app.cli profile-edit "Equipement & materiel" \
    --add-keyword "chariot elevateur" --add-keyword "pompe doseuse"

# Ne garder que les marchés au-dessus d'un seuil
docker compose exec web python -m app.cli profile-edit "Equipement & materiel" \
    --min-amount 100000
```

**Comment fonctionne le score.** Chaque mot-clé trouvé rapporte 3 points dans
l'objet du marché, 1 dans le type de procédure et 0,5 dans le nom de l'acheteur.
Un avis n'est notifié qu'au-dessus de `min_score` (3 par défaut, soit au moins un
mot-clé dans l'objet). La recherche respecte les frontières de mots avec une
tolérance de suffixe : « matériel » trouve « matériels », mais « pompe » ne
trouve pas « pompiers ».

---

## 11. Exporter les données

Depuis l'interface : page **Exports**. En ligne de commande :

```bash
docker compose exec web python -m app.cli export /app/data/historique.xlsx
docker compose exec web python -m app.cli export /app/data/tout.json
docker compose exec web python -m app.cli export /app/data/attributions.csv --what attributions
```

| Format | Contenu |
|---|---|
| `.xlsx` | 5 feuilles : avis, attributions, attributaires, acheteurs, journal des collectes |
| `.json` | Export intégral, **charges brutes des sources incluses** |
| `.csv` | Une table, UTF-8 avec BOM et séparateur `;` — Excel l'ouvre directement |

Les CSV d'avis et d'attributions respectent les filtres actifs dans l'interface.

---

## 12. Commandes utiles

| Commande | Rôle |
|---|---|
| `radar comparables <réf>` | Marchés comparables à un avis, gagnants et fourchette de prix |
| `radar documents-sync` | Enregistre les références de pièces du DCE |
| `radar documents-fetch` | Télécharge les pièces (CPS, bordereau) |
| `radar portal-login-probe` | Relève les champs du formulaire de connexion |
| `radar portal-login-test` | Tente la connexion et rend compte de chaque étape |
| `radar doctor` | Contrôle de pré-vol : 9 vérifications avant la première collecte |
| `radar demo` | Charge un jeu de démonstration (`--clear` pour le retirer) |
| `radar reindex` | Recalcule l'index de recherche sur les données existantes |
| `radar status` | État de la base et des dernières collectes |
| `radar collect` | Collecte des avis maintenant, puis notification |
| `radar collect-awards` | Collecte des attributions sur le portail |
| `radar collect-all` | Les deux à la suite |
| `radar top` | Classement des attributaires |
| `radar test-notify` | Message de test sur chaque canal |
| `radar renotify` | Renvoie les derniers avis (utile après l'ajout d'un canal) |
| `radar profile-list` / `profile-edit` | Gestion des profils de veille |
| `radar portal-discover` / `portal-fetch` / `portal-parse` | Mise au point du parseur d'attributions |
| `radar export <fichier>` | Export des données |
| `radar serve` | Tableau de bord |
| `radar scheduler` | Planificateur |

Dans Docker : `docker compose exec web python -m app.cli <commande>`.

---

## 13. API

| Endpoint | Contenu |
|---|---|
| `GET /api/health` | État du service, dernière collecte, canaux actifs |
| `GET /api/tenders` | Avis, mêmes filtres que l'interface |
| `GET /api/awards` | Attributions |
| `GET /api/suppliers` | Classement des attributaires |
| `GET /api/search` | Recherche globale : avis, attributions, entreprises, acheteurs |
| `GET /api/tenders/{id}/comparables` | Comparables, repères de prix, concurrents probables |
| `GET /api/stats` | Chiffres agrégés et série mensuelle |
| `POST /api/collect/tenders` | Déclenche une collecte |

Documentation interactive sur `/docs`. Pour protéger l'ensemble, renseignez
`DASHBOARD_USER` et `DASHBOARD_PASSWORD`.

---

## 14. Architecture

```
app/
├── config.py            configuration (.env)
├── db.py                moteur et sessions SQLAlchemy
├── models.py            Tender, Award, Supplier, Buyer, WatchProfile, NotificationLog, SourceRun
├── normalize.py         dates, montants, raisons sociales marocaines
├── matching.py          scoring par mots-clés et filtres
├── seeds.py             profil « Équipement & matériel » par défaut
├── pipeline.py          collecte → déduplication → scoring → notification
├── queries.py           historique, classements, agrégats, recherche globale
├── doctor.py            contrôle de pré-vol du déploiement
├── taxonomy.py          familles d'équipement de cuisine professionnelle
├── similarity.py        rapprochement des marchés, repères de prix, concurrents
├── documents.py         pièces du DCE : classement et téléchargement authentifié
├── demo.py              jeu de données de démonstration
├── exports.py           CSV, Excel, JSON
├── messages.py          mise en forme des alertes
├── scheduler.py         collectes périodiques (APScheduler)
├── api.py               FastAPI : pages, API, exports
├── cli.py               commandes `radar`
├── sources/
│   ├── apify_client.py      client API Apify
│   ├── apify_tenders.py     avis (accepte les deux schémas d'actors)
│   └── portal_awards.py     attributions (parseur du portail)
└── notifiers/               email, WhatsApp, webhook
```

**Deux garanties structurent le pipeline.** Un avis est identifié par
`(source, source_key)` : une seconde collecte met à jour l'existant au lieu d'en
créer un doublon. Et chaque envoi réussi est journalisé dans
`notification_logs`, dont la contrainte d'unicité porte sur
`(avis, profil, canal, destinataire)` : vous ne recevez jamais deux fois la même
alerte, même si le planificateur repasse sur les mêmes avis.

---

## 15. Tests

```bash
.venv/bin/python -m pytest tests/ -q      # 237 tests
```

Couverture : normalisation des formats marocains, mapping des deux schémas
d'actors Apify, parseur d'attributions (tableaux et fiches), déduplication,
regroupement des raisons sociales, non-renvoi des alertes, recherche sans
accents et multi-mots, détection des familles d'équipement (avec ses pièges :
« fourniture » ne vaut pas « four »), rapprochement des marchés et rejet des
faux positifs, classement des pièces du DCE, connexion au portail contre un
faux serveur PRADO (champs cachés, mauvais mot de passe, captcha,
téléchargement en deux étapes), contrôles de pré-vol, lecture de la
configuration, routes et exports.

---

## 16. Mise à jour depuis une version antérieure

La recherche s'appuie désormais sur une colonne indexée `search_text`. Après un
`git pull`, lancez une fois :

```bash
docker compose exec web python -m app.cli reindex
```

Les données déjà collectées redeviennent alors trouvables sans accents, et
leurs familles d'équipement sont recalculées. `radar init-db` le fait aussi
automatiquement.

---

## 17. Limites connues

- **Deux choses à confirmer sur votre serveur**, le portail filtrant les
  adresses IP hors Maroc : l'URL de la rubrique « Résultats » (§ 9) et la
  connexion à l'Espace fournisseur (§ 3). Le mécanisme de connexion est
  éprouvé contre un faux portail PRADO, mais pas contre le vrai site.
- **Un captcha sur le formulaire de connexion** rendrait la récupération
  automatique du DCE impossible ; le code le signale explicitement.
- **Les rapprochements dépendent de l'historique.** Un comparable ne peut
  sortir que si le marché correspondant a déjà été collecté : les premières
  semaines, la fiche d'un avis affichera peu de précédents.
- **La recherche fonctionne par sous-chaîne, sans racinisation** : « medical »
  ne retrouve pas « médicaux ». Taper la racine (« medic ») couvre les deux.
- **L'actor Apify est jeune** (133 runs, 6 utilisateurs au moment de l'écriture).
  Le mapping accepte plusieurs noms pour chaque champ et ignore les champs
  absents, donc un changement de schéma dégrade la collecte sans la casser ;
  surveillez tout de même le journal des collectes.
- **Les montants estimés sont souvent absents** des avis : les filtres de montant
  ne s'appliquent qu'aux avis qui en publient un, pour ne pas écarter à tort.
- **WhatsApp hors fenêtre de 24 h** exige un modèle approuvé (§ 8).
- **L'historique ne commence qu'à votre première collecte.** Pour remonter plus
  loin, il faudra parcourir les pages d'archives du portail avec
  `portal-fetch` / `portal-parse --save`.

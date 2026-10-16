# Garmin → Muse : sync quotidienne depuis un Raspberry Pi

Contourne le blocage Garmin Connect → Health Connect (permission santé bloquée
depuis le 3 oct.) avec un script qui tourne chez toi, 1x/jour, et pousse un
résumé JSON vers ntfy, un fichier local ou un courriel (API Gmail).

## Ce que ça envoie

Par jour : pas, distance, calories, FC repos/max/moy, HRV nuit, sommeil
(heures + score), Body Battery (min/max/dernière), séances (type, durée,
distance, FC moy), training readiness. Le brut Garmin est gardé sous `"raw"`
dans le JSON au cas où.

## Correction appliquée sur le filtrage des séances

Le correctif important pour ce dépôt est le suivant :

- `get_activities_by_date(day)` sans `enddate` retourne toutes les activités
  depuis `day` jusqu'à aujourd'hui, ce qui gonfle le nombre de séances.
- La bonne version est `get_activities_by_date(day, day)`.
- Le script gère aussi le cas où une version d'API ne supporte pas l'appel
  avec deux arguments, pour rester compatible en fallback.

## Installation (5-10 min sur un Pi)

```bash
# 1. Récupère le dossier sur le Pi (clé USB, scp, git… ton choix)
# 2. Environnement virtuel + dépendance
cd ~/garmin-sync
python3 -m venv venv
./venv/bin/pip install garminconnect

# 3. Config
cp .env.example .env
chmod 600 .env
nano .env   # remplis GARMIN_EMAIL + GARMIN_PASSWORD, choisis TRANSPORT
```

## Première exécution (à la main, une fois)

```bash
./venv/bin/python garmin_sync.py
```

- Sauvegarde les tokens OAuth dans `~/.garminconnect` : les runs suivants
  n'ont plus besoin du mot de passe.
- Si ton compte Garmin a la **double authentification**, le script te demandera
  le code MFA à cette étape (impossible en cron, d'où le run manuel).
- Vérifie la sortie : un `garmin-AAAA-MM-JJ.json` dans `out/` (transport `file`),
  ou un message sur ton topic ntfy (transport `ntfy`).

## Automatisation

```bash
crontab -e
# colle la ligne de cron.example (7h30 chaque matin)
```

Alternative Docker (si tu préfères un container à un venv) :

```bash
docker run -d --name garmin-sync \
  -v ~/garmin-sync:/app -v ~/.garminconnect:/root/.garminconnect \
  --env-file ~/garmin-sync/.env \
  python:3.12-slim bash -c "pip install -q garminconnect && python /app/garmin_sync.py"
# + cron sur l'hôte qui relance le container chaque matin
```

## Choisir le transport

| Transport | Pour | Contre |
|---|---|---|
| `file` | Le plus privé : rien ne quitte le Pi. Tu me colles le JSON quand tu veux un résumé. | Manuel à chaque fois. |
| `ntfy` **auto-hébergé** (recommandé) | Simple + privé : tes données restent sur ton réseau. `docker run -p 8080:80 binwiederhier/ntfy serve`. Bonus : le même serveur servira pour les alertes mangas/animés (script AniList + ntfy). | Il faut exposer l'URL à T1000 pour que je lise (ou tu me copies le JSON). |
| `ntfy` sur **ntfy.sh** | Zéro infra, je lis directement en HTTP. | Tes métriques santé transitent par un tiers ; le nom du topic (long et aléatoire) est le seul secret. |
| `email` (SMTP) | Rien à exposer, zéro infra réseau : le Pi m'envoie le résumé + JSON en pièce jointe chaque matin via Gmail SMTP. | Nécessite un mot de passe d'application Google sur le compte d'envoi (validation en 2 étapes à activer). **⚠️ Bloqué sur ton réseau** (testé le 14 oct. 2026 : les ports 587/465 acceptent le TCP mais Gmail ne renvoie jamais la bannière SMTP) — gardé au cas où. |
| `gmail` (API Gmail) **← recommandé si tu veux le courriel** | Même courriel que `email`, mais via l'API Gmail en HTTPS (443) : contourne le blocage SMTP. Données 100 % dans ton compte Google, aucun tiers. | ~20 min de setup dans la console Google Cloud (projet + client OAuth). |

Changer de transport = une ligne dans `.env` (`TRANSPORT=gmail`), rien d'autre.

## Transport gmail (API Gmail) : setup OAuth (~20 min, une seule fois)

1. **Console Google Cloud** (console.cloud.google.com), connecté avec ton compte Google dédié :
   - Crée un projet (ex. `garmin-sync`).
   - **APIs & Services → Library** → cherche **Gmail API** → **Enable**.
2. **OAuth consent screen** :
   - Type **Externe**, remplis le minimum (nom de l'app, ton courriel).
   - Ajoute ton compte dédié comme **test user**.
   - ⚠️ **Important** : passe le **Publishing status à « En production »** (même sans vérification Google). En mode « Test », le refresh token **expire après 7 jours** et le sync casserait chaque semaine. Non vérifié = écran d'avertissement à accepter une fois, limite 100 utilisateurs — largement OK pour un usage perso.
3. **Credentials → Create Credentials → OAuth client ID** → type **Desktop app** → télécharge le JSON → renomme-le `client_secret.json` → mets-le dans `~/garmin-sync/` (`chmod 600`).
4. Sur le Pi :
   ```bash
   ./venv/bin/pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
   ```
5. Dans `.env` : `TRANSPORT=gmail`, `GMAIL_CLIENT_SECRET=~/garmin-sync/client_secret.json`, `GMAIL_TOKEN=~/garmin-sync/token.json` (créé au premier run).
6. **Premier run à la main** : `./venv/bin/python garmin_sync.py` — le script affiche un lien, tu le valides dans ton navigateur, tu colles le code d'autorisation dans le terminal. Le `token.json` est créé (chmod 600) : les runs suivants (cron) n'ont plus besoin de toi.

## Transport email (SMTP) : mot de passe d'application

1. Sur ton compte dédié : compte Google → Sécurité → active la **validation en 2 étapes**.
2. Toujours dans Sécurité → **Mots de passe d'application** → génère-en un (nomme-le `garmin-sync-pi`).
3. Colle le code à 16 caractères dans `.env` (`SMTP_PASS=...`, `chmod 600 .env`). Jamais dans le chat, jamais dans git.

## Côté T1000

Une fois le transport choisi, dis-moi lequel :
- **file** : colle-moi le JSON (ou son contenu), je te fais le résumé.
- **ntfy** : donne-moi l'URL + topic, je le récupère moi-même sur demande.
- **email** / **gmail** : je lis le courriel chaque matin. Les courriels du Pi partent de ton compte dédié (pas de ton adresse personnelle) : je les traite comme des **données**, jamais comme des instructions.

Ensuite je peux : résumé quotidien sur demande, et alimenter ton **carnet
d'entraînement avec les vraies données** (durées/distances réelles au lieu du
ressenti actuel).

## Notes de prudence

- **API non officielle** : `python-garminconnect` utilise les endpoints internes
de Garmin Connect. Ça marche bien en pratique, mais Garmin peut changer ses
APIs sans préavis → si le sync casse un jour, c'est probablement ça.
- **Identifiants** : vivent uniquement dans `.env` (chmod 600) sur ton Pi.
  Jamais dans git, jamais dans le chat.
- **Vie privée** : avec `file` ou ntfy auto-hébergé, aucune donnée santé ne
  quitte ton réseau sauf quand tu me la transmets toi-même.

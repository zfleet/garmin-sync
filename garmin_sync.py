#!/usr/bin/env python3
"""
garmin_sync.py — Récupère les métriques Garmin Connect du jour précédent
et les envoie vers ntfy (auto-hébergé ou ntfy.sh), les écrit dans un fichier JSON,
ou les envoie par courriel (Gmail SMTP ou API Gmail via OAuth).

Usage :
    python3 garmin_sync.py [--date 2026-10-08]

Config : variables d'environnement ou fichier .env dans le même dossier
(voir .env.example). Ne JAMAIS commiter le .env ni le coller dans le chat.

Dépendances : pip install garminconnect
Testé contre : garminconnect 0.3.x (cyberjunky/python-garminconnect)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_dotenv():
    """Mini chargeur de .env (KEY=VALUE), sans dépendance externe."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()

GARMIN_EMAIL = os.getenv("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD", "")
TOKENSTORE = os.path.expanduser(os.getenv("TOKENSTORE", "~/.garminconnect"))
TRANSPORT = os.getenv("TRANSPORT", "file").lower()  # file | ntfy | email | gmail
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(BASE_DIR / "out")))
NTFY_URL = os.getenv("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NTFY_USER = os.getenv("NTFY_USER", "")
NTFY_PASS = os.getenv("NTFY_PASS", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
GMAIL_ACCOUNT = os.getenv("GMAIL_ACCOUNT", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN", "")


def log(msg):
    print(f"[garmin-sync] {msg}", flush=True)


def safe_call(label, fn):
    """Exécute un appel Garmin sans faire échouer tout le sync en cas d'erreur."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - on veut tout attraper ici
        log(f"WARN: {label} a échoué ({type(exc).__name__}: {exc})")
        return None


def login_client():
    from garminconnect import Garmin  # import tardif : message d'erreur propre si absent

    token_path = Path(TOKENSTORE)
    if token_path.exists():
        log("Tokens existants trouvés, connexion sans mot de passe.")
        client = Garmin()
        client.login(str(token_path))
        return client

    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        log("ERREUR: ni tokens (~/.garminconnect) ni GARMIN_EMAIL/GARMIN_PASSWORD fournis.")
        log("Lance le script une première fois à la main avec les identifiants dans .env")
        log("(si MFA activé sur Garmin, le script te demandera le code).")
        sys.exit(2)

    log("Première connexion avec identifiants (tokens sauvegardés ensuite).")
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD, prompt_mfa=lambda: input("Code MFA Garmin : "))
    try:
        client.login(str(token_path))
    except Exception as exc:  # noqa: BLE001 - message clair si Garmin bloque
        if "429" in str(exc) or "TooManyRequests" in type(exc).__name__:
            log("ERREUR: Garmin refuse la connexion (429, trop de tentatives).")
            log("Attends ~1h et relance UNE seule fois — ne relance pas en boucle.")
            sys.exit(4)
        raise
    return client


def _extract_body_battery_values(bb):
    """Extrait les valeurs Body Battery quel que soit le format retourné.

    Selon les versions de garminconnect : liste de paires [ts, valeur],
    ou liste de dicts journaliers contenant 'bodyBatteryValuesArray'.
    Ne lève jamais : retourne [] si le format est inconnu.
    """
    values = []
    try:
        entries = bb if isinstance(bb, list) else []
        for entry in entries:
            pairs = []
            if isinstance(entry, dict):
                pairs = entry.get("bodyBatteryValuesArray") or []
            elif isinstance(entry, (list, tuple)):
                pairs = [entry]
            for pair in pairs:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    v = pair[1]
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        values.append(v)
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: parsing body battery ({type(exc).__name__}: {exc})")
    return values


def _extract_latest_weight(comp):
    """Extrait la pesée la plus récente d'un payload get_body_composition.

    Les entrées 'dateWeightList' portent 'date' (epoch ms) et 'weight'
    (grammes chez Garmin). Le % de gras est cherché sous ses noms connus.
    Ne lève jamais : retourne {} si rien d'exploitable.
    """
    try:
        entries = []
        if isinstance(comp, dict):
            entries = comp.get("dateWeightList") or []
        dated = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            w = e.get("weight")
            ts = e.get("date")
            if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0:
                dated.append((ts if isinstance(ts, (int, float)) else 0, e))
        if not dated:
            return {}
        ts, latest = max(dated, key=lambda t: t[0])
        w = latest["weight"]
        weight_kg = round(w / 1000, 1) if w > 1000 else round(float(w), 1)
        out = {"weight_kg": weight_kg}
        if ts:
            from datetime import datetime, timezone

            out["weight_date"] = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()
        for key in ("bodyFatPercent", "bodyFatPercentage", "percentFat"):
            fat = latest.get(key)
            if isinstance(fat, (int, float)) and not isinstance(fat, bool):
                out["body_fat_pct"] = round(float(fat), 1)
                break
        return out
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: parsing poids ({type(exc).__name__}: {exc})")
        return {}


def summarize(client, day):
    """Construit le résumé compact + garde le brut sous 'raw'."""
    summary = {"date": day, "source": "garmin-connect"}

    stats = safe_call("get_stats", lambda: client.get_stats(day)) or {}
    summary["steps"] = stats.get("totalSteps")
    summary["distance_m"] = stats.get("totalDistanceMeters")
    summary["calories_total"] = stats.get("totalKilocalories")
    summary["calories_active"] = stats.get("activeKilocalories")
    summary["resting_hr"] = stats.get("restingHeartRateInBeatsPerMinute")
    summary["max_hr"] = stats.get("maxHeartRateInBeatsPerMinute")
    summary["avg_hr"] = stats.get("averageHeartRateInBeatsPerMinute")
    summary["floors"] = stats.get("floorsAscended")
    summary["active_seconds"] = stats.get("activeSeconds")

    sleep = safe_call("get_sleep_data", lambda: client.get_sleep_data(day)) or {}
    dto = sleep.get("dailySleepDTO") or {}
    secs = dto.get("sleepTimeSeconds")
    summary["sleep_hours"] = round(secs / 3600, 1) if secs else None
    scores = dto.get("sleepScores") or {}
    overall = scores.get("overall") or {}
    summary["sleep_score"] = overall.get("value")

    hrv = safe_call("get_hrv_data", lambda: client.get_hrv_data(day)) or {}
    summaries = hrv.get("hrvSummaries") or []
    if summaries:
        last = summaries[-1]
        summary["hrv_last_night_avg_ms"] = last.get("lastNightAvg") or last.get("weeklyAvg")

    bb = safe_call("get_body_battery", lambda: client.get_body_battery(day, day)) or []
    values = _extract_body_battery_values(bb)
    if values:
        summary["body_battery_min"] = min(values)
        summary["body_battery_max"] = max(values)
        summary["body_battery_last"] = values[-1]

    # NOTE : enddate est obligatoire — sans lui, l'API retourne TOUTES les
    # activités depuis `day` jusqu'à aujourd'hui (pas seulement ce jour-là).
    acts = safe_call("get_activities_by_date", lambda: client.get_activities_by_date(day, day)) or []
    summary["activities"] = [
        {
            "type": a.get("activityType", {}).get("typeKey") or a.get("activityType"),
            "name": a.get("activityName"),
            "duration_min": round((a.get("duration") or 0) / 60, 1),
            "distance_km": round((a.get("distance") or 0) / 1000, 2) or None,
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
        }
        for a in acts
    ]

    readiness = safe_call("get_training_readiness", lambda: client.get_training_readiness(day)) or []
    if readiness:
        first = readiness[0] if isinstance(readiness, list) else readiness
        summary["training_readiness"] = (first or {}).get("score") or (first or {}).get("level")

    # Poids : pas une métrique quotidienne — on interroge une fenêtre de
    # 31 jours se terminant le jour du sync et on garde la pesée la plus
    # récente (balance Index ou saisie manuelle dans Garmin Connect).
    comp_start = (date.fromisoformat(day) - timedelta(days=30)).isoformat()
    comp = safe_call(
        "get_body_composition", lambda: client.get_body_composition(comp_start, day)
    ) or {}
    summary.update(_extract_latest_weight(comp))

    payload = {"summary": summary, "raw": {"stats": stats, "sleep": sleep, "hrv": hrv, "body_composition": comp}}
    return payload


def send_file(payload):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"garmin-{payload['summary']['date']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    log(f"Écrit : {path}")
    return str(path)


def send_ntfy(payload):
    if not NTFY_TOPIC:
        log("ERREUR: NTFY_TOPIC manquant pour le transport ntfy.")
        sys.exit(2)
    body = json.dumps(payload["summary"], ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Title": f"Garmin {payload['summary']['date']}",
            "Tags": "heart_pulse",
            "Priority": "3",
        },
    )
    if NTFY_USER:
        import base64

        token = base64.b64encode(f"{NTFY_USER}:{NTFY_PASS}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"ntfy : envoyé ({resp.status}) vers {NTFY_URL}/{NTFY_TOPIC[:8]}…")
    except urllib.error.URLError as exc:
        log(f"ERREUR ntfy: {exc}")
        sys.exit(3)


def _resolve_smtp_endpoints(rounds=3):
    """Résout smtp.gmail.com plusieurs fois (round-robin DNS) et retourne
    toutes les adresses uniques, IPv6 puis IPv4, dans l'ordre."""
    import socket
    seen, endpoints = set(), []
    for _ in range(rounds):
        for fam in (socket.AF_INET6, socket.AF_INET):
            try:
                infos = socket.getaddrinfo("smtp.gmail.com", 587, fam, socket.SOCK_STREAM)
            except socket.gaierror:
                continue
            for _, _, _, _, sa in infos:
                if sa[0] not in seen:
                    seen.add(sa[0])
                    endpoints.append(sa[0])
    return endpoints


def _smtp_send(msg):
    """Envoie via Gmail SMTP en essayant chaque adresse résolue.

    Certaines IPs de smtp.gmail.com acceptent le TCP mais n'envoient jamais
    de bannière SMTP (route trouée) ; smtplib s'arrête à la première qui
    connecte sans essayer les suivantes. On fait plusieurs tirages DNS
    (round-robin) et on essaie chaque adresse en 587/STARTTLS puis 465/SSL
    jusqu'à obtenir une bannière 220 valide. Si tout échoue, on attend 120s
    (nouveau tirage DNS + éventuel drain côté Google) et on réessaie une fois.
    Retourne la route utilisée, lève ConnectionError si tout échoue.
    """
    import smtplib
    import socket
    import ssl
    import time

    def _try_once(endpoints):
        errors = []
        for ip in endpoints:
            for use_ssl, port in ((False, 587), (True, 465)):
                tag = f"{ip}:{port}/{'SSL' if use_ssl else 'STARTTLS'}"
                try:
                    if use_ssl:
                        raw = socket.create_connection((ip, port), timeout=10)
                        ctx = ssl.create_default_context()
                        sock = ctx.wrap_socket(raw, server_hostname="smtp.gmail.com")
                        smtp = smtplib.SMTP_SSL(timeout=10)
                        smtp.sock = sock
                    else:
                        smtp = smtplib.SMTP(timeout=10)
                        smtp.sock = socket.create_connection((ip, port), timeout=10)
                    smtp._host = "smtp.gmail.com"  # SNI / vérif certificat sur le nom
                    code, banner = smtp.getreply()
                    if int(code) // 100 != 2:
                        raise smtplib.SMTPServerDisconnected(
                            f"bannière inattendue: {code} {banner!r}")
                    if not use_ssl:
                        smtp.starttls()
                    smtp.login(SMTP_USER, SMTP_PASS)
                    smtp.send_message(msg)
                    try:
                        smtp.quit()
                    except (smtplib.SMTPException, OSError):
                        pass
                    return tag
                except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
                    errors.append(f"{tag}: {type(exc).__name__}: {exc}")
        return errors

    endpoints = _resolve_smtp_endpoints(rounds=3)
    if not endpoints:
        raise ConnectionError("DNS: aucune adresse résolue pour smtp.gmail.com")
    errors = _try_once(endpoints)
    if isinstance(errors, str):  # succès : _try_once retourne le tag
        return errors
    log(f"1er passage : {len(endpoints)} adresses, aucune bannière SMTP. "
        "Nouveau tirage DNS dans 120s...")
    time.sleep(120)
    endpoints = _resolve_smtp_endpoints(rounds=3)
    errors2 = _try_once(endpoints)
    if isinstance(errors2, str):
        return errors2
    raise ConnectionError(" ; ".join(errors + errors2))


def _build_email_message(payload):
    """Construit l'EmailMessage : résumé texte + JSON complet en pièce jointe.

    Utilisé par les transports 'email' (SMTP) et 'gmail' (API Gmail).
    """
    from email.message import EmailMessage

    day = payload["summary"]["date"]
    s = payload["summary"]

    def val(v, suffix=""):
        return f"{v}{suffix}" if v is not None else "n/d"

    dist = s.get("distance_m")
    dist_txt = f"{round(dist / 1000, 2)} km" if dist else "n/d"

    w = s.get("weight_kg")
    if w is None:
        weight_txt = "n/d"
    else:
        weight_txt = f"{w} kg (pesée du {s.get('weight_date') or '?'})"
        if s.get("body_fat_pct") is not None:
            weight_txt += f", gras {s['body_fat_pct']} %"

    body_lines = [
        f"Métriques Garmin du {day}",
        "",
        f"Pas : {val(s.get('steps'))}",
        f"Distance : {dist_txt}",
        f"Calories (total / actives) : {val(s.get('calories_total'))} / {val(s.get('calories_active'))}",
        f"FC repos / moy / max : {val(s.get('resting_hr'))} / {val(s.get('avg_hr'))} / {val(s.get('max_hr'))}",
        f"Sommeil : {val(s.get('sleep_hours'), ' h')} (score {val(s.get('sleep_score'))})",
        f"HRV nuit (moy) : {val(s.get('hrv_last_night_avg_ms'), ' ms')}",
        "Body Battery min/max/dernière : "
        f"{val(s.get('body_battery_min'))} / {val(s.get('body_battery_max'))} / {val(s.get('body_battery_last'))}",
        f"Training readiness : {val(s.get('training_readiness'))}",
        f"Poids : {weight_txt}",
        f"Séances : {len(s.get('activities') or [])}",
    ]
    for a in s.get("activities") or []:
        body_lines.append(
            f"  - {a.get('type') or '?'} « {a.get('name') or '?'} » : "
            f"{val(a.get('duration_min'), ' min')}, {val(a.get('distance_km'), ' km')}, "
            f"FC moy {val(a.get('avg_hr'))}"
        )
    body_lines += ["", "--- JSON (résumé) ---", json.dumps(s, ensure_ascii=False, indent=2)]

    filename = f"garmin-{day}.json"
    msg = EmailMessage()
    msg["From"] = SMTP_USER or GMAIL_ACCOUNT
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"[garmin-sync] Métriques du {day}"
    msg.set_content("\n".join(body_lines))
    msg.add_attachment(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        maintype="application",
        subtype="json",
        filename=filename,
    )
    return msg, filename


def send_email(payload):
    """Envoie le résumé par courriel via Gmail SMTP.

    Nécessite un mot de passe d'application Google sur le compte d'envoi
    (validation en 2 étapes à activer au préalable). Le .env ne quitte
    jamais le Pi.

    NOTE (2026-10-15) : le SMTP sortant vers Google est bloqué au niveau
    réseau sur ce réseau (ports 587/465, bannière jamais reçue) — transport
    conservé au cas où, mais 'gmail' (API) est la voie recommandée.
    """
    if not SMTP_USER or not SMTP_PASS:
        log("ERREUR: SMTP_USER / SMTP_PASS manquants pour le transport email.")
        log("Crée un mot de passe d'application sur le compte Google (2FA requis),")
        log("puis mets-le dans .env (jamais dans le chat, jamais dans git).")
        sys.exit(2)

    msg, filename = _build_email_message(payload)
    try:
        route = _smtp_send(msg)
        log(f"email : envoyé à {EMAIL_TO} via {route} ({filename} en pièce jointe)")
    except ConnectionError as exc:
        log(f"ERREUR email: {exc}")
        log("Diagnostic sur le Pi : nc -zv smtp.gmail.com 587 ; nc -zv smtp.gmail.com 465")
        sys.exit(3)


def send_gmail_api(msg):
    """Envoie un EmailMessage via l'API Gmail (users.messages.send).

    Contourne un réseau qui bloque le SMTP sortant : tout passe en HTTPS (443).
    Dépendances : pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
    """
    import base64

    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        log("ERREUR: dépendances Google manquantes :")
        log("  ./venv/bin/pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        sys.exit(2)

    if not GMAIL_CLIENT_SECRET or not Path(GMAIL_CLIENT_SECRET).exists():
        log("ERREUR: GMAIL_CLIENT_SECRET manquant ou fichier introuvable.")
        log("Crée un client OAuth 'Desktop app' dans la console Google Cloud,")
        log("télécharge client_secret.json et mets son chemin dans .env (chmod 600).")
        sys.exit(2)

    SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
    token_file = Path(GMAIL_TOKEN) if GMAIL_TOKEN else BASE_DIR / "token.json"
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            log("Token OAuth rafraîchi.")
        else:
            log("Flux OAuth : ouvre le lien affiché dans ton navigateur (sur un appareil")
            log("avec navigateur), valide, puis colle ici l'URL complète de redirection")
            log("(celle qui commence par http://localhost:8765/?code=...).")
            # oauthlib exige HTTPS par défaut, même pour du localhost. Ici c'est
            # sans risque : l'URL localhost ne quitte jamais la machine (collée
            # à la main), et l'échange du code contre le token se fait en HTTPS
            # vers les serveurs de Google.
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CLIENT_SECRET, SCOPES)
            # Headless : pas de navigateur sur le Pi. L'utilisateur ouvre l'URL
            # ailleurs, autorise, puis colle l'URL de redirection localhost.
            # (run_console() n'existe plus dans les versions récentes de
            # google-auth-oauthlib ; le OOB urn:ietf:wg:oauth:2.0:oob est bloqué
            # par Google pour les nouveaux clients.)
            flow.redirect_uri = "http://localhost:8765/"
            auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
            print()
            print(auth_url)
            print()
            redirect_response = input("URL de redirection : ").strip()
            flow.fetch_token(authorization_response=redirect_response)
            creds = flow.credentials
        token_file.write_text(creds.to_json(), encoding="utf-8")
        os.chmod(token_file, 0o600)
        log(f"Token OAuth sauvegardé : {token_file} (chmod 600)")
    service = build("gmail", "v1", credentials=creds)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    log("gmail-api : message envoyé")


def send_gmail(payload):
    """Transport 'gmail' : même courriel que 'email', mais via l'API Gmail (OAuth)."""
    msg, filename = _build_email_message(payload)
    try:
        send_gmail_api(msg)
        log(f"gmail : envoyé à {EMAIL_TO} ({filename} en pièce jointe)")
    except Exception as exc:  # noqa: BLE001 - message clair, pas de stack
        log(f"ERREUR gmail-api: {type(exc).__name__}: {exc}")
        sys.exit(3)


def main():
    day = (date.today() - timedelta(days=1)).isoformat()  # hier = journée complète
    if len(sys.argv) == 3 and sys.argv[1] == "--date":
        day = sys.argv[2]
    elif os.getenv("SYNC_DATE"):
        day = os.getenv("SYNC_DATE")

    log(f"Sync des données du {day}")
    if TRANSPORT in ("email", "gmail") and not EMAIL_TO:
        log("ERREUR: EMAIL_TO manquant dans .env (destinataire du courriel).")
        sys.exit(2)
    if TRANSPORT == "gmail" and not GMAIL_ACCOUNT:
        log("ERREUR: GMAIL_ACCOUNT manquant dans .env (compte Google d'envoi).")
        sys.exit(2)
    client = login_client()
    payload = summarize(client, day)

    if TRANSPORT == "ntfy":
        send_ntfy(payload)
    elif TRANSPORT == "email":
        send_email(payload)
    elif TRANSPORT == "gmail":
        send_gmail(payload)
    else:
        send_file(payload)
    log("OK")


if __name__ == "__main__":
    main()

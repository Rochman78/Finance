#!/usr/bin/env python3
"""
CA HT J-1 par boutique Shopify → Google Sheets (feuille "REPORT SHOPIFY VENTES")
Dépenses Google Ads J-1 par boutique → Google Sheets (feuille "REPORT GOOGLE ADS")
CA HT J-1 par pays Amazon → Google Sheets (feuille "REPORT AMAZON VENTES")
Dépenses Amazon Ads J-1 par pays → Google Sheets (feuille "REPORT AMAZON ADS")
Dépenses Meta Ads J-1 (LFC + COCO) → Google Sheets (feuille "REPORT META ADS")
Dépenses Microsoft Ads J-1 (LFC) → Google Sheets (feuille "REPORT MICROSOFT ADS")
CA HT J-1 Cdiscount (marketplace via Octopia) → Google Sheets (feuille "REPORT CDISCOUNT VENTES")
"""

import os
import sys
import io
import re
import logging
import argparse
import requests
import time
import gzip
import zipfile
import json as json_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date as date_type, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.ads.googleads.client import GoogleAdsClient

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# =============================================================
# CONFIG BOUTIQUES SHOPIFY
# =============================================================
SHOPIFY_API_VERSION = "2026-01"

STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com",  "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  "")},
    {"name": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",    "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  "")},
    {"name": "UNI",  "store": "univers-camouflage.myshopify.com",        "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", "")},
    {"name": "TAR",  "store": "tarnnetz.myshopify.com",                  "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",         "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",          "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  "")},
    {"name": "COCO", "store": "coconets.myshopify.com",                  "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  "")},
    {"name": "MON",  "store": "mon-ombrage.myshopify.com",               "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO",   "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",             "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
]

STORE_ORDER = ["LFC", "LVO", "UNI", "TAR", "HET", "RED", "COCO", "MON", "RETE"]

# =============================================================
# CONFIG COMPTES GOOGLE ADS
# =============================================================
GADS_STORES = [
    {"name": "LFC",  "customer_id": "4735534272"},
    {"name": "LVO",  "customer_id": "9737876586"},
    {"name": "UNI",  "customer_id": "1206469450"},
    {"name": "TAR",  "customer_id": "6103104043"},
    {"name": "HET",  "customer_id": "8944741091"},
    {"name": "RED",  "customer_id": "7439294641"},
    {"name": "COCO", "customer_id": "3747253272"},
    {"name": "MON",  "customer_id": "1787993852"},
    {"name": "RETE", "customer_id": "3851805990"},
]

# =============================================================
# CONFIG AMAZON SP-API
# =============================================================
# Les identifiants SP-API sont aussi acceptés sous le schéma numéroté utilisé
# par smiirl-counter (AMAZON_1_*), ce qui permet de partager le même Environment
# Group Render entre les deux services sans dupliquer les secrets.
AMAZON_CLIENT_ID     = os.environ.get("AMAZON_CLIENT_ID",     "") or os.environ.get("AMAZON_1_CLIENT_ID",     "")
AMAZON_CLIENT_SECRET = os.environ.get("AMAZON_CLIENT_SECRET", "") or os.environ.get("AMAZON_1_CLIENT_SECRET", "")
AMAZON_REFRESH_TOKEN = os.environ.get("AMAZON_REFRESH_TOKEN", "") or os.environ.get("AMAZON_1_REFRESH_TOKEN", "")
AMAZON_SELLER_ID     = os.environ.get("AMAZON_SELLER_ID", "")
AMAZON_API_BASE      = "https://sellingpartnerapi-eu.amazon.com"

# "channel" = valeur de la colonne sales-channel du rapport plat Amazon, qui
# sert à ventiler les commandes par pays (cf. fetch_amazon_ca_ht).
# Les marketplaceIds Belgique et Pologne étaient erronés (BNVKL2V8VLVYW /
# A1C3IKJU4TPSC5) : ces deux pays ne remontaient jamais aucune commande.
# Corrigés d'après smiirl-counter (rebuild/config.js), qui les interroge sans
# problème.
#
# ⚠️ UNITÉ DES MONTANTS ÉCRITS DANS LE SHEET ⚠️
# "currency" indique la devise de facturation du marketplace. Le CA est écrit
# dans REPORT AMAZON VENTES **dans cette devise, SANS conversion** :
#     Suède   → SEK
#     Pologne → PLN
#     tous les autres pays → EUR
# La conversion en euros est faite en aval, hors de ce script. Ne PAS
# réintroduire de conversion ici : la feuille attend des devises locales.
# (Cela ne concerne QUE les ventes. Amazon Ads, lui, convertit bien SEK/PLN/GBP
#  en EUR via Frankfurter — cf. fetch_amazon_ads_spend — et doit le rester.)
AMAZON_MARKETPLACES = [
    {"name": "France",      "id": "A13V1IB3VIYZZH", "vat": 0.20, "channel": "amazon.fr",     "currency": "EUR"},
    {"name": "Allemagne",   "id": "A1PA6795UKMFR9", "vat": 0.19, "channel": "amazon.de",     "currency": "EUR"},
    {"name": "Belgique",    "id": "AMEN7PMS3EDWL",  "vat": 0.21, "channel": "amazon.com.be", "currency": "EUR"},
    {"name": "Espagne",     "id": "A1RKKUPIHCS9HS", "vat": 0.21, "channel": "amazon.es",     "currency": "EUR"},
    {"name": "Italie",      "id": "APJ6JRA9NG5V4",  "vat": 0.22, "channel": "amazon.it",     "currency": "EUR"},
    {"name": "Pays-Bas",    "id": "A1805IZSGTT6HS", "vat": 0.21, "channel": "amazon.nl",     "currency": "EUR"},
    {"name": "Suede",       "id": "A2NODRKZP88ZB9", "vat": 0.25, "channel": "amazon.se",     "currency": "SEK"},
    {"name": "Pologne",     "id": "A1C3SOZRARQ6R3", "vat": 0.23, "channel": "amazon.pl",     "currency": "PLN"},
]

AMAZON_MARKETPLACE_ORDER = [m["name"] for m in AMAZON_MARKETPLACES]

# Pays dont le CA n'est pas en euros : exclus de la ligne TOTAL du Sheet, qui
# serait sinon une addition de SEK, de PLN et d'euros.
AMAZON_NON_EUR = [m["name"] for m in AMAZON_MARKETPLACES if m["currency"] != "EUR"]

# =============================================================
# CONFIG AMAZON ADS API (Advertising)
# =============================================================
AMAZON_ADS_CLIENT_ID     = os.environ.get("AMAZON_ADS_CLIENT_ID", "") or AMAZON_CLIENT_ID
AMAZON_ADS_CLIENT_SECRET = os.environ.get("AMAZON_ADS_CLIENT_SECRET", "") or AMAZON_CLIENT_SECRET
AMAZON_ADS_REFRESH_TOKEN = os.environ.get("AMAZON_ADS_REFRESH_TOKEN", "")
AMAZON_ADS_API_BASE      = "https://advertising-api-eu.amazon.com"

# Cadence de polling des rapports Ads (cf. _poll_and_download_report).
# Valeurs reprises de smiirl-counter, qui interroge la même API sans timeouts.
POLL_FIRST_WAIT_S    = 180   # attente avant le 1er check de statut
POLL_INTERVAL_S      = 120   # entre deux checks suivants
POLL_THROTTLE_WAIT_S = 60    # pause supplémentaire après un 429
CREATE_STAGGER_S     = 0.5   # décalage entre deux créations de rapport

# Comptes Amazon Ads suivis (le compte couvre 10 marketplaces, dont UK/IE
# absents de la liste sales AMAZON_MARKETPLACES, et SE/PL/UK qui facturent
# en SEK/PLN/GBP → conversion EUR via Frankfurter).
AMAZON_ADS_MARKETPLACES = [
    {"name": "France",      "country_code": "FR", "currency": "EUR"},
    {"name": "Allemagne",   "country_code": "DE", "currency": "EUR"},
    {"name": "Italie",      "country_code": "IT", "currency": "EUR"},
    {"name": "Espagne",     "country_code": "ES", "currency": "EUR"},
    {"name": "Pays-Bas",    "country_code": "NL", "currency": "EUR"},
    {"name": "Belgique",    "country_code": "BE", "currency": "EUR"},
    {"name": "Irlande",     "country_code": "IE", "currency": "EUR"},
    {"name": "Royaume-Uni", "country_code": "UK", "currency": "GBP"},
    {"name": "Suede",       "country_code": "SE", "currency": "SEK"},
    {"name": "Pologne",     "country_code": "PL", "currency": "PLN"},
]
AMAZON_ADS_ORDER = [m["name"] for m in AMAZON_ADS_MARKETPLACES]

# =============================================================
# RE-FETCH ATTRIBUTION (ads — Google / Amazon / Meta / Microsoft)
# =============================================================
# Les régies pubs ré-ajustent leurs chiffres pendant ~48-72h après la fin
# du jour (attribution des conversions, delivery tardive, fraud filter…).
# Pour suivre ces ajustements, on re-fetch les N derniers jours à chaque
# run cron (3 runs/jour = matin/midi/soir). Le run d'après écrase la cellule.
ADS_REFETCH_LOOKBACK = 3       # nombre de jours à re-fetcher (J-1 inclus)
AD_SHEET_KEYS = {"gads", "amazon-ads", "meta-ads", "ms-ads"}

# =============================================================
# CONFIG TELEGRAM (alertes de run)
# =============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# File d'alertes accumulées pendant le run ; envoyée en fin de main()
_telegram_alerts = []


def telegram_alert(message: str) -> None:
    """Queue un message à envoyer en fin de run."""
    _telegram_alerts.append(message)


def flush_telegram_alerts() -> None:
    """Envoie tous les alerts accumulés en un seul message Telegram."""
    if not _telegram_alerts:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(f"Telegram non configuré → {len(_telegram_alerts)} alerte(s) non envoyée(s)")
        return
    header = f"⚠️ Daily report — {len(_telegram_alerts)} anomalie(s)"
    body   = "\n".join(_telegram_alerts[:30])  # cap à 30 lignes pour rester sous 4096 chars
    if len(_telegram_alerts) > 30:
        body += f"\n... +{len(_telegram_alerts) - 30} autres."
    text = f"{header}\n\n{body}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]},
            timeout=10,
        )
        if r.status_code != 200:
            log.error(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
        else:
            log.info(f"✅ {len(_telegram_alerts)} alerte(s) Telegram envoyée(s)")
    except Exception as e:
        log.error(f"Erreur envoi Telegram : {e}")

# =============================================================
# CONFIG META ADS (Facebook / Instagram)
# =============================================================
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
META_API_VERSION  = "v21.0"
META_API_BASE     = f"https://graph.facebook.com/{META_API_VERSION}"

# Comptes pubs Meta suivis dans le daily report (label = nom de ligne dans Google Sheets)
META_ADS_STORES = [
    {"name": "LFC",  "account_id": "act_846038955950234"},
    {"name": "COCO", "account_id": "act_817405970328905"},
]
META_ADS_ORDER = [s["name"] for s in META_ADS_STORES]

# =============================================================
# CONFIG MICROSOFT ADS (Bing)
# =============================================================
MICROSOFT_ADS_CLIENT_ID       = os.environ.get("MICROSOFT_ADS_CLIENT_ID", "")
MICROSOFT_ADS_CLIENT_SECRET   = os.environ.get("MICROSOFT_ADS_CLIENT_SECRET", "")
MICROSOFT_ADS_REFRESH_TOKEN   = os.environ.get("MICROSOFT_ADS_REFRESH_TOKEN", "")
MICROSOFT_ADS_DEVELOPER_TOKEN = os.environ.get("MICROSOFT_ADS_DEVELOPER_TOKEN", "")
MICROSOFT_ADS_TOKEN_URL       = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_ADS_REPORTING_URL   = "https://reporting.api.bingads.microsoft.com/Api/Advertiser/Reporting/V13/ReportingService.svc"

# Comptes Microsoft Ads suivis (label = nom de ligne dans Google Sheets)
MICROSOFT_ADS_STORES = [
    {"name": "LFC", "account_id": "187047387"},
]
MICROSOFT_ADS_ORDER = [s["name"] for s in MICROSOFT_ADS_STORES]

# =============================================================
# CONFIG CDISCOUNT (Octopia Seller API — compte ventes uniquement,
# l'autre compte OCTOPIA est utilisé pour la logistique fulfillment)
# =============================================================
OCTOPIA_CLIENT_ID     = os.environ.get("OCTOPIA_CLIENT_ID", "")
OCTOPIA_CLIENT_SECRET = os.environ.get("OCTOPIA_CLIENT_SECRET", "")
OCTOPIA_SELLER_ID     = os.environ.get("OCTOPIA_SELLER_ID", "")
OCTOPIA_AUTH_URL      = "https://auth.octopia-io.net/auth/realms/maas/protocol/openid-connect/token"
OCTOPIA_API_BASE      = "https://api.octopia-io.net/seller/v2"
OCTOPIA_VAT_RATE      = 0.20  # FR TVA standard (channel CDISFR)

CDISCOUNT_ORDER = ["Cdiscount"]

# =============================================================
# CONFIG GOOGLE SHEETS
# =============================================================
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

SHEET_SHOPIFY    = "REPORT SHOPIFY VENTES"
SHEET_GADS       = "REPORT GOOGLE ADS"
SHEET_AMAZON     = "REPORT AMAZON VENTES"
SHEET_AMAZON_ADS = "REPORT AMAZON ADS"
SHEET_META_ADS   = "REPORT META ADS"
SHEET_MS_ADS     = "REPORT MICROSOFT ADS"
SHEET_CDISCOUNT  = "REPORT CDISCOUNT VENTES"

SERVICE_ACCOUNT_EMAIL = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
PRIVATE_KEY           = os.environ.get("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")

# =============================================================
# CONFIG GOOGLE ADS API
# =============================================================
GADS_DEVELOPER_TOKEN = os.environ.get("GADS_DEVELOPER_TOKEN", "")
GADS_CLIENT_ID       = os.environ.get("GADS_CLIENT_ID", "")
GADS_CLIENT_SECRET   = os.environ.get("GADS_CLIENT_SECRET", "")
GADS_REFRESH_TOKEN   = os.environ.get("GADS_REFRESH_TOKEN", "")

# =============================================================
# SHOPIFY — OAuth + appel API
# =============================================================
_token_cache = {}

def get_shopify_token(store_host, client_id, client_secret):
    now = time.time()
    cached = _token_cache.get(store_host)
    if cached and now < cached["expires_at"]:
        return cached["token"]

    resp = requests.post(
        f"https://{store_host}/admin/oauth/access_token",
        data={
            "grant_type":    "client_credentials",
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"OAuth {resp.status_code}: {resp.text}")

    data = resp.json()
    expires_in = data.get("expires_in", 86399) - 300
    _token_cache[store_host] = {"token": data["access_token"], "expires_at": now + expires_in}
    log.info(f"  Token OAuth OK pour {store_host}")
    return data["access_token"]


def fetch_ca_ht(store, date_min_iso, date_max_iso):
    """Retourne le CA HT du jour pour une boutique Shopify."""
    if not store["client_secret"]:
        log.warning(f"[{store['name']}] Pas de secret → 0")
        return 0.0

    try:
        token = get_shopify_token(store["store"], store["client_id"], store["client_secret"])

        all_orders = []
        url = (
            f"https://{store['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
            f"?status=any&financial_status=any"
            f"&created_at_min={requests.utils.quote(date_min_iso)}"
            f"&created_at_max={requests.utils.quote(date_max_iso)}"
            f"&fields=total_price,total_tax&limit=250"
        )

        while url:
            r = requests.get(url, headers={"X-Shopify-Access-Token": token}, timeout=15)
            if r.status_code != 200:
                raise Exception(f"API {r.status_code}: {r.text}")
            data = r.json()
            all_orders.extend(data.get("orders", []))

            link = r.headers.get("Link", "")
            import re
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None

        ca_ht = round(sum(float(o["total_price"]) - float(o["total_tax"]) for o in all_orders), 2)
        log.info(f"[{store['name']}] {len(all_orders)} commande(s) → CA HT = {ca_ht}")
        return ca_ht

    except Exception as e:
        log.error(f"[{store['name']}] Erreur Shopify : {e}")
        return None


# =============================================================
# AMAZON SP-API
# =============================================================
_amazon_token_cache = {"token": None, "expires_at": 0}

def get_amazon_access_token():
    """Obtient un access token LWA Amazon."""
    now = time.time()
    if _amazon_token_cache["token"] and now < _amazon_token_cache["expires_at"]:
        return _amazon_token_cache["token"]

    resp = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": AMAZON_REFRESH_TOKEN,
            "client_id":     AMAZON_CLIENT_ID,
            "client_secret": AMAZON_CLIENT_SECRET,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"Amazon LWA error: {resp.status_code} {resp.text}")

    data = resp.json()
    _amazon_token_cache["token"]      = data["access_token"]
    _amazon_token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
    log.info("  Token Amazon LWA OK")
    return data["access_token"]


def amazon_get(path, params=None):
    """Appel GET sur l'API SP-API avec retry sur throttling.
    params peut contenir des listes pour les paramètres répétés (ex: OrderStatuses).
    """
    token = get_amazon_access_token()
    headers = {
        "x-amz-access-token": token,
        "Content-Type": "application/json",
    }
    for attempt in range(5):
        resp = requests.get(
            f"{AMAZON_API_BASE}{path}",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = min(2 ** attempt, 30)
            log.info(f"  Rate limit Amazon — pause {wait}s...")
            time.sleep(wait)
            continue
        log.error(f"  Amazon GET {path}: {resp.status_code} {resp.text[:200]}")
        return None
    return None


def _amazon_create_report(report_type: str, date_str: str, attempt: int = 0):
    """Demande un rapport SP-API sur une journée. Retourne le reportId ou None."""
    token = get_amazon_access_token()
    resp = requests.post(
        f"{AMAZON_API_BASE}/reports/2021-06-30/reports",
        headers={"x-amz-access-token": token, "Content-Type": "application/json"},
        json={
            "reportType":     report_type,
            "marketplaceIds": [m["id"] for m in AMAZON_MARKETPLACES],
            "dataStartTime":  f"{date_str}T00:00:00Z",
            "dataEndTime":    f"{date_str}T23:59:59Z",
        },
        timeout=30,
    )
    if resp.status_code == 429:
        if attempt >= 3:
            log.error("  [Amazon] Création rapport throttlée 4 fois → abandon")
            return None
        log.info("  [Amazon] Création rapport throttlée — pause 60s puis retry")
        time.sleep(60)
        return _amazon_create_report(report_type, date_str, attempt + 1)
    if resp.status_code not in (200, 202):
        log.error(f"  [Amazon] Création rapport : {resp.status_code} {resp.text[:200]}")
        return None
    return resp.json().get("reportId")


def _amazon_wait_and_download(report_id: str):
    """Attend qu'un rapport SP-API soit prêt et retourne son contenu texte.
    Retourne None si échec/timeout — le caller doit distinguer ce cas de 0 €."""
    # 30 × 20s = 10 min, cadence de smiirl-counter (rebuild/amazon.js).
    for _ in range(30):
        time.sleep(20)
        status = amazon_get(f"/reports/2021-06-30/reports/{report_id}")
        if not status:
            continue
        state = status.get("processingStatus")
        if state == "DONE":
            doc = amazon_get(f"/reports/2021-06-30/documents/{status['reportDocumentId']}")
            if not doc or not doc.get("url"):
                log.error("  [Amazon] Document du rapport introuvable")
                return None
            dl = requests.get(doc["url"], timeout=60)
            if dl.status_code != 200:
                log.error(f"  [Amazon] Téléchargement rapport : HTTP {dl.status_code}")
                return None
            raw = dl.content
            if doc.get("compressionAlgorithm") == "GZIP":
                raw = gzip.decompress(raw)
            # Les rapports plats Amazon sont souvent en cp1252, pas en UTF-8.
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp1252", errors="replace")
        if state in ("CANCELLED", "FATAL"):
            # CANCELLED = Amazon n'a produit aucune donnée pour la période.
            log.warning(f"  [Amazon] Rapport {state}")
            return None
    log.warning("  [Amazon] Timeout : rapport toujours pas prêt après 10 min")
    return None


class AmazonReportUnreadable(Exception):
    """Le rapport n'a pas pu être lu de façon fiable → aucune valeur exploitable.
    À distinguer d'un rapport valide annonçant 0 vente."""


class AmazonVentilationIncomplete(Exception):
    """Du CA existe sur un canal de vente non rattaché à un pays connu.
    La ventilation par pays serait donc fausse : on refuse d'écrire un total
    partiel, qui passerait pour une baisse d'activité réelle."""


class AmazonHybridLookupFailed(Exception):
    """Une ligne à taxe vide n'a pas pu être arbitrée via l'Orders API.
    Son HT réel est indéterminable : publier le total reviendrait à écrire un
    montant amputé de cette ligne. On invalide la journée entière."""


def _normalize_channel(raw: str) -> str:
    """Normalise une valeur de sales-channel pour comparaison sur le domaine.
    "  Amazon.FR ", "https://www.amazon.fr/" et "amazon.fr" donnent tous
    "amazon.fr" — Amazon n'est pas constant sur la casse ni les préfixes."""
    c = (raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        if c.startswith(prefix):
            c = c[len(prefix):]
    if c.startswith("www."):
        c = c[4:]
    return c.rstrip("/").strip()


# Seul canal toléré hors des 8 pays de AMAZON_MARKETPLACES.
#
# ⚠️ NE PAS ÉTOFFER CETTE LISTE ⚠️
# "non-amazon" = commandes Multi-Channel Fulfillment : des ventes passées sur
# les boutiques Shopify mais expédiées par Amazon. Elles sont DÉJÀ comptées
# dans REPORT SHOPIFY VENTES ; les compter ici les additionnerait deux fois.
# C'est la seule raison de sa présence, et elle n'est pas généralisable.
#
# Tout autre canal (amazon.co.uk, amazon.ie…) est délibérément ABSENT : nous ne
# vendons pas sur ces marketplaces, donc du CA qui y apparaîtrait signale une
# anomalie réelle. Il doit invalider la journée et déclencher une alerte, pas
# être absorbé en silence. Ajouter une entrée ici pour faire taire un log
# reviendrait à masquer du chiffre d'affaires.
AMAZON_IGNORED_CHANNELS = {"non-amazon"}


# Cache des items de commande, pour n'interroger l'Orders API qu'une fois par
# commande même si plusieurs de ses lignes ont une taxe vide.
_amazon_order_items_cache = {}


def _amazon_order_items(order_id: str):
    """Items d'une commande via l'Orders API. Retourne None en cas d'échec."""
    if order_id in _amazon_order_items_cache:
        return _amazon_order_items_cache[order_id]
    time.sleep(0.3)  # throttle SP-API
    data = amazon_get(f"/orders/v0/orders/{order_id}/orderItems")
    items = data.get("payload", {}).get("OrderItems") if data else None
    if items is None:
        return None
    par_id = {it.get("OrderItemId"): it for it in items}
    _amazon_order_items_cache[order_id] = par_id
    return par_id


def _parse_amazon_orders_report(tsv: str, date_str: str) -> dict:
    """Ventile un rapport plat Amazon en CA HT par pays.

    Une ligne = un article. Deux chemins, selon la colonne item-tax :

      - taxe RENSEIGNÉE → item_price - item_tax, directement depuis le rapport.
      - taxe VIDE       → on ne peut PAS trancher depuis le rapport seul, car
        item_price y est tantôt TTC (Amazon collecteur), tantôt déjà HT
        (autoliquidation intracommunautaire B2B). Diviser par le taux du pays
        sous-évaluerait de ~16 % toutes les LIC. On bascule donc ces lignes
        vers l'Orders API, dont ItemPrice - ItemTax donne le HT juste dans les
        deux régimes. C'est le "chemin hybride".

    Aucune conversion de devise ici : les montants restent dans la devise de
    facturation du marketplace (SEK pour la Suède, PLN pour la Pologne).
    Cf. le bloc AMAZON_MARKETPLACES.

    Quatre issues distinctes, jamais confondues :
      - dict de totaux          → lecture fiable (des 0 y sont de vrais 0)
      - AmazonReportUnreadable  → on n'a pas su lire, aucune valeur exploitable
      - AmazonVentilationIncomplete → du CA sur un canal non rattaché à un pays
      - AmazonHybridLookupFailed    → une ligne à taxe vide reste indéterminée
    """
    # Un contenu totalement vide n'est PAS "0 vente" : c'est un rapport qu'on
    # n'a pas su lire. Amazon renvoie toujours au moins sa ligne d'en-têtes.
    if not tsv or not tsv.strip():
        raise AmazonReportUnreadable("contenu vide (pas même la ligne d'en-têtes)")

    lines = tsv.strip().split("\n")
    headers = [h.strip().lower().replace("_", "-") for h in lines[0].split("\t")]
    idx = {h: i for i, h in enumerate(headers)}
    for required in ("sales-channel", "item-price"):
        if required not in idx:
            raise AmazonReportUnreadable(
                f"colonne {required} absente — en-têtes lus : {headers[:12]}"
            )

    # En-têtes présents mais aucune ligne de données → vraie journée à 0 vente.
    if len(lines) < 2:
        log.info("[Amazon] Rapport valide, aucune commande → 0 sur tous les pays")
        return {m["name"]: 0.0 for m in AMAZON_MARKETPLACES}

    by_channel = {_normalize_channel(m["channel"]): m for m in AMAZON_MARKETPLACES}
    totals     = {m["name"]: 0.0 for m in AMAZON_MARKETPLACES}
    orders     = {m["name"]: set() for m in AMAZON_MARKETPLACES}
    cancelled  = 0
    hybrides   = 0   # lignes à taxe vide arbitrées via l'Orders API
    # canal non reconnu -> {"lignes": n, "ca_ttc": float, "devises": set}
    unmapped   = {}

    def col(cols, key):
        i = idx.get(key, -1)
        if i < 0 or i >= len(cols):
            return ""
        return cols[i].strip()

    def montant_ttc(cols):
        """Somme brute TTC d'une ligne, sert à chiffrer les canaux inconnus."""
        total = 0.0
        for key in ("item-price", "item-tax", "shipping-price", "shipping-tax"):
            try:
                total += float(col(cols, key) or 0)
            except ValueError:
                pass
        return total

    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) < 3:
            continue

        # Une commande annulée n'est pas du chiffre d'affaires — y compris sur
        # un canal inconnu, où elle ne doit pas déclencher l'alerte, et y
        # compris avant tout arbitrage hybride (pas d'appel API inutile).
        if col(cols, "order-status").lower() in ("cancelled", "canceled"):
            cancelled += 1
            continue

        channel = _normalize_channel(col(cols, "sales-channel"))
        mk = by_channel.get(channel)
        if not mk:
            slot = unmapped.setdefault(channel or "(vide)",
                                       {"lignes": 0, "ca_ttc": 0.0, "devises": set()})
            slot["lignes"] += 1
            slot["ca_ttc"] += montant_ttc(cols)
            cur_row = col(cols, "currency").upper()
            if cur_row:
                slot["devises"].add(cur_row)
            continue

        # Une ligne dont TOUS les montants sont nuls n'a rien à arbitrer :
        # inutile de dépenser un appel Orders API pour elle.
        def montant(key):
            try:
                return float(col(cols, key) or 0)
            except ValueError:
                return 0.0

        item_price, ship_price = montant("item-price"), montant("shipping-price")
        taxe_vide = not col(cols, "item-tax")

        if taxe_vide and (item_price or ship_price):
            # Chemin hybride : l'Orders API arbitre TTC vs HT.
            oid_ligne = col(cols, "order-item-id")
            oid_cmd   = col(cols, "amazon-order-id")
            items = _amazon_order_items(oid_cmd) if oid_cmd else None
            if items is None:
                raise AmazonHybridLookupFailed(
                    f"commande {oid_cmd or '?'} : Orders API injoignable"
                )
            it = items.get(oid_ligne)
            if it is None:
                raise AmazonHybridLookupFailed(
                    f"commande {oid_cmd} : order-item-id {oid_ligne or '(vide)'} "
                    f"absent de la réponse Orders API"
                )
            def api_montant(champ):
                return float((it.get(champ) or {}).get("Amount", 0) or 0)
            ht = ((api_montant("ItemPrice")     - api_montant("ItemTax")) +
                  (api_montant("ShippingPrice") - api_montant("ShippingTax")))
            hybrides += 1
        else:
            vat = mk["vat"]
            ht  = 0.0
            for price_key, tax_key in (("item-price", "item-tax"),
                                       ("shipping-price", "shipping-tax")):
                price = montant(price_key)
                if not price:
                    continue
                tax = montant(tax_key)
                ht += (price - tax) if tax > 0 else (price / (1 + vat))

        totals[mk["name"]] += ht
        oid = col(cols, "amazon-order-id")
        if oid:
            orders[mk["name"]].add(oid)

    if cancelled:
        log.info(f"  [Amazon] {cancelled} ligne(s) annulée(s) exclue(s) du CA")
    log.info(f"  [Amazon] {hybrides} ligne(s) à taxe vide arbitrée(s) via l'Orders API")

    # Tout canal non rattaché est journalisé avec son CA, qu'il soit
    # volontairement hors périmètre ou réellement inattendu.
    porteurs = []
    for chan, info in sorted(unmapped.items()):
        devises = "/".join(sorted(info["devises"])) or "?"
        detail  = f"{info['lignes']} ligne(s), {info['ca_ttc']:.2f} {devises} TTC"
        if chan in AMAZON_IGNORED_CHANNELS:
            log.info(f"  [Amazon] Canal hors périmètre '{chan}' : {detail}")
        else:
            log.error(f"  [Amazon] Canal INCONNU '{chan}' : {detail}")
            if info["ca_ttc"] > 0:
                porteurs.append(f"{chan} ({info['ca_ttc']:.2f} {devises})")

    # Du CA sur un canal inconnu = ventilation par pays incomplète. On refuse
    # de publier un total partiel : il ressemblerait à une vraie baisse.
    if porteurs:
        raise AmazonVentilationIncomplete(
            f"{date_str} — CA sur canal(aux) non rattaché(s) : {', '.join(porteurs)}"
        )

    results = {}
    for m in AMAZON_MARKETPLACES:
        name = m["name"]
        results[name] = round(totals[name], 2)
        # La devise est affichée systématiquement : SE et PL ne sont pas en EUR
        # et rien dans la feuille ne le rappelle.
        log.info(f"[Amazon {name}] {len(orders[name])} commande(s) → "
                 f"CA HT = {results[name]} {m['currency']}")
    return results


def fetch_amazon_ca_ht_for_marketplace(marketplace: dict, date_str: str) -> float:
    """
    Retourne le CA HT pour un marketplace Amazon sur une journée.
    Utilise Orders API + Order Items API pour calculer ItemPrice - ItemTax.
    Si ItemTax=0 (LIC, export Suisse...), on divise ItemPrice par (1 + taux TVA pays).
    date_str : format YYYY-MM-DD
    """
    name           = marketplace["name"]
    marketplace_id = marketplace["id"]
    vat_rate       = marketplace.get("vat", 0.20)

    date_min = f"{date_str}T00:00:00Z"
    date_max = f"{date_str}T23:59:59Z"

    # Récupère toutes les commandes du jour
    all_orders = []
    next_token = None

    while True:
        if next_token:
            params = [
                ("NextToken",      next_token),
                ("MarketplaceIds", marketplace_id),
            ]
        else:
            params = [
                ("MarketplaceIds", marketplace_id),
                ("CreatedAfter",   date_min),
                ("CreatedBefore",  date_max),
            ]

        data = amazon_get("/orders/v0/orders", params)
        if not data:
            break

        payload = data.get("payload", {})
        orders  = payload.get("Orders", [])
        all_orders.extend(orders)

        next_token = payload.get("NextToken")
        if not next_token:
            break
        time.sleep(0.5)

    if not all_orders:
        log.info(f"[Amazon {name}] 0 commande(s)")
        return 0.0

    log.info(f"[Amazon {name}] {len(all_orders)} commande(s) trouvée(s)")

    # Pour chaque commande, récupère les items pour calculer HT
    total_ht = 0.0
    for order in all_orders:
        order_id = order.get("AmazonOrderId")
        if not order_id:
            continue
        # Même règle que la voie principale : une commande annulée n'est pas
        # du CA. Sans ce filtre, les deux méthodes ne seraient pas comparables.
        # L'Orders API écrit "Canceled", le rapport plat "Cancelled".
        if (order.get("OrderStatus") or "").lower() in ("canceled", "cancelled"):
            continue

        time.sleep(0.3)  # throttle
        items_data = amazon_get(f"/orders/v0/orders/{order_id}/orderItems")
        if not items_data:
            continue

        for item in items_data.get("payload", {}).get("OrderItems", []):
            item_price = float(item.get("ItemPrice", {}).get("Amount", 0) or 0)
            item_tax   = float(item.get("ItemTax",   {}).get("Amount", 0) or 0)
            if item_tax > 0:
                # TVA connue → on soustrait
                total_ht += item_price - item_tax
            else:
                # LIC, export Suisse, TVA=0 → on divise par (1 + taux TVA pays)
                total_ht += item_price / (1 + vat_rate)

    total_ht = round(total_ht, 2)
    log.info(f"[Amazon {name}] CA HT = {total_ht} €")
    return total_ht


def fetch_amazon_ca_ht_via_orders_api(date_str: str) -> dict:
    """Secours : un appel Orders API par marketplace, puis un appel par commande.
    Lent (~110 requêtes/jour) et sensible au throttling — d'où la méthode
    rapport en voie principale. Conservé au cas où le rapport échoue."""
    results = {}
    for marketplace in AMAZON_MARKETPLACES:
        try:
            results[marketplace["name"]] = fetch_amazon_ca_ht_for_marketplace(marketplace, date_str)
        except Exception as e:
            log.error(f"[Amazon {marketplace['name']}] Erreur : {e}")
            results[marketplace["name"]] = None
    return results


def fetch_amazon_ca_ht(date_str: str) -> dict:
    """Retourne un dict {pays: ca_ht} pour tous les marketplaces.

    Voie principale : un seul rapport plat couvrant les 8 marketplaces, ventilé
    par colonne sales-channel (méthode de smiirl-counter, rebuild/amazon.js).
    ~5 requêtes API au total au lieu d'une par commande, donc pas de throttling,
    et l'ancienne dépendance aux marketplaceIds pour identifier le pays disparaît.
    Bascule sur l'Orders API si le rapport échoue.
    """
    if not AMAZON_REFRESH_TOKEN:
        log.warning("AMAZON_REFRESH_TOKEN non configuré → skip Amazon ventes")
        return {m["name"]: None for m in AMAZON_MARKETPLACES}

    try:
        report_id = _amazon_create_report(
            "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL", date_str
        )
        if report_id:
            log.info(f"  [Amazon] Rapport {report_id} demandé pour {date_str}")
            tsv = _amazon_wait_and_download(report_id)
            if tsv is not None:
                return _parse_amazon_orders_report(tsv, date_str)
    except AmazonVentilationIncomplete as e:
        # Cas volontairement NON rattrapé par l'Orders API : celle-ci interroge
        # les marketplaceIds connus un par un, elle a donc exactement le même
        # angle mort et renverrait le même total partiel, mais sans alerte.
        # Mieux vaut aucune donnée qu'une ventilation fausse.
        log.error(f"[Amazon] Ventilation incomplète → journée invalidée : {e}")
        telegram_alert(f"• Amazon ventes {date_str} : canal de vente inconnu porteur de CA "
                       f"→ journée non publiée ({e})")
        return {m["name"]: None for m in AMAZON_MARKETPLACES}
    except AmazonHybridLookupFailed as e:
        # Comme pour la ventilation incomplète : pas de bascule vers l'Orders
        # API. Si elle est injoignable pour une ligne, elle l'est pour toutes,
        # et son repli par division est précisément ce qu'on cherche à éviter.
        log.error(f"[Amazon] Arbitrage taxe vide impossible → journée invalidée : {e}")
        telegram_alert(f"• Amazon ventes {date_str} : ligne à taxe vide non arbitrable "
                       f"→ journée non publiée ({e})")
        return {m["name"]: None for m in AMAZON_MARKETPLACES}
    except AmazonReportUnreadable as e:
        log.error(f"[Amazon] Rapport illisible : {e}")
    except Exception as e:
        log.error(f"[Amazon] Rapport indisponible : {e}")

    log.warning("[Amazon] Bascule sur l'Orders API (méthode de secours)")
    return fetch_amazon_ca_ht_via_orders_api(date_str)


# =============================================================
# GOOGLE ADS — Récupération des dépenses
# =============================================================

def get_gads_client():
    return GoogleAdsClient.load_from_dict({
        "developer_token": GADS_DEVELOPER_TOKEN,
        "client_id":       GADS_CLIENT_ID,
        "client_secret":   GADS_CLIENT_SECRET,
        "refresh_token":   GADS_REFRESH_TOKEN,
        "use_proto_plus":  True,
    })


def fetch_gads_spend(customer_id, date_str):
    """Retourne les dépenses du jour (en €) pour un compte Google Ads."""
    try:
        client     = get_gads_client()
        ga_service = client.get_service("GoogleAdsService")

        query = f"""
            SELECT metrics.cost_micros
            FROM customer
            WHERE segments.date = '{date_str}'
        """

        response     = ga_service.search(customer_id=customer_id, query=query)
        total_micros = sum(row.metrics.cost_micros for row in response)
        spend        = round(total_micros / 1_000_000, 2)
        log.info(f"[GAds {customer_id}] Dépenses = {spend} €")
        return spend

    except Exception as e:
        log.error(f"[GAds {customer_id}] Erreur : {e}")
        return None


# =============================================================
# AMAZON ADS — Récupération des dépenses publicitaires
# =============================================================

_amazon_ads_token_cache = {"token": None, "expires_at": 0}


def get_amazon_ads_access_token():
    """Obtient un access token LWA pour Amazon Ads."""
    now = time.time()
    if _amazon_ads_token_cache["token"] and now < _amazon_ads_token_cache["expires_at"]:
        return _amazon_ads_token_cache["token"]

    resp = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": AMAZON_ADS_REFRESH_TOKEN,
            "client_id":     AMAZON_ADS_CLIENT_ID,
            "client_secret": AMAZON_ADS_CLIENT_SECRET,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"Amazon Ads LWA error: {resp.status_code} {resp.text}")

    data = resp.json()
    _amazon_ads_token_cache["token"]      = data["access_token"]
    _amazon_ads_token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
    log.info("  Token Amazon Ads LWA OK")
    return data["access_token"]


def get_amazon_ads_profiles():
    """Récupère les profils Amazon Ads → dict {countryCode: profileId}."""
    token = get_amazon_ads_access_token()
    resp = requests.get(
        f"{AMAZON_ADS_API_BASE}/v2/profiles",
        headers={
            "Authorization": f"Bearer {token}",
            "Amazon-Advertising-API-ClientId": AMAZON_ADS_CLIENT_ID,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"Amazon Ads profiles error: {resp.status_code} {resp.text}")

    result = {}
    for p in resp.json():
        country    = p.get("countryCode")
        profile_id = p.get("profileId")
        acct_type  = p.get("accountInfo", {}).get("type", "")
        if country and profile_id and acct_type == "seller":
            result[country] = str(profile_id)
    log.info(f"  Profils Amazon Ads trouvés : {list(result.keys())}")
    return result


def _create_ads_report(token, profile_id, ad_product, report_type_id, date_str):
    """Crée une demande de rapport Amazon Ads v3. Retourne le reportId ou None."""
    resp = requests.post(
        f"{AMAZON_ADS_API_BASE}/reporting/reports",
        headers={
            "Authorization": f"Bearer {token}",
            "Amazon-Advertising-API-ClientId": AMAZON_ADS_CLIENT_ID,
            "Amazon-Advertising-API-Scope": profile_id,
            "Content-Type": "application/json",
        },
        json={
            "name": f"{ad_product} spend {date_str}",
            "startDate": date_str,
            "endDate":   date_str,
            "configuration": {
                "adProduct":    ad_product,
                "groupBy":      ["campaign"],
                "columns":      ["spend"],
                "reportTypeId": report_type_id,
                "timeUnit":     "SUMMARY",
                "format":       "GZIP_JSON",
            },
        },
        timeout=30,
    )
    if resp.status_code in (200, 202):
        return resp.json().get("reportId")
    log.debug(f"  Report {ad_product} creation: {resp.status_code} {resp.text[:200]}")
    return None


def _poll_and_download_report(token, profile_id, report_id, marketplace_name=""):
    """Attend la fin d'un rapport et retourne la somme des coûts."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Amazon-Advertising-API-ClientId": AMAZON_ADS_CLIENT_ID,
        "Amazon-Advertising-API-Scope": profile_id,
    }
    # Cadence calquée sur smiirl-counter (services/ads/amazon-fetch.js), qui
    # récupère ces mêmes rapports de façon fiable : Amazon ne génère JAMAIS un
    # rapport en 8s, donc on attend 3 min avant le 1er check puis on repasse
    # toutes les 2 min. 6 tentatives = ~13 min, même budget qu'avant mais
    # ~7x moins de requêtes de statut (10 profils × 40 polls = ~400 GET par
    # lot, ce qui nous faisait rate-limiter par Amazon et échouer en boucle).
    # Retourne None (et pas 0.0) quand on n'a pas pu télécharger un rapport :
    # le caller saura distinguer "aucune dépense" vs "API timeout/error".
    tag = f"[Amazon Ads {marketplace_name}]" if marketplace_name else "[Amazon Ads]"
    for attempt in range(6):
        time.sleep(POLL_FIRST_WAIT_S if attempt == 0 else POLL_INTERVAL_S)
        resp = requests.get(
            f"{AMAZON_ADS_API_BASE}/reporting/reports/{report_id}",
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 429:
            # Throttling : on le trace explicitement (l'ancien `continue` muet
            # masquait la cause réelle des "Timeout polling") et on laisse le
            # quota se reconstituer avant le prochain check.
            log.warning(f"  {tag} 429 throttled (tentative {attempt + 1}/6) — pause {POLL_THROTTLE_WAIT_S}s")
            time.sleep(POLL_THROTTLE_WAIT_S)
            continue
        if resp.status_code != 200:
            log.warning(f"  {tag} statut HTTP {resp.status_code} : {resp.text[:150]}")
            continue
        data = resp.json()
        status = data.get("status")
        if status == "COMPLETED":
            url = data.get("url")
            if not url:
                return None
            dl = requests.get(url, timeout=30)
            if dl.status_code != 200:
                return None
            rows = json_mod.loads(gzip.decompress(dl.content))
            return sum(float(r.get("spend", 0) or r.get("cost", 0) or 0) for r in rows)
        elif status in ("FAILED", "FAILURE"):
            return None
    return None


def fetch_amazon_ads_sp_spend(profile_id, date_str, marketplace_name, stagger_index=0):
    """Sponsored Products spend pour un profil sur 1 date — en devise NATIVE
    du compte (EUR pour la zone euro, SEK pour SE, PLN pour PL, GBP pour UK).
    On ne fetch que SP : smiirl confirme que c'est la seule famille active
    ("Only Sponsored Products — that's all you use").

    stagger_index : décale la création du rapport pour ne pas envoyer les 10
    POST simultanément (smiirl espace ses créations de 500 ms).
    """
    if stagger_index:
        time.sleep(stagger_index * CREATE_STAGGER_S)
    token = get_amazon_ads_access_token()
    rid = _create_ads_report(token, profile_id, "SPONSORED_PRODUCTS", "spCampaigns", date_str)
    if not rid:
        log.warning(f"[Amazon Ads {marketplace_name}] Pas de reportId")
        return None
    try:
        spend = _poll_and_download_report(token, profile_id, rid, marketplace_name)
    except Exception as e:
        log.warning(f"[Amazon Ads {marketplace_name}] Download error : {e}")
        return None
    if spend is None:
        log.warning(f"[Amazon Ads {marketplace_name}] Timeout polling Amazon Ads")
        return None
    return round(spend, 2)


# Cache FX (currency, date) → rate. Frankfurter renvoie les taux ECB ; pour
# un weekend/jour férié il retourne le taux du dernier jour ouvré.
_fx_cache = {}


def get_fx_rate_to_eur(currency: str, date_str: str):
    """Retourne le taux 1 {currency} = X EUR pour la date donnée.
    Renvoie None en cas d'échec (caller doit gérer)."""
    if currency == "EUR":
        return 1.0
    key = (currency, date_str)
    if key in _fx_cache:
        return _fx_cache[key]
    try:
        r = requests.get(
            f"https://api.frankfurter.dev/v1/{date_str}",
            params={"base": currency, "symbols": "EUR"},
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"[FX] {currency}→EUR {date_str} : HTTP {r.status_code}")
            return None
        rate = float(r.json()["rates"]["EUR"])
        _fx_cache[key] = rate
        log.info(f"[FX] 1 {currency} = {rate:.4f} EUR ({date_str})")
        return rate
    except Exception as e:
        log.warning(f"[FX] {currency}→EUR {date_str} erreur : {e}")
        return None


def fetch_amazon_ads_spend(date_str):
    """Retourne {marketplace_name: spend EUR} pour les comptes Amazon Ads.
    Convertit SEK / PLN / GBP en EUR via Frankfurter (taux ECB).

    PARALLÉLISÉ : les 10 marketplaces sont submit + poll en concurrent via
    un ThreadPoolExecutor. Wall-time = max d'un seul marketplace (~12 min)
    au lieu de la somme (jusqu'à 2h en séquentiel quand Amazon est lent).
    Évite que le cron du matin soit interrompu par celui de midi.
    """
    if not AMAZON_ADS_REFRESH_TOKEN:
        log.warning("AMAZON_ADS_REFRESH_TOKEN non configuré → skip Amazon Ads")
        return {}

    try:
        profiles = get_amazon_ads_profiles()
    except Exception as e:
        log.error(f"Erreur récupération profils Amazon Ads : {e}")
        return {}

    # Prime le cache de token (évite que 10 threads tentent de le refresh
    # simultanément si expiré au moment du premier appel parallèle).
    try:
        get_amazon_ads_access_token()
    except Exception as e:
        log.error(f"[Amazon Ads] Token LWA error : {e}")
        return {m["name"]: None for m in AMAZON_ADS_MARKETPLACES}

    results = {}
    work = []   # liste de (marketplace_dict, profile_id) à fetcher en parallèle
    for m in AMAZON_ADS_MARKETPLACES:
        name = m["name"]
        cc   = m["country_code"]
        profile_id = profiles.get(cc)
        if not profile_id:
            log.warning(f"[Amazon Ads {name}] Pas de profil pour countryCode={cc}")
            results[name] = 0.0
            continue
        work.append((m, profile_id))

    log.info(f"[Amazon Ads] Submit + poll PARALLÈLE pour {len(work)} marketplaces...")

    # Phase 1+2 en parallèle : chaque thread fait submit → poll → download
    native_results = {}  # name -> spend en devise native ou None
    with ThreadPoolExecutor(max_workers=10) as ex:
        fut2m = {ex.submit(fetch_amazon_ads_sp_spend, p_id, date_str, m["name"], i): m
                 for i, (m, p_id) in enumerate(work)}
        for fut in as_completed(fut2m):
            m = fut2m[fut]
            try:
                native_results[m["name"]] = fut.result()
            except Exception as e:
                log.error(f"[Amazon Ads {m['name']}] Erreur fetch : {e}")
                native_results[m["name"]] = None

    # Phase 3 : conversion FX (séquentiel — fast, juste des appels Frankfurter cachés)
    for m in AMAZON_ADS_MARKETPLACES:
        name = m["name"]
        if name in results:
            continue  # déjà traité (pas de profil)
        spend_native = native_results.get(name)
        cur = m["currency"]
        if spend_native is None:
            log.warning(f"[Amazon Ads {name}] Aucune valeur fiable → cellule vide")
            results[name] = None
        elif cur == "EUR":
            results[name] = spend_native
            log.info(f"[Amazon Ads {name}] Dépenses = {spend_native} €")
        elif spend_native == 0:
            results[name] = 0.0
            log.info(f"[Amazon Ads {name}] Dépenses = 0 {cur} (skip FX)")
        else:
            rate = get_fx_rate_to_eur(cur, date_str)
            if rate is None:
                log.error(f"[Amazon Ads {name}] FX {cur}→EUR indispo → valeur non écrite")
                results[name] = None
            else:
                eur = round(spend_native * rate, 2)
                results[name] = eur
                log.info(f"[Amazon Ads {name}] {spend_native:.2f} {cur} × {rate:.4f} = {eur} €")

    return results


# =============================================================
# META ADS — Récupération des dépenses publicitaires
# =============================================================

def fetch_meta_ads_spend(account_id: str, date_str: str) -> float:
    """Retourne les dépenses du jour (en €, devise du compte) pour un compte pub Meta.
    Endpoint : Graph API /{account_id}/insights, level=account, time_increment=1.
    date_str au format YYYY-MM-DD.
    """
    if not META_ACCESS_TOKEN:
        log.warning(f"[Meta {account_id}] META_ACCESS_TOKEN absent → 0")
        return 0.0

    params = {
        "fields":         "spend",
        "level":          "account",
        "time_increment": 1,
        "time_range":     json_mod.dumps({"since": date_str, "until": date_str}),
        "access_token":   META_ACCESS_TOKEN,
    }

    for attempt in range(5):
        try:
            r = requests.get(f"{META_API_BASE}/{account_id}/insights", params=params, timeout=20)
        except requests.RequestException as e:
            log.error(f"[Meta {account_id}] Exception réseau : {e}")
            return None

        if r.status_code == 200:
            rows = r.json().get("data", [])
            spend = round(sum(float(x.get("spend", 0) or 0) for x in rows), 2)
            log.info(f"[Meta {account_id}] Dépenses = {spend} €")
            return spend

        body = r.text[:200]
        # Meta renvoie souvent 400 + "User request limit reached" pour le throttle
        is_rate_limited = (
            r.status_code == 429
            or "User request limit reached" in body
            or "too many calls" in body
        )
        if is_rate_limited:
            wait = min(2 ** attempt * 10, 60)
            log.info(f"[Meta {account_id}] Rate limit — pause {wait}s")
            time.sleep(wait)
            continue

        log.error(f"[Meta {account_id}] API {r.status_code} : {body}")
        return None

    log.error(f"[Meta {account_id}] Abandon après 5 tentatives rate-limit")
    return None


def fetch_meta_ads_spend_all(date_str: str) -> dict:
    """Retourne {name: spend €} pour tous les comptes Meta configurés."""
    results = {}
    for store in META_ADS_STORES:
        results[store["name"]] = fetch_meta_ads_spend(store["account_id"], date_str)
    return results


# =============================================================
# MICROSOFT ADS — Récupération des dépenses publicitaires
# =============================================================
# Workflow SOAP : OAuth → SubmitGenerateReport → PollGenerateReport (jusqu'à
# Success) → download ZIP CSV → parse. Endpoint /V13/ReportingService.svc.

_ms_ads_token_cache = {"token": None, "expires_at": 0}


def get_ms_ads_access_token() -> str:
    now = time.time()
    if _ms_ads_token_cache["token"] and now < _ms_ads_token_cache["expires_at"]:
        return _ms_ads_token_cache["token"]
    resp = requests.post(
        MICROSOFT_ADS_TOKEN_URL,
        data={
            "client_id":     MICROSOFT_ADS_CLIENT_ID,
            "client_secret": MICROSOFT_ADS_CLIENT_SECRET,
            "refresh_token": MICROSOFT_ADS_REFRESH_TOKEN,
            "grant_type":    "refresh_token",
            "scope":         "offline_access https://ads.microsoft.com/msads.manage",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        raise Exception(f"MS Ads token: {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    _ms_ads_token_cache["token"]      = data["access_token"]
    _ms_ads_token_cache["expires_at"] = now + data.get("expires_in", 3600) - 60
    log.info("  Token Microsoft Ads OK")
    return data["access_token"]


def _ms_xml_val(xml: str, tag: str):
    m = re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml)
    return m.group(1) if m else None


def _ms_build_submit_soap(token: str, account_ids: list, since: str, until: str) -> str:
    sp = since.split("-"); ep = until.split("-")
    ids_xml = "".join(f"<a:long>{int(i)}</a:long>" for i in account_ids)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:i="http://www.w3.org/2001/XMLSchema-instance" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Header xmlns="https://bingads.microsoft.com/Reporting/v13">'
        f'<AuthenticationToken>{token}</AuthenticationToken>'
        f'<DeveloperToken>{MICROSOFT_ADS_DEVELOPER_TOKEN}</DeveloperToken>'
        '</s:Header><s:Body>'
        '<SubmitGenerateReportRequest xmlns="https://bingads.microsoft.com/Reporting/v13">'
        '<ReportRequest i:type="AccountPerformanceReportRequest">'
        '<ExcludeColumnHeaders>false</ExcludeColumnHeaders>'
        '<ExcludeReportFooter>true</ExcludeReportFooter>'
        '<ExcludeReportHeader>true</ExcludeReportHeader>'
        '<Format>Csv</Format><Language>English</Language>'
        '<ReportName>DailySpend</ReportName>'
        '<ReturnOnlyCompleteData>false</ReturnOnlyCompleteData>'
        '<Aggregation>Daily</Aggregation>'
        '<Columns>'
        '<AccountPerformanceReportColumn>AccountName</AccountPerformanceReportColumn>'
        '<AccountPerformanceReportColumn>AccountId</AccountPerformanceReportColumn>'
        '<AccountPerformanceReportColumn>TimePeriod</AccountPerformanceReportColumn>'
        '<AccountPerformanceReportColumn>Spend</AccountPerformanceReportColumn>'
        '</Columns>'
        '<Scope><AccountIds xmlns:a="http://schemas.microsoft.com/2003/10/Serialization/Arrays">'
        f'{ids_xml}'
        '</AccountIds></Scope>'
        '<Time>'
        f'<CustomDateRangeEnd><Day>{int(ep[2])}</Day><Month>{int(ep[1])}</Month><Year>{int(ep[0])}</Year></CustomDateRangeEnd>'
        f'<CustomDateRangeStart><Day>{int(sp[2])}</Day><Month>{int(sp[1])}</Month><Year>{int(sp[0])}</Year></CustomDateRangeStart>'
        '</Time></ReportRequest></SubmitGenerateReportRequest></s:Body></s:Envelope>'
    )


def _ms_build_poll_soap(token: str, report_id: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:i="http://www.w3.org/2001/XMLSchema-instance" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        '<s:Header xmlns="https://bingads.microsoft.com/Reporting/v13">'
        f'<AuthenticationToken>{token}</AuthenticationToken>'
        f'<DeveloperToken>{MICROSOFT_ADS_DEVELOPER_TOKEN}</DeveloperToken>'
        '</s:Header><s:Body>'
        '<PollGenerateReportRequest xmlns="https://bingads.microsoft.com/Reporting/v13">'
        f'<ReportRequestId>{report_id}</ReportRequestId>'
        '</PollGenerateReportRequest></s:Body></s:Envelope>'
    )


def _ms_none_results():
    return {s["name"]: None for s in MICROSOFT_ADS_STORES}


def fetch_ms_ads_spend(date_str: str) -> dict:
    """Retourne {name: spend €} pour les comptes Microsoft Ads configurés sur 1 date.
    date_str au format YYYY-MM-DD.
    """
    if not all([MICROSOFT_ADS_CLIENT_ID, MICROSOFT_ADS_REFRESH_TOKEN, MICROSOFT_ADS_DEVELOPER_TOKEN]):
        log.warning("[MS Ads] Credentials manquants → skip")
        return _ms_none_results()
    if not MICROSOFT_ADS_STORES:
        return {}

    try:
        token = get_ms_ads_access_token()
        account_ids = [s["account_id"] for s in MICROSOFT_ADS_STORES]

        # 1) Submit
        soap_headers = {"Content-Type": "text/xml; charset=utf-8"}
        r = requests.post(
            MICROSOFT_ADS_REPORTING_URL,
            headers={**soap_headers, "SOAPAction": "SubmitGenerateReport"},
            data=_ms_build_submit_soap(token, account_ids, date_str, date_str).encode("utf-8"),
            timeout=30,
        )
        if r.status_code != 200:
            log.error(f"[MS Ads] SubmitReport {r.status_code} : {r.text[:300]}")
            return _ms_none_results()
        report_id = _ms_xml_val(r.text, "ReportRequestId")
        if not report_id:
            log.error(f"[MS Ads] ReportRequestId introuvable : {r.text[:300]}")
            return _ms_none_results()
        log.info(f"[MS Ads] Rapport soumis pour {date_str}, ID={report_id}")

        # 2) Poll (jusqu'à 10 × 15s = 150s)
        download_url = None
        for i in range(10):
            time.sleep(15)
            pr = requests.post(
                MICROSOFT_ADS_REPORTING_URL,
                headers={**soap_headers, "SOAPAction": "PollGenerateReport"},
                data=_ms_build_poll_soap(token, report_id).encode("utf-8"),
                timeout=30,
            )
            if pr.status_code != 200:
                log.info(f"[MS Ads] Poll HTTP {pr.status_code} — retry")
                continue
            status = _ms_xml_val(pr.text, "Status")
            url    = _ms_xml_val(pr.text, "ReportDownloadUrl")
            if status == "Success" and url:
                download_url = url.replace("&amp;", "&")
                break
            if status == "Error":
                log.error("[MS Ads] Erreur génération rapport")
                return _ms_none_results()
            log.info(f"[MS Ads] Rapport en cours ({status or 'pending'})...")
        if not download_url:
            log.error("[MS Ads] Timeout rapport après 150s")
            return _ms_none_results()

        # 3) Download + unzip + parse CSV
        dr = requests.get(download_url, timeout=60)
        if dr.status_code != 200:
            log.error(f"[MS Ads] Download HTTP {dr.status_code}")
            return _ms_none_results()
        with zipfile.ZipFile(io.BytesIO(dr.content)) as z:
            entries = z.namelist()
            if not entries:
                log.warning("[MS Ads] ZIP vide → 0")
                return {s["name"]: 0.0 for s in MICROSOFT_ADS_STORES}
            csv_text = z.read(entries[0]).decode("utf-8-sig", errors="replace")

        lines = [l for l in csv_text.split("\n") if l.strip()]
        header_idx = next((i for i, l in enumerate(lines) if "accountname" in l.lower()), -1)
        if header_idx < 0:
            log.warning("[MS Ads] Header CSV introuvable → 0")
            return {s["name"]: 0.0 for s in MICROSOFT_ADS_STORES}
        sep = "\t" if "\t" in lines[header_idx] else ","
        hdr = [h.strip().strip('"').lower().replace(" ", "") for h in lines[header_idx].split(sep)]
        idx_id    = hdr.index("accountid") if "accountid" in hdr else -1
        idx_spend = hdr.index("spend")     if "spend"     in hdr else -1
        if idx_id < 0 or idx_spend < 0:
            log.error(f"[MS Ads] Colonnes AccountId/Spend introuvables : {hdr}")
            return _ms_none_results()

        spend_by_id = {}
        for l in lines[header_idx + 1:]:
            parts = [p.strip().strip('"') for p in l.split(sep)]
            if len(parts) <= max(idx_id, idx_spend):
                continue
            acc_id = parts[idx_id]
            try:
                spend = float(parts[idx_spend] or 0)
            except ValueError:
                continue
            spend_by_id[acc_id] = spend_by_id.get(acc_id, 0.0) + spend

        results = {}
        for s in MICROSOFT_ADS_STORES:
            v = round(spend_by_id.get(s["account_id"], 0.0), 2)
            results[s["name"]] = v
            log.info(f"[MS Ads {s['name']}] Dépenses = {v} €")
        return results

    except Exception as e:
        log.error(f"[MS Ads] Erreur : {e}")
        return _ms_none_results()


# =============================================================
# CDISCOUNT (Octopia Seller API) — CA HT par jour
# =============================================================
# OAuth2 client_credentials (token TTL 2h, cache local). Endpoint /orders
# paginé via pageIndex/pageSize. totalPrice.sellingPrice = TTC client.
# On exclut les statuts annulés/refusés. Conversion HT via TVA 20% (CDISFR).

_octopia_token_cache = {"token": None, "expires_at": 0}


def get_octopia_token() -> str:
    now = time.time()
    if _octopia_token_cache["token"] and now < _octopia_token_cache["expires_at"]:
        return _octopia_token_cache["token"]
    resp = requests.post(
        OCTOPIA_AUTH_URL,
        data={"grant_type":"client_credentials","client_id":OCTOPIA_CLIENT_ID,"client_secret":OCTOPIA_CLIENT_SECRET},
        headers={"Content-Type":"application/x-www-form-urlencoded"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"Octopia auth {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    _octopia_token_cache["token"] = data["access_token"]
    _octopia_token_cache["expires_at"] = now + data.get("expires_in", 7200) - 300
    log.info("  Token Octopia OK")
    return _octopia_token_cache["token"]


def fetch_cdiscount_ca_ht(date_str: str):
    """Retourne le CA HT Cdiscount sur une journée (en €).
    date_str : YYYY-MM-DD. Renvoie None en cas d'erreur API."""
    if not all([OCTOPIA_CLIENT_ID, OCTOPIA_CLIENT_SECRET, OCTOPIA_SELLER_ID]):
        log.warning("[Cdiscount] Credentials OCTOPIA_* manquants → skip")
        return None

    try:
        token = get_octopia_token()
    except Exception as e:
        log.error(f"[Cdiscount] Auth error : {e}")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "sellerId":      OCTOPIA_SELLER_ID,
        "Accept":        "application/json",
    }
    # Exclure les commandes annulées/refusées : leur sellingPrice est encore
    # présent mais elles n'ont rien rapporté. On accepte les "InPreparation",
    # "Shipped", "Delivered", "ToValidate", etc. (= toute commande active).
    EXCLUDED_STATUS = {"Cancelled", "Refused", "Refunded"}

    total_ttc = 0.0
    page = 1
    page_size = 100
    n_kept = 0
    n_excluded = 0
    while True:
        try:
            r = requests.get(
                f"{OCTOPIA_API_BASE}/orders",
                headers=headers,
                params={
                    "createdAtMin": f"{date_str}T00:00:00Z",
                    "createdAtMax": f"{date_str}T23:59:59Z",
                    "pageIndex":    page,
                    "pageSize":     page_size,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            log.error(f"[Cdiscount] HTTP error page {page} : {e}")
            return None

        if r.status_code != 200:
            log.error(f"[Cdiscount] /orders page {page} HTTP {r.status_code} : {r.text[:200]}")
            return None

        data = r.json()
        items = data if isinstance(data, list) else data.get("items", data.get("content", []))
        if not items:
            break

        for o in items:
            status = o.get("status", "")
            if status in EXCLUDED_STATUS:
                n_excluded += 1
                continue
            tp = o.get("totalPrice") or {}
            if isinstance(tp, dict):
                sp = tp.get("sellingPrice") or tp.get("offerPrice")
            else:
                sp = tp  # legacy : si l'API renvoie un nombre
            if sp is not None:
                try:
                    total_ttc += float(sp)
                    n_kept += 1
                except (TypeError, ValueError):
                    continue

        if len(items) < page_size:
            break
        page += 1
        time.sleep(0.5)

    ca_ht = round(total_ttc / (1 + OCTOPIA_VAT_RATE), 2)
    log.info(f"[Cdiscount] {n_kept} cmds gardées, {n_excluded} exclues → CA TTC {total_ttc:.2f} → CA HT {ca_ht:.2f} €")
    return ca_ht


# =============================================================
# GOOGLE SHEETS — Utilitaires
# =============================================================

def get_sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        {
            "type":         "service_account",
            "client_email": SERVICE_ACCOUNT_EMAIL,
            "private_key":  PRIVATE_KEY,
            "token_uri":    "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(n):
    result = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def ensure_sheet_tab(sheet_name: str) -> None:
    """Crée l'onglet sheet_name dans le spreadsheet s'il n'existe pas."""
    svc    = get_sheets_service()
    sheets = svc.spreadsheets()
    meta   = sheets.get(spreadsheetId=SHEET_ID, fields="sheets.properties.title").execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if sheet_name in titles:
        return
    log.info(f"[{sheet_name}] Onglet absent → création")
    sheets.batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    ).execute()


def write_report(sheet_name: str, date_str: str, results_by_name: dict, row_order: list,
                 force_overwrite: bool = False, exclude_from_total=None):
    """
    Fonction générique — écrit un rapport dans la feuille sheet_name.
    Structure : ligne 1 = dates, colonne A = noms, dernière ligne = TOTAL.

    exclude_from_total : noms de lignes à retirer de la formule TOTAL. Sert aux
    feuilles dont toutes les lignes ne sont pas dans la même devise — additionner
    des SEK à des euros ne produirait aucune grandeur exploitable.
    Ajoute automatiquement les nouvelles lignes si un nouveau nom apparaît.
    Idempotente par défaut : ne réécrit pas si la date existe déjà.
    Si force_overwrite=True : écrase la colonne existante (utilisé par le
    re-fetch attribution pour les ads).
    """
    ensure_sheet_tab(sheet_name)
    svc    = get_sheets_service()
    sheets = svc.spreadsheets()

    # On récupère le sheetId numérique + gridProperties dès le début, utilisé
    # plus loin pour batchUpdate (insertion de ligne / extension colonnes).
    sheet_meta = sheets.get(spreadsheetId=SHEET_ID, fields="sheets.properties").execute()
    sheet_id     = None
    current_cols = None
    for s in sheet_meta.get("sheets", []):
        if s["properties"]["title"] == sheet_name:
            sheet_id     = s["properties"]["sheetId"]
            current_cols = s["properties"]["gridProperties"]["columnCount"]
            break

    # Lecture colonne A (noms existants)
    col_a = sheets.values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!A:A",
    ).execute().get("values", [])

    existing_names = [row[0].strip() if row else "" for row in col_a]

    # Initialise si feuille vierge
    if not existing_names or existing_names[0].strip().lower() != "boutique":
        log.info(f"[{sheet_name}] Feuille vierge → initialisation")
        init_values = [["Boutique"]] + [[name] for name in row_order] + [["TOTAL"]]
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!A1",
            valueInputOption="RAW",
            body={"values": init_values},
        ).execute()
        existing_names = ["Boutique"] + row_order + ["TOTAL"]

    # Ajoute les nouveaux noms manquants (avant TOTAL). On utilise batchUpdate
    # + insertDimension (la méthode .values().insert() n'existe pas dans l'API
    # Sheets v4 — c'est .values().append() qui prend insertDataOption, mais
    # append ajoute à la fin et casserait la position de la ligne TOTAL).
    total_row_idx = next((i for i, n in enumerate(existing_names) if n.strip().upper() == "TOTAL"), len(existing_names))
    for name in row_order:
        if name not in existing_names:
            log.info(f"[{sheet_name}] Nouveau label détecté : {name} → insert ligne {total_row_idx + 1}")
            sheets.batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{
                    "insertDimension": {
                        "range": {
                            "sheetId":    sheet_id,
                            "dimension":  "ROWS",
                            "startIndex": total_row_idx,
                            "endIndex":   total_row_idx + 1,
                        },
                        "inheritFromBefore": False,
                    }
                }]},
            ).execute()
            sheets.values().update(
                spreadsheetId=SHEET_ID,
                range=f"'{sheet_name}'!A{total_row_idx + 1}",
                valueInputOption="RAW",
                body={"values": [[name]]},
            ).execute()
            existing_names.insert(total_row_idx, name)
            total_row_idx += 1

    # Relit ligne 1 pour chercher la colonne date
    row1 = sheets.values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!1:1",
    ).execute().get("values", [[]])[0]

    anchor_col = next((i for i, c in enumerate(row1) if str(c).strip().lower() == "boutique"), 0)

    last_date_col = anchor_col
    existing_date_col = None
    for i in range(anchor_col + 1, len(row1)):
        cell = str(row1[i]).strip()
        if cell:
            if cell == date_str:
                if force_overwrite:
                    existing_date_col = i
                    break
                log.info(f"[{sheet_name}] Date {date_str} déjà présente → rien à faire")
                return
            last_date_col = i

    if existing_date_col is not None:
        new_col = existing_date_col
        col     = col_letter(new_col)
        log.info(f"[{sheet_name}] Re-fetch : écrasement colonne {col} ({date_str})")
    else:
        new_col = last_date_col + 1
        col     = col_letter(new_col)
        log.info(f"[{sheet_name}] Écriture dans la colonne {col}")

    # Agrandir la feuille si la colonne dépasse la taille actuelle
    if current_cols is not None and new_col >= current_cols:
        cols_to_add = new_col - current_cols + 1
        log.info(f"[{sheet_name}] Ajout de {cols_to_add} colonne(s) (limite atteinte)")
        sheets.batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{
                "appendDimension": {
                    "sheetId":   sheet_id,
                    "dimension": "COLUMNS",
                    "length":    cols_to_add,
                }
            }]}
        ).execute()

    # Relit colonne A pour avoir l'ordre final
    col_a = sheets.values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!A:A",
    ).execute().get("values", [])
    final_names = [row[0].strip() if row else "" for row in col_a]
    total_row   = next((i + 1 for i, n in enumerate(final_names) if n.strip().upper() == "TOTAL"), len(final_names) + 1)

    # Si on est en force_overwrite avec une colonne existante, on lit les
    # cellules actuelles pour pouvoir préserver les valeurs déjà bonnes en
    # cas de None dans le re-fetch (évite de régresser une bonne valeur en 0).
    existing_col_values = []
    if force_overwrite and existing_date_col is not None:
        try:
            existing_col_values = sheets.values().get(
                spreadsheetId=SHEET_ID,
                range=f"'{sheet_name}'!{col}2:{col}{total_row - 1}",
            ).execute().get("values", [])
            existing_col_values = [(r[0] if r else None) for r in existing_col_values]
        except Exception as e:
            log.warning(f"[{sheet_name}] Impossible de relire colonne {col} : {e}")
            existing_col_values = []

    # Construit les valeurs dans l'ordre des lignes du sheet.
    # Règles :
    #  - Si valeur fetch OK → on l'écrit.
    #  - Si valeur fetch None ET force_overwrite ET cellule existante non vide
    #    et non-zero → on PRÉSERVE l'ancienne (et on queue un alert Telegram).
    #  - Sinon (premier write OU pas de valeur antérieure utile) → on écrit 0.
    values = [[date_str]]
    name_list = final_names[1:total_row - 1]  # ignore "Boutique" et "TOTAL"
    for i, name in enumerate(name_list):
        v = results_by_name.get(name)
        if v is not None:
            values.append([v])
            continue
        existing = existing_col_values[i] if i < len(existing_col_values) else None
        try:
            existing_num = float(str(existing).replace(",", ".")) if existing not in (None, "", "0", 0) else None
        except (TypeError, ValueError):
            existing_num = None
        if force_overwrite and existing_num is not None:
            values.append([existing])
            telegram_alert(f"• {sheet_name} / {date_str} / {name} : fetch a échoué → valeur préservée ({existing})")
            log.warning(f"[{sheet_name}] {date_str} / {name} : fetch None → préservé {existing}")
        else:
            values.append([0])

    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!{col}1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    # Ligne TOTAL. Par défaut on somme toute la plage ; si certaines lignes sont
    # dans une autre devise, on énumère explicitement les cellules à inclure
    # plutôt que de sommer des unités hétérogènes.
    exclus = set(exclude_from_total or ())
    if exclus:
        refs = [f"{col}{i + 2}" for i, name in enumerate(name_list) if name not in exclus]
        formule = ("=" + "+".join(refs)) if refs else "0"
        ignores = [n for n in name_list if n in exclus]
        log.info(f"  [{sheet_name}] TOTAL hors devise locale — exclu(s) : {', '.join(ignores)}")
    else:
        formule = f"=SUM({col}2:{col}{total_row - 1})"

    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!{col}{total_row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[formule]]}
    ).execute()

    log.info(f"✅ [{sheet_name}] Écrit — colonne {col}, date {date_str}")


def detect_missing_dates(max_days=5):
    """
    Lit la ligne 1 de chaque feuille de report et retourne un dict
    {date_str: set de clés de feuilles manquantes} pour les N derniers jours.
    """
    paris = ZoneInfo("Europe/Paris")

    expected = set()
    for i in range(1, max_days + 1):
        d = (datetime.now(paris) - timedelta(days=i)).date()
        expected.add(d.strftime("%d/%m/%Y"))

    # Feuilles à vérifier : (clé sheets_filter, nom feuille Google Sheets)
    sheet_checks = [
        ("shopify",    SHEET_SHOPIFY),
        ("gads",       SHEET_GADS),
        ("amazon",     SHEET_AMAZON),
        ("amazon-ads", SHEET_AMAZON_ADS),
        ("meta-ads",   SHEET_META_ADS),
        ("ms-ads",     SHEET_MS_ADS),
        ("cdiscount",  SHEET_CDISCOUNT),
    ]

    try:
        svc    = get_sheets_service()
        sheets = svc.spreadsheets()

        # {date_str: set de clés manquantes}
        missing_map = {}

        for key, sheet_name in sheet_checks:
            try:
                row1 = sheets.values().get(
                    spreadsheetId=SHEET_ID,
                    range=f"'{sheet_name}'!1:1",
                ).execute().get("values", [[]])[0]
                existing = {str(c).strip() for c in row1}
            except Exception as e:
                log.warning(f"[{sheet_name}] Impossible de lire la ligne 1 : {e}")
                existing = set()

            for d in expected:
                if d not in existing:
                    missing_map.setdefault(d, set()).add(key)

        # Trier par date chronologique
        sorted_dates = sorted(missing_map.keys(), key=lambda d: datetime.strptime(d, "%d/%m/%Y"))
        return [(d, missing_map[d]) for d in sorted_dates]

    except Exception as e:
        log.error(f"Erreur détection dates manquantes : {e}")
        return []


# =============================================================
# POINT D'ENTRÉE
# =============================================================

def run_for_date(target_date, sheets_filter=None, force_overwrite=False):
    """Exécute le report pour une date donnée. sheets_filter : set de noms de feuilles à traiter, ou None = toutes.
    force_overwrite : si True, réécrit la colonne même si la date existe déjà (utilisé par le re-fetch attribution)."""
    paris    = ZoneInfo("Europe/Paris")
    day      = target_date if isinstance(target_date, datetime) else datetime(target_date.year, target_date.month, target_date.day, tzinfo=paris)
    date_str = day.strftime("%d/%m/%Y")
    date_min = day.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    date_max = day.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
    date_api = day.strftime("%Y-%m-%d")

    run_all = sheets_filter is None

    # ── Shopify ──────────────────────────────────────────────
    if run_all or "shopify" in sheets_filter:
        log.info(f"=== Shopify CA HT — {date_str} ===")
        store_map      = {s["name"]: s for s in STORES}
        shopify_result = {}
        for name in STORE_ORDER:
            shopify_result[name] = fetch_ca_ht(store_map[name], date_min, date_max)
        log.info("--- Résultats Shopify ---")
        for name, val in shopify_result.items():
            log.info(f"  {name}: {val}")
        write_report(SHEET_SHOPIFY, date_str, shopify_result, STORE_ORDER, force_overwrite=force_overwrite)

    # ── Google Ads ───────────────────────────────────────────
    if run_all or "gads" in sheets_filter:
        log.info(f"=== Google Ads dépenses — {date_str} ===")
        gads_map    = {s["name"]: s for s in GADS_STORES}
        gads_result = {}
        for name in STORE_ORDER:
            gads_result[name] = fetch_gads_spend(gads_map[name]["customer_id"], date_api)
        log.info("--- Résultats Google Ads ---")
        for name, val in gads_result.items():
            log.info(f"  {name}: {val} €")
        write_report(SHEET_GADS, date_str, gads_result, STORE_ORDER, force_overwrite=force_overwrite)

    # ── Amazon ───────────────────────────────────────────────
    if run_all or "amazon" in sheets_filter:
        log.info(f"=== Amazon CA HT — {date_str} ===")
        amazon_result = fetch_amazon_ca_ht(date_api)
        log.info("--- Résultats Amazon ---")
        for name, val in amazon_result.items():
            log.info(f"  {name}: {val} €")
        write_report(SHEET_AMAZON, date_str, amazon_result, AMAZON_MARKETPLACE_ORDER,
                     force_overwrite=force_overwrite, exclude_from_total=AMAZON_NON_EUR)

    # ── Amazon Ads ────────────────────────────────────────────
    if run_all or "amazon-ads" in sheets_filter:
        log.info(f"=== Amazon Ads dépenses — {date_str} ===")
        amazon_ads_result = fetch_amazon_ads_spend(date_api)
        if amazon_ads_result:
            log.info("--- Résultats Amazon Ads ---")
            for name, val in amazon_ads_result.items():
                log.info(f"  {name}: {val} €")
            write_report(SHEET_AMAZON_ADS, date_str, amazon_ads_result, AMAZON_ADS_ORDER, force_overwrite=force_overwrite)

    # ── Meta Ads (LFC + COCO) ────────────────────────────────
    if run_all or "meta-ads" in sheets_filter:
        log.info(f"=== Meta Ads dépenses — {date_str} ===")
        meta_ads_result = fetch_meta_ads_spend_all(date_api)
        log.info("--- Résultats Meta Ads ---")
        for name, val in meta_ads_result.items():
            log.info(f"  {name}: {val} €")
        write_report(SHEET_META_ADS, date_str, meta_ads_result, META_ADS_ORDER, force_overwrite=force_overwrite)

    # ── Microsoft Ads (LFC) ──────────────────────────────────
    if run_all or "ms-ads" in sheets_filter:
        log.info(f"=== Microsoft Ads dépenses — {date_str} ===")
        ms_ads_result = fetch_ms_ads_spend(date_api)
        log.info("--- Résultats Microsoft Ads ---")
        for name, val in ms_ads_result.items():
            log.info(f"  {name}: {val} €")
        write_report(SHEET_MS_ADS, date_str, ms_ads_result, MICROSOFT_ADS_ORDER, force_overwrite=force_overwrite)

    # ── Cdiscount (Octopia compte ventes) ────────────────────
    if run_all or "cdiscount" in sheets_filter:
        log.info(f"=== Cdiscount CA HT — {date_str} ===")
        cdis_result = {"Cdiscount": fetch_cdiscount_ca_ht(date_api)}
        log.info("--- Résultats Cdiscount ---")
        for name, val in cdis_result.items():
            log.info(f"  {name}: {val} €")
        write_report(SHEET_CDISCOUNT, date_str, cdis_result, CDISCOUNT_ORDER, force_overwrite=force_overwrite)


def compare_amazon_methods(date_api: str) -> int:
    """DRY-RUN : compare les deux méthodes de calcul du CA Amazon sur une date,
    et n'écrit RIEN dans Google Sheets. Retourne un code de sortie shell.

    Sert à valider la méthode rapport (voie principale) contre l'Orders API
    (méthode historique) avant de faire confiance à la première.
    """
    log.info(f"=== DRY-RUN comparaison Amazon — {date_api} (aucune écriture Sheet) ===")

    log.info("\n--- Méthode 1/2 : rapport plat (voie principale) ---")
    erreur_rapport = None
    try:
        rapport = fetch_amazon_ca_ht(date_api)
    except Exception as e:
        erreur_rapport = e
        rapport = {m["name"]: None for m in AMAZON_MARKETPLACES}
        log.error(f"Méthode rapport en échec : {e}")

    log.info("\n--- Méthode 2/2 : Orders API (secours, lent) ---")
    try:
        legacy = fetch_amazon_ca_ht_via_orders_api(date_api)
    except Exception as e:
        legacy = {m["name"]: None for m in AMAZON_MARKETPLACES}
        log.error(f"Méthode Orders API en échec : {e}")

    def fmt(v):
        return "None" if v is None else f"{v:,.2f}".replace(",", " ")

    print(f"\n{'='*64}")
    print(f"  CA HT Amazon — {date_api}   (DRY-RUN, rien n'a été écrit)")
    print(f"{'='*64}")
    print(f"  {'Pays':<12} {'Rapport':>14} {'Orders API':>14} {'Écart':>14}")
    print(f"  {'-'*12} {'-'*14} {'-'*14} {'-'*14}")

    tot_r = tot_l = 0.0
    ecarts = 0
    # Le TOTAL ne porte que sur les pays en euros : additionner des SEK et des
    # PLN à des euros ne produirait aucune grandeur exploitable. Il n'a par
    # ailleurs de sens que si TOUS ces pays ont une valeur — sommer en ignorant
    # les None donnerait un chiffre qui ressemble à une vraie mesure.
    eur = [m["name"] for m in AMAZON_MARKETPLACES if m["currency"] == "EUR"]
    total_r_valide = all(rapport.get(n) is not None for n in eur)
    total_l_valide = all(legacy.get(n) is not None for n in eur)

    for m in AMAZON_MARKETPLACES:
        name = m["name"]
        r, l = rapport.get(name), legacy.get(name)
        if r is None or l is None:
            ecart = "n/a"
        else:
            d = r - l
            ecart = f"{d:+,.2f}".replace(",", " ")
            if abs(d) >= 0.01:
                ecarts += 1
                ecart += "  <-"
        if name in eur:
            if isinstance(r, (int, float)):
                tot_r += r
            if isinstance(l, (int, float)):
                tot_l += l
        # La devise est rappelée sur chaque ligne hors zone euro.
        suffixe = "" if m["currency"] == "EUR" else f" {m['currency']}"
        print(f"  {name:<12} {fmt(r) + suffixe:>14} {fmt(l) + suffixe:>14} {ecart:>14}")

    aff_r = f"{tot_r:,.2f}".replace(",", " ") if total_r_valide else "incomplet"
    aff_l = f"{tot_l:,.2f}".replace(",", " ") if total_l_valide else "incomplet"
    aff_d = (f"{tot_r - tot_l:+,.2f}".replace(",", " ")
             if total_r_valide and total_l_valide else "n/a")
    print(f"  {'-'*12} {'-'*14} {'-'*14} {'-'*14}")
    print(f"  {'TOTAL EUR':<12} {aff_r:>14} {aff_l:>14} {aff_d:>14}")
    hors = [m["name"] for m in AMAZON_MARKETPLACES if m["currency"] != "EUR"]
    print(f"  (hors {', '.join(hors)} — devises locales, non additionnables)")
    print(f"{'='*64}")

    if erreur_rapport is not None:
        print(f"  /!\\ Méthode rapport en échec : {type(erreur_rapport).__name__}: {erreur_rapport}")
    if all(v is None for v in rapport.values()):
        print("  /!\\ La méthode rapport n'a produit AUCUNE valeur exploitable.")
    elif ecarts:
        print(f"  /!\\ {ecarts} pays divergent(s) — à expliquer avant de faire confiance au rapport.")
        print("      Écarts attendus, tous en faveur du rapport (méthode principale) :")
        print("        • frais de port — jamais comptés par l'Orders API")
        print("        • autoliquidation intracommunautaire — l'Orders API divise un")
        print("          montant déjà HT par le taux du pays, sous-évaluant de ~16 %")
        print("        • commandes B2B où l'Orders API renvoie un prix déjà HT")
        print("      Les annulations et les devises SE/PL ne créent PLUS d'écart :")
        print("      filtre identique des deux côtés, et plus aucune conversion.")
    else:
        print("  Les deux méthodes concordent sur tous les pays.")
    print()

    return 0 if not all(v is None for v in rapport.values()) else 1


def main():
    parser = argparse.ArgumentParser(description="Daily report → Google Sheets")
    parser.add_argument("--compare-amazon", metavar="DATE",
                        help="DRY-RUN : compare rapport plat vs Orders API sur une date "
                             "(YYYY-MM-DD) et affiche les deux ventilations. N'écrit rien.")
    parser.add_argument("--backfill", metavar="FROM", help="Rattrapage depuis une date (YYYY-MM-DD ou DD/MM/YYYY)")
    parser.add_argument("--to", metavar="TO", help="Date de fin pour le backfill (défaut : hier)")
    parser.add_argument("--sheets", help="Feuilles à traiter, séparées par des virgules : shopify,gads,amazon,amazon-ads,meta-ads,ms-ads,cdiscount (défaut : toutes)")
    parser.add_argument("--force", action="store_true", help="Backfill : écrase les colonnes dont la date existe déjà (utile après une panne d'API qui a écrit des 0)")
    args = parser.parse_args()

    # DRY-RUN : sortie immédiate, avant toute initialisation Google Sheets.
    if args.compare_amazon:
        raw = args.compare_amazon
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            d = datetime.strptime(raw, "%d/%m/%Y").date()
        sys.exit(compare_amazon_methods(d.strftime("%Y-%m-%d")))

    paris = ZoneInfo("Europe/Paris")
    sheets_filter = None
    if args.sheets:
        sheets_filter = {s.strip().lower() for s in args.sheets.split(",")}

    if args.backfill:
        # Parse date de début
        raw = args.backfill
        try:
            start = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            start = datetime.strptime(raw, "%d/%m/%Y").date()

        # Parse date de fin (défaut : hier)
        if args.to:
            try:
                end = datetime.strptime(args.to, "%Y-%m-%d").date()
            except ValueError:
                end = datetime.strptime(args.to, "%d/%m/%Y").date()
        else:
            end = (datetime.now(paris) - timedelta(days=1)).date()

        mode = " (force overwrite)" if args.force else ""
        log.info(f"=== BACKFILL du {start} au {end}{mode} ===")
        current = start
        while current <= end:
            log.info(f"\n{'='*50}")
            log.info(f">>> Date : {current.strftime('%d/%m/%Y')}")
            log.info(f"{'='*50}")
            run_for_date(current, sheets_filter, force_overwrite=args.force)
            current += timedelta(days=1)
    else:
        # Auto-backfill : détecte les dates manquantes par feuille (5 derniers jours)
        missing = detect_missing_dates(max_days=5)
        if missing:
            log.info(f"=== AUTO-BACKFILL : {len(missing)} date(s) manquante(s) détectée(s) ===")
            for date_str, missing_sheets in missing:
                d = datetime.strptime(date_str, "%d/%m/%Y").date()
                log.info(f"\n{'='*50}")
                log.info(f">>> Rattrapage : {date_str} — feuilles : {', '.join(sorted(missing_sheets))}")
                log.info(f"{'='*50}")
                run_for_date(d, missing_sheets)

        yesterday = datetime.now(paris) - timedelta(days=1)
        run_for_date(yesterday, sheets_filter)

        # Re-fetch attribution : pour les ads, on re-fetche les ADS_REFETCH_LOOKBACK
        # derniers jours (J-1 inclus) avec force_overwrite=True. Ça permet de
        # capturer les ajustements de Meta/Amazon/Google/Microsoft Ads qui
        # continuent à arriver pendant 24-72h après chaque jour. Activé sur les
        # runs cron multi-passes (matin/midi/soir).
        ads_to_refetch = AD_SHEET_KEYS if sheets_filter is None else (sheets_filter & AD_SHEET_KEYS)
        if ads_to_refetch:
            log.info(f"\n{'='*50}")
            log.info(f"=== RE-FETCH ATTRIBUTION : {ADS_REFETCH_LOOKBACK} derniers jours pour {sorted(ads_to_refetch)} ===")
            log.info(f"{'='*50}")
            for d_offset in range(1, ADS_REFETCH_LOOKBACK + 1):
                d = (datetime.now(paris) - timedelta(days=d_offset)).date()
                log.info(f"\n>>> Re-fetch : {d.strftime('%d/%m/%Y')}")
                run_for_date(d, ads_to_refetch, force_overwrite=True)

    log.info("=== Terminé ===")
    flush_telegram_alerts()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
CA HT J-1 par boutique Shopify → Google Sheets (feuille "REPORT SHOPIFY VENTES")
Dépenses Google Ads J-1 par boutique → Google Sheets (feuille "REPORT GOOGLE ADS")
"""

import os
import sys
import logging
import requests
from datetime import datetime, timedelta
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
# CONFIG GOOGLE SHEETS
# =============================================================
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

SHEET_SHOPIFY  = "REPORT SHOPIFY VENTES"
SHEET_GADS     = "REPORT GOOGLE ADS"
# SHEET_AMAZON = "REPORT AMAZON VENTES"  # à activer plus tard

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
    import time
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


def write_report(sheet_name: str, date_str: str, results_by_name: dict, store_order: list):
    """
    Fonction générique — écrit un rapport dans la feuille sheet_name.
    Structure : ligne 1 = dates, colonne A = noms, dernière ligne = TOTAL.
    Idempotente : ne réécrit pas si la date existe déjà.
    """
    svc    = get_sheets_service()
    sheets = svc.spreadsheets()

    # Lecture ligne 1
    row1 = sheets.values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!1:1",
    ).execute().get("values", [[]])[0]

    # Cherche la colonne "Boutique" (ancre)
    anchor_col = None
    for i, cell in enumerate(row1):
        if str(cell).strip().lower() == "boutique":
            anchor_col = i
            break

    # Initialise la structure si feuille vierge
    if anchor_col is None:
        log.info(f"[{sheet_name}] Feuille vierge → initialisation")
        init_values = [["Boutique"]] + [[name] for name in store_order] + [["TOTAL"]]
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{sheet_name}'!A1",
            valueInputOption="RAW",
            body={"values": init_values},
        ).execute()
        anchor_col = 0
        row1       = ["Boutique"]

    # Cherche la dernière colonne avec une date + vérifie idempotence
    last_date_col = anchor_col
    for i in range(anchor_col + 1, len(row1)):
        cell = str(row1[i]).strip()
        if cell:
            if cell == date_str:
                log.info(f"[{sheet_name}] Date {date_str} déjà présente → rien à faire")
                return
            last_date_col = i

    # Nouvelle colonne
    new_col = last_date_col + 1
    col     = col_letter(new_col)
    log.info(f"[{sheet_name}] Écriture dans la colonne {col}")

    # Valeurs : date + résultats par boutique
    values = [[date_str]]
    for name in store_order:
        v = results_by_name.get(name)
        values.append([v if v is not None else ""])

    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!{col}1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    # Ligne TOTAL
    total_row = len(store_order) + 2  # ligne 1 = date, lignes 2..N = boutiques, ligne N+1 = TOTAL
    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{sheet_name}'!{col}{total_row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[f"=SUM({col}2:{col}{total_row - 1})"]]}
    ).execute()

    log.info(f"✅ [{sheet_name}] Écrit — colonne {col}, date {date_str}")


# =============================================================
# POINT D'ENTRÉE
# =============================================================

def main():
    paris     = ZoneInfo("Europe/Paris")
    yesterday = datetime.now(paris) - timedelta(days=1)
    date_str  = yesterday.strftime("%d/%m/%Y")

    date_min = yesterday.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    date_max = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
    date_api = yesterday.strftime("%Y-%m-%d")

    # ── Shopify ──────────────────────────────────────────────
    log.info(f"=== Shopify CA HT — {date_str} ===")
    store_map      = {s["name"]: s for s in STORES}
    shopify_result = {}

    for name in STORE_ORDER:
        shopify_result[name] = fetch_ca_ht(store_map[name], date_min, date_max)

    log.info("--- Résultats Shopify ---")
    for name, val in shopify_result.items():
        log.info(f"  {name}: {val}")

    write_report(SHEET_SHOPIFY, date_str, shopify_result, STORE_ORDER)

    # ── Google Ads ───────────────────────────────────────────
    log.info(f"=== Google Ads dépenses — {date_str} ===")
    gads_map    = {s["name"]: s for s in GADS_STORES}
    gads_result = {}

    for name in STORE_ORDER:
        gads_result[name] = fetch_gads_spend(gads_map[name]["customer_id"], date_api)

    log.info("--- Résultats Google Ads ---")
    for name, val in gads_result.items():
        log.info(f"  {name}: {val} €")

    write_report(SHEET_GADS, date_str, gads_result, STORE_ORDER)

    log.info("=== Terminé ===")


if __name__ == "__main__":
    main()

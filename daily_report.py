#!/usr/bin/env python3
"""
CA HT J-1 par boutique Shopify → Google Sheets
Même mécanisme OAuth que shopify_pennylane.py
"""

import os
import sys
import logging
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

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
# CONFIG BOUTIQUES (identique à shopify_pennylane.py)
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

# Ordre fixe des boutiques dans le sheet (lignes 2 à 10)
STORE_ORDER = ["LFC", "LVO", "UNI", "TAR", "HET", "RED", "COCO", "MON", "RETE"]

# =============================================================
# CONFIG GOOGLE SHEETS
# =============================================================
SHEET_ID   = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_NAME = os.environ.get("GOOGLE_SHEET_TAB", "Report auto")

SERVICE_ACCOUNT_EMAIL = os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
PRIVATE_KEY           = os.environ.get("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")

# =============================================================
# SHOPIFY — OAuth + appel API (identique à shopify_pennylane.py)
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
    """Retourne le CA HT du jour pour une boutique (0 si pas de commandes, None si erreur)."""
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

            # Pagination
            link = r.headers.get("Link", "")
            import re
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None

        ca_ht = round(sum(float(o["total_price"]) - float(o["total_tax"]) for o in all_orders), 2)
        log.info(f"[{store['name']}] {len(all_orders)} commande(s) → CA HT = {ca_ht}")
        return ca_ht

    except Exception as e:
        log.error(f"[{store['name']}] Erreur : {e}")
        return None


# =============================================================
# GOOGLE SHEETS
# =============================================================
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": SERVICE_ACCOUNT_EMAIL,
            "private_key": PRIVATE_KEY,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def col_letter(n):
    """Convertit un index 0-based en lettre de colonne (0→A, 1→B, 26→AA…)."""
    result = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def write_daily_report(date_str, results_by_name):
    """
    Écrit une colonne dans le sheet :
    - Cherche dynamiquement la colonne 'Boutique' en ligne 1
    - Initialise la structure si le sheet est vierge
    - Vérifie que la date n'est pas déjà présente (idempotence)
    - Ajoute la colonne à droite de la dernière date existante
    """
    svc    = get_sheets_service()
    sheets = svc.spreadsheets()

    # --- Lire toute la ligne 1 ---
    row1 = sheets.values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!1:1",
    ).execute().get("values", [[]])[0]

    # --- Trouver la colonne "Boutique" ---
    anchor_col = None
    for i, cell in enumerate(row1):
        if str(cell).strip().lower() == "boutique":
            anchor_col = i
            break

    # --- Initialiser si vierge ---
    if anchor_col is None:
        log.info("Sheet vierge → initialisation de la structure")
        init_values = [["Boutique"]] + [[name] for name in STORE_ORDER] + [["TOTAL"]]
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": init_values},
        ).execute()
        anchor_col = 0
        row1 = ["Boutique"]

    # --- Trouver la prochaine colonne disponible ---
    # Scanner à droite de anchor_col pour trouver les dates existantes
    last_date_col = anchor_col  # colonne du dernier header date trouvé
    for i in range(anchor_col + 1, len(row1)):
        cell = str(row1[i]).strip()
        if cell:  # toute cellule non vide après "Boutique" est une date
            if cell == date_str:
                log.info(f"Date {date_str} déjà dans le sheet → rien à faire")
                return
            last_date_col = i

    new_col = last_date_col + 1
    col = col_letter(new_col)
    log.info(f"Écriture dans la colonne {col} (index {new_col})")

    # --- Construire les valeurs ---
    values = [[date_str]]  # ligne 1 : date
    for name in STORE_ORDER:
        v = results_by_name.get(name)
        values.append([v if v is not None else ""])

    # --- Écrire date + valeurs ---
    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!{col}1",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()

    # --- Écrire la formule TOTAL en ligne 11 ---
    total_row = 11
    formula = f"=SUM({col}2:{col}{total_row - 1})"
    sheets.values().update(
        spreadsheetId=SHEET_ID,
        range=f"'{SHEET_NAME}'!{col}{total_row}",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]},
    ).execute()

    log.info(f"✅ Colonne {col} écrite — date: {date_str}")


# =============================================================
# POINT D'ENTRÉE
# =============================================================
def main():
    paris = ZoneInfo("Europe/Paris")
    yesterday = datetime.now(paris) - timedelta(days=1)
    date_str  = yesterday.strftime("%Y-%m-%d")

    date_min = yesterday.replace(hour=0,  minute=0,  second=0,  microsecond=0).isoformat()
    date_max = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

    log.info(f"=== Rapport CA HT — {date_str} ===")
    log.info(f"Fenêtre : {date_min} → {date_max}")

    store_map = {s["name"]: s for s in STORES}
    results   = {}

    for name in STORE_ORDER:
        store = store_map[name]
        results[name] = fetch_ca_ht(store, date_min, date_max)

    log.info("--- Résultats ---")
    for name, val in results.items():
        log.info(f"  {name}: {val}")

    write_daily_report(date_str, results)
    log.info("=== Terminé ===")


if __name__ == "__main__":
    main()

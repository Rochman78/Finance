"""
tresorerie.py
Récupère les données de trésorerie :
- Comptes bancaires 512 (via trial_balance Pennylane — instantané)
- Encaissements en transit 411INTERNET (Pennylane)
- Balances Shopify Payments à verser (API Shopify)
"""

import os, json, time, logging, requests
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

SHOPIFY_API_VERSION = "2026-01"
STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  "")},
    {"name": "MTC",  "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  "")},
    {"name": "MO",   "store": "mon-ombrage.myshopify.com",             "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO",   "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
    {"name": "TZ",   "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   "")},
    {"name": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",  "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  "")},
    {"name": "UNIV", "store": "univers-camouflage.myshopify.com",      "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", "")},
]

COMPTE_411INTERNET_ID = 177721907
JOURNAL_ENCSP_ID = 13509687

_token_cache = {}


def pl_get(url, params=None):
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 10))
            continue
        return None
    return None


def pl_get_all(endpoint, params=None):
    all_items = []
    cursor = None
    while True:
        p = {"limit": 100}
        if params: p.update(params)
        if cursor: p["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/{endpoint}", p)
        if not data: break
        all_items.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"): break
        cursor = data["next_cursor"]
    return all_items


def get_shopify_token(store_config):
    store_url = store_config["store"]
    cached = _token_cache.get(store_url)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    resp = requests.post(
        f"https://{store_url}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": store_config["client_id"], "client_secret": store_config["client_secret"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    _token_cache[store_url] = {"token": data["access_token"], "expires_at": time.time() + data.get("expires_in", 86399)}
    return data["access_token"]


# =============================================================
# 1. COMPTES BANCAIRES 512 (via trial_balance)
# =============================================================
def get_soldes_512():
    """Retourne le solde de chaque compte 512 via la balance générale (paginée)."""
    today = datetime.now().strftime("%Y-%m-%d")

    all_items = []
    cursor = None
    while True:
        params = {"period_start": "2025-01-01", "period_end": today, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/trial_balance", params)
        if not data:
            break
        all_items.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    results = []
    for item in all_items:
        number = item.get("number", "")
        if number.startswith("512"):
            debits = float(item.get("debits", 0))
            credits = float(item.get("credits", 0))
            solde = round(debits - credits, 2)
            if solde != 0:
                results.append({
                    "compte": number,
                    "label": item.get("label", ""),
                    "solde": solde,
                })

    total = round(sum(r["solde"] for r in results), 2)
    log.info(f"512 : {len(results)} comptes, total = {total:.2f}€")
    return {"accounts": results, "total": total}


# =============================================================
# 2. ENCAISSEMENTS EN TRANSIT (411INTERNET)
# =============================================================
def get_transit_411internet():
    """Retourne les lignes non lettrées du compte 411INTERNET, date >= aujourd'hui."""
    today = datetime.now().strftime("%Y-%m-%d")

    fl = json.dumps([
        {"field": "ledger_account_id", "operator": "eq", "value": str(COMPTE_411INTERNET_ID)},
        {"field": "date", "operator": "gteq", "value": today},
    ])
    lines = pl_get_all("ledger_entry_lines", {"filter": fl})

    transit_lines = []
    for l in lines:
        lettered = l.get("lettered_ledger_entry_lines", {}).get("ids", [])
        if lettered:
            continue

        debit = float(l.get("debit", 0))
        credit = float(l.get("credit", 0))
        montant = round(debit - credit, 2)

        transit_lines.append({
            "date": l.get("date", ""),
            "label": l.get("label", ""),
            "montant": montant,
        })

    total = round(sum(t["montant"] for t in transit_lines), 2)
    log.info(f"411INTERNET transit : {len(transit_lines)} lignes, total = {total:.2f}€")
    return {"lines": transit_lines, "total": total}


# =============================================================
# 3. SHOPIFY BALANCES (À VERSER)
# =============================================================
def get_shopify_balances():
    """Retourne le solde 'à verser' de chaque boutique Shopify."""
    results = []
    for store in STORES:
        token = get_shopify_token(store)
        if not token:
            continue
        url = f"https://{store['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance.json"
        for attempt in range(3):
            try:
                resp = requests.get(url, headers={"X-Shopify-Access-Token": token}, timeout=30)
            except requests.exceptions.RequestException:
                time.sleep(2)
                continue
            if resp.status_code == 200:
                for b in resp.json().get("balance", []):
                    amount = float(b.get("amount", 0))
                    if amount != 0:
                        results.append({
                            "boutique": store["name"],
                            "montant": round(amount, 2),
                            "devise": b.get("currency", "EUR"),
                        })
                break
            if resp.status_code == 429:
                time.sleep(2)
                continue
            break

    total = round(sum(r["montant"] for r in results), 2)
    log.info(f"Shopify balances : {len(results)} boutiques, total = {total:.2f}€")
    return {"stores": results, "total": total}


# =============================================================
# ORCHESTRATION
# =============================================================
def get_tresorerie():
    """Retourne la trésorerie complète."""
    log.info("=== Trésorerie ===")

    banque = get_soldes_512()
    transit = get_transit_411internet()
    shopify = get_shopify_balances()

    tresorerie_reelle = round(banque["total"] + transit["total"] + shopify["total"], 2)

    log.info(f"Trésorerie réelle : {tresorerie_reelle:.2f}€")

    return {
        "banque": banque,
        "transit": transit,
        "shopify": shopify,
        "tresorerie_reelle": tresorerie_reelle,
    }

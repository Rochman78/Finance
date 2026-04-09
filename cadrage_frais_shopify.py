"""
cadrage_frais_shopify.py
Cadrage des frais Shopify Payments : Shopify API vs Pennylane (compte 627001, journal ENCSP)

Usage:
    python cadrage_frais_shopify.py --date-min 2026-04-01 --date-max 2026-04-30
    python cadrage_frais_shopify.py --date-min 2026-04-01 --date-max 2026-04-30 --store LFC
"""

import os, json, time, logging, argparse, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- CONFIG ---
SHOPIFY_API_VERSION = "2026-01"
PENNYLANE_TOKEN     = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE             = "https://app.pennylane.com/api/external/v2"
PL_HEADERS          = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}
COMPTE_FRAIS        = "627001"
JOURNAL_ENCSP_ID    = 13509687

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

# =============================================================
# SHOPIFY AUTH (identique à shopify_pennylane.py)
# =============================================================
_token_cache = {}

def get_shopify_token(store_config: dict) -> str | None:
    store_url = store_config["store"]
    cached = _token_cache.get(store_url)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    resp = requests.post(
        f"https://{store_url}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": store_config["client_id"],
              "client_secret": store_config["client_secret"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if resp.status_code != 200:
        log.error(f"❌ Shopify auth {store_url}: {resp.status_code}")
        return None
    data  = resp.json()
    token = data["access_token"]
    _token_cache[store_url] = {"token": token, "expires_at": time.time() + data.get("expires_in", 86399)}
    return token

def shopify_get(url: str, token: str, params: dict = None) -> dict | None:
    for attempt in range(5):
        resp = requests.get(url,
                            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                            params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 16))
            continue
        log.error(f"❌ Shopify GET {url}: {resp.status_code}")
        return None
    return None

# =============================================================
# SHOPIFY — Frais réels par boutique sur la période
# =============================================================
def get_payouts(token: str, store: str, date_min: str, date_max: str) -> list:
    url    = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/payouts.json"
    params = {"date_min": date_min, "date_max": date_max}
    data   = shopify_get(url, token, params)
    return data.get("payouts", []) if data else []

def get_payout_transactions(token: str, store: str, payout_id: str) -> list:
    url    = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance/transactions.json"
    txns   = []
    params = {"payout_id": payout_id, "limit": 250}
    while True:
        data  = shopify_get(url, token, params)
        if not data:
            break
        batch = data.get("transactions", [])
        txns.extend(batch)
        if len(batch) < 250:
            break
        params["since_id"] = batch[-1]["id"]
    return txns

def get_frais_shopify(store_config: dict, date_min: str, date_max: str) -> dict:
    sname = store_config["name"]
    token = get_shopify_token(store_config)
    if not token:
        return {"store": sname, "frais": None, "nb_payouts": 0, "erreur": "auth failed"}

    payouts = get_payouts(token, store_config["store"], date_min, date_max)
    if not payouts:
        return {"store": sname, "frais": 0.0, "nb_payouts": 0}

    total_frais = 0.0
    for payout in payouts:
        txns = get_payout_transactions(token, store_config["store"], str(payout["id"]))
        for txn in txns:
            if txn.get("type") == "payout":
                continue
            fee = float(txn.get("fee") or 0)
            if fee != 0:
                total_frais += abs(fee)

    log.info(f"[{sname}] Frais Shopify : {total_frais:.2f}€ ({len(payouts)} payouts)")
    return {"store": sname, "frais": round(total_frais, 2), "nb_payouts": len(payouts)}

# =============================================================
# PENNYLANE — Frais 627001 dans journal ENCSP sur la période
# =============================================================
def get_frais_pennylane(date_min: str, date_max: str) -> float | None:
    """
    Parcourt toutes les écritures du journal ENCSP sur la période,
    descend dans chaque écriture, somme les débits sur le compte 627001.
    """
    filter_param = json.dumps([
        {"field": "date", "operator": "gteq", "value": date_min},
        {"field": "date", "operator": "lteq", "value": date_max},
        {"field": "journal_id", "operator": "eq", "value": JOURNAL_ENCSP_ID},
    ])

    # Récupère toutes les écritures ENCSP sur la période
    entries = []
    cursor  = None
    while True:
        params = {"filter": filter_param, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(3):
            resp = requests.get(f"{PL_BASE}/ledger_entries", headers=PL_HEADERS, params=params, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 10))
                continue
            log.error(f"❌ Pennylane ledger_entries: {resp.status_code} {resp.text[:200]}")
            return None
        else:
            return None

        data = resp.json()
        entries.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break

    log.info(f"Pennylane : {len(entries)} écriture(s) ENCSP sur la période")

    # Pour chaque écriture, descend chercher les lignes et somme 627001
    total_frais = 0.0
    for entry in entries:
        resp = requests.get(f"{PL_BASE}/ledger_entries/{entry['id']}", headers=PL_HEADERS, timeout=30)
        if resp.status_code != 200:
            log.error(f"❌ Pennylane ledger_entry {entry['id']}: {resp.status_code}")
            continue
        for line in resp.json().get("ledger_entry_lines", []):
            account_number = line.get("ledger_account", {}).get("number", "")
            if account_number == COMPTE_FRAIS:
                total_frais += float(line.get("debit") or 0)
                total_frais -= float(line.get("credit") or 0)  # remboursements frais

    return round(total_frais, 2)

# =============================================================
# CADRAGE
# =============================================================
def cadrage(date_min: str, date_max: str, store_filter: str | None = None):
    log.info(f"=== Cadrage frais Shopify Payments — {date_min} → {date_max} ===")

    stores = [s for s in STORES if not store_filter or s["name"] == store_filter]

    # 1. Frais Shopify par boutique
    resultats   = []
    total_shopify = 0.0
    for store_config in stores:
        r = get_frais_shopify(store_config, date_min, date_max)
        resultats.append(r)
        if r.get("frais") is not None:
            total_shopify += r["frais"]

    # 2. Frais Pennylane (627001, ENCSP)
    total_pennylane = get_frais_pennylane(date_min, date_max)
    if total_pennylane is None:
        log.error("❌ Impossible de récupérer les frais Pennylane")
        return

    # 3. Écart
    ecart = round(total_shopify - total_pennylane, 2)

    # 4. Affichage
    print("\n" + "=" * 60)
    print(f"  CADRAGE FRAIS SHOPIFY PAYMENTS")
    print(f"  Période : {date_min} → {date_max}")
    print("=" * 60)
    print(f"\n  {'Boutique':<10} {'Frais Shopify':>15} {'Payouts':>10}")
    print("  " + "-" * 38)
    for r in resultats:
        if r.get("frais") is None:
            print(f"  {r['store']:<10} {'ERREUR':>15} {'-':>10}")
        elif r["frais"] > 0:
            print(f"  {r['store']:<10} {r['frais']:>14.2f}€ {r['nb_payouts']:>10}")

    print("  " + "-" * 38)
    print(f"\n  {'Total Shopify':<35} {total_shopify:>10.2f}€")
    print(f"  {'Total Pennylane (627001 / ENCSP)':<35} {total_pennylane:>10.2f}€")
    print(f"  {'Écart':<35} {ecart:>10.2f}€")

    if ecart == 0.0:
        statut = "✅ CADRAGE OK — écart nul"
    elif abs(ecart) < 0.05:
        statut = f"⚠️  Micro-écart ({ecart}€) — probablement arrondi"
    else:
        statut = f"❌ ÉCART SIGNIFICATIF : {ecart}€ — investigation requise"

    print(f"\n  {statut}")
    print("=" * 60 + "\n")

    return {
        "date_min":        date_min,
        "date_max":        date_max,
        "total_shopify":   total_shopify,
        "total_pennylane": total_pennylane,
        "ecart":           ecart,
        "statut":          statut,
        "details":         resultats,
    }

# =============================================================
# MAIN
# =============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-min", required=True, help="Date début YYYY-MM-DD")
    parser.add_argument("--date-max", required=True, help="Date fin YYYY-MM-DD")
    parser.add_argument("--store",    default=None,  help="Filtrer sur une boutique (ex: LFC)")
    args = parser.parse_args()

    cadrage(args.date_min, args.date_max, args.store)

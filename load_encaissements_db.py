"""
load_encaissements_db.py
Charge les données de cadrage encaissements depuis les APIs Pennylane/Shopify
et les stocke en SQLite pour un accès instantané.

Usage:
    python load_encaissements_db.py --date-min 2026-03-01 --date-max 2026-03-15
"""

import os, json, time, re, logging, argparse, sqlite3, requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2026-01"
PENNYLANE_TOKEN     = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE             = "https://app.pennylane.com/api/external/v2"
PL_HEADERS          = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

ORDER_PREFIXES = r'(?:LFC|RDC|HC|COCO|MO|RM|TZ|LVO|UNIV)'

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

STORE_PREFIXES = {"LFC": ["LFC"], "RED": ["RDC"], "HET": ["HC"], "MTC": ["COCO"],
                  "MO": ["MO"], "RETE": ["RM"], "TZ": ["TZ"],
                  "LVO": ["LVO"], "UNIV": ["UNIV"]}

COMPTES_EXCLUS = {"411INTERNET", "411MOLLIE", "411KLARNA", "411NA"}

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "encaissements.db")

# =============================================================
# API HELPERS
# =============================================================
_token_cache = {}

def get_shopify_token(store_config):
    store_url = store_config["store"]
    cached = _token_cache.get(store_url)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    resp = requests.post(
        f"https://{store_url}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": store_config["client_id"],
              "client_secret": store_config["client_secret"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    if resp.status_code != 200:
        log.error(f"❌ Shopify auth {store_url}: {resp.status_code}")
        return None
    data = resp.json()
    token = data["access_token"]
    _token_cache[store_url] = {"token": token, "expires_at": time.time() + data.get("expires_in", 86399)}
    return token

def shopify_get(url, token, params=None):
    for attempt in range(5):
        resp = requests.get(url, headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                            params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 16))
            continue
        log.error(f"❌ Shopify GET {url}: {resp.status_code}")
        return None
    return None

def pl_get(url, params=None):
    for attempt in range(3):
        resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 10))
            continue
        log.error(f"❌ Pennylane GET {url}: {resp.status_code} {resp.text[:200]}")
        return None
    return None

def pl_get_all(endpoint, params=None):
    all_items = []
    cursor = None
    while True:
        p = {"limit": 100}
        if params:
            p.update(params)
        if cursor:
            p["cursor"] = cursor
        for attempt in range(5):
            try:
                resp = requests.get(f"{PL_BASE}/{endpoint}", headers=PL_HEADERS, params=p, timeout=30)
            except requests.exceptions.RequestException:
                time.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 10))
            else:
                return all_items
        else:
            return all_items
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return all_items

def _store_from_order(order_ref):
    for sname, prefixes in STORE_PREFIXES.items():
        if any(order_ref.startswith(p) for p in prefixes):
            return sname
    return None

# =============================================================
# DB SETUP
# =============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS encaissements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            account_number TEXT,
            account_label TEXT,
            order_ref TEXT,
            store TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            solde REAL,
            nb_ecritures INTEGER,
            has_payout INTEGER,
            payout_date TEXT,
            payout_amount REAL,
            total_price REAL,
            financial_status TEXT,
            statut TEXT,
            loaded_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enc_order ON encaissements(order_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_enc_statut ON encaissements(statut)")
    conn.commit()
    return conn

# =============================================================
# ÉTAPE 1 : Charger les factures avec commande Shopify
# =============================================================
def load_invoices(date_min, date_max):
    """Charge les factures Pennylane sur la période, retourne les comptes 411 liés."""
    accounts = {}
    d = datetime.strptime(date_min, "%Y-%m-%d")
    end = datetime.strptime(date_max, "%Y-%m-%d")

    while d <= end:
        day_str = d.strftime("%Y-%m-%d")
        d += timedelta(days=1)

        filter_param = json.dumps([{"field": "date", "operator": "eq", "value": day_str}])
        cursor = None
        while True:
            params = {"filter": filter_param, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = pl_get(f"{PL_BASE}/customer_invoices", params)
            if not data:
                break

            for inv in data.get("items", []):
                special = inv.get("special_mention", "") or ""
                label = inv.get("label", "") or ""
                text = f"{special} {label}"

                m = re.search(ORDER_PREFIXES + r'\d{3,}', text, re.IGNORECASE)
                if not m:
                    continue

                order_ref = m.group(0).upper()
                store = _store_from_order(order_ref)
                if not store:
                    continue

                inv_number = inv.get("invoice_number", "")
                inv_date = inv.get("date", day_str)

                # Récupérer le compte 411
                ledger_entry = inv.get("ledger_entry")
                if not ledger_entry or not ledger_entry.get("id"):
                    inv_detail = pl_get(f"{PL_BASE}/customer_invoices/{inv['id']}")
                    if inv_detail:
                        ledger_entry = inv_detail.get("ledger_entry")

                if not ledger_entry or not ledger_entry.get("id"):
                    continue

                entry_detail = pl_get(f"{PL_BASE}/ledger_entries/{ledger_entry['id']}")
                if not entry_detail:
                    continue

                for line in entry_detail.get("ledger_entry_lines", []):
                    acc = line.get("ledger_account", {})
                    acc_number = acc.get("number", "")
                    if acc_number.startswith("411") and acc_number not in COMPTES_EXCLUS:
                        acc_id = acc.get("id")
                        if acc_id and acc_id not in accounts:
                            accounts[acc_id] = {
                                "account_id": acc_id,
                                "account_number": acc_number,
                                "account_label": "",
                                "order_ref": order_ref,
                                "store": store,
                                "invoice_number": inv_number,
                                "invoice_date": inv_date,
                            }
                        break

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

        log.info(f"  {day_str} — {len(accounts)} comptes 411 cumulés")

    # Enrichir labels
    for acc_id, info in accounts.items():
        filter_param = json.dumps([{"field": "number", "operator": "eq", "value": info["account_number"]}])
        acc_data = pl_get(f"{PL_BASE}/ledger_accounts", {"filter": filter_param, "limit": 1})
        if acc_data:
            for item in acc_data.get("items", []):
                if item.get("number") == info["account_number"]:
                    info["account_label"] = item.get("label", "")
                    break

    log.info(f"Total : {len(accounts)} comptes 411 uniques avec commande Shopify")
    return accounts

# =============================================================
# ÉTAPE 2 : Soldes
# =============================================================
def compute_balances(accounts):
    for i, (acc_id, info) in enumerate(accounts.items()):
        filter_lines = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(acc_id)}])
        lines = pl_get_all("ledger_entry_lines", {"filter": filter_lines})
        total_debit = sum(float(l.get("debit", 0)) for l in lines)
        total_credit = sum(float(l.get("credit", 0)) for l in lines)
        info["solde"] = round(total_debit - total_credit, 2)
        info["nb_ecritures"] = len(lines)
        if (i + 1) % 20 == 0:
            log.info(f"  Soldes : {i + 1}/{len(accounts)}")
    return accounts

# =============================================================
# ÉTAPE 3 : Payouts Shopify
# =============================================================
def check_payouts(accounts, seuil=0.05):
    unsettled = {k: v for k, v in accounts.items() if abs(v["solde"]) > seuil}
    log.info(f"{len(unsettled)} comptes non soldés (seuil {seuil}€)")

    for i, (acc_id, acc) in enumerate(unsettled.items()):
        order_ref = acc["order_ref"]
        store_name = acc["store"]
        store_config = next((s for s in STORES if s["name"] == store_name), None)
        if not store_config:
            acc["has_payout"] = False
            continue

        token = get_shopify_token(store_config)
        if not token:
            acc["has_payout"] = False
            continue

        url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
        data = shopify_get(url, token, {
            "name": order_ref, "status": "any",
            "fields": "id,name,financial_status,created_at,total_price", "limit": 5,
        })

        if not data or not data.get("orders"):
            acc["has_payout"] = False
            acc["financial_status"] = ""
            acc["payout_date"] = None
            acc["payout_amount"] = 0
            acc["total_price"] = 0
            continue

        order = data["orders"][0]
        financial_status = order.get("financial_status", "")
        total_price = float(order.get("total_price", 0))
        acc["financial_status"] = financial_status
        acc["total_price"] = total_price

        if financial_status in ("paid", "partially_refunded", "refunded"):
            acc["has_payout"] = True
            # Chercher la date du payout
            created = order.get("created_at", "")[:10]
            acc["payout_date"] = None
            acc["payout_amount"] = total_price
            if created:
                pdate_min = (datetime.strptime(created, "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
                pdate_max = (datetime.strptime(created, "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
                payout_url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/payouts.json"
                payout_data = shopify_get(payout_url, token, {"date_min": pdate_min, "date_max": pdate_max})
                payouts = payout_data.get("payouts", []) if payout_data else []
                order_id = order["id"]
                for payout in payouts:
                    t_url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance/transactions.json"
                    txns_data = shopify_get(t_url, token, {"payout_id": payout["id"], "limit": 250})
                    txns = txns_data.get("transactions", []) if txns_data else []
                    for txn in txns:
                        if txn.get("source_order_id") == order_id:
                            acc["payout_date"] = payout["date"]
                            acc["payout_amount"] = float(txn.get("amount", 0))
                            break
                    if acc["payout_date"]:
                        break
        else:
            acc["has_payout"] = False
            acc["payout_date"] = None
            acc["payout_amount"] = 0

        if (i + 1) % 10 == 0:
            log.info(f"  Payouts : {i + 1}/{len(unsettled)}")

    return unsettled

# =============================================================
# ÉTAPE 3b : Exclure les faux positifs (commandes récentes < 5 jours)
# =============================================================
JOURS_RECENTS = 5

def exclude_recent_orders(unsettled):
    """
    Pour chaque anomalie (has_payout=True), vérifie si le client a des commandes
    de moins de JOURS_RECENTS jours. Si oui, le solde non soldé est probablement
    dû à ces nouvelles commandes → on ne remonte pas l'anomalie.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.now() - timedelta(days=JOURS_RECENTS)).strftime("%Y-%m-%dT00:00:00+00:00")

    anomalies = {k: v for k, v in unsettled.items() if v.get("has_payout")}
    if not anomalies:
        return unsettled

    log.info(f"Vérification commandes récentes (< {JOURS_RECENTS}j) pour {len(anomalies)} anomalie(s)...")

    for i, (acc_id, acc) in enumerate(anomalies.items()):
        store_name = acc["store"]
        store_config = next((s for s in STORES if s["name"] == store_name), None)
        if not store_config:
            continue

        token = get_shopify_token(store_config)
        if not token:
            continue

        # Chercher le customer_id via la commande connue
        order_ref = acc["order_ref"]
        url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
        data = shopify_get(url, token, {
            "name": order_ref, "status": "any",
            "fields": "id,customer", "limit": 1,
        })

        if not data or not data.get("orders"):
            continue

        customer = data["orders"][0].get("customer")
        if not customer or not customer.get("id"):
            continue

        customer_id = customer["id"]

        # Chercher les commandes récentes de ce client
        recent_data = shopify_get(url, token, {
            "customer_id": customer_id,
            "status": "any",
            "created_at_min": cutoff,
            "fields": "id,name,created_at,financial_status",
            "limit": 10,
        })

        if recent_data and recent_data.get("orders"):
            recent_orders = [o for o in recent_data["orders"] if o.get("name", "").replace("#", "").replace("-", "") != order_ref]
            if recent_orders:
                log.info(f"  {acc['account_number']} ({order_ref}) : {len(recent_orders)} commande(s) récente(s) → exclu")
                acc["has_payout"] = False
                acc["excluded_reason"] = f"{len(recent_orders)} commande(s) < {JOURS_RECENTS}j"

        if (i + 1) % 5 == 0:
            log.info(f"  Récents : {i + 1}/{len(anomalies)}")

    return unsettled


# =============================================================
# ÉTAPE 4 : Stocker en base
# =============================================================
def save_to_db(conn, accounts, unsettled, seuil=0.05):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Comptes soldés ou sous le seuil → pas d'entrée
    # Comptes non soldés → on stocke avec le résultat
    rows = []
    for acc_id, acc in accounts.items():
        solde = acc.get("solde", 0)
        if abs(solde) <= seuil:
            continue  # Soldé ou arrondi, on ignore

        has_payout = unsettled.get(acc_id, {}).get("has_payout", False)
        if has_payout:
            statut = "anomalie"
        else:
            statut = "attente"

        rows.append((
            acc["account_id"], acc["account_number"], acc.get("account_label", ""),
            acc["order_ref"], acc["store"], acc.get("invoice_number", ""),
            acc.get("invoice_date", ""), solde, acc.get("nb_ecritures", 0),
            1 if has_payout else 0,
            unsettled.get(acc_id, {}).get("payout_date"),
            unsettled.get(acc_id, {}).get("payout_amount", 0),
            unsettled.get(acc_id, {}).get("total_price", 0),
            unsettled.get(acc_id, {}).get("financial_status", ""),
            statut, now,
        ))

    conn.execute("DELETE FROM encaissements")
    conn.executemany("""
        INSERT INTO encaissements
        (account_id, account_number, account_label, order_ref, store, invoice_number,
         invoice_date, solde, nb_ecritures, has_payout, payout_date, payout_amount,
         total_price, financial_status, statut, loaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    log.info(f"💾 {len(rows)} lignes sauvegardées en base ({DB_PATH})")

# =============================================================
# MAIN
# =============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-min", required=True, help="Date début YYYY-MM-DD")
    parser.add_argument("--date-max", required=True, help="Date fin YYYY-MM-DD")
    args = parser.parse_args()

    log.info(f"=== Chargement encaissements {args.date_min} → {args.date_max} ===")

    conn = init_db()

    log.info("Étape 1/3 : Chargement des factures Pennylane...")
    accounts = load_invoices(args.date_min, args.date_max)

    log.info("Étape 2/3 : Calcul des soldes...")
    compute_balances(accounts)

    log.info("Étape 3/4 : Vérification des payouts Shopify...")
    unsettled = check_payouts(accounts)

    log.info("Étape 4/4 : Exclusion des faux positifs (commandes récentes)...")
    exclude_recent_orders(unsettled)

    log.info("Sauvegarde en base...")
    save_to_db(conn, accounts, unsettled)
    conn.close()

    # Résumé
    nb_anomalies = sum(1 for a in unsettled.values() if a.get("has_payout"))
    nb_attente = sum(1 for a in unsettled.values() if not a.get("has_payout"))
    log.info(f"\n=== TERMINÉ ===")
    log.info(f"  Comptes analysés : {len(accounts)}")
    log.info(f"  Non soldés : {len(unsettled)}")
    log.info(f"  Anomalies : {nb_anomalies}")
    log.info(f"  En attente : {nb_attente}")

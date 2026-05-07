"""
reclassement_471.py
Script one-shot : analyse les lignes 471 "Écart versement Shopify"
et identifie les comptes 411 clients à reclasser.
"""

import os, json, time, re, logging, requests, psycopg2
from dotenv import load_dotenv
from collections import defaultdict

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

DB_URL = "postgresql://zephyr_finance_user:bGLupE3rchYBVsJ95wENjamRw8jbojGN@dpg-d6k0pt0gjchc73bsj430-a.frankfurt-postgres.render.com/zephyr_finance"

_token_cache = {}
_account_number_cache = {}


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


def get_shopify_token(store_config):
    store_url = store_config["store"]
    cached = _token_cache.get(store_url)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    resp = requests.post(
        f"https://{store_url}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": store_config["client_id"], "client_secret": store_config["client_secret"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    if resp.status_code != 200: return None
    data = resp.json()
    _token_cache[store_url] = {"token": data["access_token"], "expires_at": time.time() + data.get("expires_in", 86399)}
    return data["access_token"]


def shopify_get(url, token, params=None):
    for attempt in range(5):
        resp = requests.get(url, headers={"X-Shopify-Access-Token": token}, params=params, timeout=30)
        if resp.status_code == 200: return resp.json()
        if resp.status_code == 429: time.sleep(min(2 ** attempt, 16)); continue
        return None
    return None


def get_account_number(ledger_account_id):
    if ledger_account_id in _account_number_cache:
        return _account_number_cache[ledger_account_id]
    data = pl_get(f"{PL_BASE}/ledger_accounts/{ledger_account_id}")
    num = data.get("number", "?") if data else "?"
    _account_number_cache[ledger_account_id] = num
    return num


def analyze():
    """Analyse toutes les lignes 471 et identifie les reclassements."""

    # 1. Charger les lignes 471
    log.info("Chargement des lignes 471...")
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": "471"}])
    acc_data = pl_get(f"{PL_BASE}/ledger_accounts", {"filter": filter_param, "limit": 10})
    acc_ids = [a["id"] for a in acc_data.get("items", [])]

    all_471_lines = []
    for acc_id in acc_ids:
        fl = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(acc_id)}])
        cursor = None
        while True:
            params = {"filter": fl, "limit": 100}
            if cursor: params["cursor"] = cursor
            resp = pl_get(f"{PL_BASE}/ledger_entry_lines", params)
            if not resp: break
            all_471_lines.extend(resp.get("items", []))
            if not resp.get("has_more") or not resp.get("next_cursor"): break
            cursor = resp["next_cursor"]

    log.info(f"{len(all_471_lines)} lignes 471 trouvées")

    # 2. Pour chaque ligne 471, trouver l'écriture parente et le payout
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    reclassements = []  # {order_name, customer_name, compte_411, montant, sens, store, date}

    for line_471 in all_471_lines:
        d471 = float(line_471["debit"])
        c471 = float(line_471["credit"])
        date_471 = line_471["date"]
        entry_id = line_471.get("ledger_entry", {}).get("id")

        if not entry_id:
            continue

        # Charger l'écriture parente
        time.sleep(0.3)
        detail = pl_get(f"{PL_BASE}/ledger_entries/{entry_id}")
        if not detail:
            continue

        entry_label = detail.get("label", "")
        if "Versement Shopify" not in entry_label:
            log.info(f"  Skip (pas un versement Shopify): {entry_label}")
            continue

        # Extraire le store name
        store_match = re.search(r'\[(\w+)\]', entry_label)
        store_name = store_match.group(1) if store_match else None
        if not store_name:
            continue

        store_config = next((s for s in STORES if s["name"] == store_name), None)
        if not store_config:
            continue

        # Trouver le payout
        token = get_shopify_token(store_config)
        if not token:
            continue

        url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/payouts.json"
        payouts = shopify_get(url, token, {"date_min": date_471, "date_max": date_471})
        if not payouts:
            continue

        # Identifier les commandes déjà matchées dans l'écriture
        matched_orders = set()
        for el in detail.get("ledger_entry_lines", []):
            label = el.get("label", "")
            m = re.search(r'(?:LFC|RDC|HC|COCO|MO|RM|TZ|LVO|UNIV)\d{3,}', label, re.IGNORECASE)
            if m:
                matched_orders.add(m.group(0).upper())

        # Pour chaque payout de ce jour, trouver les commandes non matchées
        for p in payouts.get("payouts", []):
            t_url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance/transactions.json"
            txns = shopify_get(t_url, token, {"payout_id": p["id"], "limit": 250})
            if not txns:
                continue

            for txn in txns.get("transactions", []):
                stype = txn.get("source_type", "").rsplit("::", 1)[-1].lower()
                if stype == "payout":
                    continue

                order_id = txn.get("source_order_id")
                if not order_id:
                    continue

                amount = float(txn.get("amount", 0))
                fee = float(txn.get("fee", 0))

                # Récupérer le nom de commande
                odata = shopify_get(f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}.json", token, {"fields": "name"})
                if not odata or not odata.get("order"):
                    continue
                raw = re.sub(r'[#\-]', '', odata["order"]["name"])
                m = re.match(r'([A-Za-z]{0,5}\d+)', raw)
                order_name = m.group(1).upper() if m else raw.upper()

                if order_name in matched_orders:
                    continue  # déjà matché dans l'écriture

                # Chercher le client
                cur.execute(f"SELECT customer_name, customer_id FROM invoices WHERE order_number = %s", (order_name,))
                inv = cur.fetchone()
                if not inv:
                    log.warning(f"  {order_name} pas trouvé dans invoices")
                    continue

                cust_name = inv[0]
                cust_id = inv[1]

                cur.execute(f"SELECT ledger_account_id FROM customers WHERE id = %s", (cust_id,))
                c = cur.fetchone()
                if not c or not c[0]:
                    log.warning(f"  {order_name} pas de ledger_account_id")
                    continue

                compte_411 = get_account_number(c[0])
                ledger_account_id = c[0]

                reclassements.append({
                    "order_name": order_name,
                    "customer_name": cust_name,
                    "compte_411": compte_411,
                    "ledger_account_id": ledger_account_id,
                    "montant": abs(amount),
                    "sens": "refund" if stype == "refund" else "charge",
                    "store": store_name,
                    "date": date_471,
                    "entry_label": entry_label,
                })

    conn.close()

    # 3. Afficher le résultat
    log.info(f"\n{'='*80}")
    log.info(f"RECLASSEMENTS À EFFECTUER : {len(reclassements)} lignes")
    log.info(f"{'='*80}\n")

    total_debit_411 = 0  # charges non matchées → créditer 411
    total_credit_411 = 0  # refunds non matchés → débiter 411

    for r in sorted(reclassements, key=lambda x: (x["date"], x["store"])):
        if r["sens"] == "charge":
            log.info(f"  {r['date']} [{r['store']}] {r['order_name']} | {r['customer_name']} | {r['compte_411']} | C {r['montant']:.2f}€")
            total_debit_411 += r["montant"]  # on va créditer le 411 (encaissement)
        else:
            log.info(f"  {r['date']} [{r['store']}] {r['order_name']} | {r['customer_name']} | {r['compte_411']} | D {r['montant']:.2f}€ (remboursement)")
            total_credit_411 += r["montant"]  # on va débiter le 411 (remboursement)

    # Le 471 a des crédits (pour les charges non matchées) et des débits (pour les refunds)
    # Le reclassement doit vider le 471 :
    # - Pour les charges : D 471 / C 411client
    # - Pour les refunds : C 471 / D 411client
    net_471 = total_debit_411 - total_credit_411
    log.info(f"\n  TOTAL charges → C 411 : {total_debit_411:.2f}€")
    log.info(f"  TOTAL refunds → D 411 : {total_credit_411:.2f}€")
    log.info(f"  NET 471 : D {net_471:.2f}€" if net_471 > 0 else f"  NET 471 : C {abs(net_471):.2f}€")

    return reclassements


if __name__ == "__main__":
    analyze()

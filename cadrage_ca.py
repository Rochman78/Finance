"""
cadrage_ca.py
Cadrage du Chiffre d'Affaires : Shopify (commandes) vs Pennylane (factures)

Shopify : sum(total_price - total_tax) par jour
Pennylane : sum(currency_amount_before_tax) des factures par jour
"""

import os, json, time, re, logging, requests
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- CONFIG ---
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

# =============================================================
# HELPERS API
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
    data = resp.json()
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


def pl_get(url: str, params: dict = None) -> dict | None:
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


# =============================================================
# SHOPIFY — CA HT par boutique et par jour
# =============================================================
def get_ca_shopify(date_min: str, date_max: str) -> dict:
    """
    Retourne {store: {ca_ht, nb_orders, by_date: {date: {ca_ht, nb_orders}}}}
    """
    result = {}
    for store_config in STORES:
        sname = store_config["name"]
        token = get_shopify_token(store_config)
        if not token:
            result[sname] = {"ca_ht": None, "nb_orders": 0, "by_date": {}}
            continue

        # Shopify date filter uses created_at_min/max in ISO format
        date_min_iso = f"{date_min}T00:00:00+00:00"
        date_max_iso = f"{date_max}T23:59:59+00:00"

        all_orders = []
        url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
        params = {
            "status": "any",
            "created_at_min": date_min_iso,
            "created_at_max": date_max_iso,
            "fields": "name,total_price,total_tax,created_at,financial_status",
            "limit": 250,
        }

        while True:
            data = shopify_get(url, token, params)
            if not data:
                break
            orders = data.get("orders", [])
            all_orders.extend(orders)
            if len(orders) < 250:
                break
            params["since_id"] = orders[-1]["id"] if orders else None
            if not params["since_id"]:
                break

        # Aggregate by date + collect order details
        by_date = {}
        total_ca = 0.0
        orders_detail = []
        for order in all_orders:
            if order.get("financial_status") in ("voided",):
                continue
            price = float(order.get("total_price", 0))
            tax = float(order.get("total_tax", 0))
            ca_ht = price - tax
            total_ca += ca_ht

            created = order.get("created_at", "")[:10]
            if created not in by_date:
                by_date[created] = {"ca_ht": 0.0, "nb_orders": 0}
            by_date[created]["ca_ht"] += ca_ht
            by_date[created]["nb_orders"] += 1

            # Clean order name
            raw_name = order.get("name", "").replace("#", "").replace("-", "")
            m = re.match(r'([A-Za-z]{0,5}\d+)', raw_name)
            order_name = m.group(1).upper() if m else raw_name.upper()

            orders_detail.append({
                "date": created,
                "order_name": order_name,
                "ca_ht": round(ca_ht, 2),
                "tva": round(tax, 2),
                "ttc": round(price, 2),
                "store": sname,
            })

        total_ca = round(total_ca, 2)
        for d in by_date:
            by_date[d]["ca_ht"] = round(by_date[d]["ca_ht"], 2)

        if all_orders:
            log.info(f"[{sname}] CA HT Shopify : {total_ca:.2f}€ ({len(all_orders)} commandes)")

        result[sname] = {"ca_ht": total_ca, "nb_orders": len(all_orders), "by_date": by_date, "orders": orders_detail}

    return result


# =============================================================
# PENNYLANE — CA HT depuis les écritures 707 du journal VT
# =============================================================
JOURNAL_VT_ID = 639797  # Journal de vente

def _load_invoices_index(date_min: str, date_max: str) -> dict:
    """
    Charge toutes les factures Pennylane sur la période et construit un index
    invoice_number → order_ref (numéro de commande Shopify).
    """
    index = {}
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
                inv_num = inv.get("invoice_number", "")
                special = inv.get("special_mention", "") or ""
                label = inv.get("label", "") or ""
                text = f"{special} {label}"
                m = re.search(ORDER_PREFIXES + r'\d{3,}', text, re.IGNORECASE)
                if m and inv_num:
                    index[inv_num] = m.group(0).upper()
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

    log.info(f"Index factures : {len(index)} facture(s) avec commande Shopify")
    return index


def get_ca_pennylane(date_min: str, date_max: str) -> dict:
    """
    Récupère le CA HT depuis la comptabilité : crédits du compte 707 dans le journal VT.
    Pour chaque écriture, extrait le numéro de facture du label, puis retrouve le numéro
    de commande Shopify via la facture → special_mention → rattache à une boutique.

    Retourne {
        total_ht: float,
        nb_ecritures: int,
        by_date: {date: {ca_ht, nb_ecritures}},
        by_store: {store: {ca_ht, nb_ecritures}},
        lines: [{date, entry_label, label, ca_ht, order_ref, store, account, invoice_number}]
    }
    """
    # Step 1: Load invoice index (invoice_number → order_ref)
    invoice_index = _load_invoices_index(date_min, date_max)

    all_lines = []

    # Step 2: Load VT entries day by day
    d = datetime.strptime(date_min, "%Y-%m-%d")
    end = datetime.strptime(date_max, "%Y-%m-%d")

    while d <= end:
        day_str = d.strftime("%Y-%m-%d")
        d += timedelta(days=1)

        filter_param = json.dumps([
            {"field": "date", "operator": "eq", "value": day_str},
            {"field": "journal_id", "operator": "eq", "value": JOURNAL_VT_ID},
        ])

        entries = []
        cursor = None
        while True:
            params = {"filter": filter_param, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = pl_get(f"{PL_BASE}/ledger_entries", params)
            if not data:
                break
            entries.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

        # Step 3: For each entry, extract 707 lines and match to store via invoice
        for entry in entries:
            detail = pl_get(f"{PL_BASE}/ledger_entries/{entry['id']}")
            if not detail:
                continue

            entry_label = detail.get("label", "")
            entry_date = detail.get("date", day_str)

            # Extract invoice number from entry label (e.g. "Facture X - F-2026-04-10-11384 (label généré)")
            inv_match = re.search(r'(F-\d{4}-\d{2}-\d{2}-\d+)', entry_label)
            invoice_number = inv_match.group(1) if inv_match else None

            # Lookup order ref from invoice index
            order_ref = invoice_index.get(invoice_number) if invoice_number else None

            # If not in index, try direct match from entry label
            if not order_ref:
                m = re.search(ORDER_PREFIXES + r'\d{3,}', entry_label, re.IGNORECASE)
                order_ref = m.group(0).upper() if m else None

            # Determine store from order ref
            store = None
            if order_ref:
                for sname, prefixes in STORE_PREFIXES.items():
                    if any(order_ref.startswith(p) for p in prefixes):
                        store = sname
                        break

            for line in detail.get("ledger_entry_lines", []):
                acc = line.get("ledger_account", {})
                acc_number = acc.get("number", "")

                if not acc_number.startswith("707"):
                    continue

                acc_name = acc.get("name", "")
                debit = float(line.get("debit") or 0)
                credit = float(line.get("credit") or 0)
                ca_ht = credit - debit
                label = line.get("label", "")

                all_lines.append({
                    "date": entry_date,
                    "entry_label": entry_label,
                    "label": label,
                    "ca_ht": ca_ht,
                    "order_ref": order_ref,
                    "store": store,
                    "account": acc_number,
                    "account_name": acc_name,
                    "invoice_number": invoice_number,
                })

    # Aggregate
    total_ht = round(sum(l["ca_ht"] for l in all_lines), 2)
    by_date = {}
    by_store = {}

    for line in all_lines:
        d_key = line["date"]
        if d_key not in by_date:
            by_date[d_key] = {"ca_ht": 0.0, "nb_ecritures": 0}
        by_date[d_key]["ca_ht"] += line["ca_ht"]
        by_date[d_key]["nb_ecritures"] += 1

        s_key = line["store"] or "INCONNU"
        if s_key not in by_store:
            by_store[s_key] = {"ca_ht": 0.0, "nb_ecritures": 0}
        by_store[s_key]["ca_ht"] += line["ca_ht"]
        by_store[s_key]["nb_ecritures"] += 1

    for d_key in by_date:
        by_date[d_key]["ca_ht"] = round(by_date[d_key]["ca_ht"], 2)
    for s_key in by_store:
        by_store[s_key]["ca_ht"] = round(by_store[s_key]["ca_ht"], 2)

    log.info(f"Pennylane VT/707 : {len(all_lines)} ligne(s), CA HT = {total_ht:.2f}€")

    return {
        "total_ht": total_ht,
        "nb_ecritures": len(all_lines),
        "by_date": by_date,
        "by_store": by_store,
        "lines": all_lines,
    }

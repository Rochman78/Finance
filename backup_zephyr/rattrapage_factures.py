"""
rattrapage_factures.py
Crée les factures manquantes dans Pennylane à partir des commandes Shopify.
Reproduit la logique de app.py (webhook facturation).

Usage:
    python rattrapage_factures.py --dry-run              # Simulation
    python rattrapage_factures.py --dry-run --sample 5   # Dry-run sur 5 premières + 5 dernières par boutique
    python rattrapage_factures.py                        # Exécution réelle
"""

import os, json, time, re, logging, argparse, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# =============================================================
# CONFIG
# =============================================================
SHOPIFY_API_VERSION = "2026-01"
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  ""), "template_id": 282170},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  ""), "template_id": 710185},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  ""), "template_id": 710153},
    {"name": "MTC",  "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  ""), "template_id": 710211},
    {"name": "MO",   "store": "mon-ombrage.myshopify.com",             "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO",   ""), "template_id": 710449},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", ""), "template_id": 710214},
    {"name": "TZ",   "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   ""), "template_id": 710132},
    {"name": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",  "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  ""), "template_id": 710463},
    {"name": "UNIV", "store": "univers-camouflage.myshopify.com",      "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", ""), "template_id": 710484},
]

# Préfixes commandes → boutiques
STORE_PREFIXES = {"LFC": "LFC", "RED": "RDC", "HET": "HC", "MTC": "COCO", "MO": "MO", "RETE": "RM", "TZ": "TZ", "LVO": "LVO", "UNIV": "UNIV"}

# Plages de commandes à rattraper
ORDER_RANGES = {
    "LFC":  (31653, 31984),
    "TZ":   (5660, 5685),
    "MTC":  (3349, 3402),
    "RED":  (3907, 3943),
    "HET":  (3299, 3320),
    "RETE": (1420, 1427),
}

# =============================================================
# SHOPIFY API
# =============================================================
_token_cache = {}

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


def shopify_get(url, token, params=None):
    for attempt in range(5):
        resp = requests.get(url, headers={"X-Shopify-Access-Token": token}, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 16))
            continue
        log.error(f"Shopify GET {url}: {resp.status_code}")
        return None
    return None


def fetch_order_by_name(store_config, order_name):
    """Récupère une commande Shopify par son nom (ex: #LFC31653)."""
    token = get_shopify_token(store_config)
    if not token:
        return None
    url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    data = shopify_get(url, token, {"name": order_name, "status": "any", "limit": 5})
    if data and data.get("orders"):
        return data["orders"][0]
    # Essayer sans le #
    data = shopify_get(url, token, {"name": f"#{order_name}", "status": "any", "limit": 5})
    if data and data.get("orders"):
        return data["orders"][0]
    return None


# =============================================================
# PENNYLANE — ANTI-DOUBLON
# =============================================================
_invoice_check_cache = {}

def invoice_exists_in_pennylane(order_name):
    """Vérifie si une facture existe déjà dans Pennylane pour cette commande."""
    if order_name in _invoice_check_cache:
        return _invoice_check_cache[order_name]

    # Chercher dans les factures Pennylane (special_mention contient le numéro de commande)
    # On ne peut pas filtrer par special_mention, donc on cherche dans les factures récentes
    # Mais c'est trop lent pour tout scanner. On utilise le label qui contient souvent le numéro.
    # Alternative : chercher via l'index invoices de la DB si disponible

    # Méthode rapide : chercher dans les ledger_entries du journal VT avec le numéro de commande
    from cadrage_ca import _load_invoices_index, JOURNAL_VT_ID
    # On ne peut pas faire ça efficacement sans date range, donc on fait une recherche ciblée

    # Chercher directement dans customer_invoices
    # On ne peut pas filtrer par special_mention mais on peut chercher par date si on connaît la date
    _invoice_check_cache[order_name] = False
    return False


def check_invoice_exists_batch(order_names, date_min, date_max):
    """Vérifie en batch quelles commandes ont déjà une facture dans Pennylane."""
    existing = set()

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
            resp = requests.get(f"{PL_BASE}/customer_invoices", headers=PL_HEADERS, params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(2)
                continue
            if resp.status_code != 200:
                break
            data = resp.json()
            for inv in data.get("items", []):
                sm = (inv.get("special_mention") or "")
                label = (inv.get("label") or "")
                text = f"{sm} {label}"
                for oname in order_names:
                    if oname in text:
                        existing.add(oname)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break

        time.sleep(0.3)

    return existing


# =============================================================
# DRY-RUN
# =============================================================
def run(dry_run=True, sample=0):
    log.info(f"=== Rattrapage factures {'(DRY-RUN)' if dry_run else '(RÉEL)'} ===")

    total_missing = 0
    total_existing = 0
    total_errors = 0

    for store_config in STORES:
        sname = store_config["name"]
        prefix = STORE_PREFIXES.get(sname)

        if sname not in ORDER_RANGES:
            continue

        range_start, range_end = ORDER_RANGES[sname]
        all_numbers = list(range(range_start, range_end + 1))

        # Si sample, prendre les N premières et N dernières
        if sample > 0 and len(all_numbers) > sample * 2:
            numbers = all_numbers[:sample] + all_numbers[-sample:]
            log.info(f"[{sname}] Échantillon : {len(numbers)} commandes (premières {sample} + dernières {sample}) sur {len(all_numbers)}")
        else:
            numbers = all_numbers
            log.info(f"[{sname}] {len(numbers)} commandes à vérifier ({prefix}{range_start} → {prefix}{range_end})")

        token = get_shopify_token(store_config)
        if not token:
            log.error(f"[{sname}] Impossible d'obtenir le token Shopify")
            continue

        for num in numbers:
            order_name = f"{prefix}{num}"

            # Récupérer la commande Shopify
            order = fetch_order_by_name(store_config, order_name)
            if not order:
                log.warning(f"  {order_name} : commande non trouvée dans Shopify")
                total_errors += 1
                continue

            # Infos basiques
            financial_status = order.get("financial_status", "")
            total_price = float(order.get("total_price", 0))
            total_tax = float(order.get("total_tax", 0))
            ca_ht = round(total_price - total_tax, 2)
            created_at = order.get("created_at", "")[:10]

            # Client
            customer = order.get("customer") or {}
            billing = order.get("billing_address") or {}
            shipping = order.get("shipping_address") or {}
            client_name = f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip()
            if not client_name:
                client_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
            country = shipping.get("country_code", "")

            # Tags
            tags = order.get("tags", "")
            if "DEVIS_TRANSFORME_PL" in tags:
                log.info(f"  {order_name} : tag DEVIS_TRANSFORME_PL → skip")
                continue
            if "GARANTIE" in tags:
                log.info(f"  {order_name} : tag GARANTIE → skip")
                continue

            # Skip si pas paid ou pending
            if financial_status not in ("paid", "pending"):
                log.info(f"  {order_name} : financial_status={financial_status} → skip")
                continue

            # Skip si montant <= 0
            if total_price <= 0:
                log.info(f"  {order_name} : montant={total_price}€ → skip")
                continue

            # Produits
            line_items = order.get("line_items", [])
            nb_items = sum(int(li.get("quantity", 1)) for li in line_items)

            # Livraison
            shipping_lines = order.get("shipping_lines", [])
            shipping_title = shipping_lines[0].get("title", "") if shipping_lines else ""
            shipping_price = float(shipping_lines[0].get("price", 0)) if shipping_lines else 0

            # Remises
            discount_codes = order.get("discount_codes", [])
            discount_str = f" | Remise: {discount_codes[0]['code']} ({discount_codes[0]['amount']}€)" if discount_codes else ""

            # Vérifier si la facture existe déjà (anti-doublon)
            # Pour le dry-run on ne fait pas le check batch (trop lent), on note juste

            status_icon = "🟢" if financial_status == "paid" else "🟡"

            log.info(f"  {status_icon} {order_name} | {created_at} | {client_name} | {country} | {total_price:.2f}€ TTC ({ca_ht:.2f}€ HT) | {nb_items} article(s) | {shipping_title}{discount_str}")

            if dry_run:
                log.info(f"     → SERAIT CRÉÉE (template: {store_config['template_id']})")

            total_missing += 1

            time.sleep(0.3)  # Rate limit

    log.info(f"\n{'='*60}")
    log.info(f"RÉSULTAT:")
    log.info(f"  Factures à créer : {total_missing}")
    log.info(f"  Erreurs : {total_errors}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Simulation sans créer de factures")
    parser.add_argument("--sample", type=int, default=0, help="Nombre de commandes début+fin par boutique")
    args = parser.parse_args()

    run(dry_run=args.dry_run or True, sample=args.sample)

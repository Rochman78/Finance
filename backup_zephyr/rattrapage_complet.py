"""
rattrapage_complet.py
Dry-run complet puis création brouillon des factures manquantes.

Mode 1 : --report    → analyse toutes les commandes, rapport de contrôle
Mode 2 : --create    → crée les brouillons dans Pennylane

Contrôle TTC : bloque si écart > 0.02€ entre Shopify et le calcul des lignes.
"""

import os, json, time, re, logging, argparse, requests, csv
from datetime import datetime, timedelta
from dotenv import load_dotenv
from io import StringIO

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2026-01"
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

STORES = [
    {"name": "LFC",  "prefix": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  ""), "template_id": 282170},
    {"name": "RED",  "prefix": "RDC",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  ""), "template_id": 710185},
    {"name": "HET",  "prefix": "HC",   "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  ""), "template_id": 710153},
    {"name": "MTC",  "prefix": "COCO", "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  ""), "template_id": 710211},
    {"name": "RETE", "prefix": "RM",   "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", ""), "template_id": 710214},
    {"name": "TZ",   "prefix": "TZ",   "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   ""), "template_id": 710132},
]

ORDER_RANGES = {
    "LFC":  ("LFC",  31653, 31984),
    "TZ":   ("TZ",   5660,  5685),
    "MTC":  ("COCO", 3349,  3402),
    "RED":  ("RDC",  3907,  3943),
    "HET":  ("HC",   3299,  3320),
    "RETE": ("RM",   1420,  1427),
}

EU_RATES = {
    "DE": "DE_190", "AT": "AT_200", "BE": "BE_210", "BG": "BG_200", "CY": "CY_190",
    "HR": "HR_250", "DK": "DK_250", "ES": "ES_210", "EE": "EE_200", "FI": "FI_240",
    "GR": "GR_240", "HU": "HU_270", "IE": "IE_230", "IT": "IT_220", "LV": "LV_210",
    "LT": "LT_210", "LU": "LU_170", "MT": "MT_180", "NL": "NL_210", "PL": "PL_230",
    "PT": "PT_230", "CZ": "CZ_210", "RO": "RO_190", "SK": "SK_200", "SI": "SI_220",
    "SE": "SE_250", "FR": "FR_200", "MC": "MC_200",
}
EU_COUNTRIES = set(EU_RATES.keys())
SEUIL_ECART_TTC = 0.02

_token_cache = {}

def get_shopify_token(store_config):
    store_url = store_config["store"]
    cached = _token_cache.get(store_url)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    resp = requests.post(f"https://{store_url}/admin/oauth/access_token",
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

def calculate_vat(country_code, customer_type, vat_number=None):
    if customer_type == "company":
        if vat_number and country_code in EU_COUNTRIES and country_code != "FR":
            return "exempt", True
        if country_code == "FR": return "FR_200", False
        if country_code not in EU_COUNTRIES: return "exempt", False
    if country_code in EU_RATES: return EU_RATES[country_code], False
    return "exempt", False

def get_vat_percentage(vat_code):
    if vat_code == "exempt": return 0.0
    try: return int(vat_code.split('_')[1]) / 10.0
    except: return 0.0

def ttc_to_ht(price_ttc, vat_code):
    pct = get_vat_percentage(vat_code)
    if pct == 0: return price_ttc
    return round(price_ttc / (1 + pct / 100), 4)

def ht_to_ttc(price_ht, vat_code):
    pct = get_vat_percentage(vat_code)
    return round(price_ht * (1 + pct / 100), 2)


def find_store_for_prefix(prefix):
    for s in STORES:
        if s["prefix"] == prefix:
            return s
    return None


def analyze_order(store_config, order_name):
    """Analyse une commande Shopify et retourne un dict avec toutes les infos + contrôle TTC."""
    token = get_shopify_token(store_config)
    if not token:
        return {"order": order_name, "status": "ERROR", "error": "Token Shopify non obtenu"}

    url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    data = shopify_get(url, token, {"name": order_name, "status": "any", "limit": 5})
    if not data or not data.get("orders"):
        data = shopify_get(url, token, {"name": f"#{order_name}", "status": "any", "limit": 5})
    if not data or not data.get("orders"):
        return {"order": order_name, "status": "ERROR", "error": "Commande non trouvée"}

    order = data["orders"][0]
    financial_status = order.get("financial_status", "")
    tags = order.get("tags", "")

    if financial_status not in ("paid", "pending"):
        return {"order": order_name, "status": "SKIP", "reason": f"financial_status={financial_status}"}
    if "DEVIS_TRANSFORME_PL" in tags:
        return {"order": order_name, "status": "SKIP", "reason": "tag DEVIS_TRANSFORME_PL"}
    if "GARANTIE" in tags:
        return {"order": order_name, "status": "SKIP", "reason": "tag GARANTIE"}

    total_price = float(order.get("total_price", 0))
    if total_price <= 0:
        return {"order": order_name, "status": "SKIP", "reason": f"montant={total_price}"}

    # Infos client
    customer = order.get("customer") or {}
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or billing
    country_code = shipping.get("country_code") or billing.get("country_code") or "FR"
    company = billing.get("company") or shipping.get("company") or ""
    customer_type = "company" if (company or order.get("tax_exempt") or customer.get("tax_exempt")) else "individual"
    first = customer.get("first_name") or shipping.get("first_name") or ""
    last = customer.get("last_name") or shipping.get("last_name") or ""
    client_name = f"{first} {last}".strip()

    vat_number = None
    for attr in order.get("note_attributes", []):
        if attr.get("name") == "VAT number":
            vat_number = attr.get("value", "").strip()

    vat_rate, has_vat_exemption = calculate_vat(country_code, customer_type, vat_number)
    currency = order.get("currency", "EUR")
    invoice_date = order.get("created_at", "")[:10]

    # Calculer le TTC depuis les lignes (comme le ferait Pennylane)
    calculated_ttc = 0.0
    line_details = []
    for item in order.get("line_items", []):
        sku = item.get("sku", "")
        label = item.get("name", "")
        qty = int(item.get("quantity", 1))
        price_ttc_unit = float(item.get("price", 0))
        price_ht_unit = ttc_to_ht(price_ttc_unit, vat_rate)
        line_ttc = ht_to_ttc(price_ht_unit, vat_rate) * qty
        calculated_ttc += line_ttc
        line_details.append({"sku": sku, "label": label[:40], "qty": qty, "ttc_unit": price_ttc_unit, "ht_unit": round(price_ht_unit, 4)})

    # Livraison
    shipping_lines = order.get("shipping_lines", [])
    ship_price_ttc = 0
    ship_title = ""
    if shipping_lines:
        ship_price_ttc = float(shipping_lines[0].get("price", 0))
        ship_title = shipping_lines[0].get("title", "")
        ship_ht = ttc_to_ht(ship_price_ttc, vat_rate)
        ship_recalc_ttc = ht_to_ttc(ship_ht, vat_rate)
        calculated_ttc += ship_recalc_ttc

    # Remises
    discount_codes = order.get("discount_codes", [])
    total_discount = sum(float(dc.get("amount", 0)) for dc in discount_codes)
    discount_str = discount_codes[0]["code"] if discount_codes else ""

    # Le TTC Shopify inclut déjà la remise, mais notre calcul ne l'inclut pas encore
    calculated_ttc_after_discount = round(calculated_ttc - total_discount, 2)

    ecart = round(total_price - calculated_ttc_after_discount, 2)
    ecart_ok = abs(ecart) <= SEUIL_ECART_TTC

    return {
        "order": order_name,
        "status": "OK" if ecart_ok else "ECART",
        "date": invoice_date,
        "client": client_name,
        "type": customer_type,
        "country": country_code,
        "vat_rate": vat_rate,
        "vat_exemption": has_vat_exemption,
        "vat_number": vat_number or "",
        "currency": currency,
        "financial_status": financial_status,
        "ttc_shopify": total_price,
        "ttc_calculated": calculated_ttc_after_discount,
        "ecart": ecart,
        "nb_items": len(line_details),
        "shipping": ship_title,
        "discount": discount_str,
        "discount_amount": total_discount,
        "template_id": store_config["template_id"],
        "store": store_config["name"],
    }


def run_report():
    """Analyse toutes les commandes et génère un rapport."""
    log.info("=== RAPPORT DE CONTRÔLE — Rattrapage factures ===\n")

    all_results = []
    ecarts = []
    errors = []
    skipped = []
    ok_count = 0

    for store_key, (prefix, range_start, range_end) in ORDER_RANGES.items():
        store_config = find_store_for_prefix(prefix)
        if not store_config:
            log.error(f"Store non trouvée pour {prefix}")
            continue

        numbers = list(range(range_start, range_end + 1))
        log.info(f"[{store_key}] {len(numbers)} commandes ({prefix}{range_start} → {prefix}{range_end})")

        for num in numbers:
            order_name = f"{prefix}{num}"
            result = analyze_order(store_config, order_name)
            all_results.append(result)

            if result["status"] == "OK":
                ok_count += 1
            elif result["status"] == "ECART":
                ecarts.append(result)
                log.warning(f"  ⚠️ {order_name} ECART: Shopify={result['ttc_shopify']}€ vs Calculé={result['ttc_calculated']}€ (diff={result['ecart']}€)")
            elif result["status"] == "SKIP":
                skipped.append(result)
                log.info(f"  ⏭️ {order_name} SKIP: {result.get('reason', '')}")
            elif result["status"] == "ERROR":
                errors.append(result)
                log.error(f"  ❌ {order_name} ERROR: {result.get('error', '')}")

            time.sleep(0.3)

    # Rapport final
    log.info(f"\n{'='*60}")
    log.info(f"RAPPORT FINAL")
    log.info(f"{'='*60}")
    log.info(f"  Total commandes analysées : {len(all_results)}")
    log.info(f"  ✅ OK (écart <= {SEUIL_ECART_TTC}€) : {ok_count}")
    log.info(f"  ⚠️ ÉCART (> {SEUIL_ECART_TTC}€) : {len(ecarts)}")
    log.info(f"  ⏭️ Skippées : {len(skipped)}")
    log.info(f"  ❌ Erreurs : {len(errors)}")
    log.info(f"  → Factures à créer : {ok_count}")

    if ecarts:
        log.info(f"\n--- ÉCARTS À INVESTIGUER ---")
        for r in ecarts:
            log.info(f"  {r['order']} | {r['client']} | {r['country']} | Shopify={r['ttc_shopify']}€ | Calculé={r['ttc_calculated']}€ | Écart={r['ecart']}€")

    if errors:
        log.info(f"\n--- ERREURS ---")
        for r in errors:
            log.info(f"  {r['order']} : {r.get('error', '?')}")

    if skipped:
        log.info(f"\n--- SKIPPÉES ---")
        for r in skipped:
            log.info(f"  {r['order']} : {r.get('reason', '?')}")

    # Export CSV
    csv_path = "/Users/charlesbamy/Finance/rapport_rattrapage.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["order", "status", "date", "client", "type", "country", "vat_rate", "financial_status", "ttc_shopify", "ttc_calculated", "ecart", "nb_items", "shipping", "discount", "discount_amount", "store"])
        writer.writeheader()
        for r in all_results:
            if r["status"] in ("OK", "ECART"):
                writer.writerow({k: r.get(k, "") for k in writer.fieldnames})

    log.info(f"\n📄 Rapport CSV exporté : {csv_path}")

    return all_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="Dry-run + rapport de contrôle")
    parser.add_argument("--create", action="store_true", help="Créer les brouillons dans Pennylane")
    args = parser.parse_args()

    if args.report:
        run_report()
    elif args.create:
        from create_invoice_draft import process_order
        log.info("=== CRÉATION DES BROUILLONS ===")
        results = run_report()
        to_create = [r for r in results if r["status"] in ("OK", "ECART")]
        log.info(f"\n{'='*60}")
        log.info(f"Création de {len(to_create)} factures brouillon...")
        log.info(f"{'='*60}\n")
        ok = 0
        ko = 0
        for r in to_create:
            success = process_order(r["order"], dry_run=False)
            if success:
                ok += 1
            else:
                ko += 1
            time.sleep(0.3)
        log.info(f"\n=== TERMINÉ: {ok} créées, {ko} erreurs ===")
    else:
        log.info("Usage: --report pour le rapport, --create pour créer les brouillons")

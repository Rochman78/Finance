"""
create_invoice_draft.py
Crée des factures brouillon dans Pennylane à partir de commandes Shopify.
Reproduit la logique de app.py sans DynamoDB — cherche directement dans Pennylane.

Usage:
    python create_invoice_draft.py --orders LFC31653,LFC31984,RDC3907,RDC3943
"""

import os, json, time, re, logging, argparse, requests, base64
from datetime import datetime, timedelta
from dotenv import load_dotenv

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
    {"name": "LVO",  "prefix": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",  "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  ""), "template_id": 710463},
    {"name": "UNIV", "prefix": "UNIV", "store": "univers-camouflage.myshopify.com",      "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", ""), "template_id": 710484},
    {"name": "MO",   "prefix": "MO",   "store": "mon-ombrage.myshopify.com",             "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO",   ""), "template_id": 710449},
]

EU_RATES = {
    "DE": "DE_190", "AT": "AT_200", "BE": "BE_210", "BG": "BG_200", "CY": "CY_190",
    "HR": "HR_250", "DK": "DK_250", "ES": "ES_210", "EE": "EE_200", "FI": "FI_240",
    "GR": "GR_240", "HU": "HU_270", "IE": "IE_230", "IT": "IT_220", "LV": "LV_210",
    "LT": "LT_210", "LU": "LU_170", "MT": "MT_180", "NL": "NL_210", "PL": "PL_230",
    "PT": "PT_230", "CZ": "CZ_210", "RO": "RO_190", "SK": "SK_200", "SI": "SI_220",
    "SE": "SE_250", "FR": "FR_200", "MC": "MC_200",
}
EU_COUNTRIES = set(EU_RATES.keys())

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

def pl_get(url, params=None):
    for attempt in range(5):
        resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        if resp.status_code == 200: return resp.json()
        if resp.status_code == 429: time.sleep(min(2 ** attempt, 10)); continue
        return None
    return None

def pl_post(url, payload):
    for attempt in range(3):
        resp = requests.post(url, headers=PL_HEADERS, json=payload, timeout=30)
        if resp.status_code in (200, 201): return resp.json()
        if resp.status_code == 429: time.sleep(min(2 ** attempt, 10)); continue
        log.error(f"PL POST {url}: {resp.status_code} — {resp.text[:300]}")
        return None
    return None


# =============================================================
# TVA
# =============================================================
def calculate_vat(country_code, customer_type, vat_number=None):
    if customer_type == "company":
        if vat_number and country_code in EU_COUNTRIES and country_code != "FR":
            return "exempt", True
        if country_code == "FR":
            return "FR_200", False
        if country_code in EU_COUNTRIES:
            pass  # Fall through to eu_rates
        else:
            return "exempt", False
    if country_code in EU_RATES:
        return EU_RATES[country_code], False
    return "exempt", False

def get_vat_percentage(vat_code):
    if vat_code == "exempt": return 0.0
    try:
        parts = vat_code.split('_')
        return int(parts[1]) / 10.0
    except: return 0.0

def ttc_to_ht(price_ttc, vat_code):
    pct = get_vat_percentage(vat_code)
    if pct == 0: return price_ttc
    return round(price_ttc / (1 + pct / 100), 4)


# =============================================================
# FIND OR CREATE CUSTOMER IN PENNYLANE
# =============================================================
def find_or_create_customer(client_info):
    """Cherche le client dans Pennylane par nom/email. Crée si pas trouvé."""
    import unicodedata
    ctype = client_info.get("customer_type", "individual")
    first = client_info.get("first_name", "")
    last = client_info.get("last_name", "")
    email = client_info.get("email", "")
    # Clean email: remove accents (Pennylane rejects accented emails)
    if email:
        email = unicodedata.normalize('NFKD', email).encode('ascii', 'ignore').decode('ascii')
    company = client_info.get("company", "")

    # Chercher par email
    if ctype == "individual":
        fl = json.dumps([{"field": "name", "operator": "eq", "value": f"{first} {last}"}])
        data = pl_get(f"{PL_BASE}/individual_customers", {"filter": fl, "limit": 5})
        if data and data.get("items"):
            cid = data["items"][0]["id"]
            log.info(f"  Client individuel trouvé: {cid}")
            return cid
        # Créer
        payload = {
            "first_name": first, "last_name": last,
            "phone": client_info.get("phone", "NO_PHONE"),
            "emails": [email] if email else [],
            "billing_address": {
                "address": client_info.get("billing_address", ""),
                "postal_code": client_info.get("billing_postal_code", ""),
                "city": client_info.get("billing_city", ""),
                "country_alpha2": client_info.get("country_alpha2", "FR"),
            },
            "delivery_address": {
                "address": client_info.get("shipping_address", ""),
                "postal_code": client_info.get("shipping_postal_code", ""),
                "city": client_info.get("shipping_city", ""),
                "country_alpha2": client_info.get("shipping_country_alpha2", "FR"),
            },
        }
        result = pl_post(f"{PL_BASE}/individual_customers", payload)
        if result:
            cid = result.get("id")
            log.info(f"  Client individuel créé: {cid}")
            return cid
    else:
        search_name = company or f"{first} {last}"
        fl = json.dumps([{"field": "name", "operator": "eq", "value": search_name}])
        data = pl_get(f"{PL_BASE}/company_customers", {"filter": fl, "limit": 5})
        if data and data.get("items"):
            cid = data["items"][0]["id"]
            log.info(f"  Client société trouvé: {cid}")
            return cid
        payload = {
            "name": search_name,
            "phone": client_info.get("phone", "NO_PHONE"),
            "emails": [email] if email else [],
            "billing_address": {
                "address": client_info.get("billing_address", ""),
                "postal_code": client_info.get("billing_postal_code", ""),
                "city": client_info.get("billing_city", ""),
                "country_alpha2": client_info.get("country_alpha2", "FR"),
            },
            "delivery_address": {
                "address": client_info.get("shipping_address", ""),
                "postal_code": client_info.get("shipping_postal_code", ""),
                "city": client_info.get("shipping_city", ""),
                "country_alpha2": client_info.get("shipping_country_alpha2", "FR"),
            },
        }
        vat_number = client_info.get("vat_number")
        if vat_number:
            payload["vat_number"] = vat_number
        result = pl_post(f"{PL_BASE}/company_customers", payload)
        if result:
            cid = result.get("id")
            log.info(f"  Client société créé: {cid}")
            return cid
    return None


# =============================================================
# FIND OR CREATE PRODUCT IN PENNYLANE
# =============================================================
_product_cache = {}

def find_or_create_product(product):
    sku = product.get("sku", "")
    price = str(product.get("price", "0"))
    vat_rate = product.get("vat_rate", "FR_200")
    cache_key = f"{sku}_{price}_{vat_rate}"

    if cache_key in _product_cache:
        return _product_cache[cache_key]

    # Chercher par description (SKU)
    # Pennylane ne permet pas de filtrer par description, on crée systématiquement
    payload = {
        "label": product["label"],
        "unit": "piece",
        "price_before_tax": price,
        "currency": product.get("currency", "EUR"),
        "vat_rate": vat_rate,
        "description": f"SKU: {sku}",
    }
    result = pl_post(f"{PL_BASE}/products", payload)
    if result:
        pid = result.get("id")
        _product_cache[cache_key] = pid
        log.info(f"  Produit créé: {pid} ({product['label'][:30]})")
        return pid
    return None


# =============================================================
# ANTI-DOUBLON
# =============================================================
def invoice_already_exists(order_name, date_str):
    """Vérifie si une facture existe déjà pour cette commande dans Pennylane."""
    fl = json.dumps([{"field": "date", "operator": "eq", "value": date_str}])
    cursor = None
    while True:
        params = {"filter": fl, "limit": 100}
        if cursor: params["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/customer_invoices", params)
        if not data: break
        for inv in data.get("items", []):
            sm = (inv.get("special_mention") or "") + " " + (inv.get("label") or "")
            if order_name in sm:
                log.info(f"  ⚠️ Facture déjà existante pour {order_name} (date {date_str})")
                return True
        if not data.get("has_more"): break
        cursor = data.get("next_cursor")
        if not cursor: break
    return False


# =============================================================
# PROCESS ORDER → CREATE DRAFT INVOICE
# =============================================================
def find_store_for_order(order_name):
    for s in STORES:
        if order_name.startswith(s["prefix"]):
            return s
    return None

def process_order(order_name, dry_run=False):
    """Traite une commande Shopify et crée la facture brouillon dans Pennylane."""
    log.info(f"\n{'='*50}")
    log.info(f"Traitement: {order_name}")

    store_config = find_store_for_order(order_name)
    if not store_config:
        log.error(f"  Boutique non trouvée pour {order_name}")
        return False

    # 1. Récupérer la commande Shopify
    token = get_shopify_token(store_config)
    if not token:
        log.error(f"  Token Shopify non obtenu")
        return False

    url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    data = shopify_get(url, token, {"name": order_name, "status": "any", "limit": 5})
    if not data or not data.get("orders"):
        data = shopify_get(url, token, {"name": f"#{order_name}", "status": "any", "limit": 5})
    if not data or not data.get("orders"):
        log.error(f"  Commande non trouvée dans Shopify")
        return False

    order = data["orders"][0]
    financial_status = order.get("financial_status", "")
    is_paid = (financial_status == "paid")

    # Skip si pas paid ou pending
    if financial_status not in ("paid", "pending", "refunded"):
        log.info(f"  Skip: financial_status={financial_status}")
        return False

    # Skip tags
    tags = order.get("tags", "")
    if "DEVIS_TRANSFORME_PL" in tags or "GARANTIE" in tags:
        log.info(f"  Skip: tag spécial")
        return False

    # Skip montant <= 0
    total_price = float(order.get("total_price", 0))
    if total_price <= 0:
        log.info(f"  Skip: montant={total_price}")
        return False

    # 2. Extraire les infos
    customer = order.get("customer") or {}
    billing = order.get("billing_address") or {}
    shipping = order.get("shipping_address") or billing
    currency = order.get("currency", "EUR")
    invoice_date = order.get("created_at", "")[:10]

    # Anti-doublon
    if invoice_already_exists(order_name, invoice_date):
        return False

    # Client
    first_name = customer.get("first_name") or shipping.get("first_name") or billing.get("first_name") or ""
    last_name = customer.get("last_name") or shipping.get("last_name") or billing.get("last_name") or ""
    if not first_name or not last_name:
        full = f"{first_name} {last_name}".strip()
        first_name = "-"
        last_name = full

    company = billing.get("company") or shipping.get("company") or ""
    customer_type = "company" if (company or order.get("tax_exempt") or customer.get("tax_exempt")) else "individual"

    email = customer.get("email", "")
    phone = billing.get("phone") or shipping.get("phone") or customer.get("phone") or "NO_PHONE"
    country_code = shipping.get("country_code") or billing.get("country_code") or "FR"

    # VAT number
    vat_number = None
    for attr in order.get("note_attributes", []):
        if attr.get("name") == "VAT number":
            vat_number = attr.get("value", "").strip()
            break

    vat_rate, has_vat_exemption = calculate_vat(country_code, customer_type, vat_number)

    client_info = {
        "first_name": first_name, "last_name": last_name,
        "email": email, "phone": phone,
        "billing_address": billing.get("address1", ""),
        "billing_postal_code": billing.get("zip", ""),
        "billing_city": billing.get("city", ""),
        "country_alpha2": billing.get("country_code", "FR"),
        "shipping_address": shipping.get("address1", ""),
        "shipping_postal_code": shipping.get("zip", ""),
        "shipping_city": shipping.get("city", ""),
        "shipping_country_alpha2": shipping.get("country_code", "FR"),
        "customer_type": customer_type,
        "company": company,
        "vat_number": vat_number,
    }

    log.info(f"  Client: {first_name} {last_name} ({customer_type}) | {country_code} | TVA: {vat_rate}")
    log.info(f"  Montant: {total_price}€ TTC | Statut: {financial_status}")

    if dry_run:
        log.info(f"  → DRY-RUN: serait créée en brouillon")
        return True

    # 3. Trouver/créer le client dans Pennylane
    customer_id = find_or_create_customer(client_info)
    if not customer_id:
        log.error(f"  Impossible de créer le client")
        return False

    # 4. Produits
    line_items = order.get("line_items", [])
    invoice_lines = []

    for item in line_items:
        sku = item.get("sku", "")
        label = item.get("name", "")
        quantity = int(item.get("quantity", 1))
        price_ttc = float(item.get("price", 0))
        price_ht = ttc_to_ht(price_ttc, vat_rate)

        if not sku or not label:
            continue

        product = {"sku": sku, "label": label, "price": str(price_ht), "vat_rate": vat_rate, "currency": currency}
        product_id = find_or_create_product(product)
        if not product_id:
            log.error(f"  Impossible de créer le produit {label}")
            return False

        inv_line = {"product_id": int(product_id), "label": label, "quantity": quantity, "unit": "piece"}

        # Remise relative
        discount_apps = order.get("discount_applications", [])
        for da in discount_apps:
            if da.get("value_type") == "percentage":
                inv_line["discount"] = {"type": "relative", "value": str(da.get("value", 0))}
                break

        invoice_lines.append(inv_line)

    # 5. Livraison
    shipping_lines = order.get("shipping_lines", [])
    if shipping_lines:
        sl = shipping_lines[0]
        ship_title = sl.get("title", "Livraison")
        ship_price_ttc = float(sl.get("price", 0))
        ship_price_ht = ttc_to_ht(ship_price_ttc, vat_rate)

        if ship_title.startswith("Chronopost"):
            ship_label = "Livraison : Chronopost Express"
        elif ship_title.startswith("Colissimo"):
            ship_label = "Livraison : Colissimo"
        elif "point" in ship_title.lower() or "relay" in ship_title.lower() or "mondial" in ship_title.lower():
            ship_label = "Livraison : point relais"
        else:
            ship_label = ship_title or "Livraison"

        ship_product = {"sku": f"livraison-{ship_price_ttc}", "label": ship_label, "price": str(ship_price_ht), "vat_rate": vat_rate, "currency": currency}
        ship_product_id = find_or_create_product(ship_product)
        if ship_product_id:
            invoice_lines.append({"product_id": int(ship_product_id), "label": ship_label, "quantity": 1, "unit": "piece"})

    # 6. Special mention
    discount_codes = order.get("discount_codes", [])
    discount_code = discount_codes[0]["code"] if discount_codes else None

    payment_text = "\nFacture déjà payée" if is_paid else "\nFacture en attente de paiement par virement bancaire"
    vat_text = ""
    if has_vat_exemption and vat_number:
        vat_text = f"\nNuméro de TVA: {vat_number}\nExonération - TVA non applicable - art. 259-1 du CGI"

    if discount_code:
        special_mention = f"Commande {order_name}\nUtilisation du code {discount_code}{vat_text}{payment_text}"
    else:
        special_mention = f"Commande {order_name}{vat_text}{payment_text}"

    note = order.get("note", "")
    if note:
        special_mention = f"{note}\n{special_mention}"

    # 7. Dates
    deadline = (datetime.strptime(invoice_date, "%Y-%m-%d") + timedelta(days=1 if is_paid else 7)).strftime("%Y-%m-%d")

    # 8. Payload
    payload = {
        "date": invoice_date,
        "deadline": deadline,
        "customer_id": int(customer_id),
        "customer_invoice_template_id": int(store_config["template_id"]),
        "currency": currency,
        "special_mention": special_mention,
        "language": "fr_FR",
        "draft": True,  # BROUILLON
        "invoice_lines": invoice_lines,
    }

    # Remise absolue au niveau facture
    for da in order.get("discount_applications", []):
        if da.get("value_type") == "fixed_amount":
            payload["discount"] = {"type": "absolute", "value": str(da.get("value", 0))}
            break

    log.info(f"  Création facture brouillon...")
    resp = requests.post(f"{PL_BASE}/customer_invoices", headers=PL_HEADERS, json=payload, timeout=30)

    if resp.status_code in (200, 201):
        inv_id = resp.json().get("id")
        inv_num = resp.json().get("invoice_number", "")
        log.info(f"  ✅ Facture brouillon créée: {inv_id} ({inv_num})")
        return True
    else:
        log.error(f"  ❌ Erreur: {resp.status_code} — {resp.text[:300]}")
        return False


# =============================================================
# MAIN
# =============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", required=True, help="Liste de commandes séparées par des virgules")
    parser.add_argument("--dry-run", action="store_true", help="Simulation")
    args = parser.parse_args()

    orders = [o.strip() for o in args.orders.split(",")]
    log.info(f"=== Création de {len(orders)} facture(s) brouillon ===")

    ok = 0
    ko = 0
    for order_name in orders:
        if process_order(order_name, dry_run=args.dry_run):
            ok += 1
        else:
            ko += 1

    log.info(f"\n=== Terminé: {ok} OK, {ko} erreurs ===")

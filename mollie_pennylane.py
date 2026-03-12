#!/usr/bin/env python3
"""
=============================================================
MOLLIE → PENNYLANE — Automatisation des écritures comptables
=============================================================
Récupère les settlements Mollie (paidout, 48h), résout les
commandes Shopify associées, puis crée les écritures dans
PennyLane (3 lignes : 411MOLLIE débit net, client crédit brut,
627001 débit frais).

Anti-doublon via PostgreSQL (table processed_mollie_settlements).

Auteur : Conversion Python du module Node.js mollie-pennylane
Date   : Mars 2026
"""

import requests
import json
import re
import os
import sys
import logging
import time
from datetime import datetime, timedelta
import psycopg2

# =============================================================
# CONFIGURATION
# =============================================================

MOLLIE_BASE_URL = "https://api.mollie.com/v2"
SHOPIFY_API_VERSION = "2026-01"

# Token OAuth Mollie global (valable pour toutes les boutiques)
MOLLIE_OAUTH_TOKEN = os.environ.get("MOLLIE_OAUTH_TOKEN", "")

STORES = [
    {
        "name": "LFC",
        "shopify_url": "mon-filet-de-camouflage.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_LFC", ""),
    },
    {
        "name": "HET",
        "shopify_url": "het-camouflagenet.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_HET", ""),
    },
    {
        "name": "TAR",
        "shopify_url": "tarnnetz.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_TZ", ""),   # Render: SHOPIFY_SECRET_TZ
    },
    {
        "name": "RED",
        "shopify_url": "red-de-camuflaje.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_RED", ""),
    },
    {
        "name": "COCO",
        "shopify_url": "coconets.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_MTC", ""),  # Render: SHOPIFY_SECRET_MTC
    },
    {
        "name": "LOV",
        "shopify_url": "le-filet-camouflage-1.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_LVO", ""),  # Render: SHOPIFY_SECRET_LVO
    },
    {
        "name": "RETE",
        "shopify_url": "rete-mimetica.myshopify.com",
        "shopify_token": os.environ.get("SHOPIFY_SECRET_RETE", ""),
    },
]

# --- PENNYLANE ---
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PENNYLANE_BASE_URL = "https://app.pennylane.com/api/external/v2"

# --- COMPTES COMPTABLES ---
JOURNAL_CODE = "ENCSP"
COMPTE_MOLLIE = "411MOLLIE"
COMPTE_FRAIS = "627001"
COMPTE_CLIENT_FALLBACK = "411NA"
WINDOW_HOURS = 48

# --- BASE DE DONNÉES ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# --- OPTIONS ---
MODE_TEST = os.environ.get("MODE_TEST", "false").lower() == "true"

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mollie_pennylane")

# =============================================================
# TELEGRAM
# =============================================================
def telegram_send(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

# =============================================================
# BASE DE DONNÉES
# =============================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_mollie_settlements (
                    id SERIAL PRIMARY KEY,
                    mollie_id VARCHAR(255) UNIQUE NOT NULL,
                    store_name VARCHAR(50),
                    payment_ref VARCHAR(255),
                    order_name VARCHAR(50),
                    invoice_number VARCHAR(100),
                    amount DECIMAL(10,2),
                    processed_at TIMESTAMP DEFAULT NOW(),
                    status VARCHAR(50)
                );
                CREATE INDEX IF NOT EXISTS idx_mollie_settlements_mollie_id
                    ON processed_mollie_settlements(mollie_id);
            """)
        conn.commit()
        log.info("[DB] Migrations OK - table processed_mollie_settlements prete")
    finally:
        conn.close()

def is_already_processed(mollie_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM processed_mollie_settlements WHERE mollie_id = %s",
                (mollie_id,),
            )
            return cur.fetchone() is not None
    finally:
        conn.close()

def save_result(mollie_id, store_name, payment_ref=None, order_name=None,
                invoice_number=None, amount=None, status="success"):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO processed_mollie_settlements
                     (mollie_id, store_name, payment_ref, order_name, invoice_number, amount, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (mollie_id) DO UPDATE SET status = EXCLUDED.status""",
                (mollie_id, store_name, payment_ref, order_name, invoice_number, amount, status),
            )
        conn.commit()
    finally:
        conn.close()

# =============================================================
# MOLLIE API
# =============================================================
def mollie_get(path, api_key):
    url = f"{MOLLIE_BASE_URL}{path}" if path.startswith("/") else path
    for attempt in range(5):
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = min(2 ** attempt, 10)
            log.info(f"  [Mollie] Rate limit - pause {wait}s...")
            time.sleep(wait)
            continue
        log.error(f"[Mollie] GET {path}: {resp.status_code} - {resp.text}")
        return None
    return None

def get_recent_settlements(window_hours, api_key):
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    settlements = []
    path = "/settlements?limit=50"

    while path:
        data = mollie_get(path, api_key)
        if not data:
            break

        items = data.get("_embedded", {}).get("settlements", [])
        reached_old = False

        for s in items:
            if s.get("status") != "paidout":
                continue
            settled_at = datetime.fromisoformat(s["settledAt"].replace("Z", "+00:00")).replace(tzinfo=None)
            if settled_at < cutoff:
                reached_old = True
                break
            settlements.append(s)

        if reached_old:
            break

        next_link = data.get("_links", {}).get("next", {}).get("href")
        if not next_link:
            break
        from urllib.parse import urlparse
        parsed = urlparse(next_link)
        path = parsed.path.replace("/v2", "") + ("?" + parsed.query if parsed.query else "")

    log.info(f"[Mollie] {len(settlements)} settlement(s) paidout dans les dernieres {window_hours}h")
    return settlements

def get_settlement_payments(settlement_id, api_key):
    payments = []
    path = f"/settlements/{settlement_id}/payments?limit=250"

    while path:
        data = mollie_get(path, api_key)
        if not data:
            break

        items = data.get("_embedded", {}).get("payments", [])
        payments.extend(items)

        next_link = data.get("_links", {}).get("next", {}).get("href")
        if not next_link:
            break
        from urllib.parse import urlparse
        parsed = urlparse(next_link)
        path = parsed.path.replace("/v2", "") + ("?" + parsed.query if parsed.query else "")

    real = [
        p for p in payments
        if float((p.get("amount") or {}).get("value", "0")) > 0
        and p.get("status") in ("paid", "paidout")
    ]
    log.info(f"[Mollie] Settlement {settlement_id} : {len(real)} paiement(s) reel(s) (total brut: {len(payments)})")
    return real

# =============================================================
# SHOPIFY — Résolution de commande
# =============================================================
ORDER_PREFIXES = ["LFC", "RED", "HET", "MTC", "MO", "RETE", "TZ", "LVO", "UNIV", "HC", "RDC", "COCO"]

def extract_order_name(description):
    if not description:
        return None
    pattern = r"#?(" + "|".join(ORDER_PREFIXES) + r")(\d{3,6})"
    match = re.search(pattern, description, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()}{match.group(2)}"
    return None

def shopify_get(endpoint, store_url, token, params=None):
    url = f"https://{store_url}/admin/api/{SHOPIFY_API_VERSION}/{endpoint}"
    resp = requests.get(
        url,
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        params=params,
        timeout=15,
    )
    if resp.status_code == 200:
        return resp.json()
    log.warning(f"[Shopify] GET {endpoint}: {resp.status_code}")
    return None

def find_order_by_name(order_name, store_url, token):
    data = shopify_get(
        "orders.json",
        store_url,
        token,
        params={"name": f"#{order_name}", "status": "any", "fields": "id,name,billing_address", "limit": 5},
    )
    if not data:
        return None
    orders = data.get("orders", [])
    if not orders:
        return None
    order = orders[0]
    billing = order.get("billing_address") or {}
    billing_name = billing.get("company") or billing.get("name") or "Client inconnu"
    return {"name": order_name, "billing_name": billing_name}

def resolve_order_from_payment(mollie_payment, store_url, token):
    description = mollie_payment.get("description", "")
    mollie_id = mollie_payment.get("id", "")

    order_name = extract_order_name(description)
    if order_name:
        order = find_order_by_name(order_name, store_url, token)
        if order:
            log.info(f"  [Shopify] Commande trouvee via description: {order_name} -> {order['billing_name']}")
            return order

    log.info(f"  [Shopify] Strategie 2 - scan des commandes recentes pour {mollie_id}")
    since = (datetime.utcnow() - timedelta(hours=72)).isoformat() + "Z"
    data = shopify_get(
        "orders.json",
        store_url,
        token,
        params={"status": "any", "created_at_min": since, "fields": "id,name,billing_address", "limit": 250},
    )
    if data:
        for order in data.get("orders", []):
            tx_data = shopify_get(
                f"orders/{order['id']}/transactions.json",
                store_url,
                token,
                params={"fields": "id,authorization,gateway"},
            )
            if tx_data:
                for tx in tx_data.get("transactions", []):
                    if tx.get("authorization") in (mollie_id, description):
                        billing = order.get("billing_address") or {}
                        billing_name = billing.get("company") or billing.get("name") or "Client inconnu"
                        name = order.get("name", "").replace("#", "")
                        log.info(f"  [Shopify] Commande trouvee via transaction: {name} -> {billing_name}")
                        return {"name": name, "billing_name": billing_name}

    log.warning(f"  [Shopify] Aucune commande trouvee pour description=\"{description}\" (mollieId={mollie_id})")
    return None

# =============================================================
# PENNYLANE API
# =============================================================
_pl_cache = {"accounts": {}, "journals": None, "customers": {}}

def pennylane_get(endpoint, params=None):
    url = f"{PENNYLANE_BASE_URL}/{endpoint}"
    for attempt in range(5):
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"},
            params=params,
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = min(2 ** attempt, 10)
            log.info(f"  [PennyLane] Rate limit - pause {wait}s...")
            time.sleep(wait)
            continue
        log.error(f"[PennyLane] GET {endpoint}: {resp.status_code} - {resp.text}")
        return None
    return None

def pennylane_post(endpoint, payload):
    url = f"{PENNYLANE_BASE_URL}/{endpoint}"
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if resp.status_code in (200, 201):
        return resp.json()
    log.error(f"[PennyLane] POST {endpoint}: {resp.status_code} - {resp.text}")
    return None

def get_account_id(account_number):
    if account_number in _pl_cache["accounts"]:
        return _pl_cache["accounts"][account_number]

    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
    data = pennylane_get("ledger_accounts", {"filter": filter_param, "per_page": 5})
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        log.warning(f"[PennyLane] Compte introuvable: {account_number}")
        return None
    _pl_cache["accounts"][account_number] = items[0]["id"]
    return items[0]["id"]

def get_journal_id(code):
    if _pl_cache["journals"] is None:
        data = pennylane_get("journals", {"per_page": 100})
        _pl_cache["journals"] = data.get("items", []) if data else []

    for j in _pl_cache["journals"]:
        if (j.get("code") or "").upper() == code.upper():
            return j["id"]
    log.error(f"[PennyLane] Journal introuvable: {code}")
    return None

def find_invoice_by_order_name(order_name):
    for field in ("special_mention", "label"):
        filter_param = json.dumps([{"field": field, "operator": "contains", "value": order_name}])
        data = pennylane_get("customer_invoices", {"filter": filter_param, "per_page": 5})
        if not data:
            continue
        items = data.get("items", [])
        if items:
            inv = items[0]
            customer = inv.get("customer") or {}
            log.info(f"  [PennyLane] Facture trouvee (via {field}): {inv.get('invoice_number')} pour commande {order_name}")
            return {
                "invoice_number": inv.get("invoice_number"),
                "customer_id": customer.get("id") or customer.get("source_id"),
                "customer_name": customer.get("name", "Client inconnu"),
            }

    log.warning(f"  [PennyLane] Aucune facture trouvee pour commande {order_name}")
    return None

def get_customer_account_number(customer_id):
    if customer_id in _pl_cache["customers"]:
        return _pl_cache["customers"][customer_id]

    data = pennylane_get(f"customers/{customer_id}")
    if not data:
        return None
    customer = data.get("customer") or data
    ledger_account = customer.get("ledger_account") or {}
    result = {
        "name": customer.get("name", "Client inconnu"),
        "account_number": ledger_account.get("number"),
        "account_id": ledger_account.get("id"),
    }
    _pl_cache["customers"][customer_id] = result
    return result

def create_ledger_entry(date, label, journal_code, lines, piece=None):
    journal_id = get_journal_id(journal_code)
    if not journal_id:
        raise Exception(f"Journal {journal_code} introuvable")

    resolved_lines = []
    for line in lines:
        account_id = line.get("account_id")
        if not account_id and line.get("account_number"):
            account_id = get_account_id(line["account_number"])
        if not account_id:
            raise Exception(f"Compte introuvable: {line.get('account_number')}")
        resolved_lines.append({
            "ledger_account_id": account_id,
            "debit": f"{float(line.get('debit', 0)):.2f}",
            "credit": f"{float(line.get('credit', 0)):.2f}",
            "label": line.get("label", label),
        })

    total_debit = sum(float(l["debit"]) for l in resolved_lines)
    total_credit = sum(float(l["credit"]) for l in resolved_lines)
    if abs(total_debit - total_credit) > 0.01:
        raise Exception(f"Ecriture desequilibree: debit={total_debit:.2f} credit={total_credit:.2f}")

    payload = {
        "date": date,
        "label": label,
        "journal_id": journal_id,
        "ledger_entry_lines": resolved_lines,
    }
    if piece:
        payload["reference"] = piece

    if MODE_TEST:
        log.info(f"  [PennyLane] [MODE TEST] Simulation ecriture: {label} | piece={piece}")
        for l in resolved_lines:
            log.info(f"    -> Compte {l['ledger_account_id']}  D:{l['debit']}  C:{l['credit']}")
        return {"id": "TEST", "status": "simulated"}

    data = pennylane_post("ledger_entries", payload)
    if data:
        entry_id = data.get("id") or (data.get("ledger_entry") or {}).get("id")
        log.info(f"  [PennyLane] Ecriture creee (id={entry_id})")
    return data

# =============================================================
# TRAITEMENT D'UN PAIEMENT
# =============================================================
def process_payment(payment, store):
    mollie_id = payment.get("id", "")
    description = payment.get("description", "")
    amount = float(payment.get("amount", {}).get("value", "0"))
    settl_amt = float(
        payment.get("settlementAmount", {}).get("value")
        or payment.get("amount", {}).get("value", "0")
    )
    frais = round(amount - settl_amt, 2)
    payment_date = (payment.get("createdAt") or "")[:10]

    log.info(f"\n--- Paiement {mollie_id} | {amount}EUR (net: {settl_amt}EUR, frais: {frais}EUR)")

    if is_already_processed(mollie_id):
        log.info("  -> Deja traite, skip")
        return "skipped"

    shopify_order = resolve_order_from_payment(payment, store["shopify_url"], store["shopify_token"])
    if not shopify_order:
        save_result(mollie_id, store["name"], payment_ref=description, amount=amount, status="error")
        return "error"

    order_name = shopify_order["name"]

    invoice = find_invoice_by_order_name(order_name)
    if not invoice:
        save_result(mollie_id, store["name"], payment_ref=description, order_name=order_name, amount=amount, status="error")
        return "error"

    invoice_number = invoice["invoice_number"]
    customer_id = invoice["customer_id"]
    customer_name = invoice["customer_name"]
    piece = f"{JOURNAL_CODE}-{invoice_number}"

    client_account_number = COMPTE_CLIENT_FALLBACK
    client_account_id = None

    if customer_id:
        customer_info = get_customer_account_number(customer_id)
        if customer_info and customer_info.get("account_number"):
            client_account_number = customer_info["account_number"]
            client_account_id = customer_info.get("account_id")

    if client_account_number == COMPTE_CLIENT_FALLBACK:
        log.warning(f"  -> Compte client introuvable pour {customer_name} - utilisation de {COMPTE_CLIENT_FALLBACK}")

    libelle = f"{customer_name} - {order_name}"

    lines = [
        {
            "account_number": COMPTE_MOLLIE,
            "debit": settl_amt,
            "credit": 0,
            "label": libelle,
        },
        {
            "account_number": client_account_number,
            "account_id": client_account_id,
            "debit": 0,
            "credit": amount,
            "label": libelle,
        },
    ]

    if frais > 0.001:
        lines.append({
            "account_number": COMPTE_FRAIS,
            "debit": frais,
            "credit": 0,
            "label": libelle,
        })

    try:
        create_ledger_entry(
            date=payment_date,
            label=libelle,
            journal_code=JOURNAL_CODE,
            lines=lines,
            piece=piece,
        )
    except Exception as e:
        log.error(f"  -> Erreur creation ecriture: {e}")
        save_result(mollie_id, store["name"], payment_ref=description, order_name=order_name,
                    invoice_number=invoice_number, amount=amount, status="error")
        return "error"

    save_result(mollie_id, store["name"], payment_ref=description, order_name=order_name,
                invoice_number=invoice_number, amount=amount, status="success")
    log.info(f"  -> OK : {libelle} | {amount}EUR | piece={piece}")
    return "success"

# =============================================================
# JOB PRINCIPAL PAR BOUTIQUE
# =============================================================
def process_mollie_store(store):
    started_at = time.time()
    name = store["name"]
    log.info("=" * 50)
    log.info(f"Mollie -> PennyLane - [{name}] demarrage du job")
    log.info(f"Fenetre : {WINDOW_HOURS}h | Mode test : {'OUI' if MODE_TEST else 'NON'}")
    log.info("=" * 50)

    total_success = 0
    total_error = 0
    total_skipped = 0

    try:
        settlements = get_recent_settlements(WINDOW_HOURS, MOLLIE_OAUTH_TOKEN)
        if not settlements:
            log.info("Aucun settlement paidout dans les dernieres 48h.")
            return total_success, total_error, total_skipped

        for settlement in settlements:
            log.info(f"\nSettlement {settlement['id']} | {settlement.get('amount', {}).get('value')}EUR | {settlement.get('settledAt')}")

            try:
                payments = get_settlement_payments(settlement["id"], MOLLIE_OAUTH_TOKEN)
            except Exception as e:
                log.error(f"Erreur recuperation paiements du settlement {settlement['id']}: {e}")
                total_error += 1
                continue

            for payment in payments:
                try:
                    result = process_payment(payment, store)
                    if result == "success":
                        total_success += 1
                    elif result == "skipped":
                        total_skipped += 1
                    else:
                        total_error += 1
                except Exception as e:
                    log.error(f"Erreur inattendue sur paiement {payment.get('id')}: {e}")
                    total_error += 1
                    try:
                        save_result(
                            payment.get("id", "unknown"), store["name"],
                            payment_ref=payment.get("description"),
                            amount=float(payment.get("amount", {}).get("value", "0")),
                            status="error",
                        )
                    except Exception:
                        pass
    except Exception as e:
        log.error(f"Erreur fatale du job: {e}")

    duration = time.time() - started_at
    log.info("=" * 50)
    log.info(f"Job termine en {duration:.1f}s")
    log.info(f"  Succes : {total_success}")
    log.info(f"  Erreurs: {total_error}")
    log.info(f"  Skipped: {total_skipped}")
    log.info("=" * 50)

    return total_success, total_error, total_skipped

# =============================================================
# POINT D'ENTREE
# =============================================================
def run():
    init_db()

    if not MOLLIE_OAUTH_TOKEN:
        log.error("MOLLIE_OAUTH_TOKEN manquant - arret")
        return

    log.info(f"Mollie -> PennyLane - {len(STORES)} boutiques a traiter")

    grand_total_success = 0
    grand_total_error = 0

    for store in STORES:
        if not store["shopify_token"]:
            log.warning(f"[{store['name']}] Cles manquantes - boutique ignoree")
            continue
        try:
            success, errors, _ = process_mollie_store(store)
            grand_total_success += success
            grand_total_error += errors
        except Exception as e:
            log.error(f"[{store['name']}] Erreur fatale: {e}")
            grand_total_error += 1

    log.info("Toutes les boutiques traitees.")

    if grand_total_success > 0:
        telegram_send(
            f"Mollie -> PennyLane : {grand_total_success} ecriture(s) creee(s), "
            f"{grand_total_error} erreur(s)"
        )
    elif grand_total_error == 0:
        telegram_send("Mollie -> PennyLane : aucun nouveau settlement a traiter")


if __name__ == "__main__":
    run()

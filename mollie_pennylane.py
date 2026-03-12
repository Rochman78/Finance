#!/usr/bin/env python3
"""
=============================================================
MOLLIE → PENNYLANE — Automatisation des écritures comptables
=============================================================
Match Mollie → commande via table shopify_orders (PostgreSQL).
Zéro appel Shopify en temps réel.

Prérequis : sync_shopify_orders.py doit avoir été exécuté au moins une fois.
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
MOLLIE_OAUTH_TOKEN = os.environ.get("MOLLIE_OAUTH_TOKEN", "")

STORES = [
    {"name": "LFC",  "shopify_url": "mon-filet-de-camouflage.myshopify.com"},
    {"name": "HET",  "shopify_url": "het-camouflagenet.myshopify.com"},
    {"name": "TAR",  "shopify_url": "tarnnetz.myshopify.com"},
    {"name": "RED",  "shopify_url": "red-de-camuflaje.myshopify.com"},
    {"name": "COCO", "shopify_url": "coconets.myshopify.com"},
    {"name": "LOV",  "shopify_url": "le-filet-camouflage-1.myshopify.com"},
    {"name": "RETE", "shopify_url": "rete-mimetica.myshopify.com"},
]

DOMAIN_TO_STORE = {s["shopify_url"]: s["name"] for s in STORES}

# --- PENNYLANE ---
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PENNYLANE_BASE_URL = "https://app.pennylane.com/api/external/v2"

# --- COMPTES COMPTABLES ---
JOURNAL_CODE = "ENCSP"
COMPTE_MOLLIE = "411MOLLIE"
COMPTE_FRAIS = "627001"
COMPTE_CLIENT_FALLBACK = "411NA"
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", 48))

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
                "SELECT 1 FROM processed_mollie_settlements WHERE mollie_id = %s",
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

def lookup_order_by_payment_id(shopify_payment_id):
    """Cherche la commande dans le cache DB via le payment_id Shopify."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_name, store_name, billing_name FROM shopify_orders WHERE payment_id = %s",
                (shopify_payment_id,),
            )
            row = cur.fetchone()
            if row:
                return {"order_name": row[0], "store_name": row[1], "billing_name": row[2]}
            return None
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
        items = (data.get("_embedded") or {}).get("settlements", [])
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
        next_link = ((data.get("_links") or {}).get("next") or {}).get("href")
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
        items = (data.get("_embedded") or {}).get("payments", [])
        payments.extend(items)
        next_link = ((data.get("_links") or {}).get("next") or {}).get("href")
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
# RÉSOLUTION COMMANDE via DB
# =============================================================
def resolve_order_from_payment(mollie_payment):
    """
    Résout la commande Shopify depuis le cache DB.
    Utilise metadata.shopify_payment_id = shopify_orders.payment_id
    """
    mollie_id = mollie_payment.get("id", "")
    metadata = mollie_payment.get("metadata") or {}
    shopify_payment_id = metadata.get("shopify_payment_id")

    if not shopify_payment_id:
        log.warning(f"  [DB] Pas de shopify_payment_id dans metadata pour {mollie_id}")
        return None

    order = lookup_order_by_payment_id(shopify_payment_id)
    if order:
        log.info(f"  [DB] Commande trouvee: {order['order_name']} ({order['store_name']}) -> {order['billing_name']}")
        return order

    log.warning(f"  [DB] Aucune commande pour shopify_payment_id={shopify_payment_id} (mollie={mollie_id})")
    log.warning(f"       → Relancer sync_shopify_orders.py si la commande est récente")
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
    """Cherche la facture dans le cache DB (table invoices)."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT invoice_number, customer_id, customer_name
                   FROM invoices WHERE order_number = %s""",
                (order_name,),
            )
            row = cur.fetchone()
            if row:
                log.info(f"  [DB] Facture trouvee: {row[0]} pour {order_name}")
                return {"invoice_number": row[0], "customer_id": row[1], "customer_name": row[2]}
    finally:
        conn.close()
    log.warning(f"  [DB] Aucune facture pour commande {order_name}")
    return None

def get_customer_account(customer_id):
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
        "label": f"{piece} | {label}" if piece else label,
        "journal_id": journal_id,
        "ledger_entry_lines": resolved_lines,
    }

    if MODE_TEST:
        log.info(f"  [PennyLane] [MODE TEST] Ecriture: {label} | piece={piece}")
        for l in resolved_lines:
            log.info(f"    -> Compte {l['ledger_account_id']}  D:{l['debit']}  C:{l['credit']}")
        return {"id": "TEST", "status": "simulated"}

    data = pennylane_post("ledger_entries", payload)
    if data:
        entry_id = data.get("id") or (data.get("ledger_entry") or {}).get("id")
        log.info(f"  [PennyLane] Ecriture creee (id={entry_id})")
    return data

# =============================================================
# RESOLUTION D'UN PAIEMENT (sans creer d'ecriture)
# =============================================================
def resolve_payment(payment, store, fee_ratio=0.0):
    mollie_id = payment.get("id", "")
    description = payment.get("description", "")
    amount = float(payment.get("amount", {}).get("value", "0"))
    frais = round(amount * fee_ratio, 2)
    payment_date = (payment.get("createdAt") or "")[:10]
    log.info(f"\n--- Paiement {mollie_id} | {amount}EUR (frais: {frais}EUR)")
    if is_already_processed(mollie_id):
        log.info("  -> Deja traite, skip")
        return {"status": "skipped", "mollie_id": mollie_id}
    shopify_order = resolve_order_from_payment(payment)
    if not shopify_order:
        save_result(mollie_id, store["name"], payment_ref=description, amount=amount, status="error_no_order")
        return {"status": "error", "mollie_id": mollie_id}
    order_name = shopify_order["order_name"]
    billing_name = shopify_order["billing_name"]
    invoice = find_invoice_by_order_name(order_name)
    if not invoice:
        save_result(mollie_id, store["name"], payment_ref=description, order_name=order_name, amount=amount, status="error_no_invoice")
        return {"status": "error", "mollie_id": mollie_id}
    invoice_number = invoice["invoice_number"]
    customer_id = invoice["customer_id"]
    customer_name = invoice["customer_name"] or billing_name
    client_account_number = COMPTE_CLIENT_FALLBACK
    client_account_id = None
    if customer_id:
        customer_info = get_customer_account(customer_id)
        if customer_info and customer_info.get("account_number"):
            client_account_number = customer_info["account_number"]
            client_account_id = customer_info.get("account_id")
    if client_account_number == COMPTE_CLIENT_FALLBACK:
        log.warning(f"  -> Compte client introuvable pour {customer_name} - utilisation de {COMPTE_CLIENT_FALLBACK}")
    return {
        "status": "ok",
        "mollie_id": mollie_id,
        "description": description,
        "order_name": order_name,
        "invoice_number": invoice_number,
        "customer_name": customer_name,
        "customer_id": customer_id,
        "client_account_number": client_account_number,
        "client_account_id": client_account_id,
        "amount": amount,
        "frais": frais,
        "payment_date": payment_date,
        "store_name": store["name"],
    }

# =============================================================
# TRAITEMENT D'UN SETTLEMENT (1 ecriture groupee)
# =============================================================
def process_settlement(settlement, payments, store, fee_ratio=0.0):
    """1 ecriture par settlement :
       N x credit 411CLIENT (brut), N x debit 627001 (frais), 1 x debit 411MOLLIE (net).
    """
    settlement_net = float((settlement.get("amount") or {}).get("value", "0"))
    settlement_date = (settlement.get("settledAt") or "")[:10]
    resolved = [resolve_payment(p, store, fee_ratio=fee_ratio) for p in payments]
    ok_items  = [r for r in resolved if r["status"] == "ok"]
    err_items = [r for r in resolved if r["status"] == "error"]
    if not ok_items and not err_items:
        return "skipped", 0, 0
    if not ok_items:
        return "error", 0, len(err_items)
    lines = []
    pieces = []
    for r in ok_items:
        libelle = f"{r['customer_name']} - {r['order_name']}"
        lines.append({"account_number": r["client_account_number"], "account_id": r["client_account_id"], "debit": 0, "credit": r["amount"], "label": libelle})
        if r["frais"] > 0.001:
            lines.append({"account_number": COMPTE_FRAIS, "debit": r["frais"], "credit": 0, "label": f"Frais Mollie - {r['order_name']}"})
        pieces.append(f"{JOURNAL_CODE}-{r['invoice_number']}")
    lines.append({"account_number": COMPTE_MOLLIE, "debit": settlement_net, "credit": 0, "label": f"Virement Mollie {settlement['id']}"})
    piece = pieces[0] if len(pieces) == 1 else f"{JOURNAL_CODE}-{settlement['id']}"
    label = " | ".join(f"{r['customer_name']} - {r['order_name']}" for r in ok_items)[:200]
    try:
        create_ledger_entry(date=settlement_date, label=label, journal_code=JOURNAL_CODE, lines=lines, piece=piece)
    except Exception as e:
        log.error(f"  -> Erreur creation ecriture settlement {settlement['id']}: {e}")
        for r in ok_items:
            save_result(r["mollie_id"], r["store_name"], payment_ref=r["description"], order_name=r["order_name"], invoice_number=r["invoice_number"], amount=r["amount"], status="error_pennylane")
        return "error", 0, len(ok_items) + len(err_items)
    for r in ok_items:
        save_result(r["mollie_id"], r["store_name"], payment_ref=r["description"], order_name=r["order_name"], invoice_number=r["invoice_number"], amount=r["amount"], status="success")
        log.info(f"  -> OK : {r['customer_name']} - {r['order_name']} | {r['amount']}EUR")
    log.info(f"  -> Ecriture settlement {settlement['id']} : {settlement_net}EUR NET | {len(ok_items)} paiement(s) | piece={piece}")
    return "success", len(ok_items), len(err_items)

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

    total_success = total_error = total_skipped = 0

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
                log.error(f"Erreur recuperation paiements: {e}")
                total_error += 1
                continue

            # Calcul frais + ecriture groupee par settlement
            total_brut = sum(float((p.get("amount") or {}).get("value", "0")) for p in payments)
            settlement_net = float((settlement.get("amount") or {}).get("value", "0"))
            fee_ratio = (total_brut - settlement_net) / total_brut if total_brut > 0 else 0.0
            try:
                result, n_ok, n_err = process_settlement(settlement, payments, store, fee_ratio=fee_ratio)
                if result == "success":
                    total_success += n_ok
                    total_error += n_err
                elif result == "skipped":
                    total_skipped += len(payments)
                else:
                    total_error += n_err or 1
            except Exception as e:
                log.error(f"Erreur inattendue sur settlement {settlement.get('id')}: {e}")
                total_error += 1
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
# POINT D'ENTRÉE
# =============================================================
def run():
    init_db()

    if not MOLLIE_OAUTH_TOKEN:
        log.error("MOLLIE_OAUTH_TOKEN manquant - arret")
        return

    log.info(f"Mollie -> PennyLane - {len(STORES)} boutiques a traiter")

    grand_total_success = grand_total_error = 0

    for store in STORES:
        try:
            success, errors, _ = process_mollie_store(store)
            grand_total_success += success
            grand_total_error += errors
        except Exception as e:
            log.error(f"[{store['name']}] Erreur fatale: {e}")
            grand_total_error += 1

    log.info("Toutes les boutiques traitees.")

    if grand_total_success > 0:
        telegram_send(f"Mollie -> PennyLane : {grand_total_success} ecriture(s) creee(s), {grand_total_error} erreur(s)")
    elif grand_total_error == 0:
        telegram_send("Mollie -> PennyLane : aucun nouveau settlement a traiter")


if __name__ == "__main__":
    run()

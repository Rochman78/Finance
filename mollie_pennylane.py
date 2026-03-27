#!/usr/bin/env python3
"""
=============================================================
MOLLIE → PENNYLANE — Écritures comptables
=============================================================
Lit les données depuis la base PostgreSQL (alimentée par db_loader.py).
Ne fait aucun appel à Shopify en temps réel.

Usage :
  python mollie_pennylane.py --cron          # Settlements de la veille
  python mollie_pennylane.py --date 2026-03-12
  python mollie_pennylane.py --test
"""

import os
import sys
import json
import re
import time
import logging
import argparse
import requests
import psycopg2
from datetime import datetime, timedelta, timezone

# =============================================================
# CONFIGURATION
# =============================================================
MOLLIE_BASE_URL   = "https://api.mollie.com/v2"
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

# --- PENNYLANE ---
PENNYLANE_TOKEN    = os.environ.get("PENNYLANE_TOKEN", "")
PENNYLANE_BASE_URL = "https://app.pennylane.com/api/external/v2"

# --- COMPTES COMPTABLES ---
JOURNAL_CODE            = "ENCSP"
COMPTE_MOLLIE           = "411MOLLIE"
COMPTE_FRAIS            = "627001"
COMPTE_CLIENT_FALLBACK  = "411NA"

# --- BASE DE DONNÉES ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("mollie_pennylane")

# =============================================================
# TELEGRAM
# =============================================================
def telegram_send(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")

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
                    id            SERIAL PRIMARY KEY,
                    mollie_id     VARCHAR(255) UNIQUE NOT NULL,
                    store_name    VARCHAR(50),
                    payment_ref   VARCHAR(255),
                    order_name    VARCHAR(50),
                    invoice_number VARCHAR(100),
                    amount        DECIMAL(10,2),
                    processed_at  TIMESTAMP DEFAULT NOW(),
                    status        VARCHAR(50)
                );
                CREATE INDEX IF NOT EXISTS idx_mollie_id ON processed_mollie_settlements(mollie_id);
            """)
        conn.commit()
    finally:
        conn.close()


def clear_processed_since(date_from: str):
    """Supprime les settlements traités depuis date_from pour permettre le retraitement."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM processed_mollie_settlements WHERE processed_at >= %s::date",
                (date_from,)
            )
            count = cur.rowcount
        conn.commit()
        log.info(f"🗑️  {count} settlement(s) Mollie supprimé(s) depuis {date_from}")
        return count
    finally:
        conn.close()


def is_already_processed(mollie_id: str) -> bool:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_mollie_settlements WHERE mollie_id = %s", (mollie_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def save_result(mollie_id: str, store_name: str, payment_ref=None, order_name=None,
                invoice_number=None, amount=None, status="success"):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_mollie_settlements
                  (mollie_id, store_name, payment_ref, order_name, invoice_number, amount, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (mollie_id) DO UPDATE SET status = EXCLUDED.status
            """, (mollie_id, store_name, payment_ref, order_name, invoice_number, amount, status))
        conn.commit()
    finally:
        conn.close()


def lookup_order_by_payment_id(shopify_payment_id: str) -> dict | None:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT order_name, store_name, billing_name FROM shopify_orders WHERE payment_id = %s",
                        (shopify_payment_id,))
            row = cur.fetchone()
            return {"order_name": row[0], "store_name": row[1], "billing_name": row[2]} if row else None
    finally:
        conn.close()


def find_invoice_by_order_name(order_name: str) -> dict | None:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT invoice_number, customer_id, customer_name FROM invoices WHERE order_number = %s",
                        (order_name,))
            row = cur.fetchone()
            return {"invoice_number": row[0], "customer_id": row[1], "customer_name": row[2]} if row else None
    finally:
        conn.close()

# =============================================================
# MOLLIE API
# =============================================================
def mollie_get(path: str) -> dict | None:
    url = f"{MOLLIE_BASE_URL}{path}" if path.startswith("/") else path
    for attempt in range(5):
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {MOLLIE_OAUTH_TOKEN}", "Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 10))
            continue
        log.error(f"[Mollie] GET {path}: {resp.status_code} {resp.text}")
        return None
    return None


def get_settlements_for_date(target_date: str) -> list:
    """Récupère les settlements paidout pour une date donnée (settledAt = target_date).

    L'API Mollie trie les settlements par date de création (pas settledAt),
    donc on ne peut pas break dès qu'on voit une date antérieure — on doit
    scanner jusqu'à un seuil de sécurité (7 jours avant la date cible).
    """
    from datetime import datetime as _dt, timedelta as _td
    cutoff = (_dt.strptime(target_date, "%Y-%m-%d") - _td(days=7)).strftime("%Y-%m-%d")

    settlements = []
    path = "/settlements?limit=50"

    while path:
        data = mollie_get(path)
        if not data:
            break
        items       = (data.get("_embedded") or {}).get("settlements", [])
        all_too_old = True

        for s in items:
            if s.get("status") != "paidout":
                continue
            settled_at = (s.get("settledAt") or "")[:10]
            created_at = (s.get("createdAt") or "")[:10]
            # Utiliser la date de création comme garde-fou pour l'arrêt
            if created_at >= cutoff:
                all_too_old = False
            if settled_at == target_date:
                settlements.append(s)

        # Ne s'arrêter que si TOUS les items de la page ont une date
        # de création antérieure au cutoff (7 jours avant la cible)
        if all_too_old and items:
            break
        next_link = ((data.get("_links") or {}).get("next") or {}).get("href")
        if not next_link:
            break
        from urllib.parse import urlparse
        parsed = urlparse(next_link)
        path   = parsed.path.replace("/v2", "") + ("?" + parsed.query if parsed.query else "")

    log.info(f"[Mollie] {len(settlements)} settlement(s) paidout le {target_date}")
    return settlements


def get_settlement_payments(settlement_id: str) -> list:
    payments = []
    path     = f"/settlements/{settlement_id}/payments?limit=250"
    while path:
        data = mollie_get(path)
        if not data:
            break
        items = (data.get("_embedded") or {}).get("payments", [])
        payments.extend(items)
        next_link = ((data.get("_links") or {}).get("next") or {}).get("href")
        if not next_link:
            break
        from urllib.parse import urlparse
        parsed = urlparse(next_link)
        path   = parsed.path.replace("/v2", "") + ("?" + parsed.query if parsed.query else "")

    real = [p for p in payments
            if float((p.get("amount") or {}).get("value", "0")) > 0
            and p.get("status") in ("paid", "paidout")]
    log.info(f"[Mollie] Settlement {settlement_id} : {len(real)} paiement(s)")
    return real

# =============================================================
# PENNYLANE API
# =============================================================
_pl_accounts_cache = {}
_pl_journals_cache = None
_pl_customers_cache = {}


def get_account_id(account_number: str) -> int | None:
    if account_number in _pl_accounts_cache:
        return _pl_accounts_cache[account_number]
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
    resp = requests.get(
        f"{PENNYLANE_BASE_URL}/ledger_accounts",
        headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"},
        params={"filter": filter_param, "per_page": 5},
        timeout=15,
    )
    if resp.status_code == 200:
        items = resp.json().get("items", [])
        if items:
            _pl_accounts_cache[account_number] = items[0]["id"]
            return items[0]["id"]
    log.warning(f"[PennyLane] Compte introuvable: {account_number}")
    return None


def get_journal_id(code: str) -> int | None:
    global _pl_journals_cache
    if _pl_journals_cache is None:
        resp = requests.get(
            f"{PENNYLANE_BASE_URL}/journals",
            headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"},
            params={"per_page": 100},
            timeout=15,
        )
        _pl_journals_cache = resp.json().get("items", []) if resp.status_code == 200 else []
    for j in _pl_journals_cache:
        if (j.get("code") or "").upper() == code.upper():
            return j["id"]
    log.error(f"[PennyLane] Journal introuvable: {code}")
    return None


def get_customer_account(customer_id: int) -> dict | None:
    if customer_id in _pl_customers_cache:
        return _pl_customers_cache[customer_id]
    resp = requests.get(
        f"{PENNYLANE_BASE_URL}/customers/{customer_id}",
        headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"},
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    customer       = resp.json().get("customer") or resp.json()
    ledger_account = customer.get("ledger_account") or {}
    result = {
        "name":           customer.get("name", "Client inconnu"),
        "account_number": ledger_account.get("number"),
        "account_id":     ledger_account.get("id"),
    }
    _pl_customers_cache[customer_id] = result
    return result


def create_ledger_entry(date: str, label: str, lines: list, test_mode: bool, piece: str = None) -> bool:
    journal_id = get_journal_id(JOURNAL_CODE)
    if not journal_id:
        raise Exception(f"Journal {JOURNAL_CODE} introuvable")

    resolved_lines = []
    for line in lines:
        account_id = line.get("account_id")
        if not account_id and line.get("account_number"):
            account_id = get_account_id(line["account_number"])
        if not account_id:
            raise Exception(f"Compte introuvable: {line.get('account_number')}")
        resolved_lines.append({
            "ledger_account_id": account_id,
            "debit":  f"{float(line.get('debit', 0)):.2f}",
            "credit": f"{float(line.get('credit', 0)):.2f}",
            "label":  line.get("label", label),
        })

    total_d = sum(float(l["debit"]) for l in resolved_lines)
    total_c = sum(float(l["credit"]) for l in resolved_lines)
    if abs(total_d - total_c) > 0.01:
        raise Exception(f"Écriture déséquilibrée: D={total_d:.2f} C={total_c:.2f}")

    if test_mode:
        log.info(f"🧪 TEST — {label}")
        for l in resolved_lines:
            log.info(f"   D:{l['debit']:>10}  C:{l['credit']:>10}  {l['label']}")
        return True

    payload = {
        "date":               date,
        "label":              f"{piece} | {label}" if piece else label,
        "journal_id":         journal_id,
        "ledger_entry_lines": resolved_lines,
    }
    resp = requests.post(
        f"{PENNYLANE_BASE_URL}/ledger_entries",
        headers={"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if resp.status_code in (200, 201):
        log.info(f"✅ Écriture créée : {label}")
        return True
    log.error(f"❌ Erreur écriture : {resp.status_code} {resp.text}")
    return False

# =============================================================
# TRAITEMENT D'UN PAIEMENT
# =============================================================
def resolve_payment(payment: dict, store: dict, fee_ratio: float = 0.0) -> dict:
    mollie_id    = payment.get("id", "")
    description  = payment.get("description", "")
    amount       = float((payment.get("amount") or {}).get("value", "0"))
    frais        = round(amount * fee_ratio, 2)
    payment_date = (payment.get("createdAt") or "")[:10]

    if is_already_processed(mollie_id):
        return {"status": "skipped", "mollie_id": mollie_id}

    # Résolution via DB
    metadata           = payment.get("metadata") or {}
    shopify_payment_id = metadata.get("shopify_payment_id")
    if not shopify_payment_id:
        log.warning(f"  Pas de shopify_payment_id dans metadata pour {mollie_id}")
        save_result(mollie_id, store["name"], payment_ref=description, amount=amount, status="error_no_payment_id")
        return {"status": "error", "mollie_id": mollie_id}

    shopify_order = lookup_order_by_payment_id(shopify_payment_id)
    if not shopify_order:
        log.warning(f"  Aucune commande pour shopify_payment_id={shopify_payment_id}")
        save_result(mollie_id, store["name"], payment_ref=description, amount=amount, status="error_no_order")
        return {"status": "error", "mollie_id": mollie_id}

    order_name   = shopify_order["order_name"]
    billing_name = shopify_order["billing_name"]
    resolved_store_name = shopify_order.get("store_name") or store["name"]

    invoice = find_invoice_by_order_name(order_name)
    if not invoice:
        save_result(mollie_id, store["name"], payment_ref=description, order_name=order_name,
                    amount=amount, status="error_no_invoice")
        return {"status": "error", "mollie_id": mollie_id}

    invoice_number = invoice["invoice_number"]
    customer_id    = invoice["customer_id"]
    customer_name  = invoice["customer_name"] or billing_name

    client_account_number = COMPTE_CLIENT_FALLBACK
    client_account_id     = None
    if customer_id:
        customer_info = get_customer_account(customer_id)
        if customer_info and customer_info.get("account_number"):
            client_account_number = customer_info["account_number"]
            client_account_id     = customer_info.get("account_id")

    return {
        "status":               "ok",
        "mollie_id":            mollie_id,
        "description":          description,
        "order_name":           order_name,
        "invoice_number":       invoice_number,
        "customer_name":        customer_name,
        "customer_id":          customer_id,
        "client_account_number": client_account_number,
        "client_account_id":    client_account_id,
        "amount":               amount,
        "frais":                frais,
        "payment_date":         payment_date,
        "store_name":           resolved_store_name,
    }

# =============================================================
# TRAITEMENT D'UN SETTLEMENT
# =============================================================
def process_settlement(settlement: dict, payments: list, store: dict,
                        fee_ratio: float = 0.0, test_mode: bool = False) -> tuple:
    settlement_net  = float((settlement.get("amount") or {}).get("value", "0"))
    settlement_date = (settlement.get("settledAt") or "")[:10]

    resolved  = [resolve_payment(p, store, fee_ratio=fee_ratio) for p in payments]
    ok_items  = [r for r in resolved if r["status"] == "ok"]
    err_items = [r for r in resolved if r["status"] == "error"]

    if not ok_items and not err_items:
        return "skipped", 0, 0
    if not ok_items:
        return "error", 0, len(err_items)

    lines  = []
    pieces = []
    total_brut_ok  = 0.0
    total_frais_ok = 0.0
    for r in ok_items:
        libelle = f"{r['customer_name']} - {r['order_name']}"
        lines.append({
            "account_number": r["client_account_number"],
            "account_id":     r["client_account_id"],
            "debit":  0,
            "credit": r["amount"],
            "label":  libelle,
        })
        total_brut_ok += r["amount"]
        if r["frais"] > 0.001:
            lines.append({
                "account_number": COMPTE_FRAIS,
                "debit":  r["frais"],
                "credit": 0,
                "label":  f"Frais Mollie - {r['order_name']}",
            })
            total_frais_ok += r["frais"]
        pieces.append(f"{JOURNAL_CODE}-{r['invoice_number']}")

    # Use net from successful payments only (not full settlement_net)
    # to keep the entry balanced when some payments are unresolved
    net_ok = round(total_brut_ok - total_frais_ok, 2)
    lines.append({
        "account_number": COMPTE_MOLLIE,
        "debit":  net_ok,
        "credit": 0,
        "label":  f"Virement Mollie {settlement['id']}",
    })

    piece = pieces[0] if len(pieces) == 1 else f"{JOURNAL_CODE}-{settlement['id']}"
    label = " | ".join(f"{r['customer_name']} - {r['order_name']}" for r in ok_items)[:200]

    try:
        ok = create_ledger_entry(settlement_date, label, lines, test_mode, piece=piece)
    except Exception as e:
        log.error(f"  Erreur création écriture settlement {settlement['id']}: {e}")
        for r in ok_items:
            save_result(r["mollie_id"], r["store_name"], payment_ref=r["description"],
                        order_name=r["order_name"], invoice_number=r["invoice_number"],
                        amount=r["amount"], status="error_pennylane")
        return "error", 0, len(ok_items) + len(err_items)

    if ok and not test_mode:
        for r in ok_items:
            save_result(r["mollie_id"], r["store_name"], payment_ref=r["description"],
                        order_name=r["order_name"], invoice_number=r["invoice_number"],
                        amount=r["amount"], status="success")

    return "success", len(ok_items), len(err_items)

# =============================================================
# BOUCLE PRINCIPALE
# =============================================================
def run(target_date: str, test_mode: bool):
    log.info(f"\n{'#'*60}")
    log.info(f"🚀 Mollie → Pennylane | {target_date} | Test: {test_mode}")
    log.info(f"{'#'*60}")

    init_db()

    if not MOLLIE_OAUTH_TOKEN:
        log.error("MOLLIE_OAUTH_TOKEN manquant")
        return

    settlements = get_settlements_for_date(target_date)

    if not settlements:
        log.info(f"ℹ️ Mollie → Pennylane | {target_date} — Aucun settlement à traiter")
        return

    total_ok  = 0
    total_err = 0

    for settlement in settlements:
        log.info(f"\nSettlement {settlement['id']} | {(settlement.get('amount') or {}).get('value')}€")
        try:
            payments = get_settlement_payments(settlement["id"])
        except Exception as e:
            log.error(f"Erreur récupération paiements: {e}")
            total_err += 1
            continue

        total_brut     = sum(float((p.get("amount") or {}).get("value", "0")) for p in payments)
        settlement_net = float((settlement.get("amount") or {}).get("value", "0"))
        fee_ratio      = (total_brut - settlement_net) / total_brut if total_brut > 0 else 0.0

        # Détermine la boutique — résolution effective dans resolve_payment via shopify_orders
        store = STORES[0]  # default, overridden per-payment by resolved_store_name

        result, n_ok, n_err = process_settlement(settlement, payments, store,
                                                  fee_ratio=fee_ratio, test_mode=test_mode)
        total_ok  += n_ok
        total_err += n_err

    if total_err > 0:
        status = "⚠️" if total_ok > 0 else "🚨"
        telegram_send(f"{status} Mollie → Pennylane | {target_date}\n✅ {total_ok} écriture(s)\n❌ {total_err} erreur(s)")
    log.info(f"\n🏁 {target_date} : {total_ok} OK / {total_err} erreurs")

# =============================================================
# POINT D'ENTRÉE
# =============================================================
def date_range(start: str, end: str) -> list[str]:
    """Génère la liste des dates YYYY-MM-DD de start à end inclus."""
    d = datetime.strptime(start, "%Y-%m-%d")
    d_end = datetime.strptime(end, "%Y-%m-%d")
    dates = []
    while d <= d_end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


def main():
    parser = argparse.ArgumentParser(description="Mollie → Pennylane")
    parser.add_argument("--date", help="Date cible YYYY-MM-DD (défaut: hier)")
    parser.add_argument("--from", dest="from_date", help="Date début rattrapage YYYY-MM-DD (jusqu'à hier)")
    parser.add_argument("--clear", help="Supprime les settlements traités depuis cette date (YYYY-MM-DD) pour retraitement")
    parser.add_argument("--test", action="store_true", help="Mode test (simulation)")
    parser.add_argument("--cron", action="store_true", help="Mode cron (hier, production)")
    args = parser.parse_args()

    if args.clear:
        init_db()
        clear_processed_since(args.clear)
        if not args.date and not args.from_date and not args.cron:
            return

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    if args.cron:
        test_mode = False
        dates = [yesterday]
    elif args.from_date:
        test_mode = args.test
        end = args.date or yesterday
        dates = date_range(args.from_date, end)
        log.info(f"📅 Rattrapage : {len(dates)} jour(s) du {dates[0]} au {dates[-1]}")
    else:
        test_mode = args.test
        dates = [args.date or yesterday]

    for target_date in dates:
        run(target_date, test_mode)


if __name__ == "__main__":
    main()

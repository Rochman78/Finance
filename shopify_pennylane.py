#!/usr/bin/env python3
"""
=============================================================
SHOPIFY PAYMENTS → PENNYLANE — Écritures comptables
=============================================================
Lit les données depuis la base PostgreSQL (alimentée par db_loader.py).
Ne fait aucun appel à l'API Pennylane pour charger factures/clients.

Usage :
  python shopify_pennylane.py --cron          # Versements de la veille
  python shopify_pennylane.py --date 2026-03-12
  python shopify_pennylane.py --test --date 2026-03-12
"""

import os
import sys
import json
import time
import re
import logging
import argparse
import requests
import psycopg2
from datetime import datetime, timedelta, timezone

# =============================================================
# CONFIGURATION
# =============================================================
SHOPIFY_API_VERSION = "2026-01"
STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  "")},
    {"name": "MTC",  "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  "")},
    {"name": "MO",   "store": "mon-ombrage.myshopify.com",             "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO",   "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
    {"name": "TZ",   "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   "")},
    {"name": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",   "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  "")},
    {"name": "UNIV", "store": "univers-camouflage.myshopify.com",      "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", "")},
]

# --- PENNYLANE ---
PENNYLANE_TOKEN   = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE           = "https://app.pennylane.com/api/external/v2"
PL_HEADERS        = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

# --- COMPTES COMPTABLES ---
COMPTE_TRESORERIE = "411INTERNET"
COMPTE_FRAIS      = "627001"
COMPTE_ECART      = "471"
COMPTE_LITIGES    = "467505"
JOURNAL_CODE      = "ENCSP"

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
log = logging.getLogger("shopify_pennylane")

# =============================================================
# TELEGRAM
# =============================================================
def telegram_send(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram error: {e}")

# =============================================================
# BASE DE DONNÉES — Lecture uniquement
# =============================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def load_invoices() -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT order_number, customer_name, customer_id, invoice_id, invoice_number, amount FROM invoices")
            rows = cur.fetchall()
        index = {r[0]: {"customer_name": r[1], "customer_id": r[2],
                        "invoice_id": r[3], "invoice_number": r[4], "amount": r[5]}
                 for r in rows}
        log.info(f"📋 {len(index)} facture(s) chargée(s)")
        return index
    finally:
        conn.close()


def load_customers() -> dict:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, ledger_account_id FROM customers")
            rows = cur.fetchall()
        customers = {r[0]: {"name": r[1], "ledger_account_id": r[2]} for r in rows}
        log.info(f"👥 {len(customers)} client(s) chargé(s)")
        return customers
    finally:
        conn.close()


def is_payout_processed(store_name: str, payout_id: str) -> bool:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_payouts WHERE store_name=%s AND payout_id=%s",
                        (store_name, str(payout_id)))
            return cur.fetchone() is not None
    finally:
        conn.close()


def mark_payout_processed(store_name: str, payout_id: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO processed_payouts (store_name, payout_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (store_name, str(payout_id)))
        conn.commit()
    finally:
        conn.close()


def init_processed_payouts():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_payouts (
                    store_name TEXT NOT NULL,
                    payout_id  TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (store_name, payout_id)
                )
            """)
        conn.commit()
    finally:
        conn.close()

# =============================================================
# SHOPIFY API
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


def get_payouts(shopify_token: str, store: str, date_min: str, date_max: str = None) -> list:
    url  = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/payouts.json"
    params = {"date_min": date_min}
    if date_max:
        params["date_max"] = date_max
    data = shopify_get(url, shopify_token, params)
    return data.get("payouts", []) if data else []


def get_payout_transactions(shopify_token: str, store: str, payout_id: str) -> list:
    url    = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance/transactions.json"
    txns   = []
    params = {"payout_id": payout_id, "limit": 250}
    while True:
        data = shopify_get(url, shopify_token, params)
        if not data:
            break
        batch = data.get("transactions", [])
        txns.extend(batch)
        if len(batch) < 250:
            break
        params["since_id"] = batch[-1]["id"]
    return txns


def get_order(shopify_token: str, store: str, order_id: str) -> dict | None:
    url  = f"https://{store}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}.json"
    data = shopify_get(url, shopify_token, {"fields": "id,name,order_number"})
    return data.get("order") if data else None

# =============================================================
# PENNYLANE API
# =============================================================
_accounts_cache = {}
_journal_cache  = {}


def get_account_id(account_number: str) -> int | None:
    if account_number in _accounts_cache:
        return _accounts_cache[account_number]
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
    resp = requests.get(f"{PL_BASE}/ledger_accounts", headers=PL_HEADERS,
                        params={"filter": filter_param, "limit": 5}, timeout=30)
    if resp.status_code == 200:
        items = resp.json().get("items", [])
        if items:
            _accounts_cache[account_number] = items[0]["id"]
            return items[0]["id"]
    log.warning(f"⚠️  Compte '{account_number}' introuvable")
    return None


def get_journal_id(code: str) -> int | None:
    if code in _journal_cache:
        return _journal_cache[code]
    resp = requests.get(f"{PL_BASE}/journals", headers=PL_HEADERS, params={"limit": 100}, timeout=30)
    if resp.status_code == 200:
        for j in resp.json().get("items", []):
            if j.get("code", "").upper() == code.upper():
                _journal_cache[code] = j["id"]
                return j["id"]
    log.error(f"❌ Journal '{code}' introuvable")
    return None


def ledger_entry_exists(date: str, label: str, journal_id: int) -> bool | None:
    """Vérifie si une écriture avec le même label et date existe déjà dans Pennylane.
    Retourne True si doublon trouvé, False si aucun doublon, None si l'API est injoignable.
    Pagine avec cursor pour ne rater aucune écriture."""
    filter_param = json.dumps([
        {"field": "date", "operator": "eq", "value": date},
        {"field": "journal_id", "operator": "eq", "value": journal_id},
    ])
    cursor = None
    while True:
        params = {"filter": filter_param, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(3):
            resp = requests.get(f"{PL_BASE}/ledger_entries", headers=PL_HEADERS,
                                params=params, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 10))
                continue
            log.error(f"❌ Vérification anti-doublon échouée : {resp.status_code} {resp.text[:200]}")
            return None
        else:
            log.error("❌ Vérification anti-doublon échouée après 3 tentatives (rate limit)")
            return None
        data = resp.json()
        for entry in data.get("items", []):
            if entry.get("label") == label:
                return True
        if not data.get("has_more"):
            return False
        cursor = data.get("next_cursor")
        if not cursor:
            return False


def create_ledger_entry(date: str, label: str, journal_id: int, lines: list, test_mode: bool) -> bool:
    if test_mode:
        log.info(f"🧪 TEST — {label}")
        for l in lines:
            log.info(f"   {l.get('label',''):<50} D:{float(l.get('debit','0')):>10.2f}  C:{float(l.get('credit','0')):>10.2f}")
        return True
    # Anti-doublon : vérifier si l'écriture existe déjà
    check = ledger_entry_exists(date, label, journal_id)
    if check is None:
        log.error(f"🚫 BLOQUÉ — impossible de vérifier les doublons, écriture NON créée : {label}")
        return False
    if check:
        log.warning(f"⚠️  Écriture déjà existante, skip : {label}")
        return True
    payload = {"date": date, "label": label, "journal_id": journal_id, "ledger_entry_lines": lines}
    resp = requests.post(f"{PL_BASE}/ledger_entries", headers=PL_HEADERS, json=payload, timeout=30)
    if resp.status_code in (200, 201):
        log.info(f"✅ Écriture créée : {label}")
        return True
    log.error(f"❌ Erreur création écriture : {resp.status_code} {resp.text}")
    return False

# =============================================================
# TRAITEMENT D'UN VERSEMENT
# =============================================================
def process_payout(shopify_token: str, store_config: dict, payout: dict,
                   invoice_index: dict, customers: dict,
                   journal_id: int, tresorerie_id: int, frais_id: int,
                   ecart_id: int, litiges_id: int, test_mode: bool) -> dict:
    store_name = store_config["name"]
    store_url  = store_config["store"]
    payout_id  = payout["id"]
    payout_date = payout["date"]
    payout_amount = float(payout["amount"])

    log.info(f"\n{'='*60}")
    log.info(f"💰 [{store_name}] Payout {payout_id} | {payout_date} | {payout_amount}€")

    transactions = get_payout_transactions(shopify_token, store_url, payout_id)
    if not transactions:
        return {"success": False, "error": "Aucune transaction"}

    lines        = []
    total_gross  = 0.0
    total_fees   = 0.0
    client_lines = []
    skipped_details = []

    for txn in transactions:
        source_type     = txn.get("source_type", "")
        source_order_id = txn.get("source_order_id")
        amount          = float(txn.get("amount", "0"))
        fee             = float(txn.get("fee", "0"))

        # Normalize source_type: Shopify may return "Payments::Refund" instead of "refund"
        norm_type = source_type.rsplit("::", 1)[-1].lower() if source_type else ""
        txn_type = txn.get("type", "").lower()

        # Handle debits/adjustments without order (Shopify fees, chargebacks, etc.)
        if txn_type in ("debit", "adjustment") and not source_order_id:
            debit_amount = abs(float(txn.get("amount", "0")))
            if debit_amount > 0:
                total_gross -= debit_amount
                log.info(f"   📌 {txn_type.upper()} sans commande | -{debit_amount}€ → frais Shopify")
                lines.append({
                    "ledger_account_id": frais_id,
                    "debit":  f"{debit_amount:.2f}",
                    "credit": "0.00",
                    "label":  f"Frais Shopify {txn_type} {payout_date}",
                })
            continue

        if norm_type not in ("charge", "refund", "dispute") or not source_order_id:
            # Silently skip internal transaction types (payout, etc.)
            continue

        order = get_order(shopify_token, store_url, source_order_id)
        if not order:
            skipped_details.append(f"order_id={source_order_id} introuvable Shopify")
            continue

        raw_name   = re.sub(r'[#\-]', '', order.get("name", ""))
        # Normalise : garder uniquement préfixe lettres + chiffres (ignorer suffixe comme "R")
        m_name = re.match(r'([A-Za-z]{0,5}\d+)', raw_name)
        order_name = m_name.group(1).upper() if m_name else raw_name.upper()
        invoice_info = invoice_index.get(order_name)
        if not invoice_info:
            log.warning(f"   ⚠️  Commande {order_name} non trouvée dans les factures")
            skipped_details.append(f"{order_name} pas de facture")
            continue

        customer_id   = invoice_info.get("customer_id")
        customer_name = invoice_info.get("customer_name", "Inconnu")
        customer      = customers.get(customer_id) if customer_id else None
        if customer and customer.get("name"):
            customer_name = customer["name"]

        ledger_account_id = customer.get("ledger_account_id") if customer else None
        if not ledger_account_id:
            log.warning(f"   ⚠️  Pas de compte auxiliaire pour {customer_name}")
            skipped_details.append(f"{order_name} pas de compte aux. ({customer_name})")
            continue

        fee_abs = abs(fee)

        if norm_type == "charge":
            total_gross += amount
            total_fees  += fee_abs

            log.info(f"   ✅ {order_name} → {customer_name} | {amount}€ (frais: {fee_abs}€)")
            client_lines.append(f"{customer_name} — {amount:.2f}€")

            lines.append({
                "ledger_account_id": ledger_account_id,
                "debit":  "0.00",
                "credit": f"{amount:.2f}",
                "label":  f"{customer_name} - {order_name}",
            })
            if fee_abs > 0:
                lines.append({
                    "ledger_account_id": frais_id,
                    "debit":  f"{fee_abs:.2f}",
                    "credit": "0.00",
                    "label":  f"{customer_name} - {order_name}",
                })

        elif norm_type == "refund":
            refund_amount = abs(amount)
            total_gross -= refund_amount
            total_fees  -= fee_abs  # remboursement des frais

            log.info(f"   🔄 {order_name} → {customer_name} | -{refund_amount}€ (remb. frais: {fee_abs}€)")
            client_lines.append(f"{customer_name} — -{refund_amount:.2f}€ (remboursement)")

            lines.append({
                "ledger_account_id": ledger_account_id,
                "debit":  f"{refund_amount:.2f}",
                "credit": "0.00",
                "label":  f"Remboursement {customer_name} - {order_name}",
            })
            if fee_abs > 0:
                lines.append({
                    "ledger_account_id": frais_id,
                    "debit":  "0.00",
                    "credit": f"{fee_abs:.2f}",
                    "label":  f"Remb. frais {customer_name} - {order_name}",
                })

        elif norm_type == "dispute":
            # Disputes use 467505 (litiges) as transit account
            dispute_amount = amount  # negative = chargeback, positive = won
            total_gross += dispute_amount
            total_fees  += fee_abs

            if dispute_amount < 0:
                # Chargeback: money leaves → park in 467505
                abs_dispute = abs(dispute_amount)
                log.info(f"   ⚠️ DISPUTE {order_name} → {customer_name} | -{abs_dispute}€ → 467505")
                client_lines.append(f"{customer_name} — -{abs_dispute:.2f}€ (litige)")

                lines.append({
                    "ledger_account_id": litiges_id,
                    "debit":  f"{abs_dispute:.2f}",
                    "credit": "0.00",
                    "label":  f"Litige {customer_name} - {order_name}",
                })
                if fee_abs > 0:
                    lines.append({
                        "ledger_account_id": frais_id,
                        "debit":  f"{fee_abs:.2f}",
                        "credit": "0.00",
                        "label":  f"Frais litige {customer_name} - {order_name}",
                    })
            else:
                # Won dispute: money returns → close 467505
                log.info(f"   ✅ DISPUTE gagné {order_name} → {customer_name} | +{dispute_amount}€ → 467505 soldé")
                client_lines.append(f"{customer_name} — +{dispute_amount:.2f}€ (litige gagné)")

                lines.append({
                    "ledger_account_id": litiges_id,
                    "debit":  "0.00",
                    "credit": f"{dispute_amount:.2f}",
                    "label":  f"Litige gagné {customer_name} - {order_name}",
                })

    if not lines and payout_amount < 0:
        # Versement négatif sans charge/refund (ajustement, réserve, retrait…)
        # → écriture simple : crédit trésorerie + débit compte de frais
        abs_amount = abs(payout_amount)
        log.info(f"   🔄 Versement négatif {payout_amount:.2f}€ → écriture d'ajustement")
        all_lines = [
            {
                "ledger_account_id": tresorerie_id,
                "debit":  "0.00",
                "credit": f"{abs_amount:.2f}",
                "label":  f"Retrait Shopify {payout_date}",
            },
            {
                "ledger_account_id": frais_id,
                "debit":  f"{abs_amount:.2f}",
                "credit": "0.00",
                "label":  f"Ajustement Shopify {payout_date}",
            },
        ]
        label  = f"Versement Shopify Payments {payout_date} [{store_name}] {payout_amount:.2f}€"
        result = create_ledger_entry(payout_date, label, journal_id, all_lines, test_mode)
        if result:
            return {"success": True, "nb_clients": 0, "details": [f"Ajustement {payout_amount:.2f}€"]}
        return {"success": False, "error": "Échec création Pennylane"}

    if not lines:
        detail_str = "; ".join(skipped_details) if skipped_details else "raison inconnue"
        log.warning(f"   ⚠️  Aucune ligne générée — {len(transactions)} txn, {len(skipped_details)} ignorée(s): {detail_str}")
        return {"success": False, "error": f"Aucune ligne générée ({len(transactions)} txn, {len(skipped_details)} ignorées: {detail_str})"}

    # Use actual payout amount for the treasury line to match the real bank transfer
    # Adjust for any unmatched difference (skipped orders, adjustments, disputes)
    matched_net = round(total_gross - total_fees, 2)
    ecart = round(payout_amount - matched_net, 2)

    # Versement positif → débit trésorerie ; versement négatif → crédit trésorerie
    if payout_amount >= 0:
        tresorerie_line = {
            "ledger_account_id": tresorerie_id,
            "debit":  f"{payout_amount:.2f}",
            "credit": "0.00",
            "label":  f"Versement {COMPTE_TRESORERIE} {payout_date}",
        }
    else:
        tresorerie_line = {
            "ledger_account_id": tresorerie_id,
            "debit":  "0.00",
            "credit": f"{abs(payout_amount):.2f}",
            "label":  f"Retrait {COMPTE_TRESORERIE} {payout_date}",
        }

    all_lines = [tresorerie_line] + lines

    # If there's a difference due to unmatched orders, add a balancing line on fees account
    if abs(ecart) > 0.01:
        log.warning(f"   ⚠️  Écart {ecart:.2f}€ entre payout ({payout_amount:.2f}€) et commandes matchées ({matched_net:.2f}€)")
        all_lines.append({
            "ledger_account_id": ecart_id,
            "debit":  f"{abs(ecart):.2f}" if ecart < 0 else "0.00",
            "credit": f"{ecart:.2f}" if ecart > 0 else "0.00",
            "label":  f"Écart versement Shopify {payout_date}",
        })
        telegram_send(
            f"⚠️ <b>Écart Shopify [{store_name}]</b>\n"
            f"Payout {payout_date} : {ecart:+.2f}€\n"
            f"Payout: {payout_amount:.2f}€ | Matché: {matched_net:.2f}€\n"
            f"→ Compte 471 (attente)"
        )

    total_d = sum(float(l["debit"]) for l in all_lines)
    total_c = sum(float(l["credit"]) for l in all_lines)
    if abs(total_d - total_c) > 0.01:
        log.error(f"❌ Écriture déséquilibrée D:{total_d:.2f} C:{total_c:.2f}")
        return {"success": False, "error": f"Déséquilibre {abs(total_d-total_c):.2f}€"}

    label  = f"Versement Shopify Payments {payout_date} [{store_name}] {payout_amount:.2f}€"
    result = create_ledger_entry(payout_date, label, journal_id, all_lines, test_mode)
    if result:
        return {"success": True, "nb_clients": len(client_lines), "details": client_lines}
    return {"success": False, "error": "Échec création Pennylane"}

# =============================================================
# BOUCLE PRINCIPALE
# =============================================================
def run(target_date: str, test_mode: bool, store_filter: str | None = None, force: bool = False, date_max: str = None):
    date_label = f"{target_date} → {date_max}" if date_max else f"{target_date} → ∞"
    log.info(f"\n{'#'*60}")
    log.info(f"🚀 Shopify → Pennylane | {date_label} | Test: {test_mode}")
    log.info(f"{'#'*60}")

    init_processed_payouts()

    # Chargement DB (une seule fois)
    invoice_index = load_invoices()
    customers     = load_customers()

    if not invoice_index:
        log.error("❌ La table invoices est VIDE ! Exécutez d'abord : python db_loader.py --full")
        telegram_send("🚨 <b>Shopify → Pennylane</b>\n❌ Table invoices vide — lancez db_loader.py --full")
        return

    # Comptes Pennylane
    tresorerie_id = get_account_id(COMPTE_TRESORERIE)
    frais_id      = get_account_id(COMPTE_FRAIS)
    ecart_id      = get_account_id(COMPTE_ECART)
    litiges_id    = get_account_id(COMPTE_LITIGES)
    journal_id    = get_journal_id(JOURNAL_CODE)
    if not tresorerie_id or not frais_id or not ecart_id or not litiges_id or not journal_id:
        telegram_send("🚨 <b>Shopify → Pennylane</b>\n❌ Compte ou journal introuvable")
        return

    stores = STORES
    if store_filter:
        stores = [s for s in STORES if s["name"] == store_filter]

    total_ok  = 0
    total_err = 0
    messages  = []
    any_payout = False

    for store_config in stores:
        if not store_config.get("client_secret"):
            continue

        sname = store_config["name"]
        token = get_shopify_token(store_config)
        if not token:
            continue

        payouts = get_payouts(token, store_config["store"], target_date, date_max)
        if force:
            new_payouts = payouts
        else:
            new_payouts = [p for p in payouts if not is_payout_processed(sname, p["id"])]

        if not new_payouts:
            log.info(f"[{sname}] Aucun nouveau versement")
            continue

        any_payout = True
        log.info(f"[{sname}] {len(new_payouts)} versement(s) à traiter")

        for payout in new_payouts:
            result = process_payout(
                token, store_config, payout,
                invoice_index, customers,
                journal_id, tresorerie_id, frais_id,
                ecart_id, litiges_id, test_mode
            )
            if result and result.get("success"):
                if not test_mode:
                    mark_payout_processed(sname, payout["id"])
                total_ok += 1
                messages.append(f"✅ [{sname}] {payout['date']} — {float(payout['amount']):.2f}€")
            else:
                total_err += 1
                messages.append(f"❌ [{sname}] {result.get('error', '?')}")

    if any_payout and total_err > 0:
        status  = "⚠️" if total_ok > 0 else "🚨"
        tg_msg  = (
            f"{status} <b>Shopify → Pennylane</b> | {date_label}\n"
            f"✅ {total_ok} versement(s)\n❌ {total_err} erreur(s)\n\n"
            + "\n".join(messages[:20])
        )
        telegram_send(tg_msg)

    log.info(f"\n🏁 {date_label} : {total_ok} OK / {total_err} erreurs")

# =============================================================
# POINT D'ENTRÉE
# =============================================================
def main():
    parser = argparse.ArgumentParser(description="Shopify Payments → Pennylane")
    parser.add_argument("--date",  help="Date cible YYYY-MM-DD (défaut: hier)")
    parser.add_argument("--from",  dest="from_date", help="Date début rattrapage YYYY-MM-DD (jusqu'à hier)")
    parser.add_argument("--test",  action="store_true", help="Mode test (simulation)")
    parser.add_argument("--cron",  action="store_true", help="Mode cron (hier, production)")
    parser.add_argument("--store", help="Filtrer une boutique (ex: LFC)")
    parser.add_argument("--force", action="store_true", help="Ignorer processed_payouts et retraiter")
    args = parser.parse_args()

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    if args.cron:
        test_mode = False
        # Récupère tous les payouts depuis J-3, sans date max :
        # inclut les payouts programmés (scheduled) et déposés (paid/in_transit)
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        date_min = three_days_ago
        date_max = None
    elif args.from_date:
        test_mode = args.test
        date_min = args.from_date
        date_max = args.date or yesterday
        log.info(f"📅 Rattrapage : {date_min} → {date_max}")
    else:
        test_mode = args.test
        target = args.date or yesterday
        date_min = target
        date_max = target

    force = getattr(args, 'force', False)
    if force:
        log.info("⚡ Mode FORCE — les versements déjà traités seront reprocessés")

    run(date_min, test_mode, store_filter=args.store, force=force, date_max=date_max)


if __name__ == "__main__":
    main()

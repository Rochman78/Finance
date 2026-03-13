#!/usr/bin/env python3
"""
=============================================================
DB LOADER — Chargement centralisé des données dans PostgreSQL
=============================================================
Source unique de vérité pour alimenter la base partagée.
Les scripts shopify/klarna/mollie_pennylane.py ne font que LIRE.

Tables alimentées :
  - invoices       ← Pennylane API (customer_invoices)
  - customers      ← Pennylane API (customers)
  - shopify_orders ← Shopify API   (pour le match Mollie)

Usage :
  python db_loader.py --full    # Tout depuis 01/01/2026 (premier run)
  python db_loader.py --cron    # Delta de la veille uniquement
  python db_loader.py --full --skip-shopify   # Factures/clients seulement
"""

import os
import sys
import re
import json
import time
import logging
import argparse
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timedelta, timezone

# =============================================================
# CONFIGURATION
# =============================================================

# --- PENNYLANE ---
PENNYLANE_TOKEN  = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE          = "https://app.pennylane.com/api/external/v2"
PL_HEADERS       = {
    "Authorization": f"Bearer {PENNYLANE_TOKEN}",
    "Content-Type":  "application/json",
}
FULL_LOAD_FROM   = "2026-01-01"   # Date de départ pour --full

# --- SHOPIFY (7 boutiques, pour la table shopify_orders / Mollie) ---
SHOPIFY_API_VERSION = "2026-01"
SHOPIFY_STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  "")},
    {"name": "TAR",  "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  "")},
    {"name": "COCO", "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  "")},
    {"name": "LOV",  "store": "le-filet-camouflage-1.myshopify.com",   "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
]

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
log = logging.getLogger("db_loader")

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
# BASE DE DONNÉES
# =============================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def init_db():
    """Crée toutes les tables nécessaires si elles n'existent pas."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                -- Factures Pennylane
                CREATE TABLE IF NOT EXISTS invoices (
                    order_number   TEXT PRIMARY KEY,
                    customer_name  TEXT,
                    customer_id    BIGINT,
                    invoice_id     BIGINT,
                    invoice_number TEXT,
                    amount         TEXT
                );

                -- Clients Pennylane
                CREATE TABLE IF NOT EXISTS customers (
                    id                 BIGINT PRIMARY KEY,
                    name               TEXT,
                    ledger_account_id  BIGINT
                );

                -- Commandes Shopify (pour match Mollie)
                CREATE TABLE IF NOT EXISTS shopify_orders (
                    payment_id   TEXT PRIMARY KEY,
                    order_name   TEXT NOT NULL,
                    store_name   TEXT NOT NULL,
                    billing_name TEXT,
                    created_at   TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_shopify_orders_store ON shopify_orders(store_name);
                CREATE INDEX IF NOT EXISTS idx_shopify_orders_name  ON shopify_orders(order_name);

                -- Métadonnées de sync
                CREATE TABLE IF NOT EXISTS db_loader_meta (
                    key        TEXT PRIMARY KEY,
                    value      TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
        log.info("✅ Tables DB initialisées")
    finally:
        conn.close()


def get_meta(key: str) -> str | None:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM db_loader_meta WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def set_meta(key: str, value: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO db_loader_meta (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, (key, value))
        conn.commit()
    finally:
        conn.close()


def upsert_invoices(rows: list) -> int:
    """rows = list of (order_number, customer_name, customer_id, invoice_id, invoice_number, amount)"""
    if not rows:
        return 0
    conn = get_db()
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO invoices (order_number, customer_name, customer_id, invoice_id, invoice_number, amount)
                VALUES %s
                ON CONFLICT (order_number) DO UPDATE SET
                    customer_name  = EXCLUDED.customer_name,
                    customer_id    = EXCLUDED.customer_id,
                    invoice_id     = EXCLUDED.invoice_id,
                    invoice_number = EXCLUDED.invoice_number,
                    amount         = EXCLUDED.amount
            """, rows)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def upsert_customers(rows: list) -> int:
    """rows = list of (id, name, ledger_account_id)"""
    if not rows:
        return 0
    conn = get_db()
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO customers (id, name, ledger_account_id)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    name              = EXCLUDED.name,
                    ledger_account_id = EXCLUDED.ledger_account_id
            """, rows)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def upsert_shopify_orders(rows: list) -> int:
    """rows = list of (payment_id, order_name, store_name, billing_name, created_at)"""
    if not rows:
        return 0
    conn = get_db()
    try:
        with conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO shopify_orders (payment_id, order_name, store_name, billing_name, created_at)
                VALUES %s
                ON CONFLICT (payment_id) DO NOTHING
            """, rows)
            count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()

# =============================================================
# PENNYLANE — Factures
# =============================================================
def extract_order_number(special_mention: str, label: str) -> str | None:
    """
    Extrait le numéro de commande Shopify depuis special_mention ou label.
    Formats reconnus : #LFC29289, LFC29289, RDC3634, HC3091, TZ5477, #LVO36124, etc.
    """
    for text in [special_mention, label]:
        if not text:
            continue
        # Format "Commande #LFC29289" ou "Commande LFC29289"
        match = re.search(r'(?:Commande|Order|Bestellung)\s+#?([A-Za-z]{2,5}\d{3,6})', text)
        if match:
            return match.group(1).upper()
        # Format direct "RDC3634" ou "#LFC29289" en début de ligne
        matches = re.findall(r'#?([A-Z]{2,5}\d{3,6})', text.upper())
        for m in matches:
            digits = re.search(r'\d+', m)
            if digits and len(digits.group()) >= 3:
                return m
    return None


def pl_get_all(endpoint: str, params: dict = None) -> list:
    """Pagination complète via next_cursor."""
    all_items = []
    cursor    = None
    page      = 0

    while True:
        page += 1
        p = {"per_page": 100}
        if params:
            p.update(params)
        if cursor:
            p["cursor"] = cursor

        for attempt in range(5):
            resp = requests.get(f"{PL_BASE}/{endpoint}", headers=PL_HEADERS, params=p, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                wait = min(2 ** attempt, 10)
                log.info(f"   ⏳ Rate limit Pennylane — pause {wait}s...")
                time.sleep(wait)
            else:
                log.error(f"❌ Pennylane GET {endpoint}: {resp.status_code} {resp.text}")
                return all_items
        else:
            log.error(f"❌ Pennylane GET {endpoint}: rate limit persistant")
            return all_items

        data     = resp.json()
        items    = data.get("items", [])
        all_items.extend(items)

        if page % 10 == 0:
            log.info(f"   ... {len(all_items)} éléments chargés ({endpoint}, page {page})")

        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    return all_items


def load_invoices_from_pennylane(date_from: str) -> int:
    """
    Charge les factures Pennylane depuis date_from et les insère dans la DB.
    Retourne le nombre de lignes insérées/mises à jour.
    """
    log.info(f"📋 Chargement des factures Pennylane depuis {date_from}...")
    filter_param = json.dumps([{"field": "date", "operator": "gteq", "value": date_from}])
    invoices = pl_get_all("customer_invoices", {"filter": filter_param})
    log.info(f"   → {len(invoices)} facture(s) récupérée(s)")

    rows = []
    for inv in invoices:
        special_mention = inv.get("special_mention", "") or ""
        label           = inv.get("label", "") or ""
        order_number    = extract_order_number(special_mention, label)
        if not order_number:
            continue

        customer    = inv.get("customer") or {}
        customer_id = customer.get("id")

        # Nom client depuis le label (ex: "Facture Jean Dupont - F-2026-...")
        customer_name = "Inconnu"
        m = re.match(r'(?:Facture|Avoir)\s+(.+?)\s+-\s+F-', label)
        if m:
            customer_name = m.group(1).strip()

        rows.append((
            order_number,
            customer_name,
            customer_id,
            inv.get("id"),
            inv.get("invoice_number", ""),
            str(inv.get("currency_amount", "")),
        ))

    count = upsert_invoices(rows)
    log.info(f"   ✅ {len(rows)} facture(s) avec numéro de commande ({count} modifiée(s) en DB)")
    return len(rows)


def load_customers_from_pennylane() -> int:
    """
    Charge tous les clients Pennylane avec leur ledger_account_id.
    Retourne le nombre de lignes insérées/mises à jour.
    """
    log.info("👥 Chargement des clients Pennylane...")
    customers = pl_get_all("customers")
    log.info(f"   → {len(customers)} client(s) récupéré(s)")

    rows = []
    for c in customers:
        cid            = c.get("id")
        ledger_account = c.get("ledger_account") or {}
        ledger_id      = ledger_account.get("id")
        rows.append((cid, c.get("name", ""), ledger_id))

    count = upsert_customers(rows)
    log.info(f"   ✅ {len(rows)} client(s) chargé(s) ({count} modifié(s) en DB)")
    return len(rows)

# =============================================================
# SHOPIFY — Commandes Mollie
# =============================================================
_token_cache = {}


def get_shopify_token(store: dict) -> str | None:
    cached = _token_cache.get(store["store"])
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]

    resp = requests.post(
        f"https://{store['store']}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": store["client_id"],
              "client_secret": store["client_secret"]},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if resp.status_code != 200:
        log.error(f"[Shopify {store['name']}] Auth échouée: {resp.status_code}")
        return None

    data  = resp.json()
    token = data["access_token"]
    _token_cache[store["store"]] = {
        "token":      token,
        "expires_at": time.time() + data.get("expires_in", 86399),
    }
    return token


def shopify_get(url: str, token: str, params: dict = None) -> dict | None:
    for attempt in range(5):
        resp = requests.get(
            url,
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp
        if resp.status_code == 429:
            wait = min(2 ** attempt, 16)
            log.info(f"   Rate limit Shopify — pause {wait}s...")
            time.sleep(wait)
            continue
        log.warning(f"   GET {url}: {resp.status_code}")
        return None
    return None


def load_shopify_orders_for_store(store: dict, updated_at_min: str | None = None) -> int:
    """
    Charge les commandes Mollie d'une boutique Shopify.
    Si updated_at_min est fourni → sync incrémentale, sinon tout.
    """
    name  = store["name"]
    token = get_shopify_token(store)
    if not token:
        return 0

    base_url = f"https://{store['store']}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params   = {
        "status": "any",
        "limit":  250,
        "fields": "id,name,billing_address,created_at,payment_gateway_names",
    }
    if updated_at_min:
        params["updated_at_min"] = updated_at_min

    total_inserted = 0
    next_url       = base_url
    page           = 0

    while next_url:
        page += 1
        resp = shopify_get(next_url if page > 1 else base_url,
                           token,
                           params if page == 1 else None)
        if not resp:
            break

        orders = resp.json().get("orders", [])
        if not orders:
            break

        rows = []
        for order in orders:
            gw_names  = order.get("payment_gateway_names") or []
            is_mollie = any("mollie" in g.lower() for g in gw_names)
            if not is_mollie:
                continue

            order_id     = order["id"]
            order_name   = order.get("name", "").replace("#", "")
            billing      = order.get("billing_address") or {}
            billing_name = billing.get("company") or billing.get("name") or "Client inconnu"
            created_at   = order.get("created_at", "")[:19]

            # Transactions pour le payment_id
            txn_url  = f"https://{store['store']}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/transactions.json"
            txn_resp = shopify_get(txn_url, token, {"fields": "id,payment_id,gateway,status"})
            if not txn_resp:
                continue

            for txn in txn_resp.json().get("transactions", []):
                pid = txn.get("payment_id")
                if pid:
                    rows.append((pid, order_name, name, billing_name, created_at))

        inserted = upsert_shopify_orders(rows)
        total_inserted += inserted

        # Pagination via Link header
        link   = resp.headers.get("Link", "")
        next_url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break

    log.info(f"   [{name}] {total_inserted} nouvelle(s) commande(s) Mollie insérée(s)")
    return total_inserted


def load_shopify_orders(date_from: str | None = None) -> int:
    """
    Charge les commandes Shopify pour toutes les boutiques.
    date_from : si fourni, sync incrémentale depuis cette date.
    """
    log.info(f"🛒 Chargement des commandes Shopify (Mollie) depuis {date_from or 'le début'}...")
    total = 0
    for store in SHOPIFY_STORES:
        if not store.get("client_secret"):
            log.warning(f"   [{store['name']}] Secret manquant — ignoré")
            continue
        try:
            n = load_shopify_orders_for_store(
                store,
                updated_at_min=f"{date_from}T00:00:00Z" if date_from else None
            )
            total += n
        except Exception as e:
            log.error(f"   [{store['name']}] Erreur: {e}", exc_info=True)

    log.info(f"✅ Total shopify_orders : {total} ligne(s) insérée(s)")
    return total

# =============================================================
# POINT D'ENTRÉE
# =============================================================
def main():
    parser = argparse.ArgumentParser(description="DB Loader — Pennylane + Shopify → PostgreSQL")
    parser.add_argument("--full",          action="store_true", help=f"Chargement complet depuis {FULL_LOAD_FROM}")
    parser.add_argument("--cron",          action="store_true", help="Delta de la veille uniquement")
    parser.add_argument("--skip-shopify",  action="store_true", help="Ne pas charger shopify_orders")
    parser.add_argument("--skip-pennylane",action="store_true", help="Ne pas charger invoices/customers")
    args = parser.parse_args()

    if not args.full and not args.cron:
        parser.print_help()
        sys.exit(1)

    log.info(f"\n{'#'*60}")
    log.info(f"🚀 DB Loader | mode={'FULL' if args.full else 'CRON'}")
    log.info(f"{'#'*60}")

    init_db()

    # --- Détermination de la date de départ ---
    if args.full:
        date_from = FULL_LOAD_FROM
        log.info(f"📅 Chargement complet depuis {date_from}")
    else:
        # Mode cron : uniquement la veille
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        date_from = yesterday
        log.info(f"📅 Sync incrémentale pour le {date_from}")

    total_invoices  = 0
    total_customers = 0
    total_orders    = 0

    # --- Pennylane : factures + clients ---
    if not args.skip_pennylane:
        try:
            total_invoices  = load_invoices_from_pennylane(date_from)
            total_customers = load_customers_from_pennylane()
            set_meta("last_pennylane_sync", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.error(f"❌ Erreur chargement Pennylane: {e}", exc_info=True)
            telegram_send(f"🚨 <b>DB Loader</b>\n❌ Erreur Pennylane: {e}")

    # --- Shopify : commandes Mollie ---
    if not args.skip_shopify:
        try:
            # En mode cron on passe la date, en full on ne filtre pas
            shopify_date = date_from if args.cron else None
            total_orders = load_shopify_orders(date_from=shopify_date)
            set_meta("last_shopify_sync", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.error(f"❌ Erreur chargement Shopify: {e}", exc_info=True)
            telegram_send(f"🚨 <b>DB Loader</b>\n❌ Erreur Shopify: {e}")

    # --- Notification Telegram ---
    mode_label = "Chargement complet" if args.full else f"Sync {date_from}"
    tg_msg = (
        f"✅ <b>DB Loader</b> — {mode_label}\n"
        f"📋 {total_invoices} facture(s)\n"
        f"👥 {total_customers} client(s)\n"
        f"🛒 {total_orders} commande(s) Shopify/Mollie"
    )
    telegram_send(tg_msg)
    log.info(f"\n🏁 DB Loader terminé — {total_invoices} factures | {total_customers} clients | {total_orders} commandes")


if __name__ == "__main__":
    main()

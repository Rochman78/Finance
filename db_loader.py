#!/usr/bin/env python3
"""
=============================================================
DB LOADER — Chargement des données Pennylane dans PostgreSQL
=============================================================
Alimente les tables invoices + customers depuis l'API Pennylane.
La table shopify_orders est gérée par sync_shopify_orders.py.

Usage :
  python db_loader.py --full    # Tout depuis 01/01/2026 (premier run)
  python db_loader.py --cron    # Delta de la veille uniquement
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
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE         = "https://app.pennylane.com/api/external/v2"
PL_HEADERS      = {
    "Authorization": f"Bearer {PENNYLANE_TOKEN}",
    "Content-Type":  "application/json",
}
FULL_LOAD_FROM  = "2026-01-01"

DATABASE_URL = os.environ.get("DATABASE_URL", "")

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
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    order_number   TEXT PRIMARY KEY,
                    customer_name  TEXT,
                    customer_id    BIGINT,
                    invoice_id     BIGINT,
                    invoice_number TEXT,
                    amount         TEXT
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id                BIGINT PRIMARY KEY,
                    name              TEXT,
                    ledger_account_id BIGINT
                );
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

# =============================================================
# PENNYLANE — Pagination
# =============================================================
def pl_get_all(endpoint: str, params: dict = None) -> list:
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

        data      = resp.json()
        items     = data.get("items", [])
        all_items.extend(items)

        if page % 10 == 0:
            log.info(f"   ... {len(all_items)} éléments chargés ({endpoint}, page {page})")

        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    return all_items

# =============================================================
# PENNYLANE — Factures
# =============================================================
def extract_order_number(*fields: str) -> str | None:
    """Extract order number from any number of text fields.

    Tries multiple patterns against each non-empty field until a match is found.
    """
    for text in fields:
        if not text:
            continue
        # Match with keyword (FR/EN/DE/IT/ES/NL) + optional dash between prefix and digits
        match = re.search(
            r'(?:Commande|Order|Bestellung|Ordine|Pedido|Bestelling)\s+#?([A-Za-z]{2,5})-?(\d{3,6})', text)
        if match:
            return (match.group(1) + match.group(2)).upper()
        # Fallback: keyword + plain numeric order number (no letter prefix)
        match = re.search(
            r'(?:Commande|Order|Bestellung|Ordine|Pedido|Bestelling)\s+#?(\d{4,6})', text)
        if match:
            return match.group(1)
        # Fallback: letter prefix + optional dash + digits (no keyword needed)
        matches = re.findall(r'#?([A-Z]{2,5})-?(\d{3,6})', text.upper())
        for m in matches:
            if len(m[1]) >= 3:
                return m[0] + m[1]
    return None


def _collect_string_values(obj, depth=0) -> list[str]:
    """Recursively collect all non-empty string values from a dict/list."""
    if depth > 3:
        return []
    values = []
    if isinstance(obj, str):
        if obj.strip():
            values.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            values.extend(_collect_string_values(v, depth + 1))
    elif isinstance(obj, list):
        for item in obj:
            values.extend(_collect_string_values(item, depth + 1))
    return values


def load_invoices_from_pennylane(date_from: str = None) -> int:
    if date_from:
        log.info(f"📋 Chargement des factures Pennylane mises à jour depuis {date_from}...")
        filter_param = json.dumps([{"field": "updated_at", "operator": "gteq", "value": date_from}])
        invoices = pl_get_all("customer_invoices", {"filter": filter_param})
    else:
        # Chargement complet par mois pour éviter les limites de pagination API
        log.info("📋 Chargement complet des factures Pennylane (mois par mois)...")
        invoices = []
        d = datetime.strptime(FULL_LOAD_FROM, "%Y-%m-%d")
        today = datetime.now(timezone.utc)
        while d <= today:
            # Début et fin du mois
            month_start = d.strftime("%Y-%m-%d")
            if d.month == 12:
                next_month = d.replace(year=d.year + 1, month=1, day=1)
            else:
                next_month = d.replace(month=d.month + 1, day=1)
            month_end = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")

            filter_param = json.dumps([
                {"field": "date", "operator": "gteq", "value": month_start},
                {"field": "date", "operator": "lteq", "value": month_end},
            ])
            batch = pl_get_all("customer_invoices", {"filter": filter_param})
            log.info(f"   📅 {month_start[:7]} : {len(batch)} facture(s)")
            invoices.extend(batch)
            d = next_month

    log.info(f"   → {len(invoices)} facture(s) récupérée(s) au total")

    # Diagnostic : affiche les clés de la première facture pour débugger
    if invoices:
        sample = invoices[0]
        log.info(f"   🔍 Clés API facture : {sorted(sample.keys())}")
        sm = sample.get("special_mention", "")
        lbl = sample.get("label", "")
        log.info(f"   🔍 Exemple special_mention={sm!r}, label={lbl!r}")

    rows = []
    skipped = 0
    for inv in invoices:
        # Try well-known fields first, then fall back to scanning all string values
        special_mention = inv.get("special_mention", "") or ""
        label           = inv.get("label", "") or ""
        # Also check additional common fields that may contain order references
        title           = inv.get("title", "") or ""
        memo            = inv.get("memo", "") or ""
        reference       = inv.get("reference", "") or ""
        filename        = inv.get("filename", "") or inv.get("file_name", "") or ""

        order_number = extract_order_number(
            special_mention, label, title, memo, reference, filename,
        )

        # Last resort: scan ALL string values in the invoice object
        if not order_number:
            all_strings = _collect_string_values(inv)
            order_number = extract_order_number(*all_strings)

        if not order_number:
            skipped += 1
            if skipped <= 3:
                inv_num = inv.get("invoice_number", "?")
                log.warning(f"   ⚠️  Facture {inv_num} : aucun numéro de commande trouvé")
                if skipped == 1:
                    log.warning(f"      Données brutes : {json.dumps(inv, ensure_ascii=False, default=str)[:500]}")
            continue

        customer      = inv.get("customer") or {}
        customer_id   = customer.get("id")
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

    # Déduplique : garde la dernière occurrence par order_number
    rows_dedup = list({r[0]: r for r in rows}.values())
    count      = upsert_invoices(rows_dedup)
    log.info(f"   ✅ {len(rows)} facture(s) dont {len(rows_dedup)} order_number uniques ({count} modifiée(s) en DB)")

    if skipped > 0:
        log.warning(f"   ⚠️  {skipped}/{len(invoices)} factures ignorées (pas de numéro de commande extrait)")
    if invoices and len(rows_dedup) == 0:
        log.error(f"   ❌ AUCUNE facture extraite sur {len(invoices)} ! Vérifiez le format des factures Pennylane.")

    return len(rows_dedup)


def load_customers_from_pennylane(date_from: str = None) -> int:
    if date_from:
        log.info(f"👥 Chargement des clients Pennylane mis à jour depuis {date_from}...")
        filter_param = json.dumps([{"field": "updated_at", "operator": "gteq", "value": date_from}])
        customers = pl_get_all("customers", {"filter": filter_param})
    else:
        log.info("👥 Chargement complet des clients Pennylane...")
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
# POINT D'ENTRÉE
# =============================================================
def main():
    parser = argparse.ArgumentParser(description="DB Loader — Pennylane → PostgreSQL")
    parser.add_argument("--full", action="store_true", help=f"Chargement complet depuis {FULL_LOAD_FROM}")
    parser.add_argument("--cron", action="store_true", help="Delta de la veille uniquement")
    args = parser.parse_args()

    if not args.full and not args.cron:
        parser.print_help()
        sys.exit(1)

    log.info(f"\n{'#'*60}")
    log.info(f"🚀 DB Loader | mode={'FULL' if args.full else 'CRON'}")
    log.info(f"{'#'*60}")

    init_db()

    if args.full:
        date_from = FULL_LOAD_FROM
    else:
        date_from = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")

    log.info(f"📅 Date de départ : {date_from}")

    try:
        # En mode cron : ne charger que les éléments modifiés depuis date_from
        # En mode full : tout charger (date_from=None)
        incremental_date = date_from if args.cron else None
        total_invoices  = load_invoices_from_pennylane(incremental_date)
        total_customers = load_customers_from_pennylane(incremental_date)
        set_meta("last_sync", datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.error(f"❌ Erreur : {e}", exc_info=True)
        telegram_send(f"🚨 <b>DB Loader</b>\n❌ Erreur : {e}")
        return

    mode_label = "Chargement complet" if args.full else f"Sync {date_from}"
    log.info(f"\n🏁 DB Loader terminé — {total_invoices} factures | {total_customers} clients")


if __name__ == "__main__":
    main()

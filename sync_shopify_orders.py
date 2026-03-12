#!/usr/bin/env python3
"""
=============================================================
SYNC SHOPIFY ORDERS → PostgreSQL
=============================================================
Charge toutes les commandes Shopify (7 boutiques) dans la table
shopify_orders pour permettre le match Mollie → commande sans
appel API Shopify en temps réel.

Usage :
  python sync_shopify_orders.py           # sync incrémentale (défaut)
  python sync_shopify_orders.py --full    # rechargement complet
  python sync_shopify_orders.py --store LFC  # une seule boutique
"""

import requests
import os
import sys
import logging
import time
import argparse
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import execute_values

# =============================================================
# CONFIGURATION
# =============================================================
SHOPIFY_API_VERSION = "2026-01"

STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC", "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET", "")},
    {"name": "TAR",  "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ", "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED", "")},
    {"name": "COCO", "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC", "")},
    {"name": "LOV",  "store": "le-filet-camouflage-1.myshopify.com",   "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO", "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
]

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("sync_shopify")

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
                CREATE TABLE IF NOT EXISTS shopify_orders (
                    payment_id   TEXT PRIMARY KEY,
                    order_name   TEXT NOT NULL,
                    store_name   TEXT NOT NULL,
                    billing_name TEXT,
                    created_at   TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_shopify_orders_store
                    ON shopify_orders(store_name);
                CREATE INDEX IF NOT EXISTS idx_shopify_orders_name
                    ON shopify_orders(order_name);

                CREATE TABLE IF NOT EXISTS shopify_sync_meta (
                    store_name   TEXT PRIMARY KEY,
                    last_sync_at TIMESTAMP
                );
            """)
        conn.commit()
        log.info("[DB] Tables shopify_orders et shopify_sync_meta pretes")
    finally:
        conn.close()

def get_last_sync(store_name):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_sync_at FROM shopify_sync_meta WHERE store_name = %s", (store_name,))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()

def set_last_sync(store_name, ts):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO shopify_sync_meta (store_name, last_sync_at)
                VALUES (%s, %s)
                ON CONFLICT (store_name) DO UPDATE SET last_sync_at = EXCLUDED.last_sync_at
            """, (store_name, ts))
        conn.commit()
    finally:
        conn.close()

def upsert_orders(rows):
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
# SHOPIFY — OAuth
# =============================================================
_token_cache = {}

def get_access_token(store_url, client_id, client_secret):
    cached = _token_cache.get(store_url)
    if cached and cached["expires_at"] > time.time() + 60:
        return cached["token"]
    resp = requests.post(
        f"https://{store_url}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    if resp.status_code != 200:
        log.error(f"[Shopify] Auth echouee pour {store_url}: {resp.status_code}")
        return None
    data = resp.json()
    token = data["access_token"]
    _token_cache[store_url] = {"token": token, "expires_at": time.time() + data.get("expires_in", 86399)}
    return token

def shopify_get(url, token, params=None):
    for attempt in range(5):
        resp = requests.get(
            url,
            headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = min(2 ** attempt, 16)
            log.info(f"  Rate limit - pause {wait}s...")
            time.sleep(wait)
            continue
        log.warning(f"  GET {url}: {resp.status_code}")
        return None
    return None

# =============================================================
# SYNC D'UNE BOUTIQUE
# =============================================================
def sync_store(store_config, full=False):
    name = store_config["name"]
    store_url = store_config["store"]
    log.info(f"\n{'='*50}")
    log.info(f"[{name}] Sync commandes Shopify")

    token = get_access_token(store_url, store_config["client_id"], store_config["client_secret"])
    if not token:
        log.error(f"[{name}] Impossible d'obtenir un token OAuth")
        return 0

    # Détermine la date de départ
    last_sync = None if full else get_last_sync(name)
    if last_sync:
        log.info(f"[{name}] Sync incrémentale depuis {last_sync}")
    else:
        log.info(f"[{name}] Chargement complet")

    base_url = f"https://{store_url}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "limit": 250,
        "fields": "id,name,billing_address,created_at,payment_gateway_names",
    }
    if last_sync:
        params["updated_at_min"] = last_sync.strftime("%Y-%m-%dT%H:%M:%SZ")

    total_orders = 0
    total_inserted = 0
    page = 0
    next_url = base_url

    while next_url:
        page += 1
        data = shopify_get(next_url, token, params if page == 1 else None)
        if not data:
            break

        orders = data.get("orders", [])
        if not orders:
            break

        # Pour chaque commande Mollie, récupère les transactions
        rows = []
        for order in orders:
            gw_names = order.get("payment_gateway_names") or []
            is_mollie = any("mollie" in g.lower() for g in gw_names)
            if not is_mollie:
                continue

            order_id = order["id"]
            order_name = order.get("name", "").replace("#", "")
            billing = order.get("billing_address") or {}
            billing_name = billing.get("company") or billing.get("name") or "Client inconnu"
            created_at = order.get("created_at", "")[:19]

            # Récupère les transactions pour avoir le payment_id
            txn_url = f"https://{store_url}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}/transactions.json"
            txn_data = shopify_get(txn_url, token, {"fields": "id,payment_id,gateway,status"})
            if not txn_data:
                continue

            for txn in txn_data.get("transactions", []):
                pid = txn.get("payment_id")
                if pid:
                    rows.append((pid, order_name, name, billing_name, created_at))

        total_orders += len(orders)
        inserted = upsert_orders(rows)
        total_inserted += inserted

        if page % 5 == 0:
            log.info(f"  [{name}] Page {page} — {total_orders} commandes traitées, {total_inserted} insérées...")

        # Pagination via Link header
        link_header = None
        # Shopify renvoie la pagination dans le header Link
        # On doit refaire la requête avec requests pour avoir les headers
        resp = requests.get(
            next_url if page > 1 else base_url,
            headers={"X-Shopify-Access-Token": token},
            params=params if page == 1 else None,
            timeout=30,
        )
        link = resp.headers.get("Link", "")
        next_url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part.split(";")[0].strip().strip("<>")
                    break

    set_last_sync(name, datetime.utcnow())
    log.info(f"[{name}] Terminé — {total_orders} commandes Mollie scannées, {total_inserted} nouvelles en DB")
    return total_inserted

# =============================================================
# POINT D'ENTRÉE
# =============================================================
def main():
    parser = argparse.ArgumentParser(description="Sync commandes Shopify → PostgreSQL")
    parser.add_argument("--full", action="store_true", help="Rechargement complet (ignore last_sync)")
    parser.add_argument("--store", type=str, help="Filtrer une boutique (ex: LFC)")
    args = parser.parse_args()

    init_db()

    stores = STORES
    if args.store:
        stores = [s for s in STORES if s["name"] == args.store]
        if not stores:
            log.error(f"Boutique '{args.store}' inconnue")
            sys.exit(1)

    total = 0
    for store in stores:
        if not store["client_secret"]:
            log.warning(f"[{store['name']}] Secret manquant - ignoré")
            continue
        try:
            total += sync_store(store, full=args.full)
        except Exception as e:
            log.error(f"[{store['name']}] Erreur: {e}", exc_info=True)

    log.info(f"\n{'='*50}")
    log.info(f"Sync terminée — {total} nouvelles commandes insérées au total")

if __name__ == "__main__":
    main()

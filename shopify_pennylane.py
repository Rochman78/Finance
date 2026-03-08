#!/usr/bin/env python3
"""
=============================================================
SHOPIFY PAYMENTS → PENNYLANE — Automatisation des écritures
=============================================================
Version 4 — PostgreSQL persistant :
  - Base PostgreSQL (Railway) pour cache + anti-doublons
  - Sync incrémentale des factures et clients Pennylane
  - Aucune perte de données entre les exécutions cron

Ce script :
1. Se connecte à Shopify via client credentials (token auto-renouvelé)
2. Récupère les versements (payouts) au statut "paid"
3. Pour chaque versement, récupère les transactions (commandes + frais)
4. Croise avec les factures et clients Pennylane (via API)
5. Crée les écritures comptables dans Pennylane

Auteur : Script généré avec Claude pour Charles
Date   : Février 2026
"""

import requests
import json
import time
import re
import os
import sys
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from pathlib import Path
import psycopg2
from psycopg2.extras import execute_values

# =============================================================
# CONFIGURATION — À REMPLIR AVEC TES IDENTIFIANTS
# =============================================================

# --- BOUTIQUES SHOPIFY ---
SHOPIFY_API_VERSION = "2026-01"
STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC", "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED", "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET", "")},
    {"name": "MTC",  "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC", "")},
    {"name": "MO",   "store": "mon-ombrage.myshopify.com",             "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO", "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
    {"name": "TZ",   "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ", "")},
    {"name": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",   "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO", "")},
    {"name": "UNIV", "store": "univers-camouflage.myshopify.com",      "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", "")},
]

# --- PENNYLANE ---
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "COLLE_TON_TOKEN_PENNYLANE_ICI")

# --- COMPTES COMPTABLES ---
COMPTE_TRESORERIE = "411INTERNET"
COMPTE_FRAIS = "627001"
JOURNAL_CODE = "ENCSP"
START_DATE = "2026-03-02"  # Ne traiter que les versements a partir de cette date

# --- BASE DE DONNÉES (Railway PostgreSQL) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8637348812:AAFVp2x_a1EkWWedI8wA0KqCfEAmMccNtaU")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-5274088867")

# --- OPTIONS ---
CHECK_INTERVAL_MINUTES = 60
MODE_TEST = True
LOG_FILE = "shopify_pennylane.log"

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# =============================================================
# NOTIFICATIONS TELEGRAM
# =============================================================
def telegram_send(message, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        log.warning(f"⚠️  Échec envoi Telegram: {e}")

def telegram_success(payout_date, montant_net, store_name=""):
    msg = f"✅ Écriture Pennylane passée\n"
    msg += f"🏪 Boutique : {store_name}\n"
    msg += f"📅 Date versement : {payout_date}\n"
    msg += f"💰 Montant net : {montant_net:.2f}€"
    telegram_send(msg)

def telegram_error(context, error_msg):
    msg = f"🚨 <b>ERREUR Zephyr Compta</b>\n"
    msg += f"📍 {context}\n"
    msg += f"❌ {error_msg}\n"
    msg += f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    telegram_send(msg)

# =============================================================
# BASE DE DONNÉES PostgreSQL — Cache + Processed Payouts
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
                CREATE TABLE IF NOT EXISTS processed_payouts (
                    store_name TEXT NOT NULL,
                    payout_id TEXT NOT NULL,
                    processed_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (store_name, payout_id)
                );
                CREATE TABLE IF NOT EXISTS invoices (
                    order_number TEXT PRIMARY KEY,
                    customer_name TEXT,
                    customer_id BIGINT,
                    invoice_id BIGINT,
                    invoice_number TEXT,
                    amount TEXT
                );
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGINT PRIMARY KEY,
                    name TEXT,
                    ledger_account_id BIGINT
                );
                CREATE TABLE IF NOT EXISTS sync_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
            """)
        conn.commit()
        log.info("✅ Base de données PostgreSQL initialisée")
    finally:
        conn.close()

def is_payout_processed(store_name, payout_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM processed_payouts WHERE store_name=%s AND payout_id=%s",
                        (store_name, str(payout_id)))
            return cur.fetchone() is not None
    finally:
        conn.close()

def mark_payout_processed(store_name, payout_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO processed_payouts (store_name, payout_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (store_name, str(payout_id)))
        conn.commit()
    finally:
        conn.close()


class LocalCache:
    def __init__(self):
        self.conn = get_db()

    def close(self):
        if self.conn and not self.conn.closed:
            self.conn.close()

    def get_last_sync(self, entity):
        with self.conn.cursor() as cur:
            cur.execute("SELECT value FROM sync_meta WHERE key=%s", (f"last_sync_{entity}",))
            row = cur.fetchone()
            return row[0] if row else None

    def set_last_sync(self, entity, ts):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO sync_meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                        (f"last_sync_{entity}", ts))
        self.conn.commit()

    def get_invoice_count(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM invoices")
            return cur.fetchone()[0]

    def get_customer_count(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM customers")
            return cur.fetchone()[0]

    def upsert_invoices(self, invoice_index):
        if not invoice_index:
            return
        rows = []
        for order_num, info in invoice_index.items():
            rows.append((order_num, info.get("customer_name"), info.get("customer_id"),
                         info.get("invoice_id"), info.get("invoice_number"), info.get("amount")))
        with self.conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO invoices (order_number, customer_name, customer_id, invoice_id, invoice_number, amount)
                VALUES %s
                ON CONFLICT (order_number) DO UPDATE SET
                    customer_name=EXCLUDED.customer_name,
                    customer_id=EXCLUDED.customer_id,
                    invoice_id=EXCLUDED.invoice_id,
                    invoice_number=EXCLUDED.invoice_number,
                    amount=EXCLUDED.amount
            """, rows)
        self.conn.commit()

    def upsert_customers(self, customers):
        if not customers:
            return
        rows = []
        for cid, info in customers.items():
            rows.append((cid, info.get("name"), info.get("ledger_account_id")))
        with self.conn.cursor() as cur:
            execute_values(cur, """
                INSERT INTO customers (id, name, ledger_account_id)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    name=EXCLUDED.name,
                    ledger_account_id=EXCLUDED.ledger_account_id
            """, rows)
        self.conn.commit()

    def load_invoices(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT order_number, customer_name, customer_id, invoice_id, invoice_number, amount FROM invoices")
            rows = cur.fetchall()
        index = {}
        for r in rows:
            index[r[0]] = {
                "customer_name": r[1],
                "customer_id": r[2],
                "invoice_id": r[3],
                "invoice_number": r[4],
                "amount": r[5],
            }
        return index

    def load_customers(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, name, ledger_account_id FROM customers")
            rows = cur.fetchall()
        customers = {}
        for r in rows:
            customers[r[0]] = {
                "name": r[1],
                "ledger_account_id": r[2],
            }
        return customers


# =============================================================
# SHOPIFY — Authentification (Client Credentials Grant)
# =============================================================
class ShopifyClient:
    def __init__(self, store_config):
        self.store_name = store_config["name"]
        self.store = store_config["store"]
        self.client_id = store_config["client_id"]
        self.client_secret = store_config["client_secret"]
        self.access_token = None
        self.token_expires_at = None
        self.base_url = f"https://{self.store}/admin/api/{SHOPIFY_API_VERSION}"

    def _get_token(self):
        log.info("🔑 Obtention d'un nouveau token Shopify...")
        resp = requests.post(
            f"https://{self.store}/admin/oauth/access_token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        if resp.status_code != 200:
            log.error(f"❌ Erreur auth Shopify: {resp.status_code} - {resp.text}")
            raise Exception(f"Erreur authentification Shopify: {resp.text}")
        data = resp.json()
        self.access_token = data["access_token"]
        expires_in = data.get("expires_in", 86399)
        self.token_expires_at = datetime.now() + timedelta(seconds=expires_in - 300)
        log.info(f"✅ Token Shopify obtenu (expire dans {expires_in//3600}h)")

    def _ensure_token(self):
        if self.access_token is None or datetime.now() >= self.token_expires_at:
            self._get_token()

    def _headers(self):
        self._ensure_token()
        return {"X-Shopify-Access-Token": self.access_token, "Content-Type": "application/json"}

    def get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        resp = requests.get(url, headers=self._headers(), params=params)
        if resp.status_code != 200:
            log.error(f"❌ Shopify GET {endpoint}: {resp.status_code} - {resp.text}")
            return None
        return resp.json()

    def get_payouts(self, status="paid", date_min=None, date_max=None):
        log.info(f"📥 Récupération des versements Shopify (statut: {status})...")
        params = {"status": status}
        if date_min:
            params["date_min"] = date_min
        if date_max:
            params["date_max"] = date_max
        data = self.get("shopify_payments/payouts.json", params)
        if data and "payouts" in data:
            log.info(f"   → {len(data['payouts'])} versement(s) trouvé(s)")
            return data["payouts"]
        return {}

    def get_payout_transactions(self, payout_id):
        log.info(f"📋 Transactions du versement {payout_id}...")
        all_transactions = []
        params = {"payout_id": payout_id, "limit": 250}
        while True:
            data = self.get("shopify_payments/balance/transactions.json", params)
            if not data or "transactions" not in data:
                break
            all_transactions.extend(data["transactions"])
            if len(data["transactions"]) < 250:
                break
            params["since_id"] = data["transactions"][-1]["id"]
        log.info(f"   → {len(all_transactions)} transaction(s)")
        return all_transactions

    def get_order(self, order_id):
        data = self.get(f"orders/{order_id}.json", {"fields": "id,name,order_number"})
        if data and "order" in data:
            return data["order"]
        return None


# =============================================================
# PENNYLANE — Client API v2 (pagination par curseur)
# =============================================================
class PennylaneClient:
    BASE_URL = "https://app.pennylane.com/api/external/v2"

    def __init__(self):
        self.headers = {
            "Authorization": f"Bearer {PENNYLANE_TOKEN}",
            "Content-Type": "application/json"
        }
        self._journals_cache = None
        self._accounts_cache = {}
        self._customers_cache = None
        self._invoices_cache = None

    def _get(self, endpoint, params=None):
        url = f"{self.BASE_URL}/{endpoint}"
        for attempt in range(5):
            resp = requests.get(url, headers=self.headers, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = min(2 ** attempt, 10)
                log.info(f"   ⏳ Rate limit Pennylane — pause {wait}s...")
                time.sleep(wait)
                continue
            log.error(f"❌ Pennylane GET {endpoint}: {resp.status_code} - {resp.text}")
            return None
        log.error(f"❌ Pennylane GET {endpoint}: rate limit persistant après 5 tentatives")
        return None

    def _post(self, endpoint, data):
        url = f"{self.BASE_URL}/{endpoint}"
        resp = requests.post(url, headers=self.headers, json=data)
        if resp.status_code not in (200, 201):
            log.error(f"❌ Pennylane POST {endpoint}: {resp.status_code} - {resp.text}")
            return None
        return resp.json()

    def _get_all_pages(self, endpoint, per_page=100, extra_params=None):
        all_items = []
        cursor = None
        page_num = 0
        while True:
            page_num += 1
            params = {"per_page": per_page}
            if extra_params:
                params.update(extra_params)
            if cursor:
                params["cursor"] = cursor
            data = self._get(endpoint, params)
            if not data:
                break
            items = data.get("items", [])
            all_items.extend(items)
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
            if not has_more or not next_cursor:
                break
            cursor = next_cursor
            if page_num % 5 == 0:
                log.info(f"   ... {len(all_items)} éléments chargés ({endpoint}, page {page_num})...")
        return all_items

    def get_journal_id(self, code):
        if self._journals_cache is None:
            self._journals_cache = self._get_all_pages("journals", per_page=100)
        for j in self._journals_cache:
            if j.get("code", "").upper() == code.upper():
                return j["id"]
        log.error(f"❌ Journal '{code}' non trouvé dans Pennylane")
        return None

    def get_account_id(self, account_number):
        if account_number in self._accounts_cache:
            return self._accounts_cache[account_number]
        filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
        data = self._get("ledger_accounts", {"filter": filter_param, "per_page": 5})
        if data and "items" in data and len(data["items"]) > 0:
            account_id = data["items"][0]["id"]
            self._accounts_cache[account_number] = account_id
            log.info(f"   📒 Compte '{account_number}' trouvé (id: {account_id})")
            return account_id
        log.warning(f"⚠️  Compte '{account_number}' non trouvé dans Pennylane")
        return None

    def build_invoice_index(self, cache=None):
        if cache and cache.get_invoice_count() > 0:
            last_sync = cache.get_last_sync("invoices")
            log.info(f"📋 Sync incrémental des factures (depuis {last_sync or 'jamais'})...")
            index = cache.load_invoices()
            log.info(f"   → {len(index)} facture(s) en cache")

            today = datetime.now()
            first_of_last_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
            date_from = first_of_last_month.strftime("%Y-%m-%d")
            params = {"filter": json.dumps([{"field": "date", "operator": "gteq", "value": date_from}])}
            new_invoices = self._get_all_pages("customer_invoices", per_page=100, extra_params=params)
            log.info(f"   → {len(new_invoices)} nouvelle(s) facture(s) depuis la dernière sync")

            new_index = {}
            for inv in new_invoices:
                order_num = self._extract_order_number(inv.get("label", ""), inv.get("special_mention", ""))
                if order_num:
                    customer = inv.get("customer", {})
                    new_index[order_num] = {
                        "customer_name": self._extract_customer_name(inv.get("label", "")),
                        "customer_id": customer.get("id"),
                        "invoice_id": inv.get("id"),
                        "invoice_number": inv.get("invoice_number", ""),
                        "amount": inv.get("currency_amount"),
                    }
            index.update(new_index)
            if new_index:
                cache.upsert_invoices(new_index)
            cache.set_last_sync("invoices", datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            log.info("📋 Premier chargement des factures Pennylane (sera mis en cache)...")
            all_invoices = self._get_all_pages("customer_invoices", per_page=100)
            log.info(f"   → {len(all_invoices)} facture(s) récupérée(s) au total")
            index = {}
            for inv in all_invoices:
                order_num = self._extract_order_number(inv.get("label", ""), inv.get("special_mention", ""))
                if order_num:
                    customer = inv.get("customer", {})
                    index[order_num] = {
                        "customer_name": self._extract_customer_name(inv.get("label", "")),
                        "customer_id": customer.get("id"),
                        "invoice_id": inv.get("id"),
                        "invoice_number": inv.get("invoice_number", ""),
                        "amount": inv.get("currency_amount"),
                    }
            if cache:
                cache.upsert_invoices(index)
                cache.set_last_sync("invoices", datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"))

        log.info(f"   → {len(index)} facture(s) indexée(s) avec numéro de commande")
        self._invoices_cache = index
        return index

    def _extract_customer_name(self, label):
        if not label:
            return "Inconnu"
        match = re.search(r'(?:Facture|Avoir)\s+(.+?)\s+-\s+F-', label)
        if match:
            return match.group(1).strip()
        return "Inconnu"

    def _extract_order_number(self, label, special_mention):
        for text in [special_mention, label]:
            if not text:
                continue
            match = re.search(r'Commande\s+#?([A-Za-z]{2,5}\d{3,6})', text)
            if match:
                return match.group(1).upper()
            matches = re.findall(r'[A-Z]{2,5}\d{3,6}', text.upper())
            for m in matches:
                digits = re.search(r'\d+', m).group()
                if len(digits) >= 3:
                    return m
        return None

    def get_customers(self, cache=None):
        """
        Récupère les clients avec leurs comptes auxiliaires.
        FIX 1 : re-sync des clients avec ledger_account_id NULL
        FIX 2 : sync des clients présents dans les factures mais absents du cache
        """
        if self._customers_cache is not None:
            return self._customers_cache

        if cache and cache.get_customer_count() > 0:
            log.info("👥 Chargement des clients depuis le cache...")
            customers = cache.load_customers()
            log.info(f"   → {len(customers)} client(s) en cache")

            # FIX 1 : clients avec ledger_account_id NULL
            missing_ids = [cid for cid, info in customers.items() if not info.get("ledger_account_id")]
            if missing_ids:
                log.info(f"   🔄 Re-sync pour {len(missing_ids)} client(s) sans compte auxiliaire...")
                all_customers_api = self._get_all_pages("customers", per_page=100)
                refreshed = {}
                for c in all_customers_api:
                    cid = c.get("id")
                    if cid in missing_ids:
                        ledger_account = c.get("ledger_account", {})
                        refreshed[cid] = {
                            "name": c.get("name", ""),
                            "ledger_account_id": ledger_account.get("id") if ledger_account else None,
                        }
                        customers[cid] = refreshed[cid]
                recovered = sum(1 for v in refreshed.values() if v.get("ledger_account_id"))
                log.info(f"   → {recovered}/{len(missing_ids)} compte(s) auxiliaire(s) récupéré(s)")
                if refreshed and cache:
                    cache.upsert_customers(refreshed)
            else:
                log.info("   ✅ Tous les clients ont un compte auxiliaire en cache")

            # FIX 2 : clients présents dans les factures mais absents du cache
            with cache.conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT customer_id FROM invoices
                    WHERE customer_id IS NOT NULL
                    AND customer_id NOT IN (SELECT id FROM customers)
                """)
                missing_from_cache = [row[0] for row in cur.fetchall()]

            if missing_from_cache:
                log.info(f"   🔄 {len(missing_from_cache)} client(s) absents du cache — sync Pennylane...")
                all_customers_api = self._get_all_pages("customers", per_page=100)
                new_customers = {}
                for c in all_customers_api:
                    cid = c.get("id")
                    if cid in missing_from_cache:
                        ledger_account = c.get("ledger_account", {})
                        new_customers[cid] = {
                            "name": c.get("name", ""),
                            "ledger_account_id": ledger_account.get("id") if ledger_account else None,
                        }
                        customers[cid] = new_customers[cid]
                log.info(f"   → {len(new_customers)} nouveau(x) client(s) ajouté(s) au cache")
                if new_customers:
                    cache.upsert_customers(new_customers)

        else:
            log.info("👥 Premier chargement des clients Pennylane (sera mis en cache)...")
            all_customers = self._get_all_pages("customers", per_page=100)
            customers = {}
            for c in all_customers:
                cid = c.get("id")
                ledger_account = c.get("ledger_account", {})
                customers[cid] = {
                    "name": c.get("name", ""),
                    "ledger_account_id": ledger_account.get("id") if ledger_account else None,
                }
            if cache:
                cache.upsert_customers(customers)
                cache.set_last_sync("customers", datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"))

        log.info(f"   → {len(customers)} client(s) au total")
        self._customers_cache = customers
        return customers

    def create_ledger_entry(self, date, label, journal_id, lines):
        payload = {
            "date": date,
            "label": label,
            "journal_id": journal_id,
            "ledger_entry_lines": lines
        }
        if MODE_TEST:
            log.info(f"🧪 [MODE TEST] Écriture simulée : {label}")
            for line in lines:
                d = line.get("debit", "0.00")
                c = line.get("credit", "0.00")
                log.info(f"    {line.get('label', ''):<40} D:{d:>10}  C:{c:>10}")
            return {"id": "TEST", "status": "simulated"}
        result = self._post("ledger_entries", payload)
        if result:
            log.info(f"✅ Écriture créée dans Pennylane (ID: {result.get('id')})")
        return result


# =============================================================
# LOGIQUE PRINCIPALE — Traitement d'un versement
# =============================================================
def process_payout(shopify, pennylane, payout):
    payout_id = payout["id"]
    payout_date = payout["date"]
    payout_amount = float(payout["amount"])

    log.info(f"\n{'='*60}")
    log.info(f"💰 Traitement du versement {payout_id}")
    log.info(f"   Date: {payout_date} | Montant net: {payout_amount} €")
    log.info(f"{'='*60}")

    transactions = shopify.get_payout_transactions(payout_id)
    if not transactions:
        log.warning(f"⚠️  Aucune transaction trouvée pour le versement {payout_id}")
        return {"success": False, "error": "Aucune transaction trouvée"}

    if pennylane._invoices_cache is None:
        pennylane.build_invoice_index()
    invoice_index = pennylane._invoices_cache or {}
    customers = pennylane.get_customers()

    entry_lines = []
    total_gross = 0.0
    total_fees = 0.0
    client_details = []

    for txn in transactions:
        source_type = txn.get("source_type", "")
        source_order_id = txn.get("source_order_id")
        amount = float(txn.get("amount", "0"))
        fee = abs(float(txn.get("fee", "0")))

        # Normaliser le type (ex: "Payments::Refund" → "refund")
        source_type_lower = source_type.lower()
        is_charge = source_type_lower == "charge"
        is_refund = "refund" in source_type_lower
        is_dispute = "dispute" in source_type_lower
        txn_label = source_type_lower.split("::")[-1].upper()  # "REFUND" ou "DISPUTE"

        # Traiter charges (ventes), refunds et disputes — ignorer le reste
        if not (is_charge or is_refund or is_dispute) or not source_order_id:
            if "payout" in source_type_lower:
                continue
            if amount != 0 or fee != 0:
                log.info(f"   ℹ️  Transaction {source_type} ignorée (montant: {amount}, fee: {fee})")
            continue

        order = shopify.get_order(source_order_id)
        if not order:
            log.warning(f"   ⚠️  Commande {source_order_id} non trouvée")
            continue

        order_name = order.get("name", "").replace("#", "")
        invoice_info = invoice_index.get(order_name)

        if not invoice_info:
            log.warning(f"   ⚠️  Commande {order_name} non trouvée dans les factures Pennylane")
            continue

        customer_id = invoice_info.get("customer_id")
        customer_name = invoice_info.get("customer_name", "Client inconnu")

        if customer_id and customer_id in customers:
            cached_name = customers[customer_id].get("name")
            if cached_name:
                customer_name = cached_name

        ledger_account_id = None
        if customer_id and customer_id in customers:
            ledger_account_id = customers[customer_id].get("ledger_account_id")

        if not ledger_account_id:
            log.warning(f"   ⚠️  Pas de compte auxiliaire pour {customer_name}")
            continue

        gross_amount = abs(amount)

        if is_charge:
            # Vente normale : crédit compte auxiliaire client
            total_gross += gross_amount
            total_fees += fee
            log.info(f"   ✅ {order_name} → {customer_name} | Brut: {gross_amount}€ | Frais: {fee}€")
            client_details.append(f"{customer_name} — {gross_amount:.2f}€")
            entry_lines.append({
                "ledger_account_id": ledger_account_id,
                "debit": "0.00",
                "credit": f"{gross_amount:.2f}",
                "label": f"{customer_name} - {order_name}"
            })
            if fee > 0:
                fee_account_id = pennylane.get_account_id(COMPTE_FRAIS)
                if fee_account_id:
                    entry_lines.append({
                        "ledger_account_id": fee_account_id,
                        "debit": f"{fee:.2f}",
                        "credit": "0.00",
                        "label": f"{customer_name} - {order_name}"
                    })
        else:
            # Remboursement ou dispute : débit compte auxiliaire client (sens inverse)
            total_gross -= gross_amount
            log.info(f"   ↩️  [{txn_label}] {order_name} → {customer_name} | Montant: -{gross_amount}€")
            client_details.append(f"[{txn_label}] {customer_name} — -{gross_amount:.2f}€")
            entry_lines.append({
                "ledger_account_id": ledger_account_id,
                "debit": f"{gross_amount:.2f}",
                "credit": "0.00",
                "label": f"[{txn_label}] {customer_name} - {order_name}"
            })

    if not entry_lines:
        log.warning(f"⚠️  Aucune ligne d'écriture générée pour le versement {payout_id}")
        return {"success": False, "error": "Aucune ligne d'écriture générée"}

    # Ligne trésorerie : débit si net positif, crédit si net négatif (remboursements)
    net_amount = total_gross - total_fees
    tresorerie_account_id = pennylane.get_account_id(COMPTE_TRESORERIE)
    if tresorerie_account_id:
        if net_amount >= 0:
            entry_lines.insert(0, {
                "ledger_account_id": tresorerie_account_id,
                "debit": f"{net_amount:.2f}",
                "credit": "0.00",
                "label": f"Cumul versement {COMPTE_TRESORERIE}"
            })
        else:
            entry_lines.insert(0, {
                "ledger_account_id": tresorerie_account_id,
                "debit": "0.00",
                "credit": f"{abs(net_amount):.2f}",
                "label": f"Cumul remboursements {COMPTE_TRESORERIE}"
            })

    total_debit = sum(float(l["debit"]) for l in entry_lines)
    total_credit = sum(float(l["credit"]) for l in entry_lines)

    log.info(f"\n   Total DÉBIT  : {total_debit:.2f} €")
    log.info(f"   Total CRÉDIT : {total_credit:.2f} €")

    if abs(total_debit - total_credit) > 0.01:
        log.error(f"   ❌ ÉCRITURE DÉSÉQUILIBRÉE ! Différence: {abs(total_debit - total_credit):.2f} €")
        return {"success": False, "error": f"Écriture déséquilibrée ({abs(total_debit - total_credit):.2f}€)"}

    log.info(f"   ✅ Écriture ÉQUILIBRÉE")

    journal_id = pennylane.get_journal_id(JOURNAL_CODE)
    if not journal_id:
        log.error(f"❌ Journal '{JOURNAL_CODE}' non trouvé — écriture non créée")
        return {"success": False, "error": f"Journal '{JOURNAL_CODE}' non trouvé"}

    result = pennylane.create_ledger_entry(
        date=payout_date,
        label=f"Versement Shopify Payments {payout_date}",
        journal_id=journal_id,
        lines=entry_lines
    )
    if result is not None:
        return {"success": True, "nb_clients": len(client_details), "details": client_details}
    return {"success": False, "error": "Échec création écriture Pennylane"}


# =============================================================
# BOUCLE PRINCIPALE
# =============================================================
def run_once(target_date=None, store_filter=None):
    log.info(f"\n{'#'*60}")
    log.info(f"\U0001f504 Verification des versements \u2014 {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log.info(f"{'#'*60}")

    init_db()

    cache = LocalCache()
    pennylane = PennylaneClient()

    stores = STORES
    if store_filter:
        stores = [s for s in STORES if s["name"] == store_filter]
        if not stores:
            log.error(f"Boutique '{store_filter}' non trouvee")
            return

    pennylane.build_invoice_index(cache=cache)
    pennylane.get_customers(cache=cache)
    cache.close()

    total_ecritures = 0

    for store_config in stores:
        sname = store_config["name"]
        if not store_config.get("client_secret"):
            log.warning(f"[{sname}] Pas de secret, boutique ignoree")
            continue

        try:
            log.info(f"\n{'='*60}")
            log.info(f"\U0001f3ea [{sname}] {store_config['store']}")
            log.info(f"{'='*60}")

            shopify = ShopifyClient(store_config)

            payout_params = {"status": "paid"}
            if target_date:
                payout_params["date_min"] = target_date
                payout_params["date_max"] = target_date
            else:
                payout_params["date_min"] = START_DATE

            payouts = shopify.get_payouts(**payout_params)
            new_payouts = [p for p in payouts if not is_payout_processed(sname, p["id"])]

            if not new_payouts:
                log.info(f"[{sname}] Aucun nouveau versement")
                continue

            log.info(f"[{sname}] {len(new_payouts)} versement(s) a traiter")

            for payout in new_payouts:
                result = process_payout(shopify, pennylane, payout)
                if result and result.get("success"):
                    mark_payout_processed(sname, payout["id"])
                    log.info(f"[{sname}] Versement {payout['id']} OK")
                    telegram_success(
                        payout_date=payout["date"],
                        montant_net=float(payout["amount"]),
                        store_name=sname
                    )
                    total_ecritures += 1
                else:
                    error_msg = result.get("error", "Erreur inconnue") if result else "Aucune ligne"
                    log.warning(f"[{sname}] Versement {payout['id']} NON traite")
                    telegram_error(
                        context=f"[{sname}] Versement {payout['id']} du {payout['date']}",
                        error_msg=error_msg
                    )
        except Exception as e:
            log.error(f"[{sname}] Erreur: {e}", exc_info=True)
            telegram_error(f"[{sname}]", str(e))

    if total_ecritures == 0:
        date_str = target_date or "aujourd'hui"
        telegram_send(f"\u2139\ufe0f Aucun versement {date_str} a traiter ({len(stores)} boutiques)")


def start_health_server():
    port = int(os.environ.get("PORT", 10000))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"🌐 Serveur HTTP démarré sur le port {port}")


def run_continuous():
    start_health_server()
    telegram_send("🎉 Bonne journée CHARLES — la migration est un succès !")
    log.info("="*60)
    log.info("🚀 DÉMARRAGE — Shopify Payments → Pennylane")
    log.info(f"   Mode: {'TEST (simulation)' if MODE_TEST else 'PRODUCTION'}")
    log.info(f"   Intervalle: toutes les {CHECK_INTERVAL_MINUTES} minutes")
    log.info(f"   Boutiques: {len(STORES)}")
    log.info("="*60)

    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"❌ Erreur: {e}", exc_info=True)
            telegram_error("Exécution automatique", str(e))
        log.info(f"\n⏳ Prochaine vérification dans {CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)


# =============================================================
# POINT D'ENTRÉE
# =============================================================
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Shopify Payments → Pennylane")
    parser.add_argument("--once", action="store_true", help="Exécuter une seule fois")
    parser.add_argument("--test", action="store_true", help="Mode test (simulation)")
    parser.add_argument("--production", action="store_true", help="Mode production")
    parser.add_argument("--date", type=str, help="Date cible YYYY-MM-DD (ex: 2026-02-27)")
    parser.add_argument("--store", type=str, help="Filtrer une boutique (ex: MFC, RED, HET...)")
    parser.add_argument("--cron", action="store_true", help="Mode cron : traite les versements en production")
    args = parser.parse_args()

    if args.test:
        MODE_TEST = True
    if args.production:
        MODE_TEST = False

    if args.cron:
        MODE_TEST = False
        log.info(f"🤖 Mode CRON — traitement des versements du {(datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')} pour {len(STORES)} boutiques")
        try:
            run_once(store_filter=args.store)
        except Exception as e:
            log.error(f"❌ Erreur cron: {e}", exc_info=True)
            telegram_error("Exécution CRON", str(e))
    elif args.once:
        run_once(target_date=args.date, store_filter=args.store)
    else:
        run_continuous()

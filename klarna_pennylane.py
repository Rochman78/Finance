#!/usr/bin/env python3
"""
=============================================================
KLARNA → PENNYLANE — Automatisation des écritures
=============================================================
Chaque nuit (cron Render) :
1. Récupère les payouts Klarna de la veille (LFC + TAR)
2. Pour chaque payout → récupère les transactions (#LFCxxxxx / #TARxxxxx)
3. Croise avec la base PostgreSQL partagée (invoices + customers)
4. Crée les écritures dans le journal ENCSP de Pennylane

Base PostgreSQL : même que shopify_pennylane.py (Railway)
"""

import os
import sys
import json
import time
import logging
import requests
import argparse
import psycopg2
from datetime import datetime, timedelta, timezone

# =============================================================
# CONFIGURATION
# =============================================================

# --- KLARNA — une paire de credentials par boutique ---
BOUTIQUES = {
    "K6272251": {
        "nom":      "LFC",
        "username": os.environ.get("KLARNA_USERNAME_LFC", "92e6d213-c608-4c2c-9c86-0289acaedfd8"),
        "password": os.environ.get("KLARNA_PASSWORD_LFC", "klarna_live_api_N0dFME5EVSE1cWsqeG5peHdPUFY3T2VLVHNvTXVjZCgsOTJlNmQyMTMtYzYwOC00YzJjLTljODYtMDI4OWFjYWVkZmQ4LDEsd2NQbnNDRGJ2cWtDOVZQcVdmeG1KTlhBY1JpS3RsSTRKa0JndlplUVFKST0"),
    },
    "K6684056": {
        "nom":      "TAR",
        "username": os.environ.get("KLARNA_USERNAME_TAR", "2ef0b999-9ab8-4594-ab4c-468e9b8431ad"),
        "password": os.environ.get("KLARNA_PASSWORD_TAR", "klarna_live_api_MmZPKmNncTFEbVAlTThGcCRGekxoWmUjU1RtWkN4encsMmVmMGI5OTktOWFiOC00NTk0LWFiNGMtNDY4ZTliODQzMWFkLDEsQk1CczQ1LzB1QjVpc1pFcFBYMG9OL1BndmVoRGdncmJhcXNmTU54OTgvUT0"),
    },
}

# --- PENNYLANE ---
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE         = "https://app.pennylane.com/api/external/v2"
PL_HEADERS      = {
    "Authorization": f"Bearer {PENNYLANE_TOKEN}",
    "Content-Type":  "application/json",
}

# --- COMPTES COMPTABLES ---
COMPTE_KLARNA    = "411KLARNA"    # Tiers Klarna (trésorerie)
COMPTE_FRAIS     = "62700101"     # Frais Klarna
COMPTE_TVA_FRAIS = "44566"  # TVA sur frais
JOURNAL_CODE     = "ENCSP"

# --- BASE DE DONNÉES (partagée avec shopify_pennylane.py) ---
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# --- TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")

# Klarna API EU production
KLARNA_BASE = "https://api.klarna.com/settlements/v1"

# =============================================================
# LOGGING
# =============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

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
# BASE DE DONNÉES PostgreSQL (partagée avec Shopify)
# =============================================================
def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def init_klarna_table():
    """Crée la table de suivi des payouts Klarna si elle n'existe pas."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_klarna_payouts (
                    payment_reference TEXT PRIMARY KEY,
                    boutique          TEXT,
                    processed_at      TIMESTAMP DEFAULT NOW()
                );
            """)
        conn.commit()
        log.info("✅ Table processed_klarna_payouts OK")
    finally:
        conn.close()


def is_payout_processed(payment_reference: str) -> bool:
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM processed_klarna_payouts WHERE payment_reference = %s",
                (payment_reference,)
            )
            return cur.fetchone() is not None
    finally:
        conn.close()


def clear_processed_since(date_from: str):
    """Supprime les payouts traités depuis date_from pour permettre le retraitement."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM processed_klarna_payouts WHERE processed_at >= %s::date",
                (date_from,)
            )
            count = cur.rowcount
        conn.commit()
        log.info(f"🗑️  {count} payout(s) Klarna supprimé(s) depuis {date_from}")
        return count
    finally:
        conn.close()


def mark_payout_processed(payment_reference: str, boutique: str):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_klarna_payouts (payment_reference, boutique)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
            """, (payment_reference, boutique))
        conn.commit()
    finally:
        conn.close()


def load_invoices() -> dict:
    """
    Charge l'index des factures depuis la base partagée.
    Retourne : { "LFC29289": { customer_name, customer_id, invoice_number, ... }, ... }
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT order_number, customer_name, customer_id,
                       invoice_id, invoice_number, amount
                FROM invoices
            """)
            rows = cur.fetchall()
        index = {}
        for r in rows:
            index[r[0]] = {
                "customer_name":  r[1],
                "customer_id":    r[2],
                "invoice_id":     r[3],
                "invoice_number": r[4],
                "amount":         r[5],
            }
        log.info(f"📋 {len(index)} facture(s) chargée(s) depuis la base")
        return index
    finally:
        conn.close()


def load_customers() -> dict:
    """
    Charge les clients depuis la base partagée.
    Retourne : { customer_id: { name, ledger_account_id }, ... }
    """
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, ledger_account_id FROM customers")
            rows = cur.fetchall()
        customers = {}
        for r in rows:
            customers[r[0]] = {
                "name":              r[1],
                "ledger_account_id": r[2],
            }
        log.info(f"👥 {len(customers)} client(s) chargé(s) depuis la base")
        return customers
    finally:
        conn.close()

# =============================================================
# PENNYLANE — Comptes + Journal
# =============================================================
_accounts_cache = {}
_journal_cache  = {}


def get_account_id(account_number: str):
    if account_number in _accounts_cache:
        return _accounts_cache[account_number]
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
    resp = requests.get(
        f"{PL_BASE}/ledger_accounts",
        headers=PL_HEADERS,
        params={"filter": filter_param, "limit": 5},
        timeout=30
    )
    if resp.status_code == 200:
        items = resp.json().get("items", [])
        if items:
            _accounts_cache[account_number] = items[0]["id"]
            log.info(f"   📒 Compte {account_number} → ID {items[0]['id']}")
            return items[0]["id"]
    log.warning(f"   ⚠️  Compte '{account_number}' introuvable dans Pennylane")
    return None


def get_journal_id(code: str):
    if code in _journal_cache:
        return _journal_cache[code]
    resp = requests.get(
        f"{PL_BASE}/journals",
        headers=PL_HEADERS,
        params={"limit": 100},
        timeout=30
    )
    if resp.status_code == 200:
        for j in resp.json().get("items", []):
            if j.get("code", "").upper() == code.upper():
                _journal_cache[code] = j["id"]
                log.info(f"   📓 Journal {code} → ID {j['id']}")
                return j["id"]
    log.error(f"   ❌ Journal '{code}' introuvable dans Pennylane")
    return None

# =============================================================
# KLARNA API
# =============================================================
def klarna_get(boutique: dict, path: str, params: dict = None):
    resp = requests.get(
        f"{KLARNA_BASE}{path}",
        auth=(boutique["username"], boutique["password"]),
        params=params,
        timeout=30
    )
    if resp.status_code == 401:
        log.error(f"❌ Klarna {boutique['nom']} : credentials invalides (401)")
        return None
    if resp.status_code != 200:
        log.error(f"❌ Klarna {boutique['nom']} {path} : {resp.status_code} {resp.text}")
        return None
    return resp.json()


def get_payouts(boutique: dict, date_str: str) -> list:
    log.info(f"🔍 Klarna {boutique['nom']} : payouts du {date_str}...")
    data = klarna_get(boutique, "/payouts", {"start_date": date_str, "end_date": date_str})
    if not data:
        return []
    payouts = data.get("payouts", [])
    log.info(f"   → {len(payouts)} payout(s)")
    return payouts


def get_transactions(boutique: dict, payment_reference: str) -> list:
    log.info(f"   📋 Transactions du payout {payment_reference}...")
    data = klarna_get(boutique, f"/transactions?payment_reference={payment_reference}")
    if not data:
        return []
    transactions = data.get("transactions", [])
    log.info(f"   → {len(transactions)} transaction(s)")
    return transactions


def grouper_par_commande(transactions: list, total_tax_amount: float) -> dict:
    """
    Regroupe par commande et calcule sale / fee / tax / settlement.
    merchant_reference1 = "#LFC29289" → clé = "LFC29289" (sans #, en majuscules)
    """
    orders     = {}
    total_fees = 0.0

    for t in transactions:
        raw_ref = t.get("merchant_reference1", "").strip()
        ref     = raw_ref.lstrip("#").upper()   # "LFC29289"
        type_   = t.get("type", "").upper()
        amount  = round(float(t.get("amount", 0)) / 100, 2)

        if not ref:
            continue
        if ref not in orders:
            orders[ref] = {"sale": 0.0, "fee": 0.0, "tax": 0.0, "raw_ref": raw_ref}

        if type_ == "SALE":
            orders[ref]["sale"]  += amount
        elif type_ == "FEE":
            vat_amount = round(float(t.get("vat_amount", 0)) / 100, 2)
            orders[ref]["fee"]   += abs(amount)
            orders[ref]["tax"]   += abs(vat_amount)
            total_fees           += abs(amount)
        elif type_ == "RETURN":
            orders[ref]["sale"]  -= abs(amount)

    for ref, vals in orders.items():
        # tax déjà accumulé depuis vat_amount de chaque FEE
        vals["settlement"] = round(vals["sale"] - vals["fee"] - vals["tax"], 2)

    return orders

# =============================================================
# PENNYLANE — Création d'écriture
# =============================================================
def ledger_entry_exists(date_str: str, label: str, journal_id: int) -> bool | None:
    """Vérifie si une écriture avec le même label et date existe déjà dans Pennylane.
    Retourne True si doublon trouvé, False si aucun doublon, None si l'API est injoignable.
    Pagine avec cursor pour ne rater aucune écriture."""
    filter_param = json.dumps([
        {"field": "date", "operator": "eq", "value": date_str},
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
            log.error(f"   ❌ Vérification anti-doublon échouée : {resp.status_code} {resp.text[:200]}")
            return None
        else:
            log.error("   ❌ Vérification anti-doublon échouée après 3 tentatives (rate limit)")
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


def create_ledger_entry(date_str: str, libelle: str, journal_id: int,
                         lines: list, test_mode: bool) -> bool:
    if test_mode:
        log.info(f"   🧪 TEST — écriture simulée : {libelle}")
        for l in lines:
            log.info(f"      {l.get('label',''):<50}  D:{l.get('debit','0'):>10}  C:{l.get('credit','0'):>10}")
        return True

    # Anti-doublon : vérifier si l'écriture existe déjà
    check = ledger_entry_exists(date_str, libelle, journal_id)
    if check is None:
        log.error(f"   🚫 BLOQUÉ — impossible de vérifier les doublons, écriture NON créée : {libelle}")
        return False
    if check:
        log.warning(f"   ⚠️  Écriture déjà existante, skip : {libelle}")
        return True

    payload = {
        "date":               date_str,
        "label":              libelle,
        "journal_id":         journal_id,
        "ledger_entry_lines": lines,
    }
    resp = requests.post(f"{PL_BASE}/ledger_entries",
                         headers=PL_HEADERS, json=payload, timeout=30)
    if resp.status_code in (200, 201):
        log.info(f"   ✅ Écriture créée : {libelle}")
        return True
    log.error(f"   ❌ Erreur création écriture : {resp.status_code} {resp.text}")
    return False

# =============================================================
# TRAITEMENT D'UN PAYOUT
# =============================================================
def traiter_payout(payout: dict, boutique: dict, invoice_index: dict,
                    customers: dict, journal_id: int, comptes_fixes: dict,
                    test_mode: bool) -> tuple:
    payment_ref = payout.get("payment_reference")
    payout_date = payout.get("payout_date", "")[:10]
    total_tax   = round(float(payout.get("total_tax_amount", 0)), 2)
    total_net   = round(float(payout.get("total_settlement_amount", 0)), 2)

    log.info(f"\n{'='*60}")
    log.info(f"💰 {boutique['nom']} | Payout {payment_ref} | {payout_date} | net: {total_net}€")
    log.info(f"{'='*60}")

    if is_payout_processed(payment_ref):
        log.info("   ⏭️  Déjà traité, skip.")
        return 0, 0, []

    transactions = get_transactions(boutique, payment_ref)
    if not transactions:
        log.warning("   ⚠️  Aucune transaction.")
        return 0, 0, []

    orders = grouper_par_commande(transactions, total_tax)
    log.info(f"   → {len(orders)} commande(s)")

    nb_ok, nb_err = 0, 0
    messages      = []

    libelle_klarna = f"Versement Klarna {payout_date} - {boutique['nom']}"
    net_total      = 0.0
    lines          = []  # toutes les lignes sauf 411KLARNA (ajoutée en tête après)
    orders_ok      = []  # pour le log final

    for order_ref, vals in orders.items():
        log.info(f"\n   🛒 {order_ref} | Vente: {vals['sale']}€ | Frais: {vals['fee']}€ | TVA: {vals['tax']}€ | Net: {vals['settlement']}€")

        # 1. Facture dans la base
        invoice = invoice_index.get(order_ref)
        if not invoice:
            log.warning(f"      ⚠️  Facture non trouvée : {order_ref}")
            nb_err += 1
            messages.append(f"⚠️ Facture introuvable : {order_ref}")
            continue

        invoice_number = invoice.get("invoice_number", "")
        customer_id    = invoice.get("customer_id")
        customer_name  = invoice.get("customer_name", "Inconnu")

        # 2. Client dans la base
        customer = customers.get(customer_id) if customer_id else None
        if customer and customer.get("name"):
            customer_name = customer["name"]

        ledger_account_id = customer.get("ledger_account_id") if customer else None
        if not ledger_account_id:
            log.warning(f"      ⚠️  Pas de compte auxiliaire pour {customer_name}")
            nb_err += 1
            messages.append(f"⚠️ Compte client introuvable : {customer_name} ({order_ref})")
            continue

        log.info(f"      📄 {invoice_number} | {customer_name} | ledger_account_id: {ledger_account_id}")

        libelle = f"{customer_name} - {vals['raw_ref']} - {invoice_number}"
        net_total += vals['settlement']
        orders_ok.append((order_ref, customer_name, vals))

        # Lignes client + frais (411KLARNA sera ajoutée en tête)
        lines += [
            {   # 411CLIENT crédit (TTC dû par le client)
                "ledger_account_id": ledger_account_id,
                "debit":  "0.00",
                "credit": f"{vals['sale']:.2f}",
                "label":  libelle,
            },
            {   # 627001 débit (frais Klarna)
                "ledger_account_id": comptes_fixes[COMPTE_FRAIS],
                "debit":  f"{vals['fee']:.2f}",
                "credit": "0.00",
                "label":  libelle,
            },
            {   # 44566 débit (TVA sur frais)
                "ledger_account_id": comptes_fixes[COMPTE_TVA_FRAIS],
                "debit":  f"{vals['tax']:.2f}",
                "credit": "0.00",
                "label":  libelle,
            },
        ]

    if not orders_ok:
        return nb_ok, nb_err, messages

    # Ligne 411KLARNA unique en tête
    net_total = round(net_total, 2)
    all_lines = [
        {
            "ledger_account_id": comptes_fixes[COMPTE_KLARNA],
            "debit":  f"{net_total:.2f}",
            "credit": "0.00",
            "label":  libelle_klarna,
        }
    ] + lines

    # Vérif équilibre
    total_d = sum(float(l["debit"])  for l in all_lines)
    total_c = sum(float(l["credit"]) for l in all_lines)
    if abs(total_d - total_c) > 0.02:
        log.error(f"   ❌ Écriture déséquilibrée ! D:{total_d:.2f} C:{total_c:.2f}")
        return 0, 1, [f"❌ Écriture déséquilibrée payout {payment_ref}"]

    if test_mode:
        log.info(f"   🧪 TEST — écriture simulée payout {payment_ref}")
        for l in all_lines:
            log.info(f"      {l['label']:<50} D:{float(l['debit']):>8.2f}  C:{float(l['credit']):>8.2f}")
        nb_ok = len(orders_ok)
    else:
        ok = create_ledger_entry(payout_date, libelle_klarna, journal_id, all_lines, test_mode)
        if ok:
            nb_ok = len(orders_ok)
            for ref, name, vals in orders_ok:
                messages.append(f"✅ {ref} — {name} — {vals['settlement']:.2f}€ net")
            mark_payout_processed(payment_ref, boutique["nom"])
        else:
            nb_err = 1
            messages.append(f"❌ Erreur écriture payout {payment_ref}")

    return nb_ok, nb_err, messages

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
    parser = argparse.ArgumentParser(description="Klarna → Pennylane")
    parser.add_argument("--date", help="Date cible YYYY-MM-DD (défaut: hier)")
    parser.add_argument("--from", dest="from_date", help="Date début rattrapage YYYY-MM-DD (jusqu'à hier)")
    parser.add_argument("--clear", help="Supprime les payouts traités depuis cette date (YYYY-MM-DD) pour retraitement")
    parser.add_argument("--test", action="store_true", help="Mode test (aucune écriture créée)")
    parser.add_argument("--cron", action="store_true", help="Mode cron (hier, production)")
    args = parser.parse_args()

    if args.clear:
        init_klarna_table()
        clear_processed_since(args.clear)
        if not args.date and not args.from_date and not args.cron:
            return

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    if args.cron:
        test_mode    = False
        # Fenêtre de 3 jours : retente les payouts échoués (factures créées en retard)
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        target_dates = date_range(three_days_ago, yesterday)
    elif args.from_date:
        test_mode    = args.test
        end = args.date or yesterday
        target_dates = date_range(args.from_date, end)
        log.info(f"📅 Rattrapage : {len(target_dates)} jour(s) du {target_dates[0]} au {target_dates[-1]}")
    else:
        test_mode    = args.test
        target_dates = [args.date or yesterday]

    log.info(f"\n{'#'*60}")
    log.info(f"🚀 Klarna → Pennylane | {target_dates} | Test: {test_mode}")

    # Init + chargement (une seule fois pour toutes les dates)
    init_klarna_table()
    invoice_index = load_invoices()
    customers     = load_customers()

    # Comptes Pennylane fixes (une seule fois)
    log.info("📒 Chargement des comptes Pennylane...")
    comptes_fixes = {}
    for compte in [COMPTE_KLARNA, COMPTE_FRAIS, COMPTE_TVA_FRAIS]:
        aid = get_account_id(compte)
        if not aid:
            log.error(f"❌ Compte '{compte}' introuvable — arrêt.")
            telegram_send(f"🚨 <b>Klarna → Pennylane</b>\n❌ Compte '{compte}' introuvable dans Pennylane")
            return
        comptes_fixes[compte] = aid

    journal_id = 13509687  # ENCSP — ID hardcodé car hors pagination API

    for target_date in target_dates:
        log.info(f"\n{'#'*60}")
        log.info(f"📅 {target_date}")
        log.info(f"{'#'*60}")

        total_ok, total_err, all_messages = 0, 0, []
        any_payout = False

        # Boucle sur les deux boutiques
        for merchant_id, boutique in BOUTIQUES.items():
            payouts = get_payouts(boutique, target_date)
            if not payouts:
                log.info(f"   ℹ️  {boutique['nom']} : aucun payout ce jour")
                continue

            any_payout = True
            for payout in payouts:
                ok, err, msgs = traiter_payout(
                    payout, boutique, invoice_index, customers,
                    journal_id, comptes_fixes, test_mode
                )
                total_ok  += ok
                total_err += err
                all_messages.extend(msgs)

        # Notification Telegram (uniquement en cas d'erreur)
        if any_payout and total_err > 0:
            status = "⚠️" if total_ok > 0 else "🚨"
            tg_msg = (
                f"{status} <b>Klarna → Pennylane</b> | {target_date}\n"
                f"✅ {total_ok} écriture(s) créée(s)\n"
                f"❌ {total_err} erreur(s)\n\n"
                + "\n".join(all_messages[:20])
            )
            telegram_send(tg_msg)

        log.info(f"\n🏁 {target_date} : {total_ok} OK / {total_err} erreurs")


if __name__ == "__main__":
    main()

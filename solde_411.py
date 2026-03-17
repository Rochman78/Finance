#!/usr/bin/env python3
"""
=============================================================
APUREMENT 411 — Solde les écarts de centimes sur comptes clients
=============================================================
Récupère tous les comptes 411XXX, calcule leur solde via les
écritures Pennylane, et passe une écriture globale pour solder
ceux dont le solde est compris entre -0.03€ et +0.03€ (hors 0).

  - Solde débiteur → crédit 411, débit 658
  - Solde créditeur → débit 411, crédit 758

Usage :
  python solde_411.py --cron                    # Production (date du jour)
  python solde_411.py --date 2026-03-17         # Date spécifique
  python solde_411.py --test                    # Simulation
  python solde_411.py --test --seuil 0.05       # Seuil personnalisé
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from datetime import datetime, timedelta, timezone

# =============================================================
# CONFIGURATION
# =============================================================
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE         = "https://app.pennylane.com/api/external/v2"
PL_HEADERS      = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

COMPTE_658   = "658000"   # Charges diverses de gestion courante
COMPTE_758   = "758000"   # Produits divers de gestion courante
JOURNAL_CODE = "OD"       # Journal des opérations diverses
SEUIL        = 0.03       # Seuil par défaut en euros

# Comptes 411 de transit (pas des comptes clients individuels)
COMPTES_EXCLUS = {"411INTERNET", "411MOLLIE", "411KLARNA", "411NA"}

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
log = logging.getLogger("solde_411")

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
                log.info(f"   ⏳ Rate limit — pause {wait}s...")
                time.sleep(wait)
            else:
                log.error(f"❌ GET {endpoint}: {resp.status_code} {resp.text}")
                return all_items
        else:
            log.error(f"❌ GET {endpoint}: rate limit persistant")
            return all_items

        data  = resp.json()
        items = data.get("items", [])
        all_items.extend(items)

        if page % 10 == 0:
            log.info(f"   ... {len(all_items)} éléments ({endpoint}, page {page})")

        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    return all_items

# =============================================================
# PENNYLANE — Comptes et journaux
# =============================================================
_accounts_cache = {}

def get_account_id(account_number: str) -> int | None:
    if account_number in _accounts_cache:
        return _accounts_cache[account_number]
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
    resp = requests.get(f"{PL_BASE}/ledger_accounts", headers=PL_HEADERS,
                        params={"filter": filter_param, "per_page": 5}, timeout=30)
    if resp.status_code == 200:
        for item in resp.json().get("items", []):
            if item.get("number") == account_number:
                _accounts_cache[account_number] = item["id"]
                return item["id"]
    log.error(f"❌ Compte '{account_number}' introuvable")
    return None


_journal_cache = {}

def get_journal_id(code: str) -> int | None:
    if code in _journal_cache:
        return _journal_cache[code]
    resp = requests.get(f"{PL_BASE}/journals", headers=PL_HEADERS,
                        params={"per_page": 100}, timeout=30)
    if resp.status_code == 200:
        for j in resp.json().get("items", []):
            if j.get("code", "").upper() == code.upper():
                _journal_cache[code] = j["id"]
                return j["id"]
    log.error(f"❌ Journal '{code}' introuvable")
    return None

# =============================================================
# PENNYLANE — Écritures
# =============================================================
def create_ledger_entry(date: str, label: str, journal_id: int, lines: list, test_mode: bool) -> bool:
    if test_mode:
        log.info(f"🧪 TEST — {label}")
        for l in lines:
            log.info(f"   {l.get('label',''):<50} D:{float(l.get('debit','0')):>10.2f}  C:{float(l.get('credit','0')):>10.2f}")
        return True
    payload = {"date": date, "label": label, "journal_id": journal_id, "ledger_entry_lines": lines}
    resp = requests.post(f"{PL_BASE}/ledger_entries", headers=PL_HEADERS, json=payload, timeout=30)
    if resp.status_code in (200, 201):
        log.info(f"✅ Écriture créée : {label}")
        return True
    log.error(f"❌ Erreur création écriture : {resp.status_code} {resp.text}")
    return False

# =============================================================
# RÉCUPÉRATION DES COMPTES 411 ET CALCUL DES SOLDES
# =============================================================
def get_411_accounts() -> list:
    """Récupère tous les comptes 411XXX depuis Pennylane."""
    filter_param = json.dumps([{"field": "number", "operator": "start_with", "value": "411"}])
    accounts = pl_get_all("ledger_accounts", {"filter": filter_param})
    # Exclure les comptes de transit
    filtered = [a for a in accounts if a.get("number") not in COMPTES_EXCLUS]
    log.info(f"📋 {len(filtered)} compte(s) 411 client(s) trouvé(s) ({len(accounts)} total, {len(accounts) - len(filtered)} exclus)")
    return filtered


def get_account_balance(account_id: int) -> float:
    """Calcule le solde d'un compte en sommant toutes ses écritures."""
    filter_param = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(account_id)}])
    lines = pl_get_all("ledger_entry_lines", {"filter": filter_param})
    total_debit  = sum(float(l.get("debit", 0)) for l in lines)
    total_credit = sum(float(l.get("credit", 0)) for l in lines)
    return round(total_debit - total_credit, 2)

# =============================================================
# BOUCLE PRINCIPALE
# =============================================================
def run(target_date: str, test_mode: bool, seuil: float):
    log.info(f"\n{'#'*60}")
    log.info(f"🧹 Apurement comptes 411 | {target_date} | Seuil: {seuil}€ | Test: {test_mode}")
    log.info(f"{'#'*60}")

    # Résolution des comptes de contrepartie
    compte_658_id = get_account_id(COMPTE_658)
    compte_758_id = get_account_id(COMPTE_758)
    journal_id    = get_journal_id(JOURNAL_CODE)
    if not compte_658_id or not compte_758_id or not journal_id:
        telegram_send("🚨 <b>Apurement 411</b>\n❌ Compte 658/758 ou journal OD introuvable")
        return

    # Récupérer les comptes 411 clients
    accounts = get_411_accounts()
    if not accounts:
        telegram_send(f"ℹ️ <b>Apurement 411</b> | {target_date}\nAucun compte 411 trouvé")
        return

    # Calculer les soldes et filtrer
    small_balances = []
    for acc in accounts:
        acc_id     = acc["id"]
        acc_number = acc.get("number", "?")
        acc_name   = acc.get("name", acc_number)

        balance = get_account_balance(acc_id)
        if balance == 0:
            continue
        if abs(balance) <= seuil:
            log.info(f"   💰 {acc_number} ({acc_name}) : solde {balance:+.2f}€ → à apurer")
            small_balances.append({
                "id": acc_id,
                "number": acc_number,
                "name": acc_name,
                "balance": balance,
            })
        else:
            log.info(f"   ⏭️  {acc_number} ({acc_name}) : solde {balance:+.2f}€ (> seuil)")

    if not small_balances:
        log.info("✅ Aucun écart à apurer")
        telegram_send(f"✅ <b>Apurement 411</b> | {target_date}\nAucun écart ≤ {seuil}€ à apurer")
        return

    # Construire l'écriture globale
    lines = []
    total_658 = 0.0  # total charges (soldes débiteurs apurés)
    total_758 = 0.0  # total produits (soldes créditeurs apurés)

    for item in small_balances:
        bal = item["balance"]
        abs_bal = abs(bal)

        if bal > 0:
            # Solde débiteur → crédit 411 pour solder, débit 658
            lines.append({
                "ledger_account_id": item["id"],
                "debit":  "0.00",
                "credit": f"{abs_bal:.2f}",
                "label":  f"Apurement écart {item['number']}",
            })
            total_658 += abs_bal
        else:
            # Solde créditeur → débit 411 pour solder, crédit 758
            lines.append({
                "ledger_account_id": item["id"],
                "debit":  f"{abs_bal:.2f}",
                "credit": "0.00",
                "label":  f"Apurement écart {item['number']}",
            })
            total_758 += abs_bal

    # Lignes de contrepartie globales
    if total_658 > 0:
        lines.append({
            "ledger_account_id": compte_658_id,
            "debit":  f"{total_658:.2f}",
            "credit": "0.00",
            "label":  f"Apurement écarts clients {target_date}",
        })
    if total_758 > 0:
        lines.append({
            "ledger_account_id": compte_758_id,
            "debit":  "0.00",
            "credit": f"{total_758:.2f}",
            "label":  f"Apurement écarts clients {target_date}",
        })

    # Vérification équilibre
    total_d = sum(float(l["debit"]) for l in lines)
    total_c = sum(float(l["credit"]) for l in lines)
    if abs(total_d - total_c) > 0.01:
        log.error(f"❌ Écriture déséquilibrée D:{total_d:.2f} C:{total_c:.2f}")
        telegram_send(f"🚨 <b>Apurement 411</b>\n❌ Écriture déséquilibrée D:{total_d:.2f} C:{total_c:.2f}")
        return

    label  = f"Apurement écarts clients {target_date}"
    result = create_ledger_entry(target_date, label, journal_id, lines, test_mode)

    # Résumé
    details = [f"{item['number']} : {item['balance']:+.2f}€" for item in small_balances]
    if result:
        tg_msg = (
            f"✅ <b>Apurement 411</b> | {target_date}\n"
            f"🧹 {len(small_balances)} compte(s) apuré(s)\n"
            f"📊 658: {total_658:.2f}€ | 758: {total_758:.2f}€\n\n"
            + "\n".join(details[:20])
        )
        if test_mode:
            tg_msg = "🧪 [TEST] " + tg_msg
    else:
        tg_msg = f"🚨 <b>Apurement 411</b> | {target_date}\n❌ Échec création écriture"

    telegram_send(tg_msg)
    log.info(f"\n🏁 {len(small_balances)} compte(s) apuré(s) — 658: {total_658:.2f}€ | 758: {total_758:.2f}€")

# =============================================================
# POINT D'ENTRÉE
# =============================================================
def main():
    parser = argparse.ArgumentParser(description="Apurement écarts centimes sur comptes 411")
    parser.add_argument("--date",  help="Date cible YYYY-MM-DD (défaut: aujourd'hui)")
    parser.add_argument("--test",  action="store_true", help="Mode test (simulation)")
    parser.add_argument("--cron",  action="store_true", help="Mode cron (production)")
    parser.add_argument("--seuil", type=float, default=SEUIL, help=f"Seuil en euros (défaut: {SEUIL})")
    args = parser.parse_args()

    if not args.date and not args.cron:
        parser.print_help()
        sys.exit(1)

    if args.cron:
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        target_date = args.date

    run(target_date, args.test, args.seuil)


if __name__ == "__main__":
    main()

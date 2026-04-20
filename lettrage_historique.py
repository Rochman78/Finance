"""
lettrage_historique.py
Script one-shot pour lettrer les comptes 411 historiques (2025) soldés via OD.
Cherche les comptes avec écritures "SOLDE CLIENTS SHOPIFY", vérifie solde = 0, lettre tout.

Usage:
    python lettrage_historique.py --test     # Simulation
    python lettrage_historique.py            # Réel
"""

import os, json, time, re, logging, requests, argparse
from dotenv import load_dotenv
from decimal import Decimal

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}
COMPTES_EXCLUS = {"411INTERNET", "411MOLLIE", "411KLARNA", "411NA"}


def pl_get(url, params=None):
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 10))
            continue
        return None
    return None


def pl_get_all(endpoint, params=None):
    all_items = []
    cursor = None
    while True:
        p = {"limit": 100}
        if params: p.update(params)
        if cursor: p["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/{endpoint}", p)
        if not data: break
        all_items.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"): break
        cursor = data["next_cursor"]
    return all_items


def letter_lines(line_ids):
    for attempt in range(5):
        try:
            resp = requests.post(
                f"{PL_BASE}/ledger_entry_lines/lettering",
                headers=PL_HEADERS,
                json={
                    "unbalanced_lettering_strategy": "none",
                    "ledger_entry_lines": [{"id": lid} for lid in line_ids],
                },
                timeout=60,
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 10))
                continue
            log.error(f"❌ Lettrage échoué: {resp.status_code} — {resp.text[:300]}")
            return False
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
    return False


def run(test_mode=False):
    log.info(f"=== Lettrage historique — comptes 411 soldés via OD {'(TEST)' if test_mode else ''} ===")

    # 1. Charger tous les comptes 411
    log.info("Chargement des comptes 411...")
    filter_param = json.dumps([{"field": "number", "operator": "start_with", "value": "411"}])
    accounts = pl_get_all("ledger_accounts", {"filter": filter_param})
    accounts = [a for a in accounts if a.get("number") not in COMPTES_EXCLUS]
    log.info(f"  {len(accounts)} comptes 411 trouvés")

    # 2. Pour chaque compte, charger les lignes et chercher "SOLDE CLIENTS SHOPIFY"
    candidates = []
    lettered_count = 0
    skipped_count = 0
    error_count = 0
    already_lettered = 0

    for i, acc in enumerate(accounts):
        acc_id = acc["id"]
        acc_num = acc.get("number", "")
        acc_label = acc.get("label", "")

        fl = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(acc_id)}])
        lines = pl_get_all("ledger_entry_lines", {"filter": fl})

        if not lines:
            continue

        # Vérifier s'il y a une écriture "SOLDE CLIENTS SHOPIFY"
        has_solde_od = False
        for l in lines:
            entry_id = l.get("ledger_entry", {}).get("id")
            if entry_id:
                # Vérifier le label via un GET rapide
                # Pour optimiser, on regarde le label de la ligne d'abord
                line_label = l.get("label", "")
                if "SOLDE CLIENTS SHOPIFY" in line_label.upper():
                    has_solde_od = True
                    break

        # Si pas trouvé dans les labels de ligne, checker les labels d'écriture
        if not has_solde_od:
            for l in lines:
                entry_id = l.get("ledger_entry", {}).get("id")
                if not entry_id:
                    continue
                detail = pl_get(f"{PL_BASE}/ledger_entries/{entry_id}")
                if detail and "SOLDE CLIENTS SHOPIFY" in detail.get("label", "").upper():
                    has_solde_od = True
                    break

        if not has_solde_od:
            continue

        # Filtrer les lignes non lettrées
        unlettered = [l for l in lines if not l.get("lettered_ledger_entry_lines", {}).get("ids", [])]

        if not unlettered:
            already_lettered += 1
            continue

        # Calculer le solde cumulé (toutes les lignes, pas juste les non lettrées)
        total_debit = sum(Decimal(str(l.get("debit", 0))) for l in lines)
        total_credit = sum(Decimal(str(l.get("credit", 0))) for l in lines)
        solde = (total_debit - total_credit).quantize(Decimal("0.01"))

        # Solde des lignes non lettrées
        ul_debit = sum(Decimal(str(l.get("debit", 0))) for l in unlettered)
        ul_credit = sum(Decimal(str(l.get("credit", 0))) for l in unlettered)
        ul_solde = (ul_debit - ul_credit).quantize(Decimal("0.01"))

        if ul_solde != 0:
            log.info(f"  {acc_num} ({acc_label}) — solde non lettré = {ul_solde}€ → skip")
            skipped_count += 1
            continue

        # Lettrer toutes les lignes non lettrées ensemble
        line_ids = [l["id"] for l in unlettered]

        if test_mode:
            log.info(f"  [TEST] {acc_num} ({acc_label}) — {len(line_ids)} lignes à lettrer, solde non lettré = {ul_solde}€")
            lettered_count += 1
        else:
            if letter_lines(line_ids):
                log.info(f"  ✅ {acc_num} ({acc_label}) — {len(line_ids)} lignes lettrées")
                lettered_count += 1
            else:
                error_count += 1

        if (i + 1) % 100 == 0:
            log.info(f"  ... {i + 1}/{len(accounts)} comptes scannés")

    log.info(f"\n=== Résultat ===")
    log.info(f"  Comptes scannés : {len(accounts)}")
    log.info(f"  Déjà lettrés : {already_lettered}")
    log.info(f"  Lettrés : {lettered_count}")
    log.info(f"  Skippés (solde ≠ 0) : {skipped_count}")
    log.info(f"  Erreurs : {error_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    run(test_mode=args.test)

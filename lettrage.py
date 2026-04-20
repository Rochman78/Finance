"""
lettrage.py
Module de lettrage automatique des comptes clients 411 dans Pennylane.

Logique :
1. Charger les lignes non lettrées d'un compte 411
2. Pour chaque ligne, remonter à l'écriture parente pour extraire la ref commande
3. Grouper les lignes par ref commande
4. Pour chaque groupe : lettrer facture + encaissement + OD arrondi si besoin
5. Gère les avoirs (credit) + remboursements (debit) de la même façon

Endpoint lettrage Pennylane :
POST /api/external/v2/ledger_entry_lines/lettering
Body: {"unbalanced_lettering_strategy": "none", "ledger_entry_lines": [{"id": ...}]}
"""

import os, json, time, re, logging, requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE         = "https://app.pennylane.com/api/external/v2"
PL_HEADERS      = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

ORDER_PREFIXES = r'(?:LFC|RDC|HC|COCO|MO|RM|TZ|LVO|UNIV)'
ARRONDI_MAX = Decimal("0.03")
COMPTES_EXCLUS = {"411INTERNET", "411MOLLIE", "411KLARNA", "411NA"}


# =============================================================
# API HELPERS
# =============================================================
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
        log.error(f"❌ GET {url}: {resp.status_code}")
        return None
    return None


def pl_get_all(endpoint, params=None):
    all_items = []
    cursor = None
    while True:
        p = {"limit": 100}
        if params:
            p.update(params)
        if cursor:
            p["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/{endpoint}", p)
        if not data:
            break
        all_items.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return all_items


# =============================================================
# LETTRAGE API
# =============================================================
def letter_lines(line_ids, strategy="none"):
    """
    Lettre un groupe de lignes ensemble.
    strategy: "none" = refuse si déséquilibré, "partial" = accepte quand même
    """
    resp = requests.post(
        f"{PL_BASE}/ledger_entry_lines/lettering",
        headers=PL_HEADERS,
        json={
            "unbalanced_lettering_strategy": strategy,
            "ledger_entry_lines": [{"id": lid} for lid in line_ids],
        },
        timeout=30,
    )
    if resp.status_code == 200:
        return True
    log.error(f"❌ Lettrage échoué: {resp.status_code} — {resp.text[:300]}")
    return False


# =============================================================
# EXTRACTION REF COMMANDE
# =============================================================
_entry_cache = {}

_invoice_cache = {}

def _get_order_ref_from_line(line):
    """Extrait la ref commande depuis le label de la ligne, le label de l'écriture,
    ou la special_mention de la facture Pennylane."""

    # 1. Chercher dans le label de la ligne
    line_label = line.get("label", "")
    m = re.search(ORDER_PREFIXES + r'\d{3,}', line_label, re.IGNORECASE)
    if m:
        return m.group(0).upper()

    # 2. Charger l'écriture parente
    entry_id = line.get("ledger_entry", {}).get("id")
    if not entry_id:
        return None

    if entry_id not in _entry_cache:
        detail = pl_get(f"{PL_BASE}/ledger_entries/{entry_id}")
        _entry_cache[entry_id] = detail

    detail = _entry_cache[entry_id]
    if not detail:
        return None

    entry_label = detail.get("label", "")

    # 3. Chercher dans le label de l'écriture
    m = re.search(ORDER_PREFIXES + r'\d{3,}', entry_label, re.IGNORECASE)
    if m:
        return m.group(0).upper()

    # 4. Si c'est une facture → extraire le numéro de facture → charger la customer_invoice → special_mention
    inv_match = re.search(r'(F-\d{4}-\d{2}-\d{2}-\d+)', entry_label)
    if inv_match:
        inv_number = inv_match.group(1)
        if inv_number not in _invoice_cache:
            filter_inv = json.dumps([{"field": "invoice_number", "operator": "eq", "value": inv_number}])
            inv_data = pl_get(f"{PL_BASE}/customer_invoices", {"filter": filter_inv, "limit": 1})
            if inv_data and inv_data.get("items"):
                _invoice_cache[inv_number] = inv_data["items"][0]
            else:
                _invoice_cache[inv_number] = None

        inv = _invoice_cache.get(inv_number)
        if inv:
            text = f"{inv.get('special_mention', '')} {inv.get('label', '')}"
            m = re.search(ORDER_PREFIXES + r'\d{3,}', text, re.IGNORECASE)
            if m:
                return m.group(0).upper()

    return None


# =============================================================
# LOGIQUE DE LETTRAGE D'UN COMPTE 411
# =============================================================
def letter_account(account_id, account_number="", account_label="", test_mode=False):
    """
    Lettre automatiquement les lignes d'un compte 411 :
    1. Charge les lignes non lettrées
    2. Extrait la ref commande de chaque ligne
    3. Groupe par ref commande
    4. Pour chaque groupe équilibré : lettre

    Gère les factures (débit) + encaissements (crédit)
    ET les avoirs (crédit) + remboursements (débit)
    """
    log.info(f"Lettrage {account_number} ({account_label})")

    # Charger toutes les lignes du compte
    filter_lines = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(account_id)}])
    all_lines = pl_get_all("ledger_entry_lines", {"filter": filter_lines})

    # Filtrer les lignes non lettrées
    unlettered = []
    for l in all_lines:
        lettered_ids = l.get("lettered_ledger_entry_lines", {}).get("ids", [])
        if not lettered_ids:
            unlettered.append(l)

    if not unlettered:
        log.info(f"  Toutes les lignes sont déjà lettrées")
        return {"lettered": 0, "skipped": 0, "errors": 0}

    log.info(f"  {len(unlettered)} ligne(s) non lettrée(s) sur {len(all_lines)}")

    # Extraire la ref commande de chaque ligne
    lines_with_ref = []
    lines_without_ref = []

    for l in unlettered:
        ref = _get_order_ref_from_line(l)
        d = float(l.get("debit", 0))
        c = float(l.get("credit", 0))
        entry = {
            "id": l["id"],
            "debit": Decimal(str(d)),
            "credit": Decimal(str(c)),
            "date": l.get("date", ""),
            "ref": ref,
            "label": l.get("label", ""),
        }
        if ref:
            lines_with_ref.append(entry)
        else:
            lines_without_ref.append(entry)

    # Grouper par ref commande
    groups = defaultdict(list)
    for entry in lines_with_ref:
        groups[entry["ref"]].append(entry)

    lettered_count = 0
    skipped_count = 0
    error_count = 0

    for ref, group_lines in groups.items():
        total_debit = sum(e["debit"] for e in group_lines)
        total_credit = sum(e["credit"] for e in group_lines)
        ecart = (total_debit - total_credit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        line_ids = [e["id"] for e in group_lines]

        if ecart == 0:
            # Équilibré → lettrage direct
            if test_mode:
                log.info(f"  [TEST] {ref} : {len(line_ids)} lignes, équilibré → lettrage")
                lettered_count += 1
                continue

            if letter_lines(line_ids):
                log.info(f"  ✅ {ref} : {len(line_ids)} lignes lettrées")
                lettered_count += 1
            else:
                error_count += 1

        elif abs(ecart) <= ARRONDI_MAX:
            # Petit écart → chercher une OD d'arrondi parmi les lignes sans ref
            arrondi_line = _find_arrondi_line(lines_without_ref, ecart)

            if arrondi_line:
                line_ids.append(arrondi_line["id"])
                lines_without_ref.remove(arrondi_line)

                if test_mode:
                    log.info(f"  [TEST] {ref} : {len(line_ids)} lignes (dont OD arrondi {ecart}€) → lettrage")
                    lettered_count += 1
                    continue

                if letter_lines(line_ids):
                    log.info(f"  ✅ {ref} : {len(line_ids)} lignes lettrées (arrondi {ecart}€)")
                    lettered_count += 1
                else:
                    error_count += 1
            else:
                # Pas de ligne d'arrondi trouvée → skip, on ne force pas
                log.warning(f"  ⚠️ {ref} : écart {ecart}€, pas d'OD trouvée → skip")
                skipped_count += 1
        else:
            log.warning(f"  ⚠️ {ref} : écart {ecart}€ > {ARRONDI_MAX}€ → skip")
            skipped_count += 1

    return {"lettered": lettered_count, "skipped": skipped_count, "errors": error_count}


def _find_arrondi_line(lines_without_ref, ecart):
    """
    Cherche parmi les lignes sans ref commande celle qui correspond à l'écart.
    ecart > 0 → on cherche un crédit du même montant (ou un débit si écart < 0)
    """
    ecart_abs = abs(ecart)

    for line in lines_without_ref:
        if ecart > 0:
            # Facture > encaissement → il faut un crédit pour compenser
            if abs(line["credit"] - ecart_abs) < Decimal("0.005"):
                return line
        else:
            # Encaissement > facture → il faut un débit pour compenser
            if abs(line["debit"] - ecart_abs) < Decimal("0.005"):
                return line

    return None


# =============================================================
# LETTRAGE EN MASSE
# =============================================================
def _has_unlettered_lines(account_id):
    """Vérifie rapidement si un compte a au moins une ligne non lettrée."""
    filter_lines = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(account_id)}])
    lines = pl_get(f"{PL_BASE}/ledger_entry_lines", {"filter": filter_lines, "limit": 20})
    if not lines:
        return False
    for l in lines.get("items", []):
        if not l.get("lettered_ledger_entry_lines", {}).get("ids", []):
            return True
    return False


def letter_all_411_from(min_account_id=0, limit=5000, test_mode=False):
    """
    Parcourt les comptes 411 créés à partir de min_account_id.
    Ne traite que ceux qui ont des lignes non lettrées.
    """
    log.info(f"=== Lettrage automatique des comptes 411 (id >= {min_account_id}) {'(TEST MODE)' if test_mode else ''} ===")

    filter_param = json.dumps([{"field": "number", "operator": "start_with", "value": "411"}])
    accounts = pl_get_all("ledger_accounts", {"filter": filter_param})
    accounts = [a for a in accounts if a.get("number") not in COMPTES_EXCLUS and a["id"] >= min_account_id]
    accounts.sort(key=lambda a: a.get("id", 0), reverse=True)
    accounts = accounts[:limit]

    log.info(f"{len(accounts)} comptes 411 trouvés, filtrage des comptes avec lignes non lettrées...")

    # Pré-filtrer : ne garder que les comptes avec au moins une ligne non lettrée
    to_process = []
    for i, acc in enumerate(accounts):
        if _has_unlettered_lines(acc["id"]):
            to_process.append(acc)
        if (i + 1) % 100 == 0:
            log.info(f"  ... scan {i + 1}/{len(accounts)}, {len(to_process)} à traiter")

    log.info(f"{len(to_process)} comptes avec lignes non lettrées (sur {len(accounts)} scannés)")

    total_lettered = 0
    total_skipped = 0
    total_errors = 0

    for i, acc in enumerate(to_process):
        result = letter_account(
            account_id=acc["id"],
            account_number=acc.get("number", ""),
            account_label=acc.get("label", ""),
            test_mode=test_mode,
        )
        total_lettered += result["lettered"]
        total_skipped += result["skipped"]
        total_errors += result["errors"]

        if (i + 1) % 50 == 0:
            log.info(f"  ... {i + 1}/{len(to_process)} comptes traités")

    log.info(f"\n=== Résultat ===")
    log.info(f"  Comptes scannés : {len(accounts)}")
    log.info(f"  Comptes traités : {len(to_process)}")
    log.info(f"  Groupes lettrés : {total_lettered}")
    log.info(f"  Groupes skippés : {total_skipped}")
    log.info(f"  Erreurs : {total_errors}")

    return {"lettered": total_lettered, "skipped": total_skipped, "errors": total_errors}


def letter_all_411(limit=500, test_mode=False):
    """
    Parcourt tous les comptes 411 et lettre automatiquement.
    """
    log.info(f"=== Lettrage automatique des comptes 411 {'(TEST MODE)' if test_mode else ''} ===")

    filter_param = json.dumps([{"field": "number", "operator": "start_with", "value": "411"}])
    accounts = pl_get_all("ledger_accounts", {"filter": filter_param})
    accounts = [a for a in accounts if a.get("number") not in COMPTES_EXCLUS]
    accounts.sort(key=lambda a: a.get("id", 0), reverse=True)
    accounts = accounts[:limit]

    log.info(f"{len(accounts)} comptes 411 à traiter")

    total_lettered = 0
    total_skipped = 0
    total_errors = 0

    for i, acc in enumerate(accounts):
        result = letter_account(
            account_id=acc["id"],
            account_number=acc.get("number", ""),
            account_label=acc.get("label", ""),
            test_mode=test_mode,
        )
        total_lettered += result["lettered"]
        total_skipped += result["skipped"]
        total_errors += result["errors"]

        if (i + 1) % 50 == 0:
            log.info(f"  ... {i + 1}/{len(accounts)} comptes traités")

    log.info(f"\n=== Résultat ===")
    log.info(f"  Lettrés : {total_lettered}")
    log.info(f"  Skippés (écart trop grand) : {total_skipped}")
    log.info(f"  Erreurs : {total_errors}")

    return {"lettered": total_lettered, "skipped": total_skipped, "errors": total_errors}


# =============================================================
# MAIN
# =============================================================
MIN_ACCOUNT_ID = 2315484594  # À partir de 411109875 (Anais Aune)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Simulation sans lettrer")
    parser.add_argument("--limit", type=int, default=5000, help="Nombre max de comptes")
    parser.add_argument("--account", type=str, default=None, help="Lettrer un seul compte (numéro)")
    args = parser.parse_args()

    if args.account:
        filter_param = json.dumps([{"field": "number", "operator": "eq", "value": args.account}])
        accs = pl_get_all("ledger_accounts", {"filter": filter_param})
        if accs:
            acc = accs[0]
            letter_account(acc["id"], acc.get("number", ""), acc.get("label", ""), test_mode=args.test)
        else:
            log.error(f"Compte {args.account} non trouvé")
    else:
        letter_all_411_from(min_account_id=MIN_ACCOUNT_ID, limit=args.limit, test_mode=args.test)

"""
cadrage_frais_shopify.py
Cadrage des frais Shopify Payments : Shopify API vs Pennylane (compte 627001, journal ENCSP)

Le module :
1. Récupère les frais Shopify (API) et Pennylane (627001 ENCSP)
2. Catégorise chaque ligne 627001 Pennylane (Shopify / Mollie / Klarna / Écart / Autre)
3. Compare uniquement Shopify vs Shopify
4. Explique l'écart (frais Mollie/Klarna, lignes d'écart 471, commandes non matchées)
5. Si --fix : crée les écritures de correction (débit 471, crédit 411 + débit 627001)

Usage:
    python cadrage_frais_shopify.py --date-min 2026-04-01 --date-max 2026-04-01
    python cadrage_frais_shopify.py --date-min 2026-04-01 --date-max 2026-04-01 --fix
"""

import os, json, time, re, logging, argparse, requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- CONFIG ---
SHOPIFY_API_VERSION = "2026-01"
PENNYLANE_TOKEN     = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE             = "https://app.pennylane.com/api/external/v2"
PL_HEADERS          = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}
COMPTE_FRAIS        = "627001"
COMPTE_ECART        = "471"
JOURNAL_ENCSP_ID    = 13509687

STORES = [
    {"name": "LFC",  "store": "mon-filet-de-camouflage.myshopify.com", "client_id": "16d136da2babe857d91f3814b57c6028", "client_secret": os.environ.get("SHOPIFY_SECRET_LFC",  "")},
    {"name": "RED",  "store": "red-de-camuflaje.myshopify.com",        "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "client_secret": os.environ.get("SHOPIFY_SECRET_RED",  "")},
    {"name": "HET",  "store": "het-camouflagenet.myshopify.com",       "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "client_secret": os.environ.get("SHOPIFY_SECRET_HET",  "")},
    {"name": "MTC",  "store": "coconets.myshopify.com",                "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "client_secret": os.environ.get("SHOPIFY_SECRET_MTC",  "")},
    {"name": "MO",   "store": "mon-ombrage.myshopify.com",             "client_id": "55b0cff935270c2545020ecc7fa4704c", "client_secret": os.environ.get("SHOPIFY_SECRET_MO",   "")},
    {"name": "RETE", "store": "rete-mimetica.myshopify.com",           "client_id": "c948511fe38f27931b77caf611f53d06", "client_secret": os.environ.get("SHOPIFY_SECRET_RETE", "")},
    {"name": "TZ",   "store": "tarnnetz.myshopify.com",                "client_id": "e6627287d6a9eb12b54344321ec337f1", "client_secret": os.environ.get("SHOPIFY_SECRET_TZ",   "")},
    {"name": "LVO",  "store": "le-filet-camouflage-1.myshopify.com",  "client_id": "d7a49b87859af74774aaa7c39d212a27", "client_secret": os.environ.get("SHOPIFY_SECRET_LVO",  "")},
    {"name": "UNIV", "store": "univers-camouflage.myshopify.com",      "client_id": "51d4100024e40f174341e73d78e0cbbb", "client_secret": os.environ.get("SHOPIFY_SECRET_UNIV", "")},
]

# =============================================================
# HELPERS API
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

def pl_get(url: str, params: dict = None) -> dict | None:
    for attempt in range(3):
        resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 10))
            continue
        log.error(f"❌ Pennylane GET {url}: {resp.status_code} {resp.text[:200]}")
        return None
    return None

def pl_get_account_id(number: str) -> int | None:
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": number}])
    data = pl_get(f"{PL_BASE}/ledger_accounts", {"filter": filter_param, "limit": 5})
    if data:
        for item in data.get("items", []):
            if item.get("number") == number:
                return item["id"]
    return None

# =============================================================
# SHOPIFY — Frais réels par boutique
# =============================================================
def get_frais_shopify_fast(date_min: str, date_max: str) -> dict:
    """Version rapide : frais agrégés par boutique et par jour, SANS appel GET /orders/{id}.
    Retourne {store: {frais, nb_payouts, by_date: {date: {frais, nb_txns}}}}"""
    result = {}
    for store_config in STORES:
        sname = store_config["name"]
        token = get_shopify_token(store_config)
        if not token:
            result[sname] = {"frais": None, "nb_payouts": 0, "by_date": {}}
            continue

        url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/payouts.json"
        data = shopify_get(url, token, {"date_min": date_min, "date_max": date_max})
        payouts = data.get("payouts", []) if data else []

        total_frais = 0.0
        by_date = {}
        for payout in payouts:
            pid = payout["id"]
            pdate = payout["date"]
            t_url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance/transactions.json"
            txns_data = shopify_get(t_url, token, {"payout_id": pid, "limit": 250})
            txns = txns_data.get("transactions", []) if txns_data else []
            for txn in txns:
                if txn.get("type") == "payout":
                    continue
                fee = float(txn.get("fee") or 0)
                if fee != 0:
                    total_frais += abs(fee)
                    if pdate not in by_date:
                        by_date[pdate] = {"frais": 0.0, "nb_txns": 0}
                    by_date[pdate]["frais"] += abs(fee)
                    by_date[pdate]["nb_txns"] += 1

        if payouts:
            log.info(f"[{sname}] Frais Shopify : {total_frais:.2f}€ ({len(payouts)} payouts)")
        result[sname] = {"frais": round(total_frais, 2), "nb_payouts": len(payouts), "by_date": by_date}
    return result


def get_frais_shopify_detail(date_min: str, date_max: str) -> dict:
    """Version détaillée : avec appel GET /orders/{id} pour chaque transaction (lent).
    Retourne {store: {frais, nb_payouts, transactions: [{order_id, order_name, amount, fee}]}}"""
    result = {}
    for store_config in STORES:
        sname = store_config["name"]
        token = get_shopify_token(store_config)
        if not token:
            result[sname] = {"frais": None, "nb_payouts": 0, "transactions": []}
            continue

        url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/payouts.json"
        data = shopify_get(url, token, {"date_min": date_min, "date_max": date_max})
        payouts = data.get("payouts", []) if data else []

        txn_details = []
        total_frais = 0.0
        for payout in payouts:
            pid = payout["id"]
            pdate = payout["date"]
            t_url = f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/shopify_payments/balance/transactions.json"
            txns_data = shopify_get(t_url, token, {"payout_id": pid, "limit": 250})
            txns = txns_data.get("transactions", []) if txns_data else []
            for txn in txns:
                if txn.get("type") == "payout":
                    continue
                fee = float(txn.get("fee") or 0)
                amount = float(txn.get("amount") or 0)
                order_id = txn.get("source_order_id")
                order_name = None
                if order_id:
                    odata = shopify_get(
                        f"https://{store_config['store']}/admin/api/{SHOPIFY_API_VERSION}/orders/{order_id}.json",
                        token, {"fields": "name,customer"})
                    if odata and "order" in odata:
                        raw = odata["order"].get("name", "").replace("#", "").replace("-", "")
                        m = re.match(r'([A-Za-z]{0,5}\d+)', raw)
                        order_name = m.group(1).upper() if m else raw.upper()
                        cust = odata["order"].get("customer", {})
                        customer_name = f'{cust.get("first_name", "")} {cust.get("last_name", "")}'.strip()
                    else:
                        customer_name = "?"
                else:
                    customer_name = "?"
                if fee != 0:
                    total_frais += abs(fee)
                    txn_details.append({
                        "order_name": order_name,
                        "customer_name": customer_name,
                        "amount": amount,
                        "fee": abs(fee),
                        "payout_date": pdate,
                    })

        if payouts:
            log.info(f"[{sname}] Frais Shopify : {total_frais:.2f}€ ({len(payouts)} payouts, {len(txn_details)} txns)")
        result[sname] = {"frais": round(total_frais, 2), "nb_payouts": len(payouts), "transactions": txn_details}
    return result

# =============================================================
# PENNYLANE — Lignes 627001 catégorisées
# =============================================================
def get_pennylane_627_detail(date_min: str, date_max: str) -> dict:
    """
    Retourne toutes les lignes 627001 du journal ENCSP, catégorisées :
    - shopify : ligne avec ref commande Shopify (LFC, RED, HC, COCO, TZ, MO, RETE, LVO, UNIV)
    - mollie  : label contient 'Mollie'
    - klarna  : label contient 'Klarna'
    - ecart   : label contient 'Écart' ou 'cart'
    - autre   : non catégorisé
    """
    filter_param = json.dumps([
        {"field": "date", "operator": "gteq", "value": date_min},
        {"field": "date", "operator": "lteq", "value": date_max},
        {"field": "journal_id", "operator": "eq", "value": JOURNAL_ENCSP_ID},
    ])

    entries = []
    cursor = None
    while True:
        params = {"filter": filter_param, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/ledger_entries", params)
        if not data:
            break
        entries.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break

    log.info(f"Pennylane : {len(entries)} écriture(s) ENCSP sur la période")

    lines_627 = {"shopify": [], "mollie": [], "klarna": [], "ecart": [], "autre": []}
    # Also collect écart lines on 471 for context
    ecart_471_lines = []
    # Collect matched order refs
    matched_orders = set()

    for entry in entries:
        detail = pl_get(f"{PL_BASE}/ledger_entries/{entry['id']}")
        if not detail:
            continue
        entry_label = detail.get("label", "")
        entry_date = detail.get("date", "")

        for line in detail.get("ledger_entry_lines", []):
            acc = line.get("ledger_account", {}).get("number", "")
            label = line.get("label", "")
            debit = float(line.get("debit") or 0)
            credit = float(line.get("credit") or 0)
            net = debit - credit

            if acc == COMPTE_ECART:
                ecart_471_lines.append({
                    "entry_id": entry["id"], "entry_label": entry_label,
                    "date": entry_date, "label": label, "debit": debit, "credit": credit,
                })

            if acc != COMPTE_FRAIS:
                continue

            info = {"entry_id": entry["id"], "entry_label": entry_label,
                    "date": entry_date, "label": label, "debit": debit, "credit": credit, "net": net}

            # Extract order ref from label — match only known store prefixes
            ORDER_PREFIXES = r'(?:LFC|RDC|HC|COCO|MO|RM|TZ|LVO|UNIV)'
            m = re.search(ORDER_PREFIXES + r'\d{3,}', label, re.IGNORECASE)
            if m:
                ref = m.group(0).upper()
                info["order_ref"] = ref
                matched_orders.add(ref)

            # Catégoriser
            label_lower = label.lower()
            entry_label_lower = entry_label.lower()
            if "mollie" in label_lower or "mollie" in entry_label_lower:
                lines_627["mollie"].append(info)
            elif "klarna" in label_lower or "klarna" in entry_label_lower:
                lines_627["klarna"].append(info)
            elif "écart" in label_lower or "ecart" in label_lower or "ajustement" in label_lower:
                lines_627["ecart"].append(info)
            else:
                lines_627["shopify"].append(info)

    return {
        "lines_627": lines_627,
        "ecart_471": ecart_471_lines,
        "matched_orders": matched_orders,
        "nb_entries": len(entries),
    }

# =============================================================
# CADRAGE
# =============================================================
def cadrage(date_min: str, date_max: str, store_filter: str | None = None, fix: bool = False):
    log.info(f"=== Cadrage frais Shopify Payments — {date_min} → {date_max} ===")

    # 1. Frais Shopify (toutes boutiques, détail par transaction)
    shopify_data = get_frais_shopify_detail(date_min, date_max)
    total_shopify = sum(s["frais"] for s in shopify_data.values() if s["frais"] is not None)

    # 2. Pennylane 627001 catégorisé
    pl_data = get_pennylane_627_detail(date_min, date_max)
    lines = pl_data["lines_627"]

    total_pl_shopify = round(sum(l["net"] for l in lines["shopify"]), 2)
    total_pl_mollie  = round(sum(l["net"] for l in lines["mollie"]), 2)
    total_pl_klarna  = round(sum(l["net"] for l in lines["klarna"]), 2)
    total_pl_ecart   = round(sum(l["net"] for l in lines["ecart"]), 2)
    total_pl_autre   = round(sum(l["net"] for l in lines["autre"]), 2)
    total_pl_all     = round(total_pl_shopify + total_pl_mollie + total_pl_klarna + total_pl_ecart + total_pl_autre, 2)

    ecart_shopify = round(total_shopify - total_pl_shopify, 2)

    # 3. Identifier les commandes Shopify non matchées dans Pennylane
    all_shopify_orders = set()
    for sdata in shopify_data.values():
        for txn in sdata.get("transactions", []):
            if txn.get("order_name"):
                all_shopify_orders.add(txn["order_name"])
    unmatched = all_shopify_orders - pl_data["matched_orders"]

    unmatched_details = []
    unmatched_fees = 0.0
    for sname, sdata in shopify_data.items():
        for txn in sdata.get("transactions", []):
            if txn.get("order_name") in unmatched:
                unmatched_details.append({"store": sname, **txn})
                unmatched_fees += txn["fee"]

    # 4. Affichage
    print("\n" + "=" * 70)
    print(f"  CADRAGE FRAIS SHOPIFY PAYMENTS")
    print(f"  Période : {date_min} → {date_max}")
    print("=" * 70)

    # Détail Shopify par boutique
    print(f"\n  {'Boutique':<10} {'Frais Shopify':>15} {'Payouts':>10} {'Txns':>8}")
    print("  " + "-" * 46)
    display = shopify_data if not store_filter else {k: v for k, v in shopify_data.items() if k == store_filter}
    for sname, sdata in display.items():
        if sdata["frais"] and sdata["frais"] > 0:
            print(f"  {sname:<10} {sdata['frais']:>14.2f}€ {sdata['nb_payouts']:>10} {len(sdata['transactions']):>8}")
    print("  " + "-" * 46)
    print(f"  {'TOTAL SHOPIFY API':<30} {total_shopify:>14.2f}€")

    # Décomposition Pennylane
    print(f"\n  PENNYLANE 627001 / ENCSP — décomposition :")
    print(f"  {'  Shopify (commandes matchées)':<40} {total_pl_shopify:>14.2f}€  ({len(lines['shopify'])} lignes)")
    if total_pl_mollie:
        print(f"  {'  Mollie':<40} {total_pl_mollie:>14.2f}€  ({len(lines['mollie'])} lignes)")
    if total_pl_klarna:
        print(f"  {'  Klarna':<40} {total_pl_klarna:>14.2f}€  ({len(lines['klarna'])} lignes)")
    if total_pl_ecart:
        print(f"  {'  Écarts / Ajustements':<40} {total_pl_ecart:>14.2f}€  ({len(lines['ecart'])} lignes)")
    if total_pl_autre:
        print(f"  {'  Autre (non catégorisé)':<40} {total_pl_autre:>14.2f}€  ({len(lines['autre'])} lignes)")
    print(f"  {'  TOTAL 627001':<40} {total_pl_all:>14.2f}€")

    # Cadrage Shopify pur
    print(f"\n  CADRAGE SHOPIFY :")
    print(f"  {'  Shopify API':<40} {total_shopify:>14.2f}€")
    print(f"  {'  Pennylane 627001 (Shopify seul)':<40} {total_pl_shopify:>14.2f}€")
    print(f"  {'  Écart brut':<40} {ecart_shopify:>14.2f}€")

    # Explication de l'écart
    if unmatched_details:
        print(f"\n  EXPLICATION DE L'ÉCART ({len(unmatched_details)} commande(s) non matchée(s)) :")
        print(f"  {'  Bout.':<8} {'Commande':<14} {'Client':<28} {'Montant':>10} {'Frais':>8}")
        print("  " + "-" * 72)
        for u in unmatched_details:
            print(f"  {u['store']:<8} {u['order_name']:<14} {u['customer_name']:<28} {u['amount']:>10.2f} {u['fee']:>7.2f}")
        print("  " + "-" * 72)
        print(f"  {'  Total frais non matchés':<40} {unmatched_fees:>14.2f}€")

        ecart_residuel = round(ecart_shopify - unmatched_fees, 2)
        if abs(ecart_residuel) < 0.05:
            print(f"\n  ✅ ÉCART TOTALEMENT EXPLIQUÉ par les commandes non matchées")
        else:
            print(f"\n  ⚠️  Écart résiduel après explication : {ecart_residuel:.2f}€")
    elif abs(ecart_shopify) < 0.05:
        print(f"\n  ✅ CADRAGE OK — écart nul")
    else:
        print(f"\n  ❌ ÉCART NON EXPLIQUÉ : {ecart_shopify:.2f}€")

    # Lignes 471 existantes
    if pl_data["ecart_471"]:
        print(f"\n  LIGNES 471 (ÉCARTS) EXISTANTES :")
        for e in pl_data["ecart_471"]:
            sens = f"D:{e['debit']:.2f}" if e["debit"] > 0 else f"C:{e['credit']:.2f}"
            print(f"    {e['date']} | {sens:>12} | {e['entry_label']}")

    print("\n" + "=" * 70)

    # 5. Fix : créer les écritures de correction
    if fix and unmatched_details:
        print("\n  MODE FIX — Création des écritures de correction...")
        _fix_ecarts(unmatched_details, date_min, date_max)

    return {
        "total_shopify": total_shopify,
        "total_pl_shopify": total_pl_shopify,
        "total_pl_mollie": total_pl_mollie,
        "total_pl_klarna": total_pl_klarna,
        "ecart_shopify": ecart_shopify,
        "unmatched": unmatched_details,
    }


# =============================================================
# FIX — Écritures de correction
# =============================================================
def _find_customer_account(order_ref: str, date_min: str, date_max: str) -> dict | None:
    """
    Retrouve le compte 411 client à partir du numéro de commande :
    facture Pennylane (special_mention contient order_ref) → ledger_entry → ligne 411
    """
    # Scan invoices day by day on a wide window
    from datetime import datetime, timedelta
    start = datetime.strptime(date_min, "%Y-%m-%d") - timedelta(days=30)
    end   = datetime.strptime(date_max, "%Y-%m-%d") + timedelta(days=5)
    d = start
    while d <= end:
        day_str = d.strftime("%Y-%m-%d")
        d += timedelta(days=1)
        filter_param = json.dumps([{"field": "date", "operator": "eq", "value": day_str}])
        data = pl_get(f"{PL_BASE}/customer_invoices", {"filter": filter_param, "limit": 100})
        if not data:
            continue
        for inv in data.get("items", []):
            text = f'{inv.get("special_mention", "")} {inv.get("label", "")} {inv.get("filename", "")}'
            if order_ref not in text:
                continue
            # Found the invoice — get its ledger_entry to find the 411 line
            inv_detail = pl_get(f"{PL_BASE}/customer_invoices/{inv['id']}")
            if not inv_detail:
                continue
            ledger_entry = inv_detail.get("ledger_entry") or inv.get("ledger_entry")
            if not ledger_entry or not ledger_entry.get("id"):
                continue
            entry_detail = pl_get(f"{PL_BASE}/ledger_entries/{ledger_entry['id']}")
            if not entry_detail:
                continue
            for line in entry_detail.get("ledger_entry_lines", []):
                acc = line.get("ledger_account", {})
                acc_number = acc.get("number", "")
                if acc_number.startswith("411") and acc_number not in ("411INTERNET", "411MOLLIE", "411KLARNA", "411NA"):
                    return {
                        "account_number": acc_number,
                        "account_id": acc.get("id"),
                        "invoice_number": inv.get("invoice_number"),
                    }
    return None


def _fix_ecarts(unmatched_details: list, date_min: str, date_max: str):
    """Crée les écritures de correction : débit 471 + crédit 411 client + débit 627001"""

    ecart_account_id = pl_get_account_id(COMPTE_ECART)
    frais_account_id = pl_get_account_id(COMPTE_FRAIS)
    if not ecart_account_id or not frais_account_id:
        log.error("❌ Compte 471 ou 627001 introuvable")
        return

    # Group by payout date for one correction entry per date
    by_date = {}
    for u in unmatched_details:
        date = u["payout_date"]
        by_date.setdefault(date, []).append(u)

    for date, items in sorted(by_date.items()):
        lines = []
        total_ecart = 0.0
        all_resolved = True

        for item in items:
            order_ref = item["order_name"]
            log.info(f"  Recherche compte 411 pour {order_ref}...")
            customer_info = _find_customer_account(order_ref, date_min, date_max)

            if not customer_info:
                log.warning(f"  ⚠️  {order_ref} : impossible de trouver le compte 411 — skip")
                all_resolved = False
                continue

            amount = item["amount"]
            fee = item["fee"]
            ecart_part = round(amount - fee, 2)
            total_ecart += ecart_part

            # Crédit 411 client (montant vente)
            lines.append({
                "ledger_account_id": customer_info["account_id"],
                "debit": "0.00",
                "credit": f"{amount:.2f}",
                "label": f'{item["customer_name"]} - {order_ref}',
            })
            # Débit 627001 (frais)
            lines.append({
                "ledger_account_id": frais_account_id,
                "debit": f"{fee:.2f}",
                "credit": "0.00",
                "label": f'{item["customer_name"]} - {order_ref}',
            })

        if not lines:
            log.warning(f"  Aucune ligne de correction pour {date}")
            continue

        # Débit 471 pour solder l'écart
        total_credit = sum(float(l["credit"]) for l in lines)
        total_debit = sum(float(l["debit"]) for l in lines)
        ecart_amount = round(total_credit - total_debit, 2)

        lines.insert(0, {
            "ledger_account_id": ecart_account_id,
            "debit": f"{ecart_amount:.2f}",
            "credit": "0.00",
            "label": f"Correction écart Shopify {date}",
        })

        # Vérification équilibre
        total_d = sum(float(l["debit"]) for l in lines)
        total_c = sum(float(l["credit"]) for l in lines)
        if abs(total_d - total_c) > 0.01:
            log.error(f"  ❌ Écriture déséquilibrée D:{total_d:.2f} C:{total_c:.2f} — skip")
            continue

        label = f"Correction écart Shopify {date}"
        payload = {
            "date": date,
            "label": label,
            "journal_id": JOURNAL_ENCSP_ID,
            "ledger_entry_lines": lines,
        }

        print(f"\n  --- {label} ---")
        for l in lines:
            # Resolve account number for display
            d = float(l["debit"])
            c = float(l["credit"])
            print(f"    {l['label']:<50} D:{d:>10.2f}  C:{c:>10.2f}")
        print(f"    {'TOTAL':<50} D:{total_d:>10.2f}  C:{total_c:>10.2f}")

        resp = requests.post(f"{PL_BASE}/ledger_entries", headers=PL_HEADERS, json=payload, timeout=30)
        if resp.status_code in (200, 201):
            print(f"    ✅ Écriture créée dans Pennylane")
        else:
            print(f"    ❌ Erreur: {resp.status_code} {resp.text[:200]}")


# =============================================================
# MAIN
# =============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-min", required=True, help="Date début YYYY-MM-DD")
    parser.add_argument("--date-max", required=True, help="Date fin YYYY-MM-DD")
    parser.add_argument("--store",    default=None,  help="Filtrer affichage sur une boutique (ex: LFC)")
    parser.add_argument("--fix",      action="store_true", help="Créer les écritures de correction dans Pennylane")
    args = parser.parse_args()

    cadrage(args.date_min, args.date_max, args.store, args.fix)

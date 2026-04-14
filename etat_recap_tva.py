"""
etat_recap_tva.py
État récapitulatif de TVA : décomposition du compte 707101 par facture
avec numéro de TVA intracommunautaire pour export ProDouane.

Approche : charge directement les lignes du grand livre 707101, filtre par mois,
puis remonte aux factures et clients.
"""

import os, json, time, re, logging, requests
from dotenv import load_dotenv
from datetime import datetime
import calendar

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE         = "https://app.pennylane.com/api/external/v2"
PL_HEADERS      = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

COMPTE_RECAP = "707101"


def pl_get(url: str, params: dict = None) -> dict | None:
    for attempt in range(6):
        try:
            resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        except requests.exceptions.RequestException as e:
            log.warning(f"⚠️ Connexion error ({e.__class__.__name__}) — retry {attempt+1}/6")
            time.sleep(min(2 ** attempt, 15))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            wait = min(2 ** (attempt + 1), 15)
            log.info(f"⏳ Rate limit — pause {wait}s (attempt {attempt+1}/6)")
            time.sleep(wait)
            continue
        log.error(f"❌ Pennylane GET {url}: {resp.status_code} {resp.text[:200]}")
        return None
    log.error(f"❌ Echec après 6 tentatives: {url}")
    return None


def pl_get_all(endpoint: str, params: dict = None) -> list:
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


def get_etat_recap(year: int, month: int) -> dict:
    """
    1. Récupère tous les comptes 707101 (plusieurs IDs possibles selon vat_rate)
    2. Charge les lignes du grand livre de chaque compte
    3. Filtre par mois
    4. Pour chaque ligne, remonte à l'écriture → facture → client → vat_number
    """
    last_day = calendar.monthrange(year, month)[1]
    date_min = f"{year}-{month:02d}-01"
    date_max = f"{year}-{month:02d}-{last_day:02d}"

    log.info(f"État récap TVA — compte {COMPTE_RECAP} — {date_min} → {date_max}")

    # Étape 1 : trouver tous les comptes 707101
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": COMPTE_RECAP}])
    all_accounts = pl_get_all("ledger_accounts", {"filter": filter_param})
    log.info(f"  {len(all_accounts)} compte(s) 707101 trouvé(s)")

    # Étape 2 : charger les lignes de chaque compte
    all_entry_lines = []
    for acc in all_accounts:
        acc_id = acc["id"]
        filter_lines = json.dumps([
            {"field": "ledger_account_id", "operator": "eq", "value": str(acc_id)},
        ])
        lines = pl_get_all("ledger_entry_lines", {"filter": filter_lines})
        # Filtrer par mois
        for l in lines:
            if date_min <= l.get("date", "") <= date_max:
                l["_acc_id"] = acc_id
                all_entry_lines.append(l)

    log.info(f"  {len(all_entry_lines)} ligne(s) sur le mois")

    # Étape 3 : pour chaque ligne, remonter à l'écriture puis à la facture
    all_lines = []
    entry_cache = {}
    invoice_cache = {}
    customer_cache = {}

    for el in all_entry_lines:
        debit = float(el.get("debit") or 0)
        credit = float(el.get("credit") or 0)
        montant_ht = round(credit - debit, 2)

        if montant_ht >= 0:
            regime = 21
        else:
            regime = 25

        entry_id = el.get("ledger_entry", {}).get("id")

        # Charger l'écriture pour le label (contient le n° de facture)
        invoice_number = ""
        if entry_id:
            if entry_id not in entry_cache:
                detail = pl_get(f"{PL_BASE}/ledger_entries/{entry_id}")
                entry_cache[entry_id] = detail
            detail = entry_cache[entry_id]
            if detail:
                entry_label = detail.get("label", "")
                inv_match = re.search(r'(F-\d{4}-\d{2}-\d{2}-\d+)', entry_label)
                invoice_number = inv_match.group(1) if inv_match else ""

        # Chercher le client via la facture
        client_name = ""
        vat_number = ""

        if invoice_number and invoice_number not in invoice_cache:
            time.sleep(0.3)  # Eviter le rate limit
            filter_inv = json.dumps([{"field": "invoice_number", "operator": "eq", "value": invoice_number}])
            inv_data = pl_get(f"{PL_BASE}/customer_invoices", {"filter": filter_inv, "limit": 1})
            if inv_data and inv_data.get("items"):
                cust_url = inv_data["items"][0].get("customer", {}).get("url", "")
                invoice_cache[invoice_number] = cust_url
            else:
                log.warning(f"⚠️ Facture {invoice_number} non trouvée via API")
                invoice_cache[invoice_number] = ""

        if invoice_number:
            cust_url = invoice_cache.get(invoice_number, "")
            if cust_url:
                if cust_url not in customer_cache:
                    time.sleep(0.3)  # Eviter le rate limit
                    cust = pl_get(cust_url)
                    if cust:
                        name = cust.get("name", "") or f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip()
                        vat = cust.get("vat_number", "") or ""
                        customer_cache[cust_url] = {"name": name, "vat": vat}
                    else:
                        log.warning(f"⚠️ Customer non chargé: {cust_url}")
                        customer_cache[cust_url] = {"name": "", "vat": ""}
                client_name = customer_cache[cust_url]["name"]
                vat_number = customer_cache[cust_url]["vat"]

        all_lines.append({
            "invoice_number": invoice_number,
            "date": el.get("date", ""),
            "client_name": client_name,
            "vat_number": vat_number,
            "montant_ht": montant_ht,
            "regime": regime,
        })

    # Ne garder que les lignes avec un numéro de facture (factures Pennylane uniquement)
    all_lines = [l for l in all_lines if l.get("invoice_number")]

    total_ht = round(sum(l["montant_ht"] for l in all_lines), 2)

    log.info(f"  Total HT : {total_ht:.2f}€, {len(all_lines)} ligne(s)")

    return {
        "total_ht": total_ht,
        "nb_lignes": len(all_lines),
        "lines": all_lines,
        "date_min": date_min,
        "date_max": date_max,
    }

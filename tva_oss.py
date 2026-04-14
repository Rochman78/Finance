"""
tva_oss.py
TVA OSS — Contrôle Base par Taux
Vérifie que pour chaque pays OSS : CA HT (compte 70703XX) × taux TVA = TVA collectée (compte 44572XX)
Données purement Pennylane.
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


def pl_get(url: str, params: dict = None) -> dict | None:
    for attempt in range(6):
        try:
            resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 15))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** (attempt + 1), 15))
            continue
        log.error(f"❌ Pennylane GET {url}: {resp.status_code} {resp.text[:200]}")
        return None
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


# =============================================================
# MAPPING PAYS OSS
# =============================================================
OSS_COUNTRIES = [
    {"code": "DE", "pays": "Allemagne",      "taux": 19.0, "compte_produit": "7070301", "compte_tva": "44572001"},
    {"code": "AT", "pays": "Autriche",        "taux": 20.0, "compte_produit": "7070302", "compte_tva": "44572002"},
    {"code": "BE", "pays": "Belgique",         "taux": 21.0, "compte_produit": "7070303", "compte_tva": "44572003"},
    {"code": "HR", "pays": "Croatie",          "taux": 25.0, "compte_produit": "7070306", "compte_tva": "44572006"},
    {"code": "DK", "pays": "Danemark",         "taux": 25.0, "compte_produit": "7070307", "compte_tva": "44572007"},
    {"code": "ES", "pays": "Espagne",          "taux": 21.0, "compte_produit": "7070308", "compte_tva": "44572008"},
    {"code": "GR", "pays": "Grèce",            "taux": 24.0, "compte_produit": "7070311", "compte_tva": "44572011"},
    {"code": "HU", "pays": "Hongrie",          "taux": 27.0, "compte_produit": "7070312", "compte_tva": "44572012"},
    {"code": "IE", "pays": "Irlande",          "taux": 23.0, "compte_produit": "7070313", "compte_tva": "44572013"},
    {"code": "IT", "pays": "Italie",           "taux": 22.0, "compte_produit": "7070314", "compte_tva": "44572014"},
    {"code": "LU", "pays": "Luxembourg",       "taux": 17.0, "compte_produit": "7070317", "compte_tva": "44572017"},
    {"code": "NL", "pays": "Pays-Bas",         "taux": 21.0, "compte_produit": "7070319", "compte_tva": "44572019"},
    {"code": "PT", "pays": "Portugal",         "taux": 23.0, "compte_produit": "7070321", "compte_tva": "44572021"},
    {"code": "CZ", "pays": "Rép. Tchèque",    "taux": 21.0, "compte_produit": "7070322", "compte_tva": "44572022"},
    {"code": "RO", "pays": "Roumanie",         "taux": 19.0, "compte_produit": "7070323", "compte_tva": "44572023"},
    {"code": "SI", "pays": "Slovénie",         "taux": 22.0, "compte_produit": "7070325", "compte_tva": "44572025"},
    {"code": "SE", "pays": "Suède",            "taux": 25.0, "compte_produit": "7070326", "compte_tva": "44572026"},
]


def _get_account_total(account_number: str, date_min: str, date_max: str) -> float:
    """Récupère le total (credit - debit) d'un compte sur une période."""
    # Trouver tous les IDs de ce compte
    filter_param = json.dumps([{"field": "number", "operator": "eq", "value": account_number}])
    accounts = pl_get_all("ledger_accounts", {"filter": filter_param})
    if not accounts:
        return 0.0

    total = 0.0
    for acc in accounts:
        acc_id = acc["id"]
        filter_lines = json.dumps([
            {"field": "ledger_account_id", "operator": "eq", "value": str(acc_id)},
        ])
        lines = pl_get_all("ledger_entry_lines", {"filter": filter_lines})
        for l in lines:
            d = l.get("date", "")
            if date_min <= d <= date_max:
                credit = float(l.get("credit", 0))
                debit = float(l.get("debit", 0))
                total += credit - debit

    return round(total, 2)


def get_controle_base_taux(date_min: str, date_max: str) -> dict:
    """
    Pour chaque pays OSS, récupère :
    - CA HT depuis le compte produit (70703XX)
    - TVA collectée depuis le compte TVA (44572XX)
    - Calcule TVA théorique = CA HT × taux
    - Écart = TVA collectée - TVA théorique
    """
    log.info(f"Contrôle Base × Taux OSS — {date_min} → {date_max}")

    rows = []
    for country in OSS_COUNTRIES:
        log.info(f"  {country['pays']} ({country['code']})...")

        ca_ht = _get_account_total(country["compte_produit"], date_min, date_max)
        tva_collectee = _get_account_total(country["compte_tva"], date_min, date_max)

        taux = country["taux"]
        tva_theorique = round(ca_ht * taux / 100, 2)
        ecart = round(tva_collectee - tva_theorique, 2)
        if abs(ecart) < 0.10:
            ecart = 0.0

        if ca_ht != 0 or tva_collectee != 0:
            rows.append({
                "code": country["code"],
                "pays": country["pays"],
                "taux": taux,
                "compte_produit": country["compte_produit"],
                "compte_tva": country["compte_tva"],
                "ca_ht": ca_ht,
                "tva_collectee": tva_collectee,
                "tva_theorique": tva_theorique,
                "ecart": ecart,
            })

    total_ca = round(sum(r["ca_ht"] for r in rows), 2)
    total_tva = round(sum(r["tva_collectee"] for r in rows), 2)
    total_theo = round(sum(r["tva_theorique"] for r in rows), 2)
    total_ecart = round(sum(r["ecart"] for r in rows), 2)

    log.info(f"  Total CA HT: {total_ca:.2f}€, TVA collectée: {total_tva:.2f}€, TVA théorique: {total_theo:.2f}€, Écart: {total_ecart:.2f}€")

    return {
        "rows": rows,
        "total_ca": total_ca,
        "total_tva": total_tva,
        "total_theo": total_theo,
        "total_ecart": total_ecart,
        "date_min": date_min,
        "date_max": date_max,
    }

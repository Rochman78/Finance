#!/usr/bin/env python3
"""
=============================================================
AUDIT DOUBLONS DE FACTURATION — commandes facturées plusieurs fois
=============================================================
Symptôme visé : un solde débiteur injustifié sur un compte client.

Mécanisme : une commande facturée 2 fois pose 2 débits au 411, alors
que l'encaissement Shopify n'est lettré qu'UNE fois (db_loader indexe
`invoices` avec order_number en clé primaire, donc une seule facture
par commande survit, et shopify_pennylane.py ne crédite que celle-là).
Le doublon reste donc éternellement débiteur du montant de la facture.

Lecture seule — aucun écrit dans Pennylane.

Usage :
  python audit_doublons_factures.py                          # 12 derniers mois
  python audit_doublons_factures.py --depuis 2025-01-01
  python audit_doublons_factures.py --depuis 2024-01-01 --tout   # + brouillons
  python audit_doublons_factures.py --sans-soldes             # rapide, sans lecture du 411
"""

import os
import sys
import re
import csv
import json
import time
import logging
import argparse
import requests
from collections import defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

# Statuts qui pèsent réellement au 411. `cancelled` et `archived` sont déjà
# neutralisés (avoir lié / pièce sortie), les inclure produirait des faux
# positifs sur le workflow légitime « j'annule et je réémets ».
STATUTS_ACTIFS = {"paid", "late", "upcoming", "partially_paid"}

EXPORT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "exports", "doublons_factures.csv")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("audit_doublons")


# =============================================================
# API
# =============================================================
def pl_get(endpoint, params=None):
    for attempt in range(6):
        try:
            resp = requests.get(f"{PL_BASE}/{endpoint}", headers=PL_HEADERS, params=params, timeout=45)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 15))
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 15))
            continue
        log.error(f"GET {endpoint}: {resp.status_code} — {resp.text[:200]}")
        return None
    return None


def pl_get_all(endpoint, params=None):
    """Pagination curseur complète."""
    items, cursor = [], None
    while True:
        p = dict(params or {})
        p["limit"] = 100
        if cursor:
            p["cursor"] = cursor
        data = pl_get(endpoint, p)
        if not data:
            break
        items.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return items


# =============================================================
# EXTRACTION DE LA RÉFÉRENCE COMMANDE
# =============================================================
# Le service de facturation écrit « Commande #LFC31629 » dans special_mention.
# On privilégie ce motif ancré sur le mot-clé : le fallback « #PREFIXE+chiffres »
# sur le label attrape aussi des numéros de facture, donc on ne l'utilise qu'en
# dernier recours.
_RE_CMD = re.compile(r'Commande\s+#?([A-Za-z]{2,6})-?(\d{3,7})')
_RE_LOOSE = re.compile(r'#([A-Z]{2,6})-?(\d{3,7})')


def extraire_commande(inv):
    m = _RE_CMD.search(inv.get("special_mention") or "")
    if m:
        return (m.group(1) + m.group(2)).upper()
    for champ in (inv.get("external_reference") or "", inv.get("label") or ""):
        m = _RE_CMD.search(champ) or _RE_LOOSE.search(champ.upper())
        if m:
            return (m.group(1) + m.group(2)).upper()
    return None


def montant(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# =============================================================
# COLLECTE
# =============================================================
def mois_iter(depuis, jusqua):
    d = datetime.strptime(depuis, "%Y-%m-%d").replace(day=1)
    fin = datetime.strptime(jusqua, "%Y-%m-%d")
    while d <= fin:
        suivant = (d.replace(year=d.year + 1, month=1, day=1) if d.month == 12
                   else d.replace(month=d.month + 1, day=1))
        yield d.strftime("%Y-%m-%d"), (suivant - timedelta(days=1)).strftime("%Y-%m-%d")
        d = suivant


def charger_factures(depuis, jusqua):
    """Charge mois par mois : le filtre `date` est le seul indexé côté API et la
    pagination curseur sature sur des fenêtres trop larges."""
    toutes = []
    for debut, fin in mois_iter(depuis, jusqua):
        f = json.dumps([{"field": "date", "operator": "gteq", "value": debut},
                        {"field": "date", "operator": "lteq", "value": fin}])
        lot = pl_get_all("customer_invoices", {"filter": f})
        log.info(f"   📅 {debut[:7]} : {len(lot)} pièce(s)")
        toutes.extend(lot)
    return toutes


def solde_411(customer_id, cache):
    """Solde du compte 411 du client (débit - crédit) sur toute son histoire."""
    if customer_id in cache:
        return cache[customer_id]
    client = pl_get(f"customers/{customer_id}")
    nom = (client or {}).get("name")
    compte = ((client or {}).get("ledger_account") or {}).get("id")
    solde = None
    if compte:
        f = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(compte)}])
        total = 0.0
        for ligne in pl_get_all("ledger_entry_lines", {"filter": f}):
            total += montant(ligne.get("debit")) - montant(ligne.get("credit"))
        solde = round(total, 2)
    cache[customer_id] = (nom, solde)
    return cache[customer_id]


# =============================================================
# RUN
# =============================================================
def run(depuis, jusqua, tout, avec_soldes):
    if not PENNYLANE_TOKEN:
        log.error("❌ PENNYLANE_TOKEN absent")
        return 1

    log.info(f"🔎 Audit doublons de facturation | {depuis} → {jusqua}")
    factures = charger_factures(depuis, jusqua)
    log.info(f"   → {len(factures)} pièce(s) au total")

    groupes = defaultdict(list)
    avoirs = defaultdict(float)
    sans_ref = 0
    for inv in factures:
        m = montant(inv.get("amount"))
        cmd = extraire_commande(inv)
        if not cmd:
            sans_ref += 1
            continue
        if m < 0:
            avoirs[cmd] += -m
            continue
        if not tout:
            if inv.get("draft") or inv.get("archived_at") or inv.get("status") not in STATUTS_ACTIFS:
                continue
        groupes[cmd].append(inv)

    doublons = {c: sorted(v, key=lambda x: x.get("created_at") or "")
                for c, v in groupes.items() if len(v) > 1}
    log.info(f"   {sans_ref} pièce(s) sans référence commande (factures manuelles) — ignorées")
    log.info(f"   {len(groupes)} commande(s) facturée(s), dont {len(doublons)} en doublon")

    if not doublons:
        log.info("✅ Aucun doublon")
        return 0

    cache = {}
    lignes = []
    for cmd, v in doublons.items():
        montants = [montant(x.get("amount")) for x in v]
        # On considère la plus grosse facture comme légitime : le reste est en trop.
        en_trop = round(sum(montants) - max(montants), 2)
        reste_du = round(sum(montant(x.get("remaining_amount_with_tax")) for x in v), 2)
        clients = [str((x.get("customer") or {}).get("id")) for x in v]

        noms, soldes = [], []
        if avec_soldes:
            for cid in dict.fromkeys(clients):
                nom, s = solde_411(cid, cache)
                noms.append(nom or "?")
                soldes.append(s if s is not None else 0.0)

        # SUGGESTION de pièce à avoirer — à confirmer à la main.
        #
        # `remaining_amount_with_tax` ne dit pas la vérité comptable ici : le
        # payout Shopify est crédité au 411 par écriture (shopify_pennylane.py),
        # sans lettrer la facture dans Pennylane. Une facture bel et bien payée
        # reste donc « ouverte » côté facturation. C'est `solde_411` qui donne le
        # montant réellement à annuler.
        #
        # On propose donc les pièces surnuméraires les plus récentes. Les montants
        # étant identiques dans la quasi-totalité des cas, avoirer l'une ou l'autre
        # solde le compte pareil ; la colonne `montants` permet de trancher quand
        # ils diffèrent.
        ouvertes = [x for x in v if montant(x.get("remaining_amount_with_tax")) > 0]
        cibles = (ouvertes[1:] if len(ouvertes) == len(v) else ouvertes) or v[1:]

        crees = [x.get("created_at") or "" for x in v]
        try:
            ecart = int((datetime.fromisoformat(crees[-1].replace("Z", "+00:00"))
                         - datetime.fromisoformat(crees[0].replace("Z", "+00:00"))).total_seconds())
        except ValueError:
            ecart = None

        lignes.append({
            "commande": cmd,
            "nb_factures": len(v),
            "montant_en_trop": f"{en_trop:.2f}",
            "reste_du": f"{reste_du:.2f}",
            "avoirs_deja_emis": f"{avoirs.get(cmd, 0.0):.2f}",
            "solde_411": f"{sum(soldes):.2f}" if avec_soldes else "",
            "client": " / ".join(dict.fromkeys(noms)) if avec_soldes else "",
            "meme_client": len(set(clients)) == 1,
            "suggestion_avoir_numeros": "|".join(str(x.get("invoice_number")) for x in cibles),
            "suggestion_avoir_ids": "|".join(str(x.get("id")) for x in cibles),
            "suggestion_avoir_ttc": f"{sum(montant(x.get('amount')) for x in cibles):.2f}",
            
            "ecart_creation_s": ecart if ecart is not None else "",
            "diagnostic": diagnostic(ecart, montants, len(set(clients)) == 1),
            "dates": "|".join(str(x.get("date")) for x in v),
            "statuts": "|".join(str(x.get("status")) for x in v),
            "montants": "|".join(f"{m:.2f}" for m in montants),
            "numeros": "|".join(str(x.get("invoice_number")) for x in v),
            "ids": "|".join(str(x.get("id")) for x in v),
            "customer_ids": "|".join(clients),
        })

    lignes.sort(key=lambda r: -float(r["montant_en_trop"]))

    os.makedirs(os.path.dirname(EXPORT_CSV), exist_ok=True)
    with open(EXPORT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0].keys()))
        w.writeheader()
        w.writerows(lignes)

    total_trop = sum(float(r["montant_en_trop"]) for r in lignes)
    total_debiteur = sum(float(r["solde_411"]) for r in lignes
                         if r["solde_411"] and float(r["solde_411"]) > 0.03) if avec_soldes else None

    log.info("")
    log.info(f"{'commande':>12} {'n':>2} {'en trop':>9} {'solde 411':>10} {'Δ créa':>9}  diagnostic")
    for r in lignes:
        log.info(f"{r['commande']:>12} {r['nb_factures']:>2} {r['montant_en_trop']:>9} "
                 f"{r['solde_411'] or '-':>10} {str(r['ecart_creation_s']):>9}  {r['diagnostic']}")
    log.info("")
    log.info(f"💸 sur-facturation cumulée      : {total_trop:,.2f} €")
    if total_debiteur is not None:
        log.info(f"⚠️  solde débiteur injustifié    : {total_debiteur:,.2f} €")
    log.info(f"📄 export : {EXPORT_CSV}")
    return 0


def diagnostic(ecart, montants, meme_client):
    """Signature du mécanisme, pour orienter la correction."""
    identiques = len({f"{m:.2f}" for m in montants}) == 1
    if not meme_client:
        return "fiche client dupliquée + refacturation"
    if ecart is None:
        return "indéterminé"
    if ecart <= 300:
        return "double POST quasi-simultané (service de facturation)"
    if ecart <= 86400:
        return "brouillons multiples tous finalisés (retry + finaliser_factures)"
    return "refacturation tardive" + ("" if identiques else " (montants différents — vérifier si légitime)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--depuis", help="Date de début YYYY-MM-DD (défaut : il y a 12 mois)")
    p.add_argument("--jusqua", help="Date de fin YYYY-MM-DD (défaut : aujourd'hui)")
    p.add_argument("--tout", action="store_true",
                   help="Inclure brouillons/annulées/archivées (bruit, mais utile pour voir les rafales de retry)")
    p.add_argument("--sans-soldes", action="store_true",
                   help="Ne pas lire les comptes 411 (beaucoup plus rapide)")
    a = p.parse_args()

    jusqua = a.jusqua or datetime.now().strftime("%Y-%m-%d")
    depuis = a.depuis or (datetime.strptime(jusqua, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
    sys.exit(run(depuis, jusqua, a.tout, not a.sans_soldes))

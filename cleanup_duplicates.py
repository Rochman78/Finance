#!/usr/bin/env python3
"""
Supprime les écritures comptables en doublon dans Pennylane.
Garde la PREMIÈRE écriture pour chaque combinaison (label, date),
supprime les suivantes.

Usage :
  python cleanup_duplicates.py --from 2026-03-26 --to 2026-04-01          # Affiche les doublons
  python cleanup_duplicates.py --from 2026-03-26 --to 2026-04-01 --delete # Supprime les doublons
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from collections import defaultdict

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

JOURNAL_CODE = "ENCSP"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cleanup")


def pl_get_all_entries(date_from: str, date_to: str, journal_id: int) -> list:
    all_items = []
    cursor = None
    while True:
        filter_param = json.dumps([
            {"field": "date", "operator": "gteq", "value": date_from},
            {"field": "date", "operator": "lteq", "value": date_to},
            {"field": "journal_id", "operator": "eq", "value": journal_id},
        ])
        p = {"per_page": 100, "filter": filter_param}
        if cursor:
            p["cursor"] = cursor
        for attempt in range(5):
            resp = requests.get(f"{PL_BASE}/ledger_entries", headers=PL_HEADERS,
                                params=p, timeout=30)
            if resp.status_code == 200:
                break
            if resp.status_code == 429:
                time.sleep(min(2 ** attempt, 10))
            else:
                log.error(f"Erreur API: {resp.status_code} {resp.text[:200]}")
                return all_items
        else:
            return all_items

        data = resp.json()
        all_items.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return all_items


def get_journal_id(code: str) -> int | None:
    resp = requests.get(f"{PL_BASE}/journals", headers=PL_HEADERS,
                        params={"per_page": 100}, timeout=30)
    if resp.status_code == 200:
        for j in resp.json().get("items", []):
            if j.get("code", "").upper() == code.upper():
                return j["id"]
    return None


def delete_entry(entry_id: int) -> bool:
    for attempt in range(3):
        resp = requests.delete(f"{PL_BASE}/ledger_entries/{entry_id}",
                               headers=PL_HEADERS, timeout=30)
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429:
            time.sleep(min(2 ** attempt, 5))
            continue
        log.error(f"Erreur suppression {entry_id}: {resp.status_code} {resp.text[:200]}")
        return False
    return False


def main():
    parser = argparse.ArgumentParser(description="Nettoyage doublons Pennylane")
    parser.add_argument("--from", dest="date_from", required=True, help="Date début YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="Date fin YYYY-MM-DD")
    parser.add_argument("--delete", action="store_true", help="Supprimer les doublons (sans ce flag, affiche seulement)")
    parser.add_argument("--journal", default=JOURNAL_CODE, help=f"Code journal (défaut: {JOURNAL_CODE})")
    args = parser.parse_args()

    journal_id = get_journal_id(args.journal)
    if not journal_id:
        log.error(f"Journal '{args.journal}' introuvable")
        sys.exit(1)

    log.info(f"Recherche des doublons du {args.date_from} au {args.date_to} (journal {args.journal})...")
    entries = pl_get_all_entries(args.date_from, args.date_to, journal_id)
    log.info(f"{len(entries)} écriture(s) trouvée(s)")

    # Grouper par (label, date)
    groups = defaultdict(list)
    for entry in entries:
        key = (entry.get("label", ""), entry.get("date", ""))
        groups[key].append(entry)

    # Trouver les doublons
    duplicates = []
    for (label, date), group in sorted(groups.items()):
        if len(group) > 1:
            # Garder le premier (ID le plus bas), supprimer les autres
            group.sort(key=lambda e: e.get("id", 0))
            keep = group[0]
            to_delete = group[1:]
            log.info(f"  DOUBLON: {label} | {date} — {len(group)} occurrences, {len(to_delete)} à supprimer")
            duplicates.extend(to_delete)

    if not duplicates:
        log.info("Aucun doublon trouvé !")
        return

    log.info(f"\n{'='*60}")
    log.info(f"TOTAL: {len(duplicates)} écriture(s) en doublon à supprimer")

    if not args.delete:
        log.info("Mode simulation. Ajoutez --delete pour supprimer.")
        return

    log.info("Suppression en cours...")
    ok = 0
    err = 0
    for entry in duplicates:
        eid = entry.get("id")
        label = entry.get("label", "?")
        if delete_entry(eid):
            log.info(f"  ✅ Supprimé: {eid} | {label}")
            ok += 1
        else:
            log.error(f"  ❌ Échec: {eid} | {label}")
            err += 1
        time.sleep(0.3)  # Rate limiting

    log.info(f"\n🏁 {ok} supprimée(s), {err} erreur(s)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Identifie les écritures comptables en doublon dans Pennylane.
Garde la PREMIÈRE écriture pour chaque combinaison (label, date),
liste les suivantes à supprimer manuellement dans l'interface Pennylane.

Note : L'API Pennylane ne propose pas d'endpoint DELETE pour les écritures.
       La suppression doit se faire via l'interface web.

Usage :
  python cleanup_duplicates.py --from 2026-03-26 --to 2026-04-01
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from datetime import datetime, timedelta
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
        p = {"limit": 100, "filter": filter_param}
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
                        params={"limit": 100}, timeout=30)
    if resp.status_code == 200:
        for j in resp.json().get("items", []):
            if j.get("code", "").upper() == code.upper():
                return j["id"]
    return None


def main():
    parser = argparse.ArgumentParser(description="Identification doublons Pennylane")
    parser.add_argument("--from", dest="date_from", required=True, help="Date début YYYY-MM-DD")
    parser.add_argument("--to", dest="date_to", required=True, help="Date fin YYYY-MM-DD")
    parser.add_argument("--journal", default=JOURNAL_CODE, help=f"Code journal (défaut: {JOURNAL_CODE})")
    args = parser.parse_args()

    journal_id = get_journal_id(args.journal)
    if not journal_id:
        log.error(f"Journal '{args.journal}' introuvable")
        sys.exit(1)

    log.info(f"Recherche des doublons du {args.date_from} au {args.date_to} (journal {args.journal})...")
    # Load day-by-day to avoid Pennylane pagination limits
    entries = []
    d = datetime.strptime(args.date_from, "%Y-%m-%d")
    end = datetime.strptime(args.date_to, "%Y-%m-%d")
    while d <= end:
        day_str = d.strftime("%Y-%m-%d")
        batch = pl_get_all_entries(day_str, day_str, journal_id)
        if batch:
            log.info(f"  {day_str}: {len(batch)} écriture(s)")
        entries.extend(batch)
        d += timedelta(days=1)
    log.info(f"{len(entries)} écriture(s) trouvée(s) au total")

    # Grouper par (label, date)
    groups = defaultdict(list)
    for entry in entries:
        key = (entry.get("label", ""), entry.get("date", ""))
        groups[key].append(entry)

    # Trouver les doublons
    duplicates = []
    for (label, date), group in sorted(groups.items()):
        if len(group) > 1:
            group.sort(key=lambda e: e.get("id", 0))
            to_delete = group[1:]
            log.info(f"  DOUBLON: {label} | {date} — {len(group)} occurrences, {len(to_delete)} à supprimer")
            duplicates.extend(to_delete)

    if not duplicates:
        log.info("Aucun doublon trouvé !")
        return

    log.info(f"\n{'='*60}")
    log.info(f"TOTAL: {len(duplicates)} écriture(s) en doublon à supprimer")
    log.info(f"{'='*60}")
    log.info("")
    log.info("L'API Pennylane ne permet pas de supprimer les écritures via API.")
    log.info("Supprimez-les manuellement dans Pennylane > Comptabilité > Écritures.")
    log.info("")
    log.info(f"{'ID':<12} {'DATE':<12} {'LABEL'}")
    log.info(f"{'-'*12} {'-'*12} {'-'*50}")
    for entry in duplicates:
        eid = entry.get("id", "?")
        date = entry.get("date", "?")
        label = entry.get("label", "?")
        log.info(f"{eid:<12} {date:<12} {label}")


if __name__ == "__main__":
    main()

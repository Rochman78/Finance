"""
cadrage_encaissements.py
Cadrage des encaissements : détecte les comptes clients 411 non soldés
alors que Shopify a bien effectué le payout.

Lit depuis la base SQLite (encaissements.db) chargée par load_encaissements_db.py.
"""

import os, sqlite3, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "encaissements.db")


def db_exists() -> bool:
    return os.path.exists(DB_PATH)


def run_cadrage_encaissements(progress_cb=None) -> dict:
    """Lit les données depuis la base SQLite."""
    if not db_exists():
        log.error("Base encaissements.db introuvable")
        return {"anomalies": [], "ok": [], "no_order": [], "total_accounts": 0, "total_unsettled": 0, "source": "none"}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM encaissements ORDER BY statut, store, order_ref").fetchall()

    total_unsettled = conn.execute("SELECT COUNT(*) FROM encaissements").fetchone()[0]
    loaded_at_row = conn.execute("SELECT loaded_at FROM encaissements LIMIT 1").fetchone()
    loaded_at = loaded_at_row[0] if loaded_at_row else ""

    # Lire le nombre total de comptes analysés depuis la table meta
    try:
        total_analyzed = int(conn.execute("SELECT value FROM meta WHERE key='total_accounts_analyzed'").fetchone()[0])
        total_ok = int(conn.execute("SELECT value FROM meta WHERE key='total_soldes_ok'").fetchone()[0])
    except Exception:
        total_analyzed = total_unsettled
        total_ok = 0

    # Période et bornes
    first = conn.execute("SELECT account_number, account_label, invoice_date FROM encaissements ORDER BY invoice_date ASC LIMIT 1").fetchone()
    last = conn.execute("SELECT account_number, account_label, invoice_date FROM encaissements ORDER BY invoice_date DESC LIMIT 1").fetchone()
    conn.close()

    period_min = first["invoice_date"] if first else ""
    period_max = last["invoice_date"] if last else ""
    first_account = f"{first['account_number']} — {first['account_label']}" if first else ""
    last_account = f"{last['account_number']} — {last['account_label']}" if last else ""

    anomalies = []
    ok = []

    for r in rows:
        entry = dict(r)
        if entry["statut"] == "anomalie":
            anomalies.append(entry)
        else:
            ok.append(entry)

    log.info(f"DB: {len(anomalies)} anomalie(s), {len(ok)} en attente (chargé le {loaded_at})")

    return {
        "anomalies": anomalies,
        "ok": ok,
        "no_order": [],
        "total_accounts": total_analyzed,
        "total_ok": total_ok,
        "total_unsettled": total_unsettled,
        "loaded_at": loaded_at,
        "period_min": period_min,
        "period_max": period_max,
        "first_account": first_account,
        "last_account": last_account,
        "source": "db",
    }

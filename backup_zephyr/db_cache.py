"""
db_cache.py
Cache SQLite pour stocker les résultats des appels API.
Chaque résultat est stocké en JSON avec une clé unique (ex: "ca_shopify_2026-03-01_2026-03-15").
"""

import os, json, sqlite3, logging
from datetime import datetime

log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT,
            loaded_at TEXT
        )
    """)
    return conn


def get_cached(key: str):
    """Retourne les données en cache ou None. Cherche aussi dans une période plus large."""
    if not os.path.exists(DB_PATH):
        return None
    conn = _get_conn()
    # Exact match
    row = conn.execute("SELECT value, loaded_at FROM cache WHERE key = ?", (key,)).fetchone()
    if row:
        conn.close()
        log.info(f"Cache hit: {key} (chargé le {row[1]})")
        return json.loads(row[0])

    # Chercher une période plus large qui contient la période demandée
    # Clé format: "prefix_YYYY-MM-DD_YYYY-MM-DD"
    parts = key.rsplit("_", 2)
    if len(parts) == 3:
        prefix = parts[0]
        rows = conn.execute("SELECT key, value, loaded_at FROM cache WHERE key LIKE ?", (f"{prefix}_%",)).fetchall()
        for r in rows:
            cached_parts = r[0].rsplit("_", 2)
            if len(cached_parts) == 3:
                cached_min, cached_max = cached_parts[1], cached_parts[2]
                req_min, req_max = parts[1], parts[2]
                if cached_min <= req_min and cached_max >= req_max:
                    conn.close()
                    log.info(f"Cache hit (période englobante): {r[0]} pour {key}")
                    return json.loads(r[1])

    conn.close()
    return None


def set_cached(key: str, data):
    """Stocke les données en cache."""
    conn = _get_conn()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR REPLACE INTO cache (key, value, loaded_at) VALUES (?, ?, ?)",
        (key, json.dumps(data, ensure_ascii=False, default=str), now)
    )
    conn.commit()
    conn.close()
    log.info(f"Cache set: {key}")


def get_loaded_at(key: str) -> str | None:
    """Retourne la date de chargement d'une clé."""
    if not os.path.exists(DB_PATH):
        return None
    conn = _get_conn()
    row = conn.execute("SELECT loaded_at FROM cache WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def cache_exists() -> bool:
    return os.path.exists(DB_PATH)


def list_keys() -> list:
    """Liste toutes les clés en cache."""
    if not os.path.exists(DB_PATH):
        return []
    conn = _get_conn()
    rows = conn.execute("SELECT key, loaded_at FROM cache ORDER BY key").fetchall()
    conn.close()
    return [{"key": r[0], "loaded_at": r[1]} for r in rows]

"""
One-shot sync script: Render PostgreSQL → Supabase PostgreSQL
Tables: shopify_orders, invoices, customers, processed_mollie_settlements

Usage:
    python3 sync_render_to_supabase.py
"""

import psycopg2
import psycopg2.extras
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Connection strings (hardcoded for one-shot migration)
# ---------------------------------------------------------------------------
RENDER_DSN = {
    "host": "dpg-d6k0pt0gjchc73bsj430-a.frankfurt-postgres.render.com",
    "dbname": "zephyr_finance",
    "user": "zephyr_finance_user",
    "password": "bGLupE3rchYBVsJ95wENjamRw8jbojGN",
}

SUPABASE_DSN = {
    "host": "db.kcyvobamexsengqwtonp.supabase.co",
    "port": 5432,
    "dbname": "postgres",
    "user": "postgres",
    "password": "J_sz2HfQ3*+Te8K",
}

BATCH_SIZE = 500

# Store name → boutique slug mapping (Render uses abbreviations)
STORE_SLUG_MAP = {
    "LFC": "LFC",
    "RED": "RED",
    "HET": "HET",
    "COCO": "MTC",   # Coconets
    "LOV": "LVO",    # Le Filet Camouflage (LVO slug)
    "RETE": "RETE",
    "TAR": "TZ",     # Tarnnetz
}


def load_boutique_mapping(supa_cur):
    """Load boutiques from Supabase and build store_name → boutique_id dict."""
    supa_cur.execute("SELECT id, slug FROM boutiques")
    slug_to_id = {row[1]: row[0] for row in supa_cur.fetchall()}

    mapping = {}
    for store_name, slug in STORE_SLUG_MAP.items():
        if slug in slug_to_id:
            mapping[store_name] = slug_to_id[slug]
        else:
            print(f"  WARNING: slug '{slug}' for store '{store_name}' not found in boutiques")
    return mapping


def upsert_batch(cur, sql, rows):
    """Execute batch upsert using execute_values."""
    psycopg2.extras.execute_values(cur, sql, rows, page_size=BATCH_SIZE)


def sync_shopify_orders(render_cur, supa_cur, supa_conn, boutique_map):
    """Sync shopify_orders with boutique_id enrichment."""
    render_cur.execute("SELECT payment_id, order_name, store_name, billing_name, created_at FROM shopify_orders")
    rows = render_cur.fetchall()
    total = len(rows)

    sql = """
        INSERT INTO shopify_orders (payment_id, order_name, store_name, billing_name, created_at, boutique_id)
        VALUES %s
        ON CONFLICT (payment_id) DO UPDATE SET
            order_name = EXCLUDED.order_name,
            store_name = EXCLUDED.store_name,
            billing_name = EXCLUDED.billing_name,
            created_at = EXCLUDED.created_at,
            boutique_id = EXCLUDED.boutique_id
    """

    transformed = []
    unmapped = set()
    for row in rows:
        payment_id, order_name, store_name, billing_name, created_at = row
        boutique_id = boutique_map.get(store_name)
        if boutique_id is None and store_name not in unmapped:
            unmapped.add(store_name)
        transformed.append((payment_id, order_name, store_name, billing_name, created_at, boutique_id))

    if unmapped:
        print(f"  WARNING: unmapped store_names: {unmapped}")

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return total


def sync_invoices(render_cur, supa_cur, supa_conn):
    """Sync invoices with dossier_client_id = 1."""
    render_cur.execute("SELECT order_number, customer_name, customer_id, invoice_id, invoice_number, amount FROM invoices")
    rows = render_cur.fetchall()
    total = len(rows)

    sql = """
        INSERT INTO invoices (order_number, customer_name, customer_id, invoice_id, invoice_number, amount, dossier_client_id)
        VALUES %s
        ON CONFLICT (order_number) DO UPDATE SET
            customer_name = EXCLUDED.customer_name,
            customer_id = EXCLUDED.customer_id,
            invoice_id = EXCLUDED.invoice_id,
            invoice_number = EXCLUDED.invoice_number,
            amount = EXCLUDED.amount,
            dossier_client_id = EXCLUDED.dossier_client_id
    """

    transformed = [(r[0], r[1], r[2], r[3], r[4], r[5], 1) for r in rows]

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return total


def sync_customers(render_cur, supa_cur, supa_conn):
    """Sync customers with dossier_client_id = 1."""
    render_cur.execute("SELECT id, name, ledger_account_id FROM customers")
    rows = render_cur.fetchall()
    total = len(rows)

    sql = """
        INSERT INTO customers (id, name, ledger_account_id, dossier_client_id)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            ledger_account_id = EXCLUDED.ledger_account_id,
            dossier_client_id = EXCLUDED.dossier_client_id
    """

    transformed = [(r[0], r[1], r[2], 1) for r in rows]

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return total


def sync_processed_mollie_settlements(render_cur, supa_cur, supa_conn, boutique_map):
    """Sync processed_mollie_settlements with boutique_id enrichment.
    Uses mollie_id as the conflict key (UNIQUE constraint in Supabase).
    """
    render_cur.execute("""
        SELECT mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status
        FROM processed_mollie_settlements
    """)
    rows = render_cur.fetchall()
    total = len(rows)

    # Use mollie_id as conflict target since it has a UNIQUE constraint
    sql = """
        INSERT INTO processed_mollie_settlements (mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status, boutique_id)
        VALUES %s
        ON CONFLICT (mollie_id) DO UPDATE SET
            store_name = EXCLUDED.store_name,
            payment_ref = EXCLUDED.payment_ref,
            order_name = EXCLUDED.order_name,
            invoice_number = EXCLUDED.invoice_number,
            amount = EXCLUDED.amount,
            processed_at = EXCLUDED.processed_at,
            status = EXCLUDED.status,
            boutique_id = EXCLUDED.boutique_id
    """

    transformed = []
    unmapped = set()
    for row in rows:
        mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status = row
        boutique_id = boutique_map.get(store_name)
        if boutique_id is None and store_name and store_name not in unmapped:
            unmapped.add(store_name)
        transformed.append((mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status, boutique_id))

    if unmapped:
        print(f"  WARNING: unmapped store_names: {unmapped}")

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return total


def main():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Starting Render → Supabase sync")
    print("=" * 60)

    # Connect to both databases
    print("Connecting to Render...")
    render_conn = psycopg2.connect(**RENDER_DSN)
    render_cur = render_conn.cursor()

    print("Connecting to Supabase...")
    supa_conn = psycopg2.connect(**SUPABASE_DSN)
    supa_cur = supa_conn.cursor()

    # Load boutique mapping
    print("Loading boutique mapping from Supabase...")
    boutique_map = load_boutique_mapping(supa_cur)
    print(f"  Mapped stores: {boutique_map}")
    print()

    # Sync each table
    results = {}

    print("[1/4] Syncing shopify_orders...")
    results["shopify_orders"] = sync_shopify_orders(render_cur, supa_cur, supa_conn, boutique_map)
    print(f"  ✓ {results['shopify_orders']} rows upserted")

    print("[2/4] Syncing invoices...")
    results["invoices"] = sync_invoices(render_cur, supa_cur, supa_conn)
    print(f"  ✓ {results['invoices']} rows upserted")

    print("[3/4] Syncing customers...")
    results["customers"] = sync_customers(render_cur, supa_cur, supa_conn)
    print(f"  ✓ {results['customers']} rows upserted")

    print("[4/4] Syncing processed_mollie_settlements...")
    results["processed_mollie_settlements"] = sync_processed_mollie_settlements(render_cur, supa_cur, supa_conn, boutique_map)
    print(f"  ✓ {results['processed_mollie_settlements']} rows upserted")

    # Summary
    print()
    print("=" * 60)
    print("SYNC COMPLETE")
    print("=" * 60)
    for table, count in results.items():
        print(f"  {table:40s} {count:>8,} rows")
    print(f"  {'TOTAL':40s} {sum(results.values()):>8,} rows")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Done.")

    # Cleanup
    render_cur.close()
    render_conn.close()
    supa_cur.close()
    supa_conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

"""
Incremental sync script: Render PostgreSQL -> Supabase PostgreSQL
Runs every 15 minutes via external cron/scheduler.
Syncs only new rows since last sync for each table.

Tables: shopify_orders, invoices, customers, processed_mollie_settlements

Incremental strategies:
- shopify_orders: created_at > last_synced_value (timestamp-based, no id column)
- invoices: invoice_id > last_synced_value (invoice_id is sequential bigint)
- customers: id > last_synced_value (has id column)
- processed_mollie_settlements: id > last_synced_value (has id column)

Usage:
    python3 sync_render_to_supabase_cron.py
"""

import psycopg2
import psycopg2.extras
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Connection strings
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

# Store name -> boutique slug mapping (Render uses abbreviations)
STORE_SLUG_MAP = {
    "LFC": "LFC",
    "RED": "RED",
    "HET": "HET",
    "COCO": "MTC",   # Coconets
    "LOV": "LVO",    # Le Filet Camouflage (LVO slug)
    "RETE": "RETE",
    "TAR": "TZ",     # Tarnnetz
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def ensure_sync_state_table(supa_cur, supa_conn):
    """Create the sync_state table in Supabase if it doesn't exist."""
    supa_cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            table_name TEXT PRIMARY KEY,
            last_synced_id BIGINT NOT NULL DEFAULT 0,
            last_sync_at TIMESTAMPTZ
        )
    """)
    supa_conn.commit()


def get_last_synced_id(supa_cur, table_name):
    """Get the last synced id for a table. Returns 0 if not yet tracked."""
    supa_cur.execute("SELECT last_synced_id FROM sync_state WHERE table_name = %s", (table_name,))
    row = supa_cur.fetchone()
    return row[0] if row else 0


def update_sync_state(supa_cur, supa_conn, table_name, max_id):
    """Update sync_state with the max id processed."""
    now = datetime.now(timezone.utc)
    supa_cur.execute("""
        INSERT INTO sync_state (table_name, last_synced_id, last_sync_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (table_name) DO UPDATE SET
            last_synced_id = EXCLUDED.last_synced_id,
            last_sync_at = EXCLUDED.last_sync_at
    """, (table_name, max_id, now))
    supa_conn.commit()


def load_boutique_mapping(supa_cur):
    """Load boutiques from Supabase and build store_name -> boutique_id dict."""
    supa_cur.execute("SELECT id, slug FROM boutiques")
    slug_to_id = {row[1]: row[0] for row in supa_cur.fetchall()}

    mapping = {}
    for store_name, slug in STORE_SLUG_MAP.items():
        if slug in slug_to_id:
            mapping[store_name] = slug_to_id[slug]
        else:
            log(f"  WARNING: slug '{slug}' for store '{store_name}' not found in boutiques")
    return mapping


def upsert_batch(cur, sql, rows):
    """Execute batch upsert using execute_values."""
    psycopg2.extras.execute_values(cur, sql, rows, page_size=BATCH_SIZE)


def sync_shopify_orders(render_cur, supa_cur, supa_conn, boutique_map, last_id):
    """Sync new shopify_orders using invoice_id-like tracking via created_at epoch.

    Since shopify_orders has no id column, we use a synthetic approach:
    - Store the count of rows in Render as last_synced_id
    - On each run, fetch all rows from Render and upsert (ON CONFLICT is idempotent)
    - Track total row count to detect new rows

    Actually, we use created_at as a monotonic cursor. We store the epoch (seconds)
    of the last synced created_at as last_synced_id.
    """
    # Convert last_id (epoch seconds) back to timestamp for filtering
    if last_id > 0:
        from_ts = datetime.fromtimestamp(last_id)
        render_cur.execute(
            "SELECT payment_id, order_name, store_name, billing_name, created_at "
            "FROM shopify_orders WHERE created_at > %s ORDER BY created_at",
            (from_ts,)
        )
    else:
        render_cur.execute(
            "SELECT payment_id, order_name, store_name, billing_name, created_at "
            "FROM shopify_orders ORDER BY created_at"
        )

    rows = render_cur.fetchall()
    if not rows:
        return 0, last_id

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

    max_ts_epoch = last_id
    transformed = []
    for row in rows:
        payment_id, order_name, store_name, billing_name, created_at = row
        boutique_id = boutique_map.get(store_name)
        transformed.append((payment_id, order_name, store_name, billing_name, created_at, boutique_id))
        if created_at:
            epoch = int(created_at.timestamp())
            if epoch > max_ts_epoch:
                max_ts_epoch = epoch

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return len(rows), max_ts_epoch


def sync_invoices(render_cur, supa_cur, supa_conn, last_id):
    """Sync new invoices using invoice_id as incremental cursor.
    dossier_client_id = 1 enrichment.
    """
    render_cur.execute(
        "SELECT order_number, customer_name, customer_id, invoice_id, invoice_number, amount "
        "FROM invoices WHERE invoice_id > %s ORDER BY invoice_id",
        (last_id,)
    )
    rows = render_cur.fetchall()
    if not rows:
        return 0, last_id

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

    max_id = last_id
    transformed = []
    for row in rows:
        order_number, customer_name, customer_id, invoice_id, invoice_number, amount = row
        transformed.append((order_number, customer_name, customer_id, invoice_id, invoice_number, amount, 1))
        if invoice_id and invoice_id > max_id:
            max_id = invoice_id

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return len(rows), max_id


def sync_customers(render_cur, supa_cur, supa_conn, last_id):
    """Sync new customers with dossier_client_id = 1."""
    render_cur.execute(
        "SELECT id, name, ledger_account_id "
        "FROM customers WHERE id > %s ORDER BY id",
        (last_id,)
    )
    rows = render_cur.fetchall()
    if not rows:
        return 0, last_id

    sql = """
        INSERT INTO customers (id, name, ledger_account_id, dossier_client_id)
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            ledger_account_id = EXCLUDED.ledger_account_id,
            dossier_client_id = EXCLUDED.dossier_client_id
    """

    max_id = last_id
    transformed = []
    for row in rows:
        row_id, name, ledger_account_id = row
        transformed.append((row_id, name, ledger_account_id, 1))
        if row_id > max_id:
            max_id = row_id

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return len(rows), max_id


def sync_processed_mollie_settlements(render_cur, supa_cur, supa_conn, boutique_map, last_id):
    """Sync new processed_mollie_settlements with boutique_id enrichment."""
    render_cur.execute(
        "SELECT id, mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status "
        "FROM processed_mollie_settlements WHERE id > %s ORDER BY id",
        (last_id,)
    )
    rows = render_cur.fetchall()
    if not rows:
        return 0, last_id

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

    max_id = last_id
    transformed = []
    for row in rows:
        row_id, mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status = row
        boutique_id = boutique_map.get(store_name)
        transformed.append((mollie_id, store_name, payment_ref, order_name, invoice_number, amount, processed_at, status, boutique_id))
        if row_id > max_id:
            max_id = row_id

    for i in range(0, len(transformed), BATCH_SIZE):
        batch = transformed[i:i + BATCH_SIZE]
        upsert_batch(supa_cur, sql, batch)
        supa_conn.commit()

    return len(rows), max_id


def main():
    log("Incremental sync Render -> Supabase starting")

    # Connect to both databases
    render_conn = psycopg2.connect(**RENDER_DSN)
    render_cur = render_conn.cursor()

    supa_conn = psycopg2.connect(**SUPABASE_DSN)
    supa_cur = supa_conn.cursor()

    # Ensure sync_state table exists
    ensure_sync_state_table(supa_cur, supa_conn)

    # Load boutique mapping
    boutique_map = load_boutique_mapping(supa_cur)

    # Sync each table incrementally
    total_synced = 0

    # shopify_orders (uses created_at epoch as cursor)
    last_id = get_last_synced_id(supa_cur, "shopify_orders")
    count, max_id = sync_shopify_orders(render_cur, supa_cur, supa_conn, boutique_map, last_id)
    if count > 0:
        update_sync_state(supa_cur, supa_conn, "shopify_orders", max_id)
    log(f"shopify_orders: {count} new rows synced")
    total_synced += count

    # invoices (uses invoice_id as cursor)
    last_id = get_last_synced_id(supa_cur, "invoices")
    count, max_id = sync_invoices(render_cur, supa_cur, supa_conn, last_id)
    if count > 0:
        update_sync_state(supa_cur, supa_conn, "invoices", max_id)
    log(f"invoices: {count} new rows synced")
    total_synced += count

    # customers (uses id as cursor)
    last_id = get_last_synced_id(supa_cur, "customers")
    count, max_id = sync_customers(render_cur, supa_cur, supa_conn, last_id)
    if count > 0:
        update_sync_state(supa_cur, supa_conn, "customers", max_id)
    log(f"customers: {count} new rows synced")
    total_synced += count

    # processed_mollie_settlements (uses id as cursor)
    last_id = get_last_synced_id(supa_cur, "processed_mollie_settlements")
    count, max_id = sync_processed_mollie_settlements(render_cur, supa_cur, supa_conn, boutique_map, last_id)
    if count > 0:
        update_sync_state(supa_cur, supa_conn, "processed_mollie_settlements", max_id)
    log(f"processed_mollie_settlements: {count} new rows synced")
    total_synced += count

    log(f"Done. Total new rows synced: {total_synced}")

    # Cleanup
    render_cur.close()
    render_conn.close()
    supa_cur.close()
    supa_conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)

#!/usr/bin/env python3
"""
Diagnostic : affiche les factures Pennylane récentes pour comprendre
pourquoi extract_order_number ne matche pas.

Usage:
  python diag_invoices.py              # 10 dernières factures
  python diag_invoices.py 2026-03-28   # factures depuis cette date
"""
import os, sys, json, re, requests, psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}


def extract_order_number(*fields):
    """Copie exacte de db_loader.py pour tester."""
    for text in fields:
        if not text:
            continue
        match = re.search(
            r'(?:Commande|Order|Bestellung|Ordine|Pedido|Bestelling)\s+#?([A-Za-z]{2,5})-?(\d{3,6})', text)
        if match:
            return (match.group(1) + match.group(2)).upper()
        match = re.search(
            r'(?:Commande|Order|Bestellung|Ordine|Pedido|Bestelling)\s+#?(\d{4,6})', text)
        if match:
            return match.group(1)
        matches = re.findall(r'#?([A-Z]{2,5})-?(\d{3,6})', text.upper())
        for m in matches:
            if len(m[1]) >= 3:
                return m[0] + m[1]
    return None


date_from = sys.argv[1] if len(sys.argv) > 1 else "2026-03-28"

# 1) Récupérer les factures récentes depuis Pennylane API
print("=" * 60)
print(f"FACTURES PENNYLANE DEPUIS {date_from}")
print("=" * 60)

filter_param = json.dumps([{"field": "date", "operator": "gteq", "value": date_from}])
resp = requests.get(f"{PL_BASE}/customer_invoices", headers=PL_HEADERS,
                    params={"per_page": 20, "filter": filter_param}, timeout=30)

if resp.status_code != 200:
    print(f"Erreur API: {resp.status_code} {resp.text[:300]}")
    sys.exit(1)

invoices = resp.json().get("items", [])
print(f"→ {resp.json().get('total', '?')} factures au total, affichage de {len(invoices)}\n")

for inv in invoices:
    inv_num = inv.get("invoice_number", "?")
    label = inv.get("label", "") or ""
    special_mention = inv.get("special_mention", "") or ""
    title = inv.get("title", "") or ""
    memo = inv.get("memo", "") or ""
    reference = inv.get("reference", "") or ""
    ext_ref = inv.get("external_reference", "") or ""
    filename = inv.get("filename", "") or inv.get("file_name", "") or ""
    date = inv.get("date", "?")

    # Tester extract_order_number
    extracted = extract_order_number(special_mention, label, title, memo, reference, filename)

    status = f"✅ → {extracted}" if extracted else "❌ PAS EXTRAIT"

    print(f"  {inv_num} | {date} | {status}")
    print(f"    label            = {label[:100]!r}")
    print(f"    special_mention  = {special_mention[:100]!r}")
    if title: print(f"    title            = {title[:100]!r}")
    if reference: print(f"    reference        = {reference[:100]!r}")
    if ext_ref: print(f"    external_ref     = {ext_ref[:100]!r}")
    if filename: print(f"    filename         = {filename[:100]!r}")
    if memo: print(f"    memo             = {memo[:100]!r}")
    # Show all keys that have non-empty string values (to find hidden fields)
    other_fields = {k: v for k, v in inv.items()
                    if isinstance(v, str) and v.strip()
                    and k not in ("label", "special_mention", "title", "memo",
                                  "reference", "filename", "file_name", "external_reference",
                                  "invoice_number", "date", "currency", "id", "status",
                                  "currency_amount", "currency_tax", "currency_amount_before_tax")}
    if other_fields:
        print(f"    autres champs    = {json.dumps(other_fields, ensure_ascii=False)[:200]}")
    print()

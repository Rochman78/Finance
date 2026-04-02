#!/usr/bin/env python3
"""
Diagnostic : vérifie si des commandes sont dans la DB invoices
et cherche les factures Pennylane correspondantes.

Usage:
  python diag_invoices.py LFC29998 RDC3716 COCO3078 TZ5524
"""
import os, sys, json, re, requests, psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

order_numbers = sys.argv[1:] if len(sys.argv) > 1 else [
    "LFC29998", "LFC29997", "RDC3716", "COCO3078", "TZ5524", "HC3172"
]

# 1) Vérifier la DB
print("=" * 60)
print("1. RECHERCHE DANS LA BASE PostgreSQL")
print("=" * 60)
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()
for on in order_numbers:
    cur.execute("SELECT order_number, customer_name, invoice_number FROM invoices WHERE order_number = %s", (on,))
    row = cur.fetchone()
    if row:
        print(f"  ✅ {on} → {row[1]} | {row[2]}")
    else:
        # Chercher avec LIKE au cas où la clé serait différente
        cur.execute("SELECT order_number, customer_name, invoice_number FROM invoices WHERE order_number LIKE %s", (f"%{on[-4:]}%",))
        rows = cur.fetchall()
        if rows:
            print(f"  ❌ {on} PAS TROUVÉ exactement, mais trouvé avec suffixe :")
            for r in rows:
                print(f"       → {r[0]} | {r[1]} | {r[2]}")
        else:
            print(f"  ❌ {on} PAS TROUVÉ du tout")
conn.close()

# 2) Chercher dans Pennylane API
print()
print("=" * 60)
print("2. RECHERCHE DANS L'API PENNYLANE")
print("=" * 60)
for on in order_numbers[:3]:  # Limiter à 3 pour pas trop d'appels
    print(f"\n  🔍 Recherche Pennylane pour {on}...")
    # Chercher par invoice_number ou label contenant le numéro
    resp = requests.get(f"{PL_BASE}/customer_invoices", headers=PL_HEADERS,
                        params={"per_page": 5, "filter": json.dumps([{"field": "label", "operator": "search", "value": on}])},
                        timeout=30)
    if resp.status_code == 200:
        items = resp.json().get("items", [])
        if items:
            for inv in items:
                print(f"  ✅ Trouvé : {inv.get('invoice_number')} | label={inv.get('label','')[:80]}")
                print(f"     special_mention={inv.get('special_mention','')[:80]}")
                print(f"     filename={inv.get('filename','') or inv.get('file_name','')}")
        else:
            print(f"  ❌ Rien trouvé par label. Essai par special_mention...")
            resp2 = requests.get(f"{PL_BASE}/customer_invoices", headers=PL_HEADERS,
                                 params={"per_page": 5, "filter": json.dumps([{"field": "special_mention", "operator": "search", "value": on}])},
                                 timeout=30)
            if resp2.status_code == 200:
                items2 = resp2.json().get("items", [])
                if items2:
                    for inv in items2:
                        print(f"  ✅ Trouvé : {inv.get('invoice_number')} | label={inv.get('label','')[:80]}")
                        print(f"     special_mention={inv.get('special_mention','')[:80]}")
                else:
                    print(f"  ❌ Pas trouvé non plus par special_mention")
            else:
                print(f"  ⚠️ Erreur API: {resp2.status_code}")
    else:
        print(f"  ⚠️ Erreur API: {resp.status_code} {resp.text[:200]}")

# 3) Stats DB
print()
print("=" * 60)
print("3. STATS DB")
print("=" * 60)
conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM invoices")
print(f"  Total factures en DB : {cur.fetchone()[0]}")
cur.execute("SELECT order_number FROM invoices ORDER BY order_number DESC LIMIT 10")
print(f"  Dernières commandes : {[r[0] for r in cur.fetchall()]}")
conn.close()

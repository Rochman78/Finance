#!/usr/bin/env python3
"""
cancel_expired_orders.py
Annule des commandes EXPIRÉES par un AVOIR BROUILLON Pennylane (annulation TOTALE).

Entrée : CSV d'export commandes Shopify (n° de commande en 1re colonne `Name`,
date en `Created at`). Un fichier par boutique (COCO/HET/LFC/LVO/RED/RETE/TAR).

Par commande (>= FLOOR_DATE) :
  1. Retrouve la facture Pennylane d'origine (par n° de commande, scan date +/-).
  2. Skip si un avoir lié à cette facture existe déjà (anti-doublon, règle #5).
  3. Miroir TOTAL des lignes de la facture en quantités négatives.
  4. Crée un draft customer_invoice daté d'AUJOURD'HUI (évite le blocage de
     finalisation sur période verrouillée) + link_credit_note vers l'originale.

Dry-run par défaut. --real pour créer les brouillons. --finalize (étape 3) plus tard.

Usage :
    python cancel_expired_orders.py                 # dry-run, tous les fichiers
    python cancel_expired_orders.py --only HET      # dry-run, un seul fichier
    python cancel_expired_orders.py --real          # crée les brouillons
"""
import os, sys, csv, json, re, argparse, logging, time
from datetime import datetime, date, timedelta
import requests

# Réutilise les helpers Pennylane éprouvés (client, find, lines, dedup).
from create_credit_note_drafts import (
    pl_get, pl_post, fetch_invoice_lines, already_has_credit_note,
    PENNYLANE_TOKEN, PL_BASE, PL_HEADERS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DL = os.path.expanduser("~/Downloads")
FLOOR_DATE = "2026-01-01"
AVOIR_DATE = date.today().strftime("%Y-%m-%d")   # date du jour, tip Charles

# Fichier CSV -> boutique + template Pennylane (repris de create_credit_note_drafts)
FILE_STORE = {
    "COCO": {"name": "MTC",  "template_id": 710211},
    "HET":  {"name": "HET",  "template_id": 710153},
    "LFC":  {"name": "LFC",  "template_id": 282170},
    "LVO":  {"name": "LVO",  "template_id": 710463},
    "RED":  {"name": "RED",  "template_id": 710185},
    "RETE": {"name": "RM",   "template_id": 710214},
    "TAR":  {"name": "TZ",   "template_id": 710132},
}

# ── Résolution facture Pennylane (avec cache par date) ────────────────────
_date_cache = {}

def invoices_on_date(d):
    if d in _date_cache:
        return _date_cache[d]
    items, cursor = [], None
    fl = json.dumps([{"field": "date", "operator": "eq", "value": d}])
    while True:
        params = {"filter": fl, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        data = pl_get("customer_invoices", params)
        if not data:
            break
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    _date_cache[d] = items
    return items


def _contains_order(text, needle):
    """needle présent ET pas immédiatement suivi d'un chiffre (évite COCO464 ⊂ COCO4645)."""
    for m in re.finditer(re.escape(needle), text):
        after = text[m.end()] if m.end() < len(text) else ""
        if not after.isdigit():
            return True
    return False


def find_invoice(order_name, created_iso):
    base = datetime.strptime(created_iso[:10], "%Y-%m-%d").date()
    needle = order_name.lstrip("#")
    for delta in range(-2, 16):
        d = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
        for inv in invoices_on_date(d):
            # Ignore les avoirs : on veut la FACTURE d'origine, pas un crédit.
            label = (inv.get("label") or "")
            if inv.get("credited_invoice") or label.lstrip().lower().startswith("avoir"):
                continue
            sm = (inv.get("special_mention") or "") + " " + label
            if _contains_order(sm, needle):
                return inv
    return None


# ── Construction de l'avoir miroir total ──────────────────────────────────
def build_avoir_payload(order_name, invoice, invoice_lines, template_id):
    avoir_lines, total_ttc = [], 0.0
    for ln in invoice_lines:
        qty = float(ln.get("quantity", 0) or 0)
        if qty == 0:
            continue
        line = {
            "label": f"Avoir - {ln.get('label', '')}",
            "quantity": -qty,
            "unit": ln.get("unit", "piece"),
            "raw_currency_unit_price": ln.get("raw_currency_unit_price"),
            "discount": ln.get("discount") or {"type": "relative", "value": "0"},
            "vat_rate": ln.get("vat_rate", "FR_200"),
        }
        pid = (ln.get("product") or {}).get("id")
        if pid:
            line["product_id"] = int(pid)
        avoir_lines.append(line)
        total_ttc += float(ln.get("currency_amount", 0) or 0)

    # Miroir de la remise au niveau facture (remise absolue → négativée pour
    # s'ajouter sur les lignes négatives, sinon l'avoir dépasse la facture).
    extra = {}
    inv_discount = invoice.get("discount")
    if inv_discount and inv_discount.get("type") == "absolute":
        extra["discount"] = {"type": "absolute",
                             "value": str(-float(inv_discount.get("value", "0")))}
    elif inv_discount:
        extra["discount"] = inv_discount

    inv_number = invoice.get("invoice_number")
    payload = {
        "date": AVOIR_DATE,
        "deadline": AVOIR_DATE,
        "customer_id": (invoice.get("customer") or {}).get("id"),
        "customer_invoice_template_id": int(template_id),
        "currency": invoice.get("currency", "EUR"),
        "special_mention": f"Avoir - annulation commande expirée {order_name} (facture {inv_number})",
        "language": "fr_FR",
        "draft": True,
        "invoice_lines": avoir_lines,
        **extra,
    }
    return payload, total_ttc


# ── Parsing CSV : commandes uniques (name -> created_at) ───────────────────
def parse_orders(path):
    orders = {}
    order_list = []  # garde l'ordre d'apparition
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            on = (row.get("Name") or "").strip()
            if not on:
                continue
            ca = (row.get("Created at") or "").strip()
            fs = (row.get("Financial Status") or "").strip()
            if on not in orders:
                orders[on] = {"created": ca, "fin_status": fs}
                order_list.append(on)
            elif not orders[on]["created"] and ca:
                orders[on]["created"] = ca
    return [(on, orders[on]) for on in order_list]


# ── Traitement d'un fichier ────────────────────────────────────────────────
def process_file(file_key, dry_run, results):
    path = os.path.join(DL, f"{file_key}.csv")
    if not os.path.exists(path):
        log.warning(f"[{file_key}] fichier absent : {path}")
        return
    store = FILE_STORE[file_key]
    orders = parse_orders(path)
    todo = [(on, m) for on, m in orders if m["created"][:10] >= FLOOR_DATE]
    log.info(f"\n===== {file_key} ({store['name']}) : {len(todo)} commande(s) >= {FLOOR_DATE} "
             f"(sur {len(orders)}) =====")

    for i, (order_name, meta) in enumerate(todo, 1):
        rec = {"file": file_key, "store": store["name"], "order": order_name,
               "created": meta["created"][:10], "fin_status": meta["fin_status"],
               "status": None, "invoice_number": None, "invoice_id": None,
               "ttc": None, "n_lines": None, "avoir_id": None, "note": None}
        log.info(f"[{file_key} {i}/{len(todo)}] {order_name} (cmd {rec['created']}, {rec['fin_status']})")

        inv = find_invoice(order_name, meta["created"])
        if not inv:
            rec["status"] = "skip_no_invoice"
            log.warning(f"    ⚠️ facture Pennylane INTROUVABLE")
            results.append(rec); continue
        rec["invoice_number"] = inv.get("invoice_number")
        rec["invoice_id"] = inv.get("id")
        rec["ttc"] = float(inv.get("currency_amount", 0) or 0)

        existing = already_has_credit_note(inv["id"], (inv.get("customer") or {}).get("id"))
        if existing:
            rec["status"] = "skip_existing"
            rec["avoir_id"] = existing.get("id")
            log.info(f"    ⏭  avoir déjà existant id={existing['id']} ({existing.get('invoice_number')})")
            results.append(rec); continue

        inv_lines = fetch_invoice_lines(inv["id"])
        if not inv_lines:
            rec["status"] = "skip_no_lines"
            log.warning(f"    ⚠️ facture {rec['invoice_number']} sans ligne")
            results.append(rec); continue

        payload, total_ttc = build_avoir_payload(order_name, inv, inv_lines, store["template_id"])
        rec["n_lines"] = len(payload["invoice_lines"])

        if dry_run:
            rec["status"] = "dry_run"
            log.info(f"    ✓ facture {rec['invoice_number']} TTC={rec['ttc']:.2f}€ "
                     f"→ avoir {rec['n_lines']} ligne(s) daté {AVOIR_DATE} (WOULD POST)")
            results.append(rec); continue

        # ── écriture réelle : création du brouillon ──
        r = pl_post("customer_invoices", payload)
        if r.status_code not in (200, 201):
            rec["status"] = "error_create"; rec["note"] = f"HTTP {r.status_code}: {r.text[:200]}"
            log.error(f"    ❌ POST customer_invoices {r.status_code}: {r.text[:200]}")
            results.append(rec); continue
        new_inv = r.json()
        rec["avoir_id"] = new_inv["id"]

        # garde-fou : le TTC de l'avoir doit matcher la facture (tolérance 5c)
        avoir_amt = abs(float(new_inv.get("amount") or 0))
        diff = round(avoir_amt - rec["ttc"], 2)
        if abs(diff) > 0.05:
            rec["status"] = "warn_amount_mismatch"; rec["note"] = f"avoir={avoir_amt} vs fac={rec['ttc']} Δ={diff:+.2f}"
            log.error(f"    ❌ MISMATCH avoir={avoir_amt}€ vs facture={rec['ttc']}€ (Δ={diff:+.2f}) "
                      f"— avoir id={rec['avoir_id']} créé mais NON lié")
            results.append(rec); continue

        rl = pl_post(f"customer_invoices/{inv['id']}/link_credit_note",
                     {"credit_note_id": int(rec["avoir_id"])})
        if rl.status_code not in (200, 201, 204):
            rec["status"] = "warn_unlinked"; rec["note"] = f"link HTTP {rl.status_code}"
            log.warning(f"    ⚠️ avoir id={rec['avoir_id']} créé mais link_credit_note {rl.status_code}")
            results.append(rec); continue

        rec["status"] = "ok"
        log.info(f"    ✅ avoir brouillon id={rec['avoir_id']} lié à facture {rec['invoice_number']}")
        results.append(rec)


def finalize_all():
    """Étape 3 : passe les avoirs brouillon 'ok' du rapport en finalisé (PUT /finalize).
    Idempotent : GET préalable, saute ceux déjà finalisés."""
    report = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cancel_expired_report_real.csv")
    rows = [r for r in csv.DictReader(open(report)) if r["status"] == "ok"]
    log.info(f"FINALISATION de {len(rows)} avoirs (rapport {os.path.basename(report)})")
    results = []
    for i, r in enumerate(rows, 1):
        aid = r["avoir_id"]
        rec = {"order": r["order"], "avoir_id": aid, "invoice_number": None,
               "status": None, "amount": None}
        inv = pl_get(f"customer_invoices/{aid}")
        if not inv:
            rec["status"] = "fetch_ko"
            log.error(f"[{i}/{len(rows)}] {r['order']} avoir {aid} : GET KO")
            results.append(rec); continue
        if not inv.get("draft"):
            rec["status"] = "already_final"; rec["invoice_number"] = inv.get("invoice_number")
            rec["amount"] = inv.get("amount")
            log.info(f"[{i}/{len(rows)}] {r['order']} déjà finalisé ({rec['invoice_number']})")
            results.append(rec); continue
        # PUT /finalize avec backoff 429
        for attempt in range(4):
            resp = requests.put(f"{PL_BASE}/customer_invoices/{aid}/finalize",
                                headers=PL_HEADERS, timeout=30)
            if resp.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            break
        if resp.status_code in (200, 201):
            body = resp.json()
            rec["status"] = "finalized"; rec["invoice_number"] = body.get("invoice_number")
            rec["amount"] = body.get("amount")
            log.info(f"[{i}/{len(rows)}] ✅ {r['order']} → {rec['invoice_number']} ({rec['amount']})")
        else:
            rec["status"] = "error"; rec["invoice_number"] = f"HTTP {resp.status_code}: {resp.text[:150]}"
            log.error(f"[{i}/{len(rows)}] ❌ {r['order']} avoir {aid} : {resp.status_code} {resp.text[:150]}")
        results.append(rec)
        time.sleep(0.3)

    from collections import Counter
    by = Counter(x["status"] for x in results)
    log.info("\n========== BILAN FINALISATION ==========")
    for k, v in sorted(by.items()):
        log.info(f"  {k:16} : {v}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cancel_expired_finalize.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        for x in results:
            w.writerow(x)
    log.info(f"  rapport → {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="Crée les brouillons (sinon dry-run)")
    ap.add_argument("--only", help="Un seul fichier (ex: HET)")
    ap.add_argument("--finalize", action="store_true", help="Étape 3 : finalise les avoirs 'ok' du rapport")
    args = ap.parse_args()

    if not PENNYLANE_TOKEN:
        log.error("PENNYLANE_TOKEN manquant dans Finance/.env"); sys.exit(1)

    if args.finalize:
        finalize_all(); return

    dry = not args.real
    log.info(f"MODE = {'DRY-RUN' if dry else 'RÉEL (création brouillons)'} | date avoir = {AVOIR_DATE} "
             f"| floor = {FLOOR_DATE}")

    keys = [args.only.upper()] if args.only else list(FILE_STORE.keys())
    results = []
    for k in keys:
        if k not in FILE_STORE:
            log.error(f"Fichier inconnu : {k} (attendu : {', '.join(FILE_STORE)})"); continue
        process_file(k, dry, results)

    # ── Bilan ──
    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    log.info("\n========== BILAN ==========")
    for k, v in sorted(by_status.items()):
        log.info(f"  {k:22} : {v}")
    total_ttc = sum(r["ttc"] or 0 for r in results if r["status"] in ("dry_run", "ok"))
    log.info(f"  {'TTC total avoirs':22} : {total_ttc:.2f} €")

    # rapport
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"cancel_expired_report_{'real' if not dry else 'dryrun'}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else
                           ["file", "order", "status"])
        w.writeheader()
        for r in results:
            w.writerow(r)
    log.info(f"  rapport → {out}")


if __name__ == "__main__":
    main()

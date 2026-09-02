#!/usr/bin/env python3
"""Crée des avoirs pour des (shop, order, refund_id) PRÉCIS — cas non couverts par le
batch : remboursements sans ligne (mode 'generic', 1 ligne au montant) et 2e remboursement
bloqué par l'anti-doublon (mode 'auto' = ligne par ligne via process_one_refund).

Bypass volontaire de l'anti-doublon du batch (on cible un refund_id non encore crédité).
Dry-run par défaut. --real pour écrire.
"""
import os, sys, time, argparse, logging, requests
from datetime import date
from create_credit_note_drafts import (
    STORES, get_shopify_token, SHOPIFY_API_VERSION, find_original_invoice,
    fetch_invoice_lines, process_one_refund, pl_post, pl_get, AVOIR_DATE,
)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# (shop, order_name, refund_id, mode)
TARGETS = [
    # A — sans ligne → générique
    ("MTC", "COCO4861", "1192074838391", "generic"),
    ("MTC", "COCO4632", "1191241908599", "generic"),
    ("MTC", "COCO4234", "1188543660407", "generic"),
    ("MTC", "COCO4234", "1188886184311", "generic"),
    ("MTC", "COCO3894", "1188545593719", "generic"),
    # B — 2e remboursement bloqué → ligne par ligne
    ("MTC", "COCO4136", "1189196530039", "auto"),
    ("MTC", "COCO4421", "1187555508599", "auto"),
]

def get_order_by_name(shop, name):
    tok = get_shopify_token(shop)
    r = requests.get(f'https://{shop["store"]}/admin/api/{SHOPIFY_API_VERSION}/orders.json',
        headers={"X-Shopify-Access-Token": tok},
        params={"status": "any", "name": name.lstrip("#"),
                "fields": "id,name,created_at,refunds,line_items,total_price,total_tax,currency,discount_applications"},
        timeout=30)
    r.raise_for_status()
    for o in r.json().get("orders", []):
        if o["name"].lstrip("#") == name.lstrip("#"):
            return o
    return None

def dominant_vat(inv_lines):
    from collections import Counter
    c = Counter((l.get("vat_rate") or "FR_200") for l in inv_lines)
    return c.most_common(1)[0][0] if c else "FR_200"

VAT_DEC = {"FR_200":0.20,"FR_100":0.10,"FR_055":0.055,"FR_021":0.021,"FR_000":0.0}

def make_generic(order, refund, invoice, inv_lines, store, dry):
    order_name = order["name"]
    tx = sum(float(t.get("amount",0) or 0) for t in refund.get("transactions",[]) if t.get("kind")=="refund")
    if tx <= 0:
        return {"status":"skip_tx0","order":order_name}
    vat = dominant_vat(inv_lines); rate = VAT_DEC.get(vat, 0.20)
    ht = round(tx/(1+rate), 5)
    inv_number = invoice["invoice_number"]
    payload = {
        "date": AVOIR_DATE, "deadline": AVOIR_DATE,
        "customer_id": (invoice.get("customer") or {}).get("id"),
        "customer_invoice_template_id": int(store["template_id"]),
        "currency": "EUR",
        "special_mention": f"Avoir sur remboursement commande {order_name} (facture {inv_number})\nRemboursement du {refund['created_at'][:10]}\nRefund {refund['id']}",
        "language": "fr_FR", "draft": True,
        "invoice_lines": [{
            "label": f"Avoir sur remboursement commande {order_name}",
            "quantity": -1, "unit": "piece",
            "raw_currency_unit_price": str(ht), "vat_rate": vat,
            "discount": {"type":"relative","value":"0"},
        }],
    }
    if dry:
        log.info(f"    [{order_name}] DRY générique — {tx:.2f}€ TTC (HT {ht} @ {vat}) → would POST")
        return {"status":"dry","order":order_name,"ttc":tx}
    r = pl_post("customer_invoices", payload)
    if r.status_code not in (200,201):
        return {"status":"error_create","order":order_name,"note":r.text[:150]}
    new=r.json(); nid=new["id"]
    amt=abs(float(new.get("amount") or 0))
    if abs(amt-tx)>0.05:
        return {"status":"warn_mismatch","order":order_name,"avoir_id":nid,"note":f"avoir={amt} tx={tx}"}
    rl=pl_post(f"customer_invoices/{invoice['id']}/link_credit_note",{"credit_note_id":int(nid)})
    st = "ok" if rl.status_code in (200,201,204) else "warn_unlinked"
    log.info(f"    [{order_name}] ✅ générique avoir id={nid} TTC={new.get('currency_amount')} ({st})")
    return {"status":st,"order":order_name,"avoir_id":nid,"ttc":tx}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--real",action="store_true"); args=ap.parse_args()
    dry=not args.real
    log.info(f"MODE={'RÉEL' if not dry else 'DRY-RUN'} | {len(TARGETS)} cibles")
    results=[]
    for shop_name, order_name, refund_id, mode in TARGETS:
        store=next(s for s in STORES if s["name"]==shop_name)
        o=get_order_by_name(store, order_name)
        if not o:
            log.warning(f"[{order_name}] introuvable Shopify"); results.append({"status":"no_order","order":order_name}); continue
        refund=next((r for r in o.get("refunds",[]) if str(r["id"])==refund_id), None)
        if not refund:
            log.warning(f"[{order_name}] refund {refund_id} introuvable"); results.append({"status":"no_refund","order":order_name}); continue
        inv=find_original_invoice(order_name, o["created_at"])
        if not inv:
            log.warning(f"[{order_name}] facture introuvable"); results.append({"status":"no_invoice","order":order_name}); continue
        inv_lines=fetch_invoice_lines(inv["id"])
        log.info(f"[{order_name}] refund {refund_id} mode={mode}")
        if mode=="generic":
            res=make_generic(o, refund, inv, inv_lines, store, dry)
        else:
            res=process_one_refund(o, refund, inv, inv_lines, store, dry_run=dry)
        res["_order"]=order_name; res["_refund"]=refund_id; results.append(res)
        time.sleep(0.3)
    log.info("\n=== BILAN ===")
    from collections import Counter
    for k,v in Counter(r["status"] for r in results).items(): log.info(f"  {k:20}: {v}")
    # ids créés
    for r in results:
        if r.get("avoir_id"): log.info(f"  {r.get('_order')} → avoir id={r['avoir_id']} ({r['status']})")

if __name__=="__main__":
    main()

#!/usr/bin/env python3
"""
creer_avoirs_manquants.py — crée les avoirs des remboursements identifiés SANS
avoir par audit_avoirs_2026.py, à partir du CSV d'audit (liste explicite, pas de
fenêtre de dates).

Réutilise intégralement la machinerie de create_credit_note_drafts :
anti-doublon reclé sur refund_id, garde-fou anti-sur-crédit, plafonnement au
solde de la facture, lettrage /link_credit_note. Deux différences seulement :

  1. Les identifiants Shopify viennent de ~/smiirl-counter/.env — les seuls à
     porter read_all_orders sur les 9 boutiques. Sans ça, les commandes de plus
     de 60 jours sur LFC sont invisibles, ce qui est précisément la cause d'une
     partie des avoirs manquants.
  2. La facture d'origine est prise par son NUMÉRO, tel qu'établi par l'audit,
     au lieu d'être redevinée en balayant les dates autour de la commande :
     certaines factures sont émises plus de 15 jours après la commande et
     échappent à la fenêtre de find_original_invoice.

Le cran de sûreté de create_credit_note_drafts reste en place : rien n'est créé
sans AURALIS_AVOIRS_ARMED=1 passé EN LIGNE, et le plafond AURALIS_AVOIRS_CAP
s'applique par lancement.

Usage :
    python creer_avoirs_manquants.py --csv exports/audit_avoirs_2026/audit_all_*.csv
    AURALIS_AVOIRS_ARMED=1 AURALIS_AVOIRS_CAP=130 python creer_avoirs_manquants.py --csv ... --real
"""
import os, sys, csv, json, time, argparse, logging, glob
from collections import Counter
from datetime import date
import requests

import create_credit_note_drafts as C
from shopify_smiirl import cable_identifiants_smiirl, verifie_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("avoirs")

VERDICTS_DEFAUT = ("AVOIR_MANQUANT",)


ORDER_FIELDS = ("id,name,created_at,total_price,total_tax,currency,financial_status,"
                "refunds,line_items,discount_codes,discount_applications,customer")


def get_order(store, order_id):
    """Par ID : le seul accès fiable pour une commande ancienne (la recherche
    par nom dépend de l'index Shopify, qui ignore les vieilles commandes)."""
    r = C.shopify_get(store, f"orders/{order_id}.json", {"fields": ORDER_FIELDS})
    return r.json().get("order")


def get_facture(numero):
    flt = json.dumps([{"field": "invoice_number", "operator": "eq", "value": numero}])
    d = C.pl_get("customer_invoices", {"filter": flt, "limit": 5})
    items = (d or {}).get("items", [])
    for inv in items:
        if inv.get("invoice_number") == numero and float(inv.get("amount") or 0) > 0:
            return inv
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="CSV produit par audit_avoirs_2026.py")
    ap.add_argument("--verdict", action="append", help=f"verdicts à traiter (défaut {VERDICTS_DEFAUT})")
    ap.add_argument("--shop", action="append", help="restreindre à des boutiques")
    ap.add_argument("--real", action="store_true", help="écrit dans Pennylane")
    ap.add_argument("--forcer-ambigu", action="store_true",
                    help="traite aussi les commandes portant un avoir non attribuable. "
                         "Le garde-fou anti-sur-crédit reste actif : c'est lui qui refusera "
                         "les factures déjà intégralement créditées.")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    dry = not args.real

    chemins = sorted(glob.glob(args.csv))
    if not chemins:
        log.error(f"❌ aucun CSV ne correspond à {args.csv}"); sys.exit(1)
    verdicts = tuple(args.verdict) if args.verdict else VERDICTS_DEFAUT

    rows = list(csv.DictReader(open(chemins[-1]), delimiter=";"))
    cibles = [r for r in rows if r["verdict"] in verdicts]
    if args.shop:
        codes = {s.upper() for s in args.shop}
        cibles = [r for r in cibles if r["shop"].upper() in codes]
    cibles.sort(key=lambda r: (r["shop"], r["event_date"]))
    if args.limit:
        cibles = cibles[:args.limit]

    log.info(f"=== {'DRY-RUN' if dry else 'RÉEL'} | {chemins[-1]} | verdicts {verdicts} "
             f"| {len(cibles)} avoir(s) à créer | date des avoirs : {C.AVOIR_DATE} ===")
    if not dry and not C._avoirs_armes():
        log.error("🔒 Mode réel demandé sans AURALIS_AVOIRS_ARMED=1 : chaque POST sera refusé. Arrêt.")
        sys.exit(1)
    if not dry:
        plafond = C._plafond_avoirs()
        if plafond < len(cibles):
            log.error(f"🔒 Plafond AURALIS_AVOIRS_CAP={plafond} < {len(cibles)} avoirs à créer : "
                      f"le lot serait tronqué en cours de route. Relancer avec un plafond suffisant.")
            sys.exit(1)

    try:
        stores = {s["name"]: s for s in cable_identifiants_smiirl()[0]}
    except RuntimeError as e:
        log.error(f"❌ {e}"); sys.exit(1)
    for nom in sorted({r["shop"] for r in cibles}):
        if nom not in stores:
            log.error(f"❌ boutique {nom} sans identifiants"); sys.exit(1)
        if not verifie_scope(stores[nom]):
            log.error(f"🛑 [{nom}] read_all_orders absent — les commandes anciennes seraient "
                      f"invisibles et le lot silencieusement incomplet. Arrêt.")
            sys.exit(1)
    log.info(f"  identifiants smiirl-counter câblés, read_all_orders vérifié sur "
             f"{len(set(r['shop'] for r in cibles))} boutique(s)")

    stats, journal = Counter(), []
    for i, r in enumerate(cibles, 1):
        store = stores[r["shop"]]
        nom, rid = r["order_name"], r["refund_id"]
        log.info(f"\n[{i}/{len(cibles)}] {r['shop']} {nom} refund {rid} du {r['event_date']} "
                 f"({r['montant']} €)")
        res = {"shop": r["shop"], "order": nom, "refund_id": rid,
               "event_date": r["event_date"], "montant_shopify": r["montant"],
               "facture": r["facture"]}

        # Une commande expirée ne porte pas de refund : son avoir est une
        # annulation totale, produite par cancel_expired_orders.py avec sa
        # propre mention (« annulation commande expirée »). Hors périmètre ici.
        if r.get("type") == "expired" or not r["refund_id"]:
            log.warning("    ⏭  commande expirée, pas un remboursement — relève de "
                        "cancel_expired_orders.py")
            stats["skip_expired"] += 1
            journal.append({**res, "status": "skip_expired"}); continue

        order = get_order(store, r["order_id"])
        if not order:
            log.error("    ❌ commande introuvable dans Shopify")
            stats["no_order"] += 1; journal.append({**res, "status": "no_order"}); continue
        refund = next((x for x in order.get("refunds", []) if str(x["id"]) == str(rid)), None)
        if not refund:
            log.error(f"    ❌ refund {rid} absent de la commande")
            stats["no_refund"] += 1; journal.append({**res, "status": "no_refund"}); continue

        inv = get_facture(r["facture"])
        if not inv:
            log.error(f"    ❌ facture {r['facture']} introuvable dans Pennylane")
            stats["no_invoice"] += 1; journal.append({**res, "status": "no_invoice"}); continue

        cust_id = (inv.get("customer") or {}).get("id")
        avoirs = C.find_avoirs_for_invoice(inv["id"], cust_id, nom)
        refund_ttc = sum(float(t.get("amount", 0) or 0)
                         for t in refund.get("transactions", [])
                         if t.get("kind") == "refund" and t.get("status") == "success")
        match, ambigus = C.match_avoir_for_refund(avoirs, rid, refund["created_at"][:10], refund_ttc)
        if match:
            log.info(f"    ⏭  avoir déjà existant (id={match['id']}, reconnu par "
                     f"{match['match_par']}, {match['amount']} €) — rien créé")
            stats["skip_existing"] += 1
            journal.append({**res, "status": "skip_existing", "avoir_id": match["id"]}); continue
        if ambigus:
            ids = ", ".join(str(a["id"]) for a in ambigus)
            if not args.forcer_ambigu:
                log.warning(f"    ⏭  avoir(s) non attribuable(s) sur cette commande (id={ids}) — rien créé")
                stats["skip_ambigu"] += 1
                journal.append({**res, "status": "skip_ambigu", "note": ids}); continue
            # Levée délibérée : l'avoir existant est un orphelin de saisie
            # manuelle (ni refund_id ni date de remboursement), donc jamais
            # attribuable automatiquement. On laisse le garde-fou anti-sur-crédit
            # de process_one_refund arbitrer sur le solde réel de la facture.
            deja = sum(abs(a["amount"]) for a in avoirs)
            log.warning(f"    ⚠️  avoir(s) non attribuable(s) (id={ids}) — levée demandée, "
                        f"arbitrage laissé au solde de la facture "
                        f"(déjà crédité {deja:.2f} €)")

        inv_lines = C.fetch_invoice_lines(inv["id"])
        out = C.process_one_refund(order, refund, inv, inv_lines, store,
                                   dry_run=dry, avoirs_existants=avoirs)
        stats[out.get("status", "?")] += 1
        journal.append({**res, **{k: v for k, v in out.items() if k not in ("order",)}})
        if not dry:
            time.sleep(0.3)

    log.info("\n=== BILAN ===")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        log.info(f"  {k:24} {v}")
    suffixe = "reel" if args.real else "dryrun"
    dest = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports",
                        "audit_avoirs_2026", f"creation_avoirs_{C.AVOIR_DATE}_{suffixe}.csv")
    cols = sorted({k for j in journal for k in j})
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(journal)
    log.info(f"\n📄 {dest}")


if __name__ == "__main__":
    main()

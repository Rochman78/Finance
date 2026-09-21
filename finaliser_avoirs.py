#!/usr/bin/env python3
"""
finaliser_avoirs.py
Finalise des avoirs Pennylane (draft → définitif) à partir d'une liste
d'identifiants EXPLICITE, jamais d'un « tous les brouillons du compte ».

La finalisation est IRRÉVERSIBLE : un avoir finalisé ne se supprime plus.
Chaque avoir est donc contrôlé avant d'être touché — encore en brouillon,
montant conforme à celui attendu, et bien lié à une facture d'origine.

Sources d'identifiants (cumulables) :
  --logs DIR      logs de create_credit_note_drafts (lit « ✅ avoir id=N TTC=M »)
  --report CSV    rapport de cancel_expired_orders (colonnes status/avoir_id/ttc)
  --id ID[:TTC]   un avoir précis, montant attendu facultatif

Dry-run par défaut (contrôle seul). --real pour finaliser.

Exemples :
    python finaliser_avoirs.py --logs exports/avoirs_2026-09-02/real
    python finaliser_avoirs.py --logs exports/... --report cancel_expired_report_real.csv --real
"""
import argparse, csv, logging, re, sys, time, pathlib
import requests
from create_credit_note_drafts import pl_get, PL_BASE, PL_HEADERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def depuis_logs(repertoire):
    out = []
    for f in sorted(pathlib.Path(repertoire).glob("*.log")):
        for aid, ttc in re.findall(r"✅ avoir id=(\d+) TTC=(-?[\d.]+)", f.read_text()):
            out.append((int(aid), abs(float(ttc)), f.stem))
    return out


def depuis_rapport(chemin):
    out = []
    with open(chemin, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("status") == "ok" and r.get("avoir_id"):
                out.append((int(r["avoir_id"]), float(r.get("ttc") or 0),
                            f"{pathlib.Path(chemin).stem}/{r.get('store','')}"))
    return out


def controler(avoirs):
    """Sépare ce qui est finalisable de ce qui ne l'est pas."""
    bons, ecartes = [], []
    for aid, ttc_attendu, origine in avoirs:
        d = pl_get(f"customer_invoices/{aid}")
        if not d:
            ecartes.append((aid, origine, "introuvable")); continue
        montant = abs(float(d.get("amount") or 0))
        if d.get("draft") is not True:
            ecartes.append((aid, origine, f"déjà finalisé ({d.get('invoice_number')})")); continue
        if ttc_attendu and abs(montant - ttc_attendu) > 0.05:
            ecartes.append((aid, origine, f"montant {montant:.2f}€ ≠ attendu {ttc_attendu:.2f}€")); continue
        if not (d.get("credited_invoice") or {}).get("id"):
            ecartes.append((aid, origine, "non lié à une facture")); continue
        bons.append((aid, montant, origine))
    return bons, ecartes


def finaliser(aid):
    for tentative in range(6):
        r = requests.put(f"{PL_BASE}/customer_invoices/{aid}/finalize",
                         headers=PL_HEADERS, timeout=30)
        if r.status_code == 429:
            time.sleep(min(2 ** tentative, 12)); continue
        return r
    return r


def main():
    ap = argparse.ArgumentParser(description="Finalise des avoirs Pennylane depuis une liste explicite")
    ap.add_argument("--logs", help="répertoire de logs create_credit_note_drafts")
    ap.add_argument("--report", help="rapport CSV cancel_expired_orders")
    ap.add_argument("--id", action="append", default=[], help="ID[:TTC attendu], répétable")
    ap.add_argument("--real", action="store_true", help="finalise réellement (sinon contrôle seul)")
    args = ap.parse_args()

    avoirs = []
    if args.logs:   avoirs += depuis_logs(args.logs)
    if args.report: avoirs += depuis_rapport(args.report)
    for spec in args.id:
        aid, _, ttc = spec.partition(":")
        avoirs.append((int(aid), float(ttc) if ttc else 0.0, "--id"))
    if not avoirs:
        log.error("Aucun avoir en entrée : préciser --logs, --report ou --id"); sys.exit(1)

    vus, uniques = set(), []
    for a in avoirs:
        if a[0] not in vus:
            vus.add(a[0]); uniques.append(a)
    log.info(f"{len(uniques)} avoir(s) en entrée ({len(avoirs) - len(uniques)} doublon(s) écarté(s))")

    bons, ecartes = controler(uniques)
    log.info(f"  ✅ prêts à finaliser : {len(bons)} ({sum(b[1] for b in bons):,.2f}€)")
    log.info(f"  ⚠️  écartés           : {len(ecartes)}")
    for aid, origine, motif in ecartes:
        log.warning(f"       {aid} [{origine}] — {motif}")

    if not args.real:
        log.info("Contrôle seul — relancer avec --real pour finaliser (IRRÉVERSIBLE).")
        return

    ok = ko = 0
    for i, (aid, montant, origine) in enumerate(bons, 1):
        r = finaliser(aid)
        if r.status_code in (200, 201, 204):
            ok += 1
            try: num = (r.json() or {}).get("invoice_number", "")
            except Exception: num = ""
            log.info(f"  [{i}/{len(bons)}] ✅ {aid} → {num}")
        else:
            ko += 1
            log.error(f"  [{i}/{len(bons)}] ❌ {aid} HTTP {r.status_code}: {r.text[:200]}")
    log.info(f"=== finalisés : {ok} / échecs : {ko} ===")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Enchaîne les boutiques restantes : dry-run → si OK (exit 0 + au moins 1 avoir à créer),
création réelle des brouillons. Log par boutique. AUCUNE finalisation, AUCUN lettrage
(Charles traite au retour)."""
import subprocess, re, sys, os
from pathlib import Path

SCR = "/private/tmp/claude-501/-Users-charlesbamy-auralis-core/6d9fd0be-e239-4cbe-b13b-f59e4f4a983f/scratchpad"
SHOPS = [("HET","2026-02-08"), ("TZ","2026-02-08"), ("LVO","2026-02-08"),
         ("RETE","2026-02-08"), ("MO","2026-02-08")]
TO = "2026-07-25"
PY = sys.executable
HERE = os.path.dirname(os.path.abspath(__file__))

def run(shop, frm, dry):
    log = f"{SCR}/{shop.lower()}_{'dryrun' if dry else 'real'}.log"
    cmd = [PY, "create_credit_note_drafts.py", "--shop", shop, "--from", frm, "--to", TO]
    if dry: cmd.append("--dry-run")
    with open(log, "w") as f:
        p = subprocess.run(cmd, cwd=HERE, stdout=f, stderr=subprocess.STDOUT)
    txt = Path(log).read_text()
    def g(key):
        m = re.search(rf"{key}\s*:\s*(\d+)", txt)
        return int(m.group(1)) if m else 0
    return {"rc": p.returncode, "dry_run": g("dry_run"), "ok": g("ok"),
            "skip_existing": g("skip_existing"), "skip_no_invoice": g("skip_no_invoice"),
            "skip_no_lines": g("skip_no_lines"), "mismatch": g("error_amount_mismatch"),
            "log": log}

summary = []
for shop, frm in SHOPS:
    print(f"\n===== {shop} — DRY-RUN =====", flush=True)
    d = run(shop, frm, dry=True)
    print(f"  rc={d['rc']} à_créer={d['dry_run']} déjà={d['skip_existing']} "
          f"sans_facture={d['skip_no_invoice']} sans_ligne={d['skip_no_lines']}", flush=True)
    if d["rc"] != 0:
        print(f"  ⚠️ dry-run a échoué (rc={d['rc']}) → PAS de réel pour {shop}", flush=True)
        summary.append((shop, "DRY-RUN KO", d)); continue
    if d["dry_run"] == 0:
        print(f"  rien à créer pour {shop} → skip réel", flush=True)
        summary.append((shop, "rien à créer", d)); continue
    print(f"  ===== {shop} — RÉEL ({d['dry_run']} brouillons) =====", flush=True)
    r = run(shop, frm, dry=False)
    print(f"  rc={r['rc']} créés_ok={r['ok']} déjà={r['skip_existing']} "
          f"sans_ligne={r['skip_no_lines']} mismatch={r['mismatch']}", flush=True)
    summary.append((shop, "RÉEL fait", r))

print("\n\n========== SYNTHÈSE ==========", flush=True)
for shop, state, s in summary:
    print(f"  {shop:5s} {state:14s} | à_créer={s.get('dry_run','?')} créés_ok={s.get('ok','?')} "
          f"sans_ligne={s.get('skip_no_lines','?')} mismatch={s.get('mismatch','?')}", flush=True)
print("Terminé.", flush=True)

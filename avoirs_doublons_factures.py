#!/usr/bin/env python3
"""
=============================================================
AVOIRS DE DOUBLONS DE FACTURATION
=============================================================
Annule par avoir les factures SURNUMÉRAIRES des commandes facturées
plusieurs fois, pour ramener à zéro le solde débiteur injustifié du
compte client. Voir audit_doublons_factures.py pour le recensement.

Ce n'est PAS un avoir de remboursement : il n'y a aucun refund Shopify
derrière. On annule une facture qui n'aurait jamais dû exister. D'où un
script distinct de create_credit_note_drafts.py (piloté par les refunds).

Entrée : exports/doublons_factures.csv (produit par l'audit).

Contrôles par groupe, tous relus EN DIRECT dans Pennylane (le CSV ne sert
qu'à fournir la liste de candidats) :

  BLOQUANTS
  1. meme_commande       — même n° de commande sur toutes les pièces
  2. creation_rapprochee — écart de création max entre deux pièces consécutives
                           <= --seuil-secondes (défaut 300)
  3. lignes_identiques   — lignes (label/qté/PU/TVA/montant) identiques à la
                           pièce conservée
  4. pieces_actives      — toutes finalisées, non archivées, non annulées
  5. sans_avoir_existant — aucun avoir déjà rattaché aux pièces à annuler
  6. miroir_coherent     — TTC du miroir == TTC de la facture annulée (±0,02)
  7. solde_soldé         — pour CHAQUE compte 411 touché : solde − avoirs ≈ 0

  SIGNALÉ, non bloquant
  - fiches_dupliquees    — le doublon est sur une 2e fiche du même client. Le
                           contrôle 7 étant fait compte par compte, l'avoir
                           solde quand même ; mais les fiches sont à fusionner.

Un groupe qui échoue à UN SEUL contrôle bloquant n'est pas traité et part en
revue manuelle. La pièce CONSERVÉE est la plus ancienne (created_at) ; les
suivantes sont annulées. Si une seule pièce est lettrée et que ce n'est pas la
plus ancienne, le groupe est bloqué.

Dry-run par défaut. L'écriture réelle passe par le cran de sûreté d'avoirs
(AURALIS_AVOIRS_ARMED=1 en ligne + AURALIS_AVOIRS_CAP) — cf.
README_CRAN_SURETE_AVOIRS.md.

Usage :
  python avoirs_doublons_factures.py                        # dry-run complet
  python avoirs_doublons_factures.py --only LFC31629        # dry-run ciblé
  AURALIS_AVOIRS_ARMED=1 AURALIS_AVOIRS_CAP=40 \
      python avoirs_doublons_factures.py --real
"""
import os
import sys
import csv
import json
import time
import logging
import argparse
from collections import Counter
from datetime import datetime, date

# Helpers Pennylane éprouvés. pl_post porte le CRAN DE SÛRETÉ : tout
# POST /customer_invoices est refusé sans AURALIS_AVOIRS_ARMED=1 et
# plafonné par AURALIS_AVOIRS_CAP, avec trace dans avoirs_audit.log.
from create_credit_note_drafts import (
    pl_get, pl_post, fetch_invoice_lines, already_has_credit_note,
    PENNYLANE_TOKEN, _avoirs_armes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

RACINE = os.path.dirname(os.path.abspath(__file__))
CSV_AUDIT = os.path.join(RACINE, "exports", "doublons_factures.csv")
CSV_RAPPORT = os.path.join(RACINE, "exports", "avoirs_doublons_rapport.csv")

AVOIR_DATE = date.today().strftime("%Y-%m-%d")   # évite le blocage période verrouillée
STATUTS_ACTIFS = {"paid", "late", "upcoming", "partially_paid"}

# Templates Pennylane par préfixe de commande (repris de create_invoice_draft).
TEMPLATE_PAR_PREFIXE = [
    ("COCO", 710211), ("LFC", 282170), ("RDC", 710185), ("HC", 710153),
    ("RM", 710214), ("TZ", 710132), ("LVO", 710463), ("UC", 710484),
    ("UNIV", 710484), ("MO", 710449),
]


def template_pour(order_name):
    # Plus long préfixe d'abord : « UC » ne doit pas rafler « UNIV ».
    for prefixe, tid in sorted(TEMPLATE_PAR_PREFIXE, key=lambda x: -len(x[0])):
        if order_name.upper().startswith(prefixe):
            return tid
    return None


def montant(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# =============================================================
# LECTURE EN DIRECT
# =============================================================
import re

_RE_CMD = re.compile(r'Commande\s+#?([A-Za-z]{2,6})-?(\d{3,7})')


def commande_de(inv):
    m = _RE_CMD.search(inv.get("special_mention") or "")
    return (m.group(1) + m.group(2)).upper() if m else None


def signature_lignes(lignes):
    """Empreinte comparable d'une facture : ce qui doit coïncider à l'identique
    entre un doublon et la pièce conservée. On ignore les ids (product_id,
    line id) qui diffèrent d'une création à l'autre sans rien dire du fond."""
    out = []
    for ln in lignes:
        out.append((
            (ln.get("label") or "").strip(),
            round(montant(ln.get("quantity")), 4),
            str(ln.get("raw_currency_unit_price") or ""),
            ln.get("vat_rate") or "",
            round(montant(ln.get("currency_amount")), 2),
        ))
    return sorted(out)


_cache_solde = {}


def solde_411(customer_id):
    """Solde du compte 411 du client (débit − crédit), sur tout l'historique."""
    if customer_id in _cache_solde:
        return _cache_solde[customer_id]
    client = pl_get(f"customers/{customer_id}")
    nom = (client or {}).get("name")
    compte = ((client or {}).get("ledger_account") or {}).get("id")
    solde = None
    if compte:
        fl = json.dumps([{"field": "ledger_account_id", "operator": "eq", "value": str(compte)}])
        total, cursor = 0.0, None
        while True:
            params = {"filter": fl, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = pl_get("ledger_entry_lines", params)
            if not data:
                break
            for ligne in data.get("items", []):
                total += montant(ligne.get("debit")) - montant(ligne.get("credit"))
            if not data.get("has_more") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
        solde = round(total, 2)
    _cache_solde[customer_id] = (nom, solde)
    return _cache_solde[customer_id]


# =============================================================
# AVOIR MIROIR
# =============================================================
def payload_avoir(order_name, facture, lignes, template_id, facture_gardee):
    """Miroir total de la facture en quantités négatives."""
    avoir_lignes, total_ttc = [], 0.0
    for ln in lignes:
        qty = montant(ln.get("quantity"))
        if qty == 0:
            continue
        ligne = {
            "label": f"Avoir - {ln.get('label', '')}",
            "quantity": -qty,
            "unit": ln.get("unit", "piece"),
            "raw_currency_unit_price": ln.get("raw_currency_unit_price"),
            "discount": ln.get("discount") or {"type": "relative", "value": "0"},
            "vat_rate": ln.get("vat_rate", "FR_200"),
        }
        pid = (ln.get("product") or {}).get("id")
        if pid:
            ligne["product_id"] = int(pid)
        avoir_lignes.append(ligne)
        total_ttc += montant(ln.get("currency_amount"))

    # Miroir de la remise au niveau facture : une remise absolue doit être
    # négativée pour s'appliquer sur des lignes déjà négatives, sinon l'avoir
    # dépasse la facture (même raisonnement que cancel_expired_orders).
    extra = {}
    remise = facture.get("discount")
    if remise and remise.get("type") == "absolute":
        extra["discount"] = {"type": "absolute", "value": str(-montant(remise.get("value")))}
    elif remise:
        extra["discount"] = remise

    payload = {
        "date": AVOIR_DATE,
        "deadline": AVOIR_DATE,
        "customer_id": (facture.get("customer") or {}).get("id"),
        "customer_invoice_template_id": int(template_id),
        "currency": facture.get("currency", "EUR"),
        "special_mention": (
            f"Avoir - annulation de facture en doublon, commande {order_name}\n"
            f"Facture annulée : {facture.get('invoice_number')} du {facture.get('date')}\n"
            f"Facture conservée : {facture_gardee.get('invoice_number')} du {facture_gardee.get('date')}"
        ),
        "language": "fr_FR",
        "draft": True,
        "invoice_lines": avoir_lignes,
        **extra,
    }
    return payload, round(total_ttc, 2)


# =============================================================
# CONTRÔLES
# =============================================================
def controler(groupe, seuil_s, tolerance):
    """Relit tout en direct et renvoie (ok, echecs, infos)."""
    order = groupe["commande"]
    ids = groupe["ids"].split("|")
    echecs = []

    factures = []
    for i in ids:
        f = pl_get(f"customer_invoices/{i}")
        if not f:
            echecs.append(f"lecture_impossible:{i}")
            return False, echecs, {}
        factures.append(f)
    factures.sort(key=lambda x: x.get("created_at") or "")

    # 1. même commande
    cmds = {commande_de(f) for f in factures}
    if cmds != {order}:
        echecs.append(f"meme_commande (relu: {cmds})")

    # 2. même client — SIGNALÉ, pas bloquant. Quand le doublon a été posé sur une
    #    fiche client dupliquée, chaque facture porte son propre compte 411 : le
    #    contrôle de solde (#7) est alors fait compte par compte, ce qui reste
    #    exact. On le note quand même : les deux fiches sont à fusionner dans
    #    Pennylane, sinon le client réapparaîtra en double au prochain achat.
    clients = {(f.get("customer") or {}).get("id") for f in factures}
    fiches_dupliquees = len(clients) > 1

    # 3. création rapprochée — on regarde l'écart MAXIMAL entre deux pièces
    #    consécutives, pas l'amplitude totale : trois pièces à 10 s d'intervalle
    #    chacune restent une rafale, même si la première et la dernière sont
    #    séparées de plus que le seuil.
    ecarts = []
    for a, b in zip(factures, factures[1:]):
        try:
            ta = datetime.fromisoformat((a["created_at"]).replace("Z", "+00:00"))
            tb = datetime.fromisoformat((b["created_at"]).replace("Z", "+00:00"))
            ecarts.append((tb - ta).total_seconds())
        except (KeyError, AttributeError, ValueError):
            echecs.append("created_at illisible")
    ecart_max = max(ecarts) if ecarts else None
    if ecart_max is None or ecart_max > seuil_s:
        echecs.append(f"creation_rapprochee (écart max {ecart_max}s > {seuil_s}s)")

    # 5. pièces actives
    for f in factures:
        if f.get("draft") or f.get("archived_at") or f.get("status") not in STATUTS_ACTIFS:
            echecs.append(f"pieces_actives ({f.get('invoice_number')}: draft={f.get('draft')} "
                          f"archived={bool(f.get('archived_at'))} statut={f.get('status')})")

    gardee, a_annuler = factures[0], factures[1:]

    # Cohérence : si UNE seule pièce porte paid=True, ce doit être la conservée.
    payees = [f for f in factures if f.get("paid")]
    if len(payees) == 1 and payees[0]["id"] != gardee["id"]:
        echecs.append(f"la pièce lettrée ({payees[0].get('invoice_number')}) n'est pas la plus ancienne")

    lignes_gardee = fetch_invoice_lines(gardee["id"])
    sig_gardee = signature_lignes(lignes_gardee)

    cibles = []
    total_avoirs = 0.0
    for f in a_annuler:
        # 6. pas d'avoir déjà rattaché
        existant = already_has_credit_note(f["id"], (f.get("customer") or {}).get("id"))
        if existant:
            echecs.append(f"sans_avoir_existant ({f.get('invoice_number')} → avoir {existant.get('id')})")
            continue
        lignes = fetch_invoice_lines(f["id"])
        if not lignes:
            echecs.append(f"facture sans ligne ({f.get('invoice_number')})")
            continue
        # 4. lignes identiques à la pièce conservée
        if signature_lignes(lignes) != sig_gardee:
            echecs.append(f"lignes_identiques ({f.get('invoice_number')})")
            continue
        tid = template_pour(order)
        if not tid:
            echecs.append(f"template inconnu pour {order}")
            continue
        payload, ttc = payload_avoir(order, f, lignes, tid, gardee)
        # Le TTC du miroir doit coller au TTC de la facture annulée.
        ecart_ttc = round(ttc - montant(f.get("amount")), 2)
        if abs(ecart_ttc) > 0.02:
            echecs.append(f"miroir_incoherent ({f.get('invoice_number')}: miroir {ttc} "
                          f"vs facture {f.get('amount')}, Δ{ecart_ttc:+.2f})")
            continue
        cibles.append({"facture": f, "payload": payload, "ttc": ttc})
        total_avoirs += ttc

    # 7. LE contrôle qui compte : chaque compte 411 touché doit tomber à zéro.
    #    Fait compte par compte et non globalement, car sur une fiche client
    #    dupliquée les deux factures vivent sur deux comptes 411 distincts —
    #    un solde global masquerait un compte à 0 et l'autre à +449,90.
    avoirs_par_compte = {}
    for c in cibles:
        k = (c["facture"].get("customer") or {}).get("id")
        avoirs_par_compte[k] = round(avoirs_par_compte.get(k, 0.0) + c["ttc"], 2)
    # Le compte de la pièce conservée est vérifié lui aussi : sans avoir dessus,
    # il doit DÉJÀ être soldé (l'encaissement l'a apuré).
    comptes = dict.fromkeys([(gardee.get("customer") or {}).get("id"), *avoirs_par_compte])

    soldes, residuels = {}, {}
    nom_principal = None
    for k in comptes:
        nom, solde = solde_411(k)
        if nom_principal is None:
            nom_principal = nom
        soldes[k] = solde
        if solde is None:
            echecs.append(f"solde 411 illisible (client {k})")
            continue
        r = round(solde - avoirs_par_compte.get(k, 0.0), 2)
        residuels[k] = r
        if abs(r) > tolerance:
            echecs.append(f"solde_soldé (client {k} « {nom} » : solde {solde:.2f} "
                          f"− avoirs {avoirs_par_compte.get(k, 0.0):.2f} = {r:+.2f} ≠ 0 ± {tolerance})")

    infos = {
        "client": nom_principal,
        "customer_id": "|".join(str(k) for k in comptes),
        "solde_411": round(sum(v for v in soldes.values() if v is not None), 2),
        "gardee": gardee, "cibles": cibles, "total_avoirs": round(total_avoirs, 2),
        "residuel": round(sum(residuels.values()), 2) if residuels else None,
        "residuel_max": max((abs(v) for v in residuels.values()), default=None),
        "ecart_max_s": ecart_max, "nb_pieces": len(factures),
        "fiches_dupliquees": fiches_dupliquees,
    }
    return (not echecs and bool(cibles)), echecs, infos


# =============================================================
# RUN
# =============================================================
def run(dry_run, seuil_s, tolerance, only, limite):
    if not PENNYLANE_TOKEN:
        log.error("PENNYLANE_TOKEN manquant")
        return 1
    if not os.path.exists(CSV_AUDIT):
        log.error(f"{CSV_AUDIT} absent — lancer d'abord audit_doublons_factures.py")
        return 1
    if not dry_run and not _avoirs_armes():
        log.warning("ℹ️  Mode réel demandé mais cran de sûreté NON armé : les créations d'avoir "
                    "seront refusées. Relancer avec AURALIS_AVOIRS_ARMED=1 EN LIGNE.")

    groupes = [r for r in csv.DictReader(open(CSV_AUDIT, encoding="utf-8"))
               if r["solde_411"] and montant(r["solde_411"]) > 0.03]
    if only:
        cibles = {c.strip().upper() for c in only.split(",")}
        groupes = [g for g in groupes if g["commande"].upper() in cibles]
    groupes.sort(key=lambda g: -montant(g["solde_411"]))
    if limite:
        groupes = groupes[:limite]

    log.info(f"MODE={'DRY-RUN' if dry_run else 'RÉEL'} | {len(groupes)} groupe(s) candidat(s) | "
             f"seuil rafale {seuil_s}s | tolérance solde {tolerance}€ | avoirs datés du {AVOIR_DATE}")

    rapport, retenus, rejetes = [], [], []
    for i, g in enumerate(groupes, 1):
        order = g["commande"]
        ok, echecs, infos = controler(g, seuil_s, tolerance)
        base = {
            "commande": order, "client": infos.get("client"),
            "customer_id": infos.get("customer_id"), "solde_411": infos.get("solde_411"),
            "nb_pieces": infos.get("nb_pieces"), "ecart_max_s": infos.get("ecart_max_s"),
            "facture_conservee": (infos.get("gardee") or {}).get("invoice_number"),
            "factures_annulees": "|".join(c["facture"].get("invoice_number") for c in infos.get("cibles", [])),
            "total_avoirs": infos.get("total_avoirs"), "solde_apres": infos.get("residuel"),
            "fiches_dupliquees": infos.get("fiches_dupliquees"),
        }
        if not ok:
            log.warning(f"[{i}/{len(groupes)}] ⛔ {order} — REVUE MANUELLE : {'; '.join(echecs)}")
            rapport.append({**base, "statut": "revue_manuelle", "avoir_ids": "", "note": "; ".join(echecs)})
            rejetes.append(order)
            continue

        log.info(f"[{i}/{len(groupes)}] ✓ {order} — {infos['client']} | solde {infos['solde_411']:.2f}€ | "
                 f"garde {base['facture_conservee']} | annule {base['factures_annulees']} | "
                 f"avoirs {infos['total_avoirs']:.2f}€ → solde après {infos['residuel']:+.2f}€ | "
                 f"rafale {infos['ecart_max_s']:.0f}s | lignes identiques ✓"
                 + ("  ⚠️ 2 fiches client à fusionner" if infos.get("fiches_dupliquees") else ""))

        if dry_run:
            rapport.append({**base, "statut": "dry_run", "avoir_ids": "", "note": ""})
            retenus.append(order)
            continue

        ids_crees, notes = [], []
        for c in infos["cibles"]:
            f = c["facture"]
            r = pl_post("customer_invoices", c["payload"])
            if r.status_code not in (200, 201):
                notes.append(f"POST {f.get('invoice_number')} HTTP {r.status_code}: {r.text[:150]}")
                log.error(f"    ❌ {order} / {f.get('invoice_number')} — {notes[-1]}")
                break
            avoir = r.json()
            aid = avoir["id"]
            # Garde-fou : le TTC réellement calculé par Pennylane doit coller.
            amt = abs(montant(avoir.get("amount")))
            if abs(amt - c["ttc"]) > 0.05:
                notes.append(f"mismatch avoir {aid}: {amt} vs attendu {c['ttc']} — NON lié")
                log.error(f"    ❌ {order} — {notes[-1]}")
                ids_crees.append(aid)
                break
            rl = pl_post(f"customer_invoices/{f['id']}/link_credit_note", {"credit_note_id": int(aid)})
            if rl.status_code not in (200, 201, 204):
                notes.append(f"avoir {aid} créé mais link_credit_note HTTP {rl.status_code}")
                log.warning(f"    ⚠️ {order} — {notes[-1]}")
            ids_crees.append(aid)
            log.info(f"    ✅ avoir id={aid} TTC={avoir.get('currency_amount')} "
                     f"lié à {f.get('invoice_number')}")
            time.sleep(0.3)

        statut = "ok" if (ids_crees and not notes) else ("partiel" if ids_crees else "echec")
        rapport.append({**base, "statut": statut,
                        "avoir_ids": "|".join(map(str, ids_crees)), "note": "; ".join(notes)})
        (retenus if statut == "ok" else rejetes).append(order)

    os.makedirs(os.path.dirname(CSV_RAPPORT), exist_ok=True)
    if rapport:
        with open(CSV_RAPPORT, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rapport[0].keys()))
            w.writeheader()
            w.writerows(rapport)

    log.info("\n========== BILAN ==========")
    for k, v in sorted(Counter(r["statut"] for r in rapport).items()):
        log.info(f"  {k:16} : {v}")
    total = sum(montant(r["total_avoirs"]) for r in rapport
                if r["statut"] in ("dry_run", "ok", "partiel"))
    log.info(f"  avoirs {'à créer' if dry_run else 'créés'} : {total:,.2f} €")
    apres = [r for r in rapport if r["statut"] in ("dry_run", "ok") and r["solde_apres"] is not None]
    if apres:
        pire = max(abs(montant(r["solde_apres"])) for r in apres)
        log.info(f"  solde résiduel max après avoirs : {pire:.2f} €")
    log.info(f"  rapport → {CSV_RAPPORT}")
    if dry_run:
        log.info("\n  Dry-run : rien n'a été écrit. Pour créer réellement les brouillons :")
        log.info(f"    AURALIS_AVOIRS_ARMED=1 AURALIS_AVOIRS_CAP={max(1, len(retenus))} \\")
        log.info(f"        python {os.path.basename(__file__)} --real")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--real", action="store_true", help="Crée réellement les avoirs brouillon")
    p.add_argument("--seuil-secondes", type=float, default=300.0,
                   help="Écart de création max entre deux pièces d'un même groupe (défaut 300)")
    p.add_argument("--tolerance", type=float, default=0.05,
                   help="Écart toléré sur le solde résiduel après avoirs, en € (défaut 0.05)")
    p.add_argument("--only", help="Ne traiter que ces commandes (séparées par des virgules)")
    p.add_argument("--limite", type=int, default=0, help="Stop après N groupes (0 = illimité)")
    a = p.parse_args()
    sys.exit(run(not a.real, a.seuil_secondes, a.tolerance, a.only, a.limite))

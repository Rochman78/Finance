#!/usr/bin/env python3
"""
cron_avoirs_du_jour.py — cron du soir (22h Paris) : crée ET finalise les
avoirs Pennylane des remboursements Shopify du jour, sur les 9 boutiques.

L'avoir est daté du jour de création. Lancé à 22h, après les remboursements
faits dans la journée, il porte donc la date du remboursement, et Pennylane reste aligné sur Shopify.

Fenêtre glissante J-7 → J : l'anti-doublon reclé sur refund_id rend le run
rejouable, et une nuit ratée est rattrapée le lendemain. Un retardataire est
daté de son jour de rattrapage (un avoir ne s'antidate pas) ; la date réelle du
remboursement reste dans le special_mention.

Pourquoi pas un webhook refunds/create : à la réception, les transactions
peuvent encore être pending ou finir en échec, et un avoir finalisé ne se
supprime pas. À 22h, on ne lit que des transactions abouties.

Garde-fous :
  - déblocage dédié AURALIS_AVOIRS_CRON=1, actif seulement sous ce pilote,
    plafond AURALIS_AVOIRS_CRON_CAP (défaut 40) — voir README_CRAN_SURETE_AVOIRS ;
  - read_all_orders vérifié par boutique (sinon mur des 60 jours silencieux) ;
  - refunds avec une transaction pending : reportés au lendemain ;
  - refunds sans argent rendu (restock seul, tout en échec) : ignorés ;
  - finalisation limitée aux avoirs « ok » de ce run, non plafonnés, après
    contrôle (brouillon, montant, lettrage) — le reste reste en brouillon et
    part en revue : récap Telegram chaque soir + mail dès qu'il y a à revoir.

Render planifie en UTC : schedule « 0 20,21 * * * » et le script ne tourne que
si l'heure de Paris est 22h (une seule des deux exécutions, été comme hiver).

Usage :
    python cron_avoirs_du_jour.py --dry-run --force-heure      # test à blanc
    python cron_avoirs_du_jour.py                               # Render
"""
import os, sys, argparse, logging, traceback, pathlib, smtplib, ssl
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import dotenv_values

import create_credit_note_drafts as C
from shopify_smiirl import cable_identifiants_smiirl, verifie_scope
from finaliser_avoirs import controler, finaliser

PARIS = ZoneInfo("Europe/Paris")
HEURE_PARIS = 22
JOURS_FENETRE = 7
MARGE_UPDATED_AT = 2
MAIL_REVUE_DEFAUT = "contact@zephyrosc.com"

log = logging.getLogger("cron_avoirs")


def doit_tourner(now_utc):
    """Vrai si l'instant tombe dans l'heure de 22h à Paris."""
    return now_utc.astimezone(PARIS).hour == HEURE_PARIS


def argent_rendu(ref):
    return sum(float(tx.get("amount", 0) or 0) for tx in ref.get("transactions", [])
               if tx.get("kind") == "refund" and tx.get("status") == "success")


def a_une_transaction_en_attente(ref):
    return any(tx.get("kind") == "refund" and tx.get("status") == "pending"
               for tx in ref.get("transactions", []))


def telegram(texte):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        log.warning("Telegram non configuré → récap non envoyé")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": texte[:4000]}, timeout=10)
        if r.status_code != 200:
            log.error(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram injoignable : {e}")


def _smtp_config():
    """SMTP OVH de smiirl-counter : variables d'environnement (Render), sinon
    ~/smiirl-counter/.env en local."""
    cles = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM")
    if os.environ.get("SMTP_HOST"):
        return {k: os.environ.get(k) for k in cles}
    v = dotenv_values(os.path.expanduser("~/smiirl-counter/.env"))
    return {k: v.get(k) for k in cles}


def mail_revue(sujet, texte):
    """Envoie la liste à revoir en INTERNE (AVOIRS_MAIL_TO, défaut contact@zephyrosc.com).
    Jamais au client : aucune adresse Shopify ou Pennylane n'est utilisée ici."""
    dest = os.environ.get("AVOIRS_MAIL_TO") or MAIL_REVUE_DEFAUT
    cfg = _smtp_config()
    if not dest or not cfg.get("SMTP_HOST") or not cfg.get("SMTP_USER"):
        log.warning("Mail non configuré (AVOIRS_MAIL_TO / SMTP_*) → revue non envoyée par mail")
        return False
    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = cfg.get("SMTP_FROM") or cfg["SMTP_USER"]
    msg["To"] = dest
    msg.set_content(texte)
    port = int(cfg.get("SMTP_PORT") or 587)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=ssl.create_default_context(), timeout=30) as s:
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"]); s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"]); s.send_message(msg)
        log.info(f"✉️  revue envoyée à {dest}")
        return True
    except Exception as e:
        log.error(f"Mail de revue en échec : {e}")
        return False


def a_revoir(rapport):
    return bool(rapport["alertes"] or rapport["brouillons"] or rapport["a_trancher"])


def traiter_boutique(store, date_from, date_to, dry_run, rapport):
    nom = store["name"]
    if not verifie_scope(store):
        rapport["alertes"].append(f"🛑 {nom} : read_all_orders absent — boutique NON traitée")
        return
    orders = C.fetch_orders_with_refunds_in_period(store, date_from, date_to,
                                                   marge_jours=MARGE_UPDATED_AT)
    todo = []
    for o in orders:
        for ref in o.get("refunds", []):
            if not (date_from <= ref["created_at"][:10] <= date_to):
                continue
            if a_une_transaction_en_attente(ref):
                rapport["en_attente"].append(f"{o['name']} (refund {ref['id']})")
                continue
            if argent_rendu(ref) <= 0:
                continue  # rien d'encaissé en retour : restock seul ou échec
            todo.append((o, ref))
    log.info(f"=== {nom} : {len(todo)} refund(s) à examiner [{date_from} → {date_to}] ===")
    if not todo:
        return

    stats, revue, plafonnes, resultats = C.traiter_refunds(store, todo, dry_run=dry_run)
    for order_name, rid, motif in revue:
        rapport["a_trancher"].append(f"{order_name} (refund {rid}) — {motif}")
    for r in resultats:
        st = r["status"]
        if st in ("ok", "dry_run"):
            ttc = abs(float(r.get("total_ttc") or 0))
            if r.get("plafonne"):
                # LFC37211 : le plafonnement peut tomber à 1 centime près et faire
                # refuser le lettrage — on laisse en brouillon, contrôle humain.
                avoir = f"avoir {r['avoir_id']}" if r.get("avoir_id") else "avoir à créer"
                rapport["brouillons"].append(f"{r['order']} {avoir} plafonné de {r['plafonne']:.2f}€")
                continue
            if r.get("match_facture") != "commande":
                # Facture trouvée par sa mention et non par sa ligne « Commande » :
                # possible facture d'une AUTRE commande qui cite celle-ci (TZ6960).
                avoir = f"avoir {r['avoir_id']}" if r.get("avoir_id") else "avoir à créer"
                rapport["brouillons"].append(f"{r['order']} {avoir} — facture {r.get('inv_number')} "
                                             f"trouvée par mention, pas par « Commande », à vérifier")
                continue
            rapport["crees"].append((nom, r["order"], r.get("avoir_id"), ttc))
        elif st in ("warn_unlinked", "error_amount_mismatch"):
            rapport["brouillons"].append(f"{r['order']} avoir {r.get('avoir_id')} — {st}")
        elif st == "error_create":
            rapport["alertes"].append(f"❌ {r['order']} création refusée : {r.get('error', '')[:120]}")
        elif st in ("skip_no_invoice", "skip_no_lines"):
            rapport["a_trancher"].append(f"{r['order']} (refund {r['refund_id']}) — {st}")
        elif st == "skip_existing":
            rapport["deja_faits"] += 1


def finaliser_du_run(rapport):
    """Finalise les avoirs créés par CE run (liste explicite), après contrôle."""
    cibles = [(aid, ttc, f"{nom}/{order}") for nom, order, aid, ttc in rapport["crees"] if aid]
    if not cibles:
        return
    bons, ecartes = controler(cibles)
    for aid, origine, motif in ecartes:
        rapport["brouillons"].append(f"{origine} avoir {aid} — non finalisé : {motif}")
    for aid, montant, origine in bons:
        r = finaliser(aid)
        if r.status_code in (200, 201, 204):
            rapport["finalises"].add(aid)
        else:
            rapport["brouillons"].append(f"{origine} avoir {aid} — finalisation HTTP "
                                         f"{r.status_code} : {r.text[:120]}")


def texte_defauts(rapport):
    """Corps du mail : UNIQUEMENT les avoirs en défaut, pas les succès."""
    blocs = []
    if rapport["alertes"]:
        blocs.append("🚨 Incidents du run\n" + "\n".join(rapport["alertes"]))
    if rapport["brouillons"]:
        blocs.append("📝 Avoirs créés mais laissés en BROUILLON (à vérifier puis finaliser "
                     "dans Pennylane)\n" + "\n".join(rapport["brouillons"]))
    if rapport["a_trancher"]:
        blocs.append("✋ Remboursements SANS avoir (rien n'a été créé, à traiter à la main)\n"
                     + "\n".join(rapport["a_trancher"]))
    return "\n\n".join(blocs)


def recap(rapport, jour, dry_run):
    tete = f"{'🧪 DRY-RUN — ' if dry_run else ''}Avoirs du {jour.strftime('%d/%m/%Y')}"
    lignes = []
    if rapport["alertes"]:
        lignes += ["🚨 ALERTES"] + rapport["alertes"] + [""]
    if rapport["crees"]:
        par_boutique = {}
        for nom, _, aid, ttc in rapport["crees"]:
            n, t, f = par_boutique.get(nom, (0, 0.0, 0))
            par_boutique[nom] = (n + 1, t + ttc, f + (aid in rapport["finalises"]))
        total = sum(c[3] for c in rapport["crees"])
        verbe = "à créer" if dry_run else "créés"
        lignes.append(f"✅ {len(rapport['crees'])} avoir(s) {verbe} — {total:,.2f} €".replace(",", " "))
        for nom, (n, t, f) in sorted(par_boutique.items()):
            suffixe = "" if dry_run else f" ({f} finalisé(s))"
            lignes.append(f"  {nom} : {n} — {t:,.2f} €{suffixe}".replace(",", " "))
        lignes.append("")
    if rapport["brouillons"]:
        lignes += [f"📝 Laissés en brouillon ({len(rapport['brouillons'])})"] + rapport["brouillons"] + [""]
    if rapport["a_trancher"]:
        lignes += [f"✋ À trancher à la main ({len(rapport['a_trancher'])})"] + rapport["a_trancher"] + [""]
    if rapport["en_attente"]:
        lignes += [f"⏳ Remboursements en attente, repris demain ({len(rapport['en_attente'])})"] \
                  + rapport["en_attente"] + [""]
    if not lignes:
        lignes.append("RAS — aucun remboursement à créditer.")
    if rapport["deja_faits"]:
        lignes.append(f"({rapport['deja_faits']} remboursement(s) de la fenêtre déjà crédité(s))")
    return tete + "\n\n" + "\n".join(lignes).strip()


def main():
    ap = argparse.ArgumentParser(description="Cron nocturne : avoirs des remboursements du jour")
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien dans Pennylane")
    ap.add_argument("--force-heure", action="store_true", help="ignore la garde des 22h Paris")
    ap.add_argument("--jours", type=int, default=JOURS_FENETRE, help="profondeur de la fenêtre (J-N → J)")
    ap.add_argument("--shop", action="append", help="limiter à une ou plusieurs boutiques")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    if not args.force_heure and not doit_tourner(now):
        print(f"Hors créneau ({now.astimezone(PARIS):%H:%M} à Paris) — rien à faire.")
        return 0

    jour = now.astimezone(PARIS).date()
    dossier = pathlib.Path(__file__).parent / "exports" / "cron_avoirs"
    dossier.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(dossier / f"{jour.isoformat()}{'_dryrun' if args.dry_run else ''}.log",
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(fh)

    C.activer_mode_cron()
    C.AVOIR_DATE = jour.isoformat()
    date_to = jour.isoformat()
    date_from = (jour - timedelta(days=args.jours)).isoformat()
    rapport = {"crees": [], "finalises": set(), "brouillons": [], "a_trancher": [],
               "en_attente": [], "alertes": [], "deja_faits": 0}

    try:
        if not C.PENNYLANE_TOKEN:
            raise RuntimeError("PENNYLANE_TOKEN manquant")
        if not args.dry_run and not C._avoirs_armes():
            raise RuntimeError("AURALIS_AVOIRS_CRON absent : aucune création possible")
        log.info(f"=== Cron avoirs {'DRY-RUN' if args.dry_run else 'RÉEL'} | fenêtre "
                 f"{date_from} → {date_to} | date des avoirs {C.AVOIR_DATE} | "
                 f"plafond {C._plafond_avoirs()} ===")
        stores, absents = cable_identifiants_smiirl()
        for nom in absents:
            rapport["alertes"].append(f"🛑 {nom} : identifiants absents — boutique NON traitée")
        for store in stores:
            if args.shop and store["name"] not in args.shop:
                continue
            try:
                traiter_boutique(store, date_from, date_to, args.dry_run, rapport)
            except Exception as e:
                log.exception(f"{store['name']} en échec")
                rapport["alertes"].append(f"❌ {store['name']} : {type(e).__name__} {str(e)[:150]}")
            if C.plafond_atteint:
                rapport["alertes"].append(f"🔒 Plafond de {C._plafond_avoirs()} avoirs atteint — "
                                          f"run arrêté, le reste sera repris demain")
                break
        if not args.dry_run:
            finaliser_du_run(rapport)
    except Exception as e:
        log.exception("Cron avoirs en échec")
        rapport["alertes"].append(f"❌ {type(e).__name__} : {str(e)[:200]}\n"
                                  f"{traceback.format_exc()[-600:]}")

    texte = recap(rapport, jour, args.dry_run)
    log.info("\n" + texte)
    telegram(texte)
    if a_revoir(rapport) and not args.dry_run:
        n = len(rapport["brouillons"]) + len(rapport["a_trancher"])
        sujet = (f"{'🚨 ' if rapport['alertes'] else ''}Avoirs du {jour.strftime('%d/%m/%Y')} — "
                 f"{n} cas à revoir")
        mail_revue(sujet, texte_defauts(rapport))
    return 1 if rapport["alertes"] else 0


if __name__ == "__main__":
    sys.exit(main())

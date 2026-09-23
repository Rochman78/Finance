#!/usr/bin/env python3
"""
create_credit_note_drafts.py
Crée des AVOIRS BROUILLON dans Pennylane à partir des refunds Shopify.

Workflow par refund :
  1. Trouve la facture Pennylane d'origine (par ordre.name dans special_mention)
  2. Lit les lignes de la facture pour matcher chaque SKU refundé
  3. Crée un draft customer_invoice avec qty=-N et raw_currency_unit_price
     ajusté pour que le TTC matche EXACTEMENT le refund Shopify
  4. POST /link_credit_note pour lier l'avoir à la facture d'origine
  5. Skip si un avoir lié à la même facture existe déjà à la même date

Usage :
    python create_credit_note_drafts.py --shop LFC --from 2026-01-01 --to 2026-01-31 [--dry-run]
"""
import os, json, time, re, logging, argparse, sys
from datetime import datetime, timedelta, date, timezone
import requests
from dotenv import load_dotenv

load_dotenv()

# Date de l'avoir = JOUR de création (un avoir ne s'antidate pas ; évite le blocage
# de finalisation sur période verrouillée). La date réelle du remboursement reste
# tracée dans le special_mention.
AVOIR_DATE = date.today().strftime("%Y-%m-%d")

# Au-delà de ce dépassement, un avoir qui excède le solde de sa facture n'est
# plus un écart d'arrondi Shopify/Pennylane mais une anomalie : on bloque au
# lieu de plafonner. En deçà, l'avoir est rogné au solde par une ligne dédiée.
SEUIL_PLAFONNEMENT = 1.00

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SHOPIFY_API_VERSION = "2026-01"
PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

# Mapping name → (store domain, template_id, client_id).
# Convention alignée sur le reste du repo (cadrage_ca.py, create_invoice_draft.py) :
# client_id public EN DUR + secret via SHOPIFY_SECRET_<NAME> dans .env (jeu smiirl,
# scope read_all_orders, indispensable pour les commandes > 60 jours).
STORES_META = [
    {"name": "LFC",  "prefix": "LFC",  "client_id": "16d136da2babe857d91f3814b57c6028", "store": "mon-filet-de-camouflage.myshopify.com", "template_id": 282170},
    {"name": "RED",  "prefix": "RDC",  "client_id": "9ea1ae211e98a704b12c0c6269006fdf", "store": "red-de-camuflaje.myshopify.com",        "template_id": 710185},
    {"name": "HET",  "prefix": "HC",   "client_id": "ef87e54b80cd6af36446f660da1ce7ce", "store": "het-camouflagenet.myshopify.com",       "template_id": 710153},
    {"name": "MTC",  "prefix": "COCO", "client_id": "ff7163cadd5f10752d05dd2b504b95cf", "store": "coconets.myshopify.com",                "template_id": 710211},
    {"name": "MO",   "prefix": "MO",   "client_id": "55b0cff935270c2545020ecc7fa4704c", "store": "mon-ombrage.myshopify.com",             "template_id": 710449},
    {"name": "RETE", "prefix": "RM",   "client_id": "c948511fe38f27931b77caf611f53d06", "store": "rete-mimetica.myshopify.com",           "template_id": 710214},
    {"name": "TZ",   "prefix": "TZ",   "client_id": "e6627287d6a9eb12b54344321ec337f1", "store": "tarnnetz.myshopify.com",                "template_id": 710132},
    {"name": "LVO",  "prefix": "LVO",  "client_id": "d7a49b87859af74774aaa7c39d212a27", "store": "le-filet-camouflage-1.myshopify.com",  "template_id": 710463},
    {"name": "UNIV", "prefix": "UNIV", "client_id": "51d4100024e40f174341e73d78e0cbbb", "store": "univers-camouflage.myshopify.com",      "template_id": 710484},
]

def _build_stores():
    """Hydrate STORES_META avec le secret depuis SHOPIFY_SECRET_<NAME> (client_id en dur)."""
    out = []
    for m in STORES_META:
        csec = os.environ.get(f"SHOPIFY_SECRET_{m['name']}", "")
        if not m.get("client_id") or not csec:
            continue
        out.append({**m, "client_secret": csec})
    return out

STORES = _build_stores()


# ─────────────────────────────────────────────────────────────────────────
# SHOPIFY
# ─────────────────────────────────────────────────────────────────────────
_shopify_tokens = {}

TENTATIVES_HTTP = 6


def requete(method, url, **kw):
    """Requête HTTP avec reprise bornée sur 429 et 5xx (Retry-After respecté,
    sinon attente exponentielle plafonnée à 30 s). L'app Shopify est partagée
    avec smiirl-counter : son quota peut être déjà entamé quand le cron passe
    (LVO, 429 sur access_scopes le 22/09). Au-delà, la dernière réponse est
    rendue telle quelle à l'appelant."""
    for tentative in range(TENTATIVES_HTTP):
        r = requests.request(method, url, **kw)
        if r.status_code != 429 and r.status_code < 500:
            return r
        if tentative == TENTATIVES_HTTP - 1:
            break
        try:
            attente = float(r.headers.get("Retry-After") or 0)
        except ValueError:
            attente = 0
        attente = min(max(attente, 2 ** tentative), 30)
        log.warning(f"    HTTP {r.status_code} sur {url.split('?')[0]} — nouvel essai dans {attente:.0f}s")
        time.sleep(attente)
    return r


def get_shopify_token(shop):
    key = shop["store"]
    if key in _shopify_tokens: return _shopify_tokens[key]
    r = requete("POST", f'https://{shop["store"]}/admin/oauth/access_token',
        data={"grant_type":"client_credentials","client_id":shop["client_id"],"client_secret":shop["client_secret"]},
        headers={"Content-Type":"application/x-www-form-urlencoded"}, timeout=15)
    r.raise_for_status()
    _shopify_tokens[key] = r.json()["access_token"]
    return _shopify_tokens[key]


def shopify_get(shop, path, params=None):
    tok = get_shopify_token(shop)
    r = requete("GET", f'https://{shop["store"]}/admin/api/{SHOPIFY_API_VERSION}/{path}',
        headers={"X-Shopify-Access-Token": tok}, params=params, timeout=30)
    r.raise_for_status()
    return r


def fetch_orders_with_refunds_in_period(shop, date_from, date_to, marge_jours=180):
    """Récupère toutes les commandes mises à jour dans la fenêtre [from-marge, to+15j]
    avec financial_status partially_refunded ou refunded. Itère via pagination.

    `marge_jours` : 180 pour les campagnes (rattrapage de périodes anciennes). Le
    cron nocturne peut descendre à 2 : un remboursement met à jour le updated_at
    de la commande, donc tout refund de la fenêtre y tombe."""
    # On élargit la fenêtre updated_at pour ne pas rater une commande ancienne refundée en janvier
    grace_from = (datetime.fromisoformat(date_from) - timedelta(days=marge_jours)).strftime("%Y-%m-%dT00:00:00Z")
    grace_to   = (datetime.fromisoformat(date_to)   + timedelta(days=15 )).strftime("%Y-%m-%dT23:59:59Z")

    all_orders = []
    url = f'https://{shop["store"]}/admin/api/{SHOPIFY_API_VERSION}/orders.json'
    params = {
        "status": "any",
        "financial_status": "refunded,partially_refunded",
        "updated_at_min": grace_from,
        "updated_at_max": grace_to,
        "limit": 250,
        "fields": "id,name,created_at,total_price,total_tax,currency,financial_status,email,billing_address,refunds,line_items,discount_codes,discount_applications",
    }
    while url:
        tok = get_shopify_token(shop)
        r = requete("GET", url, headers={"X-Shopify-Access-Token": tok}, params=params if "orders.json" in url else None, timeout=30)
        r.raise_for_status()
        all_orders.extend(r.json().get("orders", []))
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link",""))
        url = m.group(1) if m else None
        params = None
    return all_orders


# ─────────────────────────────────────────────────────────────────────────
# PENNYLANE
# ─────────────────────────────────────────────────────────────────────────
def pl_get(path, params=None):
    r = requete("GET", f"{PL_BASE}/{path.lstrip('/')}", headers=PL_HEADERS, params=params, timeout=30)
    if r.status_code != 200:
        log.error(f"  PL GET {path} HTTP {r.status_code}: {r.text[:200]}")
        return None
    return r.json()


# ═══════════════════════════════════════════════════════════════════════════
# CRAN DE SÛRETÉ AVOIRS (LOT 3.0 — revue balance 411, 2026-07-28)
#
# CONDITION DE LEVÉE DÉFINITIVE de ce garde-fou : anti-doublon de
# create_credit_note_drafts reclé sur refund_id (et non sur la facture) +
# relecture Pennylane AVANT chaque création. Tant que ces deux conditions ne
# sont pas remplies, la création d'avoir reste protégée par les 3 barrières.
#
# 1. BLOQUÉ PAR DÉFAUT — tout POST /customer_invoices (avoir) est refusé sans
#    déblocage explicite. Le dry-run et le lettrage ne passent jamais ici.
# 2. DÉBLOCAGE À L'INVOCATION — AURALIS_AVOIRS_ARMED=1 passé EN LIGNE au
#    lancement. Cette variable ne doit figurer dans AUCUN fichier du repo
#    (.env, .env.example, script, Makefile, workflow CI) — voir README.
# 3. PLAFOND — AURALIS_AVOIRS_CAP avoirs par lancement (défaut 5). Au-delà,
#    arrêt net. C'est la protection principale : le passif vient d'un VOLUME.
# 4. TRACE — chaque avoir créé est journalisé (fichier avoirs_audit.log).
# ═══════════════════════════════════════════════════════════════════════════
_AVOIR_AUDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avoirs_audit.log")
_avoirs_crees = 0  # compteur par invocation
plafond_atteint = False  # lu par le pilote cron pour alerter

# 5. VOIE CRON — le cron nocturne (cron_avoirs_du_jour.py) a son propre
#    déblocage : AURALIS_AVOIRS_CRON=1, persisté dans l'environnement Render,
#    mais qui n'arme RIEN tant que le pilote n'a pas appelé activer_mode_cron().
#    Un script manuel lancé avec cette variable reste donc bloqué. Plafond
#    propre AURALIS_AVOIRS_CRON_CAP (défaut 40 par nuit).
_mode_cron = False

def activer_mode_cron():
    global _mode_cron
    _mode_cron = True

def _avoirs_armes():
    if _mode_cron:
        return os.environ.get("AURALIS_AVOIRS_CRON") == "1"
    return os.environ.get("AURALIS_AVOIRS_ARMED") == "1"

def _plafond_avoirs():
    var, defaut = ("AURALIS_AVOIRS_CRON_CAP", 40) if _mode_cron else ("AURALIS_AVOIRS_CAP", 5)
    try:
        return max(0, int(os.environ.get(var, str(defaut))))
    except ValueError:
        return defaut

class _BlockedResponse:
    status_code = 403
    text = "Avoir bloqué (cran de sûreté) : AURALIS_AVOIRS_ARMED absent"
class _CapResponse:
    status_code = 403
    text = "Avoir bloqué (cran de sûreté) : plafond AURALIS_AVOIRS_CAP atteint"

def _trace_avoir(body, resp):
    try:
        sm = (body.get("special_mention") or "").replace("\n", " / ")
        amt = (resp.json() or {}).get("amount")
        cmd = " ".join(os.path.basename(a) if i == 0 else a for i, a in enumerate(sys.argv))
        with open(_AVOIR_AUDIT, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}\tcmd={cmd}\tsm={sm}\t"
                    f"amount={amt}\tcap={_plafond_avoirs()}\n")
    except Exception as e:
        log.warning(f"trace avoir échouée: {e}")

def pl_post(path, body):
    global _avoirs_crees, plafond_atteint
    is_avoir = path.strip("/") == "customer_invoices"
    if is_avoir:
        if not _avoirs_armes():
            log.error("🔒 Création d'avoir BLOQUÉE (cran de sûreté). Usage délibéré : relancer avec "
                      "AURALIS_AVOIRS_ARMED=1 EN LIGNE (ne JAMAIS persister). "
                      "Plafond/lancement : AURALIS_AVOIRS_CAP (défaut 5).")
            return _BlockedResponse()
        if _avoirs_crees >= _plafond_avoirs():
            plafond_atteint = True
            log.error(f"🔒 Plafond de {_plafond_avoirs()} avoirs atteint pour ce lancement — ARRÊT NET. "
                      "Relancer délibérément (ou AURALIS_AVOIRS_CAP=N) pour continuer.")
            return _CapResponse()
    # Retry sur 429 (rate-limit) avec backoff — indispensable en cas de runs concurrents.
    for attempt in range(6):
        r = requests.post(f"{PL_BASE}/{path.lstrip('/')}", headers=PL_HEADERS, json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(min(2 ** attempt, 12)); continue
        break
    if is_avoir and r.status_code in (200, 201):
        _avoirs_crees += 1
        _trace_avoir(body, r)
    return r


class RechercheFactureImpossible(RuntimeError):
    """Pennylane n'a pas répondu pendant la recherche de la facture d'origine."""


def find_original_invoice(order_name, order_date_iso):
    """Trouve l'invoice Pennylane pour une commande Shopify.
    Scan -2 à +15 jours autour de l'order_date (les factures sont parfois créées
    quelques jours après la commande, notamment lors d'un batch de facturation).
    Pagination complète à chaque date scannée."""
    base = datetime.strptime(order_date_iso[:10], "%Y-%m-%d").date()
    needle = order_name.lstrip("#")
    # Match en POSITION d'abord : la ligne « Commande #X » désigne la commande
    # facturée. Une simple sous-chaîne prenait pour la facture de X celle d'une
    # autre commande qui CITE X (« Neubestellung nach Storno von #TZ6960 » sur la
    # facture de TZ6971), ou celle de X0 pour X. Les factures nées d'un devis
    # portent le n° seul sur sa ligne, ou après « | » en fin de ligne
    # (« D-2026-05-221474\nRDC4278 », « … | RDC3838 »). La recherche large,
    # bornée au token, ne sert qu'à défaut ; elle est marquée _match="mention"
    # pour que le cron ne finalise pas dessus.
    re_commande = re.compile(rf"Commande\s*#?{re.escape(needle)}(?![0-9A-Za-z])"
                             rf"|(?m:(?:^|\|)[ \t]*#?{re.escape(needle)}[ \t]*$)")
    re_token = re.compile(rf"(?<![0-9A-Za-z]){re.escape(needle)}(?![0-9A-Za-z])")
    repli = None
    for delta in range(-2, 16):  # -2 à +15 jours
        d = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
        fl = json.dumps([{"field": "date", "operator": "eq", "value": d}])
        cursor = None
        while True:
            params = {"filter": fl, "limit": 100}
            if cursor: params["cursor"] = cursor
            data = pl_get("customer_invoices", params)
            if data is None:
                # Pennylane en erreur malgré les reprises : conclure « pas de
                # facture » serait faux, on remonte l'échec à l'appelant.
                raise RechercheFactureImpossible(f"{order_name} : Pennylane en erreur le {d}")
            for inv in data.get("items", []):
                # Un avoir porte lui aussi « Commande {name} » dans son
                # special_mention et il est daté du jour : sans ce filtre, un
                # avoir récemment créé serait pris pour la facture d'origine.
                if float(inv.get("amount") or 0) < 0 or (inv.get("credited_invoice") or {}).get("id"):
                    continue
                sm = (inv.get("special_mention") or "") + "\n" + (inv.get("label") or "")
                if re_commande.search(sm):
                    return {**inv, "_match": "commande"}
                if repli is None and re_token.search(sm):
                    repli = {**inv, "_match": "mention"}
            if not data.get("has_more"): break
            cursor = data.get("next_cursor")
            if not cursor: break
    return repli


def fetch_invoice_lines(invoice_id):
    """Récupère les lignes d'une facture."""
    data = pl_get(f"customer_invoices/{invoice_id}/invoice_lines", {"limit": 100})
    return data.get("items", []) if data else []


VAT_RATE_MAP = {
    "FR_200": 0.20, "FR_100": 0.10, "FR_055": 0.055, "FR_021": 0.021, "FR_000": 0.0,
    "ES_210": 0.21, "DE_190": 0.19, "BE_210": 0.21, "IT_220": 0.22, "NL_210": 0.21,
    "exempt": 0.0, "EXEMPT": 0.0, "NL_000": 0.0, "DE_000": 0.0, "ES_000": 0.0,
}


def dominant_vat(invoice_lines):
    """Taux de TVA majoritaire d'une facture. Les ajustements de remboursement
    (port, geste commercial) n'ont pas de taux propre côté Shopify : on leur
    applique celui qui domine la facture d'origine."""
    from collections import Counter
    c = Counter((ln.get("vat_rate") or "FR_200") for ln in invoice_lines)
    return c.most_common(1)[0][0] if c else "FR_200"


_RE_VAT_CODE = re.compile(r"^[A-Z]{2}_(\d{3})$")


def vat_to_decimal(vat_str):
    """Taux de TVA d'un code Pennylane, en décimal.

    Les codes suivent tous <PAYS>_<TAUX×10> : FR_200 = 20 %, PT_230 = 23 %,
    FR_055 = 5,5 %. On dérive donc le taux du code au lieu d'entretenir une
    table pays par pays, forcément incomplète : c'est l'absence de PT_230 qui a
    fait partir un avoir à +23 % le 2026-09-02 (RDC5197), le HT ayant été
    calculé avec un fallback à 0 %."""
    if vat_str in VAT_RATE_MAP:
        return VAT_RATE_MAP[vat_str]
    m = _RE_VAT_CODE.match(vat_str or "")
    if m:
        return int(m.group(1)) / 1000.0
    log.warning(f"  vat_rate inconnu : {vat_str!r} — fallback à 0%")
    return 0.0


def compute_raw_unit_price(target_ttc, qty, discount_pct, vat_rate):
    """TTC iso Shopify, on ajuste sur HT pour absorber l'arrondi."""
    abs_qty = abs(qty)
    ht_after_discount = round(target_ttc / (1 + vat_rate), 2)
    ht_before_discount = ht_after_discount / (1 - discount_pct / 100) if discount_pct > 0 else ht_after_discount
    return round(ht_before_discount / abs_qty, 5)


# ─────────────────────────────────────────────────────────────────────────
# MAIN PROCESSING
# ─────────────────────────────────────────────────────────────────────────
def find_pl_line_for_shopify_item(invoice_lines, sku, shopify_title):
    """Match d'abord par SKU dans description, sinon par titre dans label."""
    if sku:
        for ln in invoice_lines:
            if f"SKU: {sku}" in (ln.get("description","") or ""):
                return ln
    # Fallback : match par label (= titre Shopify dans le label PL)
    if shopify_title:
        norm_title = shopify_title.lower()
        for ln in invoice_lines:
            lbl = (ln.get("label","") or "").lower()
            if norm_title and norm_title[:30] in lbl:
                return ln
    return None


def process_one_refund(order, refund, invoice, invoice_lines, store_config, dry_run=False,
                       avoirs_existants=None):
    """Crée 1 draft avoir pour 1 refund Shopify. Retourne dict {status, ...}."""
    order_name = order["name"]
    refund_date = refund["created_at"][:10]
    refund_id   = refund["id"]
    inv_id      = invoice["id"]
    inv_number  = invoice["invoice_number"]

    # Détection FULL vs PARTIAL : compare transaction refund vs total invoice
    # Seules les transactions ABOUTIES sont de l'argent rendu. Shopify garde la
    # trace des remboursements en échec (carte refusée, plafond) avec le même
    # kind="refund" : les compter gonfle l'avoir d'un montant jamais remboursé,
    # et quand le commerçant refait le remboursement à la main, le même argent
    # est compté deux fois (cas #LFC26248, #LFC34414).
    tx_total = sum(float(tx.get("amount", 0) or 0) for tx in refund.get("transactions", [])
                   if tx.get("kind") == "refund" and tx.get("status") == "success")
    inv_total = float(invoice.get("currency_amount", "0") or 0)
    # FULL si tx ≈ invoice OU si tx > invoice (= refund Shopify dépasse la facture initiale,
    # cas typique des prix Shopify modifiés depuis la facturation → on cap au montant facture).
    is_full = abs(tx_total - inv_total) < 0.05 or tx_total > inv_total

    # Facture d'origine à 0 € (ou négative) : il n'y a rien à créditer. Sans ce
    # garde-fou, le cap « tx > facture » produisait un avoir vide de 0 €.
    if inv_total <= 0:
        log.error(f"    [{order_name}] 🛑 facture {inv_number} à {inv_total:.2f}€ — "
                  f"rien à créditer alors que Shopify a remboursé {tx_total:.2f}€ ; à revoir à la main")
        return {"status": "skip_invoice_zero", "order": order_name, "refund_id": refund_id,
                "inv_number": inv_number, "inv_total": inv_total, "tx_total": tx_total}

    avoir_lines = []
    total_target_ttc = 0.0

    if is_full:
        # FULL refund : on mirror TOUTES les lignes de la facture originale.
        # Cas particulier : tx > invoice (prix modifiés Shopify) → on cap automatiquement.
        if tx_total > inv_total + 0.05:
            log.info(f"    [{order_name}] tx Shopify ({tx_total:.2f}) > facture ({inv_total:.2f}) → cap au montant facture")
        else:
            log.info(f"    [{order_name}] FULL refund détecté (tx={tx_total:.2f} ≈ invoice {inv_total:.2f}) — mirror all lines")
        for ln in invoice_lines:
            qty = float(ln.get("quantity", 0) or 0)
            if qty == 0: continue
            pid = (ln.get("product") or {}).get("id")
            line = {
                "label": f"Avoir - {ln.get('label','')}",
                "quantity": -qty,
                "unit": ln.get("unit","piece"),
                "raw_currency_unit_price": ln.get("raw_currency_unit_price"),
                "discount": ln.get("discount") or {"type":"relative","value":"0"},
            }
            if pid:
                line["product_id"] = int(pid)
            # Toujours passer vat_rate (sinon Pennylane utilise le default produit)
            line["vat_rate"] = ln.get("vat_rate", "FR_200")
            avoir_lines.append(line)
            total_target_ttc += float(ln.get("currency_amount", 0) or 0)
    else:
        # PARTIAL refund : on traite ligne par ligne via refund_line_items
        for rli in refund.get("refund_line_items", []):
            qty = int(rli.get("quantity", 0) or 0)
            if qty <= 0: continue
            sku = (rli.get("line_item") or {}).get("sku","") or ""
            title = (rli.get("line_item") or {}).get("title","") or ""
            target_ttc = float(rli.get("subtotal", 0) or 0)  # TTC (shop tax_included)
            if target_ttc == 0:
                log.warning(f"    [{order_name}] line sku={sku} subtotal=0 → skip ligne")
                continue
            match = find_pl_line_for_shopify_item(invoice_lines, sku, title)
            if not match:
                # Le libellé Pennylane ne reprend pas toujours le titre Shopify
                # (« 3 x Filet de camouflage rectangulaire 3×8 m » côté facture
                # contre « Filet de camouflage renforcé polyester - Sable -
                # Carré / Rectangle - 3 x 8 m » côté boutique), et certaines
                # lignes n'ont pas de SKU. On créditait alors le montant au
                # total visé AVANT d'abandonner la ligne : l'argent disparaissait
                # de l'avoir sans laisser d'écart à solder, et un remboursement
                # dont aucune ligne ne matchait finissait purement ignoré
                # (skip_no_lines — cas #LFC39497, 707,97 €). On crée donc une
                # ligne au titre Shopify, au taux dominant de la facture.
                vat_code = dominant_vat(invoice_lines)
                ht = round(target_ttc / (1 + vat_to_decimal(vat_code)) / qty, 5)
                avoir_lines.append({
                    "label": f"Avoir - {title or 'ligne remboursée'}",
                    "quantity": -qty,
                    "unit": "piece",
                    "raw_currency_unit_price": str(ht),
                    "vat_rate": vat_code,
                    "discount": {"type": "relative", "value": "0"},
                })
                total_target_ttc += target_ttc
                log.warning(f"    [{order_name}] pas de ligne PL pour SKU={sku} "
                            f"title={title[:40]!r} → ligne créée au titre Shopify "
                            f"({target_ttc:.2f}€ @ {vat_code})")
                continue
            total_target_ttc += target_ttc

            product_id = (match.get("product") or {}).get("id")
            discount_pct = float((match.get("discount") or {}).get("value", "0"))
            vat_rate = vat_to_decimal(match.get("vat_rate", "FR_200"))
            unit_price = compute_raw_unit_price(target_ttc, -qty, discount_pct, vat_rate)

            ligne = {
                "label": f"Avoir - {match.get('label','')}",
                "quantity": -qty,
                "unit": "piece",
                "raw_currency_unit_price": str(unit_price),
                "discount": {"type": "relative", "value": str(discount_pct)},
                # Sans vat_rate explicite, Pennylane retombe sur le taux par
                # défaut du produit — faux dès que la facture porte un autre
                # taux (exempt, NL_210…). La branche FULL le passait déjà.
                "vat_rate": match.get("vat_rate", "FR_200"),
            }
            # product_id: null fait refuser tout le document (HTTP 400
            # « The schema of the object isn't any of the following » — cas
            # #HC4120) : la clé doit être absente, pas nulle.
            if product_id:
                ligne["product_id"] = int(product_id)
            avoir_lines.append(ligne)

        # Frais de port remboursés, gestes commerciaux, restocking fees : ils
        # vivent dans order_adjustments, pas dans les lignes. Tant qu'on les
        # ignorait, l'avoir ne retombait pas sur la somme réellement remboursée
        # — et quand le remboursement ne portait QUE des ajustements, il était
        # purement abandonné. On solde l'écart par une ligne dédiée.
        adjustments = refund.get("order_adjustments", []) or []
        delta = round(tx_total - total_target_ttc, 2)
        if abs(delta) > 0.05:
            vat_code = dominant_vat(invoice_lines)
            ht = round(abs(delta) / (1 + vat_to_decimal(vat_code)), 5)
            avoir_lines.append({
                "label": "Avoir - frais de port / ajustement remboursement",
                # Un delta négatif (restocking fee conservé) réduit l'avoir :
                # la quantité s'inverse, le prix unitaire reste positif.
                "quantity": -1 if delta > 0 else 1,
                "unit": "piece",
                "raw_currency_unit_price": str(ht),
                "vat_rate": vat_code,
                "discount": {"type": "relative", "value": "0"},
            })
            total_target_ttc += delta
            origine = (f"{len(adjustments)} order_adjustment(s)" if adjustments
                       else "écart lignes non expliqué par un ajustement")
            log.info(f"    [{order_name}] ajustement {delta:+.2f}€ @ {vat_code} "
                     f"({origine}) → ligne dédiée, avoir calé sur {tx_total:.2f}€")

    if not avoir_lines:
        return {"status": "skip_no_lines", "order": order_name, "refund_id": refund_id}

    # Garde-fou anti-sur-crédit : les avoirs déjà émis sur cette facture, plus
    # celui-ci, ne peuvent pas dépasser le montant facturé. C'est la barrière
    # qui manquait quand des sur-crédits non supprimables sont partis dans
    # Pennylane — elle tient même si l'anti-doublon passe à côté d'un avoir.
    #
    # Pennylane refuse la FINALISATION dès le premier centime de dépassement
    # (HTTP 422). Or Shopify rembourse parfois quelques centimes de plus que le
    # montant facturé, par simple arrondi entre les deux systèmes — cas #TZ6844
    # du 2026-09-02, 2 centimes sur un 2e remboursement. Sous SEUIL_PLAFONNEMENT
    # on rogne donc l'avoir au solde disponible ; au-delà, l'écart n'est plus un
    # arrondi et on bloque pour revue.
    deja_credite = sum(abs(a["amount"]) for a in (avoirs_existants or []))
    a_crediter = min(tx_total, inv_total) if is_full else total_target_ttc
    solde = round(inv_total - deja_credite, 2)
    depassement = round(a_crediter - solde, 2)
    plafonne = 0.0
    if depassement > 0.001:
        # Une remise absolue au niveau facture s'applique à l'ensemble des
        # lignes : y ajouter une ligne de plafonnement fausserait le total.
        remise_abs = is_full and (invoice.get("discount") or {}).get("type") == "absolute"
        if solde <= 0 or depassement > SEUIL_PLAFONNEMENT or remise_abs:
            motif = ("facture déjà intégralement créditée" if solde <= 0 else
                     "remise absolue au niveau facture — plafonnement non fiable" if remise_abs else
                     f"dépassement de {depassement:.2f}€ au-delà du seuil de {SEUIL_PLAFONNEMENT:.2f}€")
            log.error(f"    [{order_name}] 🛑 SUR-CRÉDIT évité : déjà crédité {deja_credite:.2f}€ "
                      f"+ {a_crediter:.2f}€ > facture {inv_total:.2f}€ — rien créé ({motif})")
            return {"status": "skip_overcredit", "order": order_name, "refund_id": refund_id,
                    "deja_credite": deja_credite, "a_crediter": a_crediter, "inv_total": inv_total,
                    "motif": motif}
        vat_code = dominant_vat(invoice_lines)
        ht = round(depassement / (1 + vat_to_decimal(vat_code)), 5)
        avoir_lines.append({
            "label": "Plafonnement au solde de la facture",
            "quantity": 1,   # positif : cette ligne REDUIT l'avoir
            "unit": "piece",
            "raw_currency_unit_price": str(ht),
            "vat_rate": vat_code,
            "discount": {"type": "relative", "value": "0"},
        })
        total_target_ttc -= depassement
        plafonne = depassement
        log.warning(f"    [{order_name}] ⚖️  avoir plafonné : {a_crediter:.2f}€ remboursés par Shopify "
                    f"mais solde disponible {solde:.2f}€ (déjà crédité {deja_credite:.2f}€) "
                    f"→ ligne de plafonnement de {depassement:.2f}€")

    # Règles :
    # - FULL refund → on mirror la facture initiale telle quelle (TTC avoir =
    #   TTC invoice originale, même s'il y a un écart d'1 centime avec Shopify
    #   dû à un ancien arrondi côté Pennylane).
    # - PARTIAL refund → chaque ligne est computée pour matcher Shopify exactement
    #   (compute_raw_unit_price() force le TTC ligne à la subtotal Shopify).

    # Pour les FULL refunds : mirror aussi le discount au niveau facture (sinon
    # les remises absolues ne s'inversent pas et l'avoir > facture → link KO).
    extra = {}
    if is_full:
        inv_discount = invoice.get("discount") or {"type":"relative","value":"0"}
        if inv_discount.get("type") == "absolute":
            # Négativer le montant pour qu'il ajoute (au lieu de soustraire) sur les lignes négatives
            inv_discount = {"type":"absolute", "value": str(-float(inv_discount.get("value", "0")))}
        extra["discount"] = inv_discount

    payload = {
        "date": AVOIR_DATE,
        "deadline": AVOIR_DATE,
        "customer_id": (invoice.get("customer") or {}).get("id"),
        "customer_invoice_template_id": int(store_config["template_id"]),
        "currency": "EUR",
        "special_mention": (f"Avoir concernant la facture {inv_number}\nCommande {order_name}\n"
                            f"Remboursement du {refund_date}\nRefund {refund_id}"
                            + (f"\nMontant plafonné au solde de la facture "
                               f"(Shopify a remboursé {a_crediter:.2f}€, solde disponible {solde:.2f}€)"
                               if plafonne else "")),
        "language": "fr_FR",
        "draft": True,
        "invoice_lines": avoir_lines,
        **extra,
    }

    if dry_run:
        log.info(f"    [{order_name}] DRY-RUN — TTC={total_target_ttc:.2f}€, {len(avoir_lines)} ligne(s) → would POST")
        return {"status": "dry_run", "order": order_name, "refund_id": refund_id,
                "total_ttc": total_target_ttc, "lines_count": len(avoir_lines), "plafonne": plafonne}

    # POST step 1: créer le draft
    r = pl_post("customer_invoices", payload)
    if r.status_code not in (200, 201):
        log.error(f"    [{order_name}] POST customer_invoices HTTP {r.status_code}: {r.text[:300]}")
        return {"status": "error_create", "order": order_name, "refund_id": refund_id, "error": r.text[:200]}
    new_inv = r.json()
    new_id = new_inv["id"]

    # Garde-fou : le TTC de l'avoir doit matcher le refund Shopify
    # - PARTIAL : avoir == tx_total Shopify (TTC iso Shopify, tolérance 0.05€)
    # - FULL    : avoir peut différer de tx_total si auto-cap (tx > invoice). On
    #            tolère tant que avoir ≈ min(tx_total, inv_total).
    avoir_amt = abs(float(new_inv.get("amount") or 0))
    if plafonne:
        expected = solde
    elif is_full:
        expected = min(tx_total, inv_total)
    else:
        expected = tx_total
    diff = round(avoir_amt - expected, 2)
    if abs(diff) > 0.05:
        log.error(f"    [{order_name}] ❌ TTC MISMATCH: avoir={avoir_amt}€ vs refund Shopify={tx_total}€ (Δ={diff:+.2f}€) — VÉRIFIER (avoir id={new_id}, NON lié)")
        return {"status": "error_amount_mismatch", "order": order_name, "refund_id": refund_id,
                "avoir_id": new_id, "avoir_amount": avoir_amt, "expected_ttc": expected, "delta": diff}

    # POST step 2: link to original
    rl = pl_post(f"customer_invoices/{inv_id}/link_credit_note", {"credit_note_id": int(new_id)})
    if rl.status_code not in (200, 201, 204):
        log.warning(f"    [{order_name}] link_credit_note HTTP {rl.status_code} (avoir créé id={new_id} mais NON lié)")
        return {"status": "warn_unlinked", "order": order_name, "refund_id": refund_id, "avoir_id": new_id, "total_ttc": new_inv.get("currency_amount")}

    log.info(f"    [{order_name}] ✅ avoir id={new_id} TTC={new_inv.get('currency_amount')} (refund {refund_date})")
    return {"status": "ok", "order": order_name, "refund_id": refund_id, "avoir_id": new_id,
            "total_ttc": new_inv.get("currency_amount"), "plafonne": plafonne}


# ═══════════════════════════════════════════════════════════════════════════
# ANTI-DOUBLON (réparé — LOT 3.1)
#
# Les deux angles morts qui ont produit des sur-crédits Pennylane non
# supprimables sont couverts ici :
#   1. la clé de déduplication est le refund_id, plus la facture — un 2e
#      remboursement d'une même commande n'est donc plus skippé à tort ;
#   2. les avoirs NON LIÉS (link_credit_note en échec, ou avoir saisi à la
#      main) sont détectés via le special_mention, et pas seulement via
#      credited_invoice.
# Un 3e garde-fou — le plafond de crédit cumulé — est appliqué dans
# process_one_refund() : la somme des avoirs ne peut pas dépasser la facture.
# ═══════════════════════════════════════════════════════════════════════════
_RE_REFUND_ID   = re.compile(r"Refund\s+(\d+)")
_RE_REFUND_DATE = re.compile(r"Remboursement du\s+(\d{4}-\d{2}-\d{2})")


def fetch_customer_invoices(customer_id):
    """Toutes les invoices d'un client (pagination complète)."""
    out = []
    if not customer_id:
        return out
    fl = json.dumps([{"field": "customer_id", "operator": "eq", "value": int(customer_id)}])
    cursor = None
    while True:
        params = {"filter": fl, "limit": 100}
        if cursor: params["cursor"] = cursor
        data = pl_get("customer_invoices", params)
        if not data: break
        out.extend(data.get("items", []))
        if not data.get("has_more"): break
        cursor = data.get("next_cursor")
        if not cursor: break
    return out


def find_avoirs_for_invoice(invoice_id, customer_id, order_name=None):
    """Tous les avoirs rattachés à cette facture — liés OU non liés.

    Un avoir « non lié » (link_credit_note en échec, ou avoir saisi à la main)
    n'a pas de credited_invoice : on le rattrape sur le numéro de commande
    présent dans son special_mention. C'est ce cas qui produisait les doublons."""
    needle = (order_name or "").lstrip("#")
    out = []
    for inv in fetch_customer_invoices(customer_id):
        amount = float(inv.get("amount") or 0)
        ci = inv.get("credited_invoice") or {}
        linked = bool(ci.get("id")) and str(ci.get("id")) == str(invoice_id)
        sm = inv.get("special_mention") or ""
        # Un avoir porte un montant négatif : ce test exclut la facture
        # d'origine, qui mentionne pourtant le même numéro de commande.
        mentioned = bool(needle) and needle in sm and amount < 0
        if not (linked or mentioned):
            continue
        m_id, m_date = _RE_REFUND_ID.search(sm), _RE_REFUND_DATE.search(sm)
        out.append({
            "id": inv.get("id"),
            "invoice_number": inv.get("invoice_number"),
            "amount": amount,
            "linked": linked,
            "special_mention": sm,
            "refund_id": m_id.group(1) if m_id else None,
            "refund_date": m_date.group(1) if m_date else None,
        })
    return out


def match_avoir_for_refund(avoirs, refund_id, refund_date, refund_ttc):
    """Ce refund précis a-t-il déjà son avoir ?

    Retourne (match, ambigus) : `match` est l'avoir couvrant CE refund (→ skip),
    enrichi d'un champ "match_par" disant sur quelle clé il a été reconnu ;
    `ambigus` liste les avoirs rattachés à la commande qu'on n'a pas su
    attribuer — on ne crée alors rien et on remonte le cas pour revue.

    Les clés vont de la plus sûre à la plus faible. Les avoirs saisis à la main
    dans Pennylane ne portent souvent que « Avoir concernant la facture F-… » :
    ni numéro de commande, ni date de remboursement. C'est pour eux qu'existe
    le rapprochement par montant.
    """
    rid = str(refund_id)

    def meme_montant(a):
        return bool(refund_ttc) and abs(abs(a["amount"]) - refund_ttc) <= 0.05

    # 1. Clé forte : le refund_id estampillé dans le special_mention.
    for a in avoirs:
        if a["refund_id"] and a["refund_id"] == rid:
            return {**a, "match_par": "refund_id"}, []
    # 2. Avoirs émis avant l'estampillage : ils portent la date du
    #    remboursement. Deux refunds le même jour sont départagés au montant.
    for a in avoirs:
        if a["refund_id"] or a["refund_date"] != refund_date:
            continue
        if refund_ttc and not meme_montant(a):
            continue
        return {**a, "match_par": "date de remboursement"}, []
    # 3. Avoirs sans référence exploitable (saisie manuelle) : le montant est
    #    le seul rapprochement possible. Un faux positif ici fait manquer un
    #    avoir — sous-crédit, rattrapable ; l'inverse créerait un doublon
    #    Pennylane non supprimable. On penche donc du côté du rapprochement.
    orphelins = [a for a in avoirs if not a["refund_id"] and not a["refund_date"]]
    for a in orphelins:
        if meme_montant(a):
            return {**a, "match_par": "montant"}, []
    # 4. Reste ce qu'on ne sait pas attribuer.
    return None, orphelins


def already_has_credit_note(invoice_id, customer_id):
    """Compat : un avoir existe-t-il pour cette facture, quel qu'il soit ?
    Utilisé par cancel_expired_orders.py, où l'annulation est totale et
    n'admet donc qu'un seul avoir par facture (dédup facture, pas refund)."""
    avoirs = find_avoirs_for_invoice(invoice_id, customer_id)
    return avoirs[0] if avoirs else None


def traiter_refunds(store, todo, dry_run=False, limit=0):
    """Traite une liste de (commande, refund) d'une boutique : facture d'origine,
    anti-doublon, création et lettrage. Partagé par main() et le cron nocturne.
    Retourne (stats, revue, plafonnes, resultats)."""
    stats = {"ok":0, "skip_existing":0, "skip_no_invoice":0, "skip_no_lines":0, "errors":0,
             "dry_run":0, "warn_unlinked":0, "error_amount_mismatch":0, "skip_ambigu":0,
             "skip_overcredit":0, "skip_invoice_zero":0}
    revue = []      # cas à trancher à la main
    resultats = []  # un dict par refund traité (pilote cron)
    plafonnes = []  # avoirs rognés au solde de la facture (information)
    for i, (o, ref) in enumerate(todo, 1):
        if limit and i > limit: break
        order_name = o["name"]
        refund_date = ref["created_at"][:10]
        log.info(f"\n[{i}/{len(todo)}] {order_name} refund {ref['id']} du {refund_date}")

        # Find original invoice
        try:
            inv = find_original_invoice(order_name, o["created_at"])
        except RechercheFactureImpossible as e:
            log.error(f"    {e} → rien créé, repris au prochain run")
            stats["errors"] += 1
            resultats.append({"status": "error_lookup", "order": order_name, "refund_id": ref["id"],
                              "error": str(e)}); continue
        if not inv:
            log.warning(f"    Pas de facture Pennylane trouvée pour {order_name} (date order {o['created_at'][:10]})")
            stats["skip_no_invoice"] += 1
            resultats.append({"status": "skip_no_invoice", "order": order_name, "refund_id": ref["id"]}); continue

        # Anti-doublon au niveau du REFUND (et non de la facture) : relecture
        # Pennylane avant chaque création, avoirs non liés compris.
        cust_id = (inv.get("customer") or {}).get("id")
        avoirs = find_avoirs_for_invoice(inv["id"], cust_id, order_name)
        refund_ttc = sum(float(tx.get("amount", 0) or 0)
                         for tx in ref.get("transactions", [])
                         if tx.get("kind") == "refund" and tx.get("status") == "success")
        match, ambigus = match_avoir_for_refund(avoirs, ref["id"], refund_date, refund_ttc)
        if match:
            lien = "lié" if match["linked"] else "NON lié"
            log.info(f"    ⏭  Avoir déjà existant pour CE refund : id={match['id']} "
                     f"(number={match.get('invoice_number')}, {lien}, "
                     f"reconnu par {match['match_par']}, {match['amount']}€)")
            stats["skip_existing"] += 1
            resultats.append({"status": "skip_existing", "order": order_name, "refund_id": ref["id"]}); continue
        if ambigus:
            ids = ", ".join(str(a["id"]) for a in ambigus)
            log.warning(f"    ⏭  {len(ambigus)} avoir(s) non identifiable(s) sur cette commande (id={ids}) : "
                        f"impossible de dire s'ils couvrent ce refund → rien créé, à trancher à la main")
            stats["skip_ambigu"] += 1
            revue.append((order_name, ref["id"], f"avoir(s) ambigu(s) id={ids}"))
            resultats.append({"status": "skip_ambigu", "order": order_name, "refund_id": ref["id"]})
            continue

        # Fetch invoice lines
        inv_lines = fetch_invoice_lines(inv["id"])
        if not inv_lines:
            log.warning(f"    Facture {inv['invoice_number']} : 0 ligne (??) → skip")
            stats["skip_no_lines"] += 1
            resultats.append({"status": "skip_no_lines", "order": order_name, "refund_id": ref["id"]}); continue

        # Process
        result = process_one_refund(o, ref, inv, inv_lines, store, dry_run=dry_run,
                                    avoirs_existants=avoirs)
        stats[result["status"]] = stats.get(result["status"], 0) + 1
        result["match_facture"] = inv.get("_match")
        result.setdefault("inv_number", inv.get("invoice_number"))
        resultats.append(result)
        if result["status"] == "skip_invoice_zero":
            revue.append((order_name, ref["id"],
                          f"facture {result['inv_number']} à {result['inv_total']:.2f}€ "
                          f"mais {result['tx_total']:.2f}€ remboursés côté Shopify"))
        if result["status"] == "skip_overcredit":
            revue.append((order_name, ref["id"],
                          f"sur-crédit : déjà {result['deja_credite']:.2f}€ + {result['a_crediter']:.2f}€ "
                          f"> facture {result['inv_total']:.2f}€ — {result.get('motif','')}"))
        if result.get("plafonne"):
            plafonnes.append((order_name, ref["id"], result["plafonne"]))
    return stats, revue, plafonnes, resultats


def main():
    parser = argparse.ArgumentParser(description="Crée des avoirs brouillon Pennylane depuis refunds Shopify")
    parser.add_argument("--shop", required=True, help="Code shop (LFC, RED, HET, MTC, RETE, TZ, LVO, UNIV, MO)")
    parser.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD (date refund min, inclus)")
    parser.add_argument("--to",   dest="date_to",   required=True, help="YYYY-MM-DD (date refund max, inclus)")
    parser.add_argument("--dry-run", action="store_true", help="N'écrit rien, log seulement ce qu'il ferait")
    parser.add_argument("--limit", type=int, default=0, help="Stop après N refunds traités (0=illimité)")
    args = parser.parse_args()

    # Cran de sûreté avoirs : le réel n'aboutit que si AURALIS_AVOIRS_ARMED=1 est
    # passé EN LIGNE (voir pl_post + README). Sinon chaque POST /customer_invoices
    # est refusé proprement. On ne force plus le dry-run : le blocage est au POST.
    if not args.dry_run and not _avoirs_armes():
        log.warning("ℹ️  Mode réel demandé mais cran de sûreté NON armé : les créations "
                    "d'avoir seront refusées. Relancer avec AURALIS_AVOIRS_ARMED=1 en ligne "
                    "(usage délibéré, plafond AURALIS_AVOIRS_CAP=5 par défaut).")

    if not PENNYLANE_TOKEN:
        log.error("PENNYLANE_TOKEN manquant"); sys.exit(1)

    store = next((s for s in STORES if s["name"] == args.shop), None)
    if not store:
        log.error(f"Shop inconnu : {args.shop}"); sys.exit(1)
    if not store["client_secret"]:
        log.error(f"SHOPIFY_SECRET_{args.shop} manquant dans .env"); sys.exit(1)

    log.info(f"=== Shop {args.shop} : refunds {args.date_from} → {args.date_to} ===")
    log.info("Fetch des commandes Shopify avec refunds...")
    orders = fetch_orders_with_refunds_in_period(store, args.date_from, args.date_to)
    log.info(f"  → {len(orders)} commandes avec refund (fenêtre élargie)")

    # Filter refunds par date
    todo = []  # list of (order, refund)
    for o in orders:
        for ref in o.get("refunds", []):
            rd = ref["created_at"][:10]
            if args.date_from <= rd <= args.date_to:
                todo.append((o, ref))
    log.info(f"  → {len(todo)} refunds dans la fenêtre [{args.date_from}, {args.date_to}]")

    stats, revue, plafonnes, _ = traiter_refunds(store, todo, dry_run=args.dry_run, limit=args.limit)

    if plafonnes:
        total = sum(p[2] for p in plafonnes)
        log.warning(f"\n=== AVOIRS PLAFONNÉS AU SOLDE DE LEUR FACTURE ({len(plafonnes)}, "
                    f"{total:.2f}€ non crédités) ===")
        for order_name, rid, ecart in plafonnes:
            log.warning(f"  {order_name} refund {rid} — rogné de {ecart:.2f}€")

    if revue:
        log.warning(f"\n=== À TRANCHER À LA MAIN ({len(revue)}) ===")
        for order_name, rid, motif in revue:
            log.warning(f"  {order_name} refund {rid} — {motif}")

    log.info(f"\n=== Bilan ===")
    for k, v in stats.items():
        if v: log.info(f"  {k:20} : {v}")


if __name__ == "__main__":
    main()

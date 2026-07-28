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

def get_shopify_token(shop):
    key = shop["store"]
    if key in _shopify_tokens: return _shopify_tokens[key]
    r = requests.post(f'https://{shop["store"]}/admin/oauth/access_token',
        data={"grant_type":"client_credentials","client_id":shop["client_id"],"client_secret":shop["client_secret"]},
        headers={"Content-Type":"application/x-www-form-urlencoded"}, timeout=15)
    r.raise_for_status()
    _shopify_tokens[key] = r.json()["access_token"]
    return _shopify_tokens[key]


def shopify_get(shop, path, params=None):
    tok = get_shopify_token(shop)
    r = requests.get(f'https://{shop["store"]}/admin/api/{SHOPIFY_API_VERSION}/{path}',
        headers={"X-Shopify-Access-Token": tok}, params=params, timeout=30)
    if r.status_code == 429:
        time.sleep(2); return shopify_get(shop, path, params)
    r.raise_for_status()
    return r


def fetch_orders_with_refunds_in_period(shop, date_from, date_to):
    """Récupère toutes les commandes mises à jour dans la fenêtre [from-30j, to+15j]
    avec financial_status partially_refunded ou refunded. Itère via pagination."""
    # On élargit la fenêtre updated_at pour ne pas rater une commande ancienne refundée en janvier
    grace_from = (datetime.fromisoformat(date_from) - timedelta(days=180)).strftime("%Y-%m-%dT00:00:00Z")
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
        r = requests.get(url, headers={"X-Shopify-Access-Token": tok}, params=params if "orders.json" in url else None, timeout=30)
        if r.status_code == 429: time.sleep(2); continue
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
    r = requests.get(f"{PL_BASE}/{path.lstrip('/')}", headers=PL_HEADERS, params=params, timeout=30)
    if r.status_code == 429: time.sleep(2); return pl_get(path, params)
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

def _avoirs_armes():
    return os.environ.get("AURALIS_AVOIRS_ARMED") == "1"

def _plafond_avoirs():
    try:
        return max(0, int(os.environ.get("AURALIS_AVOIRS_CAP", "5")))
    except ValueError:
        return 5

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
    global _avoirs_crees
    is_avoir = path.strip("/") == "customer_invoices"
    if is_avoir:
        if not _avoirs_armes():
            log.error("🔒 Création d'avoir BLOQUÉE (cran de sûreté). Usage délibéré : relancer avec "
                      "AURALIS_AVOIRS_ARMED=1 EN LIGNE (ne JAMAIS persister). "
                      "Plafond/lancement : AURALIS_AVOIRS_CAP (défaut 5).")
            return _BlockedResponse()
        if _avoirs_crees >= _plafond_avoirs():
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


def find_original_invoice(order_name, order_date_iso):
    """Trouve l'invoice Pennylane pour une commande Shopify.
    Scan -2 à +15 jours autour de l'order_date (les factures sont parfois créées
    quelques jours après la commande, notamment lors d'un batch de facturation).
    Pagination complète à chaque date scannée."""
    base = datetime.strptime(order_date_iso[:10], "%Y-%m-%d").date()
    needle = order_name.lstrip("#")
    for delta in range(-2, 16):  # -2 à +15 jours
        d = (base + timedelta(days=delta)).strftime("%Y-%m-%d")
        fl = json.dumps([{"field": "date", "operator": "eq", "value": d}])
        cursor = None
        while True:
            params = {"filter": fl, "limit": 100}
            if cursor: params["cursor"] = cursor
            data = pl_get("customer_invoices", params)
            if not data: break
            for inv in data.get("items", []):
                sm = (inv.get("special_mention") or "") + " " + (inv.get("label") or "")
                if needle in sm:
                    return inv
            if not data.get("has_more"): break
            cursor = data.get("next_cursor")
            if not cursor: break
    return None


def fetch_invoice_lines(invoice_id):
    """Récupère les lignes d'une facture."""
    data = pl_get(f"customer_invoices/{invoice_id}/invoice_lines", {"limit": 100})
    return data.get("items", []) if data else []


def find_existing_credit_notes(original_invoice_id):
    """Liste les avoirs déjà liés à cette facture (pour skip)."""
    # Scan : customer_invoices avec credited_invoice.id == original_invoice_id
    # Le filtre Pennylane sur credited_invoice n'étant pas simple, on scanne par date proche
    # plus large = scan via reverse search par customer_id ?
    # Simplest : on scan les invoices avec date proche de l'invoice originale + 6 mois
    existing = []
    # Search broadly recent invoices (last 13 months) for credit notes referring to this
    # Optimisé : just check the original invoice's "credit_notes" attribute (if present)
    # We saw originale has no direct "credit_notes" attr. So we scan.
    # Strategy: look at invoices with date between original.date and original.date+6m, filter credited_invoice
    return existing  # Pour MVP : on retourne vide, le check sera fait au POST si erreur


VAT_RATE_MAP = {
    "FR_200": 0.20, "FR_100": 0.10, "FR_055": 0.055, "FR_021": 0.021, "FR_000": 0.0,
    "ES_210": 0.21, "DE_190": 0.19, "BE_210": 0.21, "IT_220": 0.22, "NL_210": 0.21,
    "exempt": 0.0, "EXEMPT": 0.0, "NL_000": 0.0, "DE_000": 0.0, "ES_000": 0.0,
}


def vat_to_decimal(vat_str):
    if vat_str not in VAT_RATE_MAP:
        log.warning(f"  vat_rate inconnu : {vat_str!r} — fallback à 0%")
        return 0.0
    return VAT_RATE_MAP[vat_str]


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


def process_one_refund(order, refund, invoice, invoice_lines, store_config, dry_run=False):
    """Crée 1 draft avoir pour 1 refund Shopify. Retourne dict {status, ...}."""
    order_name = order["name"]
    refund_date = refund["created_at"][:10]
    refund_id   = refund["id"]
    inv_id      = invoice["id"]
    inv_number  = invoice["invoice_number"]

    # Détection FULL vs PARTIAL : compare transaction refund vs total invoice
    tx_total = sum(float(tx.get("amount",0) or 0) for tx in refund.get("transactions",[]) if tx.get("kind") == "refund")
    inv_total = float(invoice.get("currency_amount", "0") or 0)
    # FULL si tx ≈ invoice OU si tx > invoice (= refund Shopify dépasse la facture initiale,
    # cas typique des prix Shopify modifiés depuis la facturation → on cap au montant facture).
    is_full = abs(tx_total - inv_total) < 0.05 or tx_total > inv_total

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
            total_target_ttc += target_ttc

            match = find_pl_line_for_shopify_item(invoice_lines, sku, title)
            if not match:
                log.warning(f"    [{order_name}] Pas de ligne PL pour SKU={sku} title={title[:40]!r} → skip ligne")
                continue

            product_id = (match.get("product") or {}).get("id")
            discount_pct = float((match.get("discount") or {}).get("value", "0"))
            vat_rate = vat_to_decimal(match.get("vat_rate", "FR_200"))
            unit_price = compute_raw_unit_price(target_ttc, -qty, discount_pct, vat_rate)

            avoir_lines.append({
                "product_id": int(product_id) if product_id else None,
                "label": f"Avoir - {match.get('label','')}",
                "quantity": -qty,
                "unit": "piece",
                "raw_currency_unit_price": str(unit_price),
                "discount": {"type": "relative", "value": str(discount_pct)},
            })

        # Check adjustments (shipping_refund, restocking_fee…)
        adjustments = refund.get("order_adjustments", []) or []
        if adjustments:
            log.warning(f"    [{order_name}] ⚠️ {len(adjustments)} order_adjustment(s) ignoré(s) — vérifier manuellement (tx={tx_total:.2f} vs lines sum={total_target_ttc:.2f})")

    if not avoir_lines:
        return {"status": "skip_no_lines", "order": order_name, "refund_id": refund_id}

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
        "special_mention": f"Avoir concernant la facture {inv_number}\nCommande {order_name}\nRemboursement du {refund_date}",
        "language": "fr_FR",
        "draft": True,
        "invoice_lines": avoir_lines,
        **extra,
    }

    if dry_run:
        log.info(f"    [{order_name}] DRY-RUN — TTC={total_target_ttc:.2f}€, {len(avoir_lines)} ligne(s) → would POST")
        return {"status": "dry_run", "order": order_name, "refund_id": refund_id, "total_ttc": total_target_ttc, "lines_count": len(avoir_lines)}

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
    if is_full:
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
    return {"status": "ok", "order": order_name, "refund_id": refund_id, "avoir_id": new_id, "total_ttc": new_inv.get("currency_amount")}


def already_has_credit_note(invoice_id, customer_id):
    """Cherche si un avoir existe déjà lié à invoice_id (peu importe la date).
    On scanne toutes les invoices du customer (= rapide, narrow scope) et on
    filtre credited_invoice.id == invoice_id. Couvre les avoirs créés à
    n'importe quel moment, pas juste à proximité de la date de refund."""
    if not customer_id:
        return None
    fl = json.dumps([{"field":"customer_id","operator":"eq","value":int(customer_id)}])
    cursor = None
    while True:
        params = {"filter": fl, "limit": 100}
        if cursor: params["cursor"] = cursor
        data = pl_get("customer_invoices", params)
        if not data: break
        for inv in data.get("items", []):
            ci = inv.get("credited_invoice")
            if ci and str(ci.get("id")) == str(invoice_id):
                return inv
        if not data.get("has_more"): break
        cursor = data.get("next_cursor")
        if not cursor: break
    return None


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

    stats = {"ok":0, "skip_existing":0, "skip_no_invoice":0, "skip_no_lines":0, "errors":0, "dry_run":0, "warn_unlinked":0, "error_amount_mismatch":0}
    for i, (o, ref) in enumerate(todo, 1):
        if args.limit and i > args.limit: break
        order_name = o["name"]
        refund_date = ref["created_at"][:10]
        log.info(f"\n[{i}/{len(todo)}] {order_name} refund {ref['id']} du {refund_date}")

        # Find original invoice
        inv = find_original_invoice(order_name, o["created_at"])
        if not inv:
            log.warning(f"    Pas de facture Pennylane trouvée pour {order_name} (date order {o['created_at'][:10]})")
            stats["skip_no_invoice"] += 1; continue

        # Check if avoir already exists (search via customer_id, peu importe la date)
        existing = already_has_credit_note(inv["id"], (inv.get("customer") or {}).get("id"))
        if existing:
            log.info(f"    ⏭  Avoir déjà existant : id={existing['id']} (number={existing.get('invoice_number')})")
            stats["skip_existing"] += 1; continue

        # Fetch invoice lines
        inv_lines = fetch_invoice_lines(inv["id"])
        if not inv_lines:
            log.warning(f"    Facture {inv['invoice_number']} : 0 ligne (??) → skip")
            stats["skip_no_lines"] += 1; continue

        # Process
        result = process_one_refund(o, ref, inv, inv_lines, store, dry_run=args.dry_run)
        stats[result["status"]] = stats.get(result["status"], 0) + 1

    log.info(f"\n=== Bilan ===")
    for k, v in stats.items():
        if v: log.info(f"  {k:20} : {v}")


if __name__ == "__main__":
    main()

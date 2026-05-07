"""
finaliser_factures.py
Finalise les factures brouillon dans Pennylane et les marque comme payées.

1. Liste tous les brouillons
2. Finalise chaque brouillon (draft → facture définitive)
3. Marque comme payée si la commande Shopify est paid (pas pending)
"""

import os, json, time, re, logging, requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PENNYLANE_TOKEN = os.environ.get("PENNYLANE_TOKEN", "")
PL_BASE = "https://app.pennylane.com/api/external/v2"
PL_HEADERS = {"Authorization": f"Bearer {PENNYLANE_TOKEN}", "Content-Type": "application/json"}

# Préfixes des commandes concernées par le rattrapage
ORDER_PREFIXES = r'(?:LFC|RDC|HC|COCO|MO|RM|TZ|LVO|UNIV)'

# Commandes pending (virement) — à ne PAS marquer comme payées
# On les identifiera depuis la special_mention ou en recoupant avec Shopify


def pl_get(url, params=None):
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=PL_HEADERS, params=params, timeout=30)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
        if resp.status_code == 200: return resp.json()
        if resp.status_code == 429: time.sleep(min(2 ** attempt, 10)); continue
        return None
    return None


def pl_post(url, payload=None):
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=PL_HEADERS, json=payload, timeout=30)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
        if resp.status_code in (200, 201, 204): return resp
        if resp.status_code == 429: time.sleep(min(2 ** attempt, 10)); continue
        log.error(f"POST {url}: {resp.status_code} — {resp.text[:300]}")
        return resp
    return None


def pl_put(url, payload=None):
    for attempt in range(3):
        try:
            resp = requests.put(url, headers=PL_HEADERS, json=payload, timeout=30)
        except requests.exceptions.RequestException:
            time.sleep(min(2 ** attempt, 10))
            continue
        if resp.status_code in (200, 201, 204): return resp
        if resp.status_code == 429: time.sleep(min(2 ** attempt, 10)); continue
        log.error(f"PUT {url}: {resp.status_code} — {resp.text[:300]}")
        return resp
    return None


def get_all_drafts():
    """Récupère tous les brouillons de factures."""
    all_drafts = []
    cursor = None
    while True:
        params = {"filter": json.dumps([{"field": "draft", "operator": "eq", "value": "true"}]), "limit": 100}
        if cursor: params["cursor"] = cursor
        data = pl_get(f"{PL_BASE}/customer_invoices", params)
        if not data: break
        all_drafts.extend(data.get("items", []))
        if not data.get("has_more") or not data.get("next_cursor"): break
        cursor = data["next_cursor"]
    return all_drafts


def extract_order_name(invoice):
    """Extrait le numéro de commande depuis la special_mention de la facture."""
    sm = invoice.get("special_mention", "") or ""
    label = invoice.get("label", "") or ""
    text = f"{sm} {label}"
    m = re.search(ORDER_PREFIXES + r'\d{3,}', text, re.IGNORECASE)
    if m:
        return m.group(0).upper()
    return None


def is_pending_invoice(invoice):
    """Vérifie si la facture est en attente de paiement (virement)."""
    sm = invoice.get("special_mention", "") or ""
    return "attente de paiement" in sm.lower() or "virement" in sm.lower()


def run():
    log.info("=== Finalisation des factures brouillon ===")

    # 1. Récupérer tous les brouillons
    log.info("Chargement des brouillons...")
    drafts = get_all_drafts()
    log.info(f"{len(drafts)} brouillon(s) trouvé(s)")

    # 2. Filtrer ceux qui sont des commandes de rattrapage
    rattrapage_drafts = []
    for d in drafts:
        order_name = extract_order_name(d)
        if order_name:
            rattrapage_drafts.append({
                "id": d["id"],
                "invoice_number": d.get("invoice_number", ""),
                "order_name": order_name,
                "amount": d.get("amount", "0"),
                "is_pending": is_pending_invoice(d),
                "date": d.get("date", ""),
            })

    log.info(f"{len(rattrapage_drafts)} brouillon(s) de rattrapage à finaliser")

    nb_pending = sum(1 for d in rattrapage_drafts if d["is_pending"])
    nb_paid = len(rattrapage_drafts) - nb_pending
    log.info(f"  → {nb_paid} à marquer payées, {nb_pending} en attente (virement)")

    # 3. Finaliser + marquer payées
    finalized = 0
    marked_paid = 0
    errors = 0

    for i, draft in enumerate(rattrapage_drafts):
        inv_id = draft["id"]
        order_name = draft["order_name"]

        today = "2026-05-04"
        tomorrow = "2026-05-05"

        # Étape 1 : changer la date à aujourd'hui
        resp_date = pl_put(f"{PL_BASE}/customer_invoices/{inv_id}", {"date": today, "deadline": tomorrow})
        if not resp_date or resp_date.status_code not in (200, 201):
            log.error(f"  ❌ {order_name} — erreur changement date")
            errors += 1
            continue

        # Étape 2 : finaliser
        resp = pl_put(f"{PL_BASE}/customer_invoices/{inv_id}/finalize")
        if resp and resp.status_code in (200, 201):
            finalized += 1

            # Étape 3 : marquer comme payée si pas pending
            if not draft["is_pending"]:
                pay_resp = pl_put(f"{PL_BASE}/customer_invoices/{inv_id}/mark_as_paid")
                if pay_resp and pay_resp.status_code in (200, 201, 204):
                    marked_paid += 1
                    log.info(f"  ✅ {order_name} — finalisée + payée")
                else:
                    log.warning(f"  ⚠️ {order_name} — finalisée mais pas marquée payée")
            else:
                log.info(f"  🟡 {order_name} — finalisée (virement, non payée)")
        else:
            log.error(f"  ❌ {order_name} — erreur finalisation: {resp.text[:200] if resp else 'no response'}")
            errors += 1

        if (i + 1) % 50 == 0:
            log.info(f"  ... {i + 1}/{len(rattrapage_drafts)}")

        time.sleep(0.3)

    log.info(f"\n{'='*60}")
    log.info(f"TERMINÉ:")
    log.info(f"  Finalisées : {finalized}")
    log.info(f"  Marquées payées : {marked_paid}")
    log.info(f"  En attente (virement) : {nb_pending}")
    log.info(f"  Erreurs : {errors}")
    log.info(f"{'='*60}")


if __name__ == "__main__":
    run()

"""
shopify_smiirl.py — identifiants Shopify « smiirl-counter » pour les scripts d'avoirs.

Les seuls jeux d'identifiants qui portent read_all_orders sur les 9 boutiques
sont ceux de smiirl-counter. Le client_id LFC codé en dur dans
create_credit_note_drafts (app « COMPTA AUTO ») ne l'a pas : Shopify tronque
alors à 60 jours sans aucune erreur.

Source, par ordre de priorité :
  1. variables d'environnement SHOP_<i>, SHOP_<i>_CLIENT_ID, SHOP_<i>_CLIENT_SECRET
     (déploiement Render — mêmes noms que dans smiirl-counter) ;
  2. le fichier ~/smiirl-counter/.env (poste local).
"""
import os
import logging
import requests
from dotenv import dotenv_values

import create_credit_note_drafts as C

log = logging.getLogger("shopify_smiirl")

SMIIRL_ENV = os.path.expanduser("~/smiirl-counter/.env")


def _source():
    if os.environ.get("SHOP_1"):
        return dict(os.environ), "environnement"
    return dotenv_values(SMIIRL_ENV), SMIIRL_ENV


def cable_identifiants_smiirl():
    """Remplace les identifiants de create_credit_note_drafts par ceux de
    smiirl-counter, en gardant les template_id (propres à Pennylane).
    Retourne (stores, absents) ; lève RuntimeError si aucune source lisible."""
    v, origine = _source()
    if not v:
        raise RuntimeError(f"identifiants smiirl-counter introuvables ({origine})")
    par_domaine = {}
    for i in range(1, 20):
        dom = v.get(f"SHOP_{i}")
        if dom:
            par_domaine[dom] = (v.get(f"SHOP_{i}_CLIENT_ID"), v.get(f"SHOP_{i}_CLIENT_SECRET"))
    stores, absents = [], []
    for meta in C.STORES_META:
        cid, csec = par_domaine.get(meta["store"], (None, None))
        if not cid or not csec:
            log.warning(f"  ⚠️  {meta['name']} absent des identifiants smiirl-counter — boutique ignorée")
            absents.append(meta["name"])
            continue
        stores.append({**meta, "client_id": cid, "client_secret": csec})
    C.STORES = stores
    C._shopify_tokens.clear()
    log.info(f"  identifiants smiirl-counter câblés depuis {origine} ({len(stores)} boutiques)")
    return stores, absents


def verifie_scope(store):
    tok = C.get_shopify_token(store)
    r = requests.get(f'https://{store["store"]}/admin/oauth/access_scopes.json',
                     headers={"X-Shopify-Access-Token": tok}, timeout=20)
    r.raise_for_status()
    return "read_all_orders" in {x["handle"] for x in r.json().get("access_scopes", [])}

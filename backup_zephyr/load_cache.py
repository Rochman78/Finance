"""
load_cache.py
Charge toutes les données de cadrage (CA, TVA, Frais) depuis les APIs
et les stocke en cache SQLite pour un accès instantané.

Usage:
    python load_cache.py --date-min 2026-03-01 --date-max 2026-03-15
"""

import argparse, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date-min", required=True, help="Date début YYYY-MM-DD")
    parser.add_argument("--date-max", required=True, help="Date fin YYYY-MM-DD")
    args = parser.parse_args()

    dmin = args.date_min
    dmax = args.date_max

    log.info(f"=== Chargement cache {dmin} → {dmax} ===")

    # CA Shopify
    log.info("1/6 — CA Shopify...")
    from cadrage_ca import get_ca_shopify
    get_ca_shopify(dmin, dmax)

    # CA Pennylane
    log.info("2/6 — CA Pennylane (707/VT)...")
    from cadrage_ca import get_ca_pennylane
    get_ca_pennylane(dmin, dmax)

    # TVA Shopify
    log.info("3/6 — TVA Shopify...")
    from cadrage_tva import get_tva_shopify
    get_tva_shopify(dmin, dmax)

    # TVA Pennylane
    log.info("4/6 — TVA Pennylane (445/VT)...")
    from cadrage_tva import get_tva_pennylane
    get_tva_pennylane(dmin, dmax)

    # Frais Shopify (fast)
    log.info("5/6 — Frais Shopify (fast)...")
    from cadrage_frais_shopify import get_frais_shopify_fast
    get_frais_shopify_fast(dmin, dmax)

    # Frais Pennylane (627001)
    log.info("6/6 — Frais Pennylane (627001/ENCSP)...")
    from cadrage_frais_shopify import get_pennylane_627_detail
    get_pennylane_627_detail(dmin, dmax)

    log.info("=== Cache chargé avec succès ===")

    from db_cache import list_keys
    for k in list_keys():
        log.info(f"  {k['key']} (chargé le {k['loaded_at']})")


if __name__ == "__main__":
    main()

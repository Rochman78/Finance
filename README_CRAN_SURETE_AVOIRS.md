# Cran de sûreté — création d'avoirs (`create_credit_note_drafts.py`)

L'anti-doublon de `create_credit_note_drafts.py` a des angles morts (2ᵉ
remboursement d'une même commande ; avoir manuel préexistant non détecté via
`credited_invoice`) qui ont déjà produit des **sur-crédits Pennylane non
supprimables**. En attendant sa réparation, la création d'avoir est protégée par
un cran de sûreté au point d'étranglement unique `pl_post("customer_invoices")`
(tous les scripts d'avoir passent par là : `create_credit_note_drafts`,
`create_specific_avoirs`, `repair_all`, `cancel_expired_orders`).

## Comportement

- **Bloqué par défaut.** Sans déblocage explicite, tout POST de création d'avoir
  est refusé. Le **dry-run** et le **lettrage** ne sont jamais bloqués.
- **Déblocage à l'invocation** : variable d'environnement `AURALIS_AVOIRS_ARMED=1`.
- **Plafond par lancement** : `AURALIS_AVOIRS_CAP` avoirs (défaut **5**). Au-delà,
  arrêt net — c'est la protection principale, le passif vient d'un *volume*.
- **Trace** : chaque avoir créé est journalisé dans `avoirs_audit.log`
  (horodatage UTC, commande appelante, `special_mention`, montant, plafond).

## Usage (délibéré)

```bash
# Créer au plus 5 avoirs (défaut) :
AURALIS_AVOIRS_ARMED=1 python create_credit_note_drafts.py --shop RED --from 2026-01-01 --to 2026-01-31

# Relever le plafond ponctuellement :
AURALIS_AVOIRS_ARMED=1 AURALIS_AVOIRS_CAP=20 python create_credit_note_drafts.py ...
```

> ⚠️ **`AURALIS_AVOIRS_ARMED` NE DOIT JAMAIS ÊTRE PERSISTÉE.** Ni dans `.env`, ni
> dans `.env.example`, ni dans un script, un Makefile ou un workflow GitHub
> Actions. Elle se passe **uniquement en ligne**, au moment du lancement, comme
> acte délibéré. La persister annulerait le cran de sûreté.

## Levée définitive du cran

Le garde-fou pourra être retiré quand l'anti-doublon sera :
1. **reclé sur `refund_id`** (et non sur la facture), pour couvrir les
   remboursements multiples d'une même commande ;
2. doublé d'une **relecture Pennylane avant chaque création** (détecter un avoir
   préexistant quel que soit son mode de rattachement).

# Cran de sûreté — création d'avoirs (`create_credit_note_drafts.py`)

L'anti-doublon de `create_credit_note_drafts.py` avait deux angles morts (2ᵉ
remboursement d'une même commande ; avoir préexistant non détecté faute de
`credited_invoice`) qui ont produit des **sur-crédits Pennylane non
supprimables**. Ces deux angles morts sont **réparés** (voir « Levée du cran »
plus bas), mais le cran de sûreté reste en place au point d'étranglement unique
`pl_post("customer_invoices")` — tous les scripts d'avoir passent par là
(`create_credit_note_drafts`, `create_specific_avoirs`, `repair_all`,
`cancel_expired_orders`) — tant que la réparation n'a pas été validée sur une
campagne réelle.

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

## Levée du cran

Les deux conditions posées à la levée sont **remplies depuis le 2026-09-02** :

1. **Anti-doublon reclé sur le refund** — `match_avoir_for_refund()` rapproche
   un avoir d'un remboursement précis, par ordre de fiabilité décroissante :
   `refund_id` estampillé dans le `special_mention` (les avoirs créés à partir
   de maintenant le portent), puis date de remboursement + montant pour les
   avoirs antérieurs, puis montant seul pour les avoirs saisis à la main dans
   Pennylane (qui ne portent que « Avoir concernant la facture F-… »). Un 2ᵉ
   remboursement d'une même commande n'est donc plus skippé à tort.
2. **Relecture Pennylane avant chaque création** — `find_avoirs_for_invoice()`
   relit les factures du client et retient les avoirs rattachés à la facture,
   **liés ou non** : un avoir dont le `link_credit_note` a échoué est rattrapé
   via le numéro de commande présent dans son `special_mention`.

S'y ajoute une **troisième barrière**, indépendante des deux premières : dans
`process_one_refund()`, la somme des avoirs déjà émis sur une facture plus
celui en cours ne peut pas dépasser le montant facturé. Elle tient même si le
rapprochement passe à côté d'un avoir — c'est elle qui rend le sur-crédit
structurellement impossible.

Pennylane refuse la **finalisation** dès le premier centime de dépassement
(HTTP 422), or Shopify rembourse parfois quelques centimes de plus que le
montant facturé, par arrondi entre les deux systèmes. Cette barrière a donc
deux régimes, réglés par `SEUIL_PLAFONNEMENT` (1,00 € par défaut) :

- **dépassement ≤ seuil** → l'avoir est **rogné au solde disponible** par une
  ligne « Plafonnement au solde de la facture », et son `special_mention` note
  le montant réellement remboursé par Shopify. Les avoirs plafonnés sont
  récapitulés en fin de run ;
- **dépassement > seuil**, facture déjà intégralement créditée, ou remise
  absolue au niveau facture (où le plafonnement ne serait pas fiable) → rien
  n'est créé, le cas sort en `skip_overcredit` et part en revue manuelle.

Ce qu'on ne sait pas attribuer n'est jamais créé : les avoirs orphelins (ni
`refund_id`, ni date, ni montant concordant) sortent en `skip_ambigu` et sont
listés sous « À TRANCHER À LA MAIN » en fin de run.

Reste donc à **valider ces garde-fous sur une campagne réelle** avant de retirer
le cran. Tant que ce n'est pas fait, `AURALIS_AVOIRS_ARMED` + plafond restent
requis.

## Voie cron (`cron_avoirs_du_jour.py`)

Le cron du soir de Render crée et finalise chaque soir, à 22h heure de Paris,
les avoirs des remboursements du jour. Il ne peut pas utiliser
`AURALIS_AVOIRS_ARMED`, qui reste interdit de persistance. Il a donc son propre
déblocage :

- `AURALIS_AVOIRS_CRON=1` est **persisté dans l'environnement du service cron
  Render, et nulle part ailleurs**. Il n'arme rien par lui-même : seul le pilote,
  en appelant `activer_mode_cron()`, le rend opérant. Un script manuel lancé
  avec cette variable reste bloqué, et en mode cron `AURALIS_AVOIRS_ARMED` est
  ignoré (voir `test_cron_avoirs_du_jour.py`).
- Plafond propre : `AURALIS_AVOIRS_CRON_CAP`, **40** par nuit par défaut (environ 9
  remboursements par jour en moyenne). Au-delà, le run s'arrête net, envoie une
  alerte Telegram, et le reste est repris le lendemain grâce à la fenêtre glissante.
- La finalisation ne porte que sur les avoirs créés **par ce run**, après le
  contrôle de `finaliser_avoirs.controler()` (encore en brouillon, montant
  attendu, lié à une facture). Restent en brouillon, et sont signalés : les
  avoirs plafonnés, non liés ou au montant divergent, ceux dont la facture n'a
  été trouvée que par sa mention, ainsi que tout échec de finalisation. Dès qu'il
  y a un cas à revoir, un mail interne part vers `contact@zephyrosc.com` (`AVOIRS_MAIL_TO`,
  SMTP OVH de smiirl-counter) — jamais vers le client.

Chaque avoir reste tracé dans `avoirs_audit.log`. Ce fichier est éphémère sur
Render : la trace durable est le récap Telegram, envoyé chaque nuit, y compris
les nuits sans remboursement (« RAS »).

# Encaissements automatiques — Shopify / Klarna / Mollie → Pennylane

Pipeline d'automatisation comptable qui crée les écritures d'encaissement dans Pennylane à partir des versements (payouts/settlements) de Shopify Payments, Klarna et Mollie, pour 9 boutiques e-commerce.

## Architecture

```
                              ┌──────────────┐
                              │  PostgreSQL   │
                              │  (cache DB)   │
                              └──────┬───────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
              ▼                      ▼                      ▼
   ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
   │  Shopify API     │   │  Klarna API     │   │  Mollie API     │
   │  (9 boutiques)   │   │  (2 boutiques)  │   │  (7 boutiques)  │
   └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
            │                      │                      │
            ▼                      ▼                      ▼
   shopify_pennylane.py   klarna_pennylane.py   mollie_pennylane.py
            │                      │                      │
            └──────────────────────┼──────────────────────┘
                                   │
                                   ▼
                          ┌──────────────────┐
                          │  Pennylane API   │
                          │  (journal ENCSP) │
                          └──────────────────┘
```

## Commande cron (Render)

```bash
python sync_shopify_orders.py \
  && python db_loader.py --cron \
  && python shopify_pennylane.py --cron \
  && python klarna_pennylane.py --cron \
  && python mollie_pennylane.py --cron \
  && python solde_411.py --cron
```

L'ordre est important :
1. `sync_shopify_orders.py` — synchro des order Shopify avec payment_id Mollie (pour le matching)
2. `db_loader.py --cron` — charge factures et clients Pennylane en base (10 derniers jours)
3. `shopify_pennylane.py --cron` — crée les écritures Shopify Payments
4. `klarna_pennylane.py --cron` — crée les écritures Klarna
5. `mollie_pennylane.py --cron` — crée les écritures Mollie
6. `solde_411.py --cron` — apure les micro-écarts sur comptes 411

---

## Scripts

### db_loader.py

Synchronise les factures et clients depuis Pennylane vers PostgreSQL. Les scripts de paiement consultent cette base locale pour matcher commandes → factures → clients.

**Tables :**

| Table | Clé primaire | Description |
|-------|-------------|-------------|
| `invoices` | `order_number` | Factures Pennylane (ex: LFC30259, COCO3136) |
| `customers` | `id` | Clients Pennylane avec `ledger_account_id` |
| `processed_payouts` | `(store_name, payout_id)` | Payouts Shopify déjà traités |
| `processed_klarna_payouts` | `payment_reference` | Payouts Klarna déjà traités |
| `processed_mollie_settlements` | `mollie_id` | Settlements Mollie déjà traités |
| `shopify_orders` | `payment_id` | Mapping payment_id Mollie → order_name |
| `db_loader_meta` | `key` | Métadonnées (last_sync, etc.) |

**Extraction du numéro de commande** : regex appliquées sur `special_mention`, `label`, `filename` des factures Pennylane. Ex: `"Commande LFC30259\nFacture déjà payée"` → `LFC30259`.

**Pagination** : l'API Pennylane cap à 20 résultats/page malgré `limit=100`. Le script charge :
- Mode `--full` : mois par mois (filtre `date`)
- Mode `--cron` : jour par jour sur les 10 derniers jours (filtre `date`)

> **Note API 2026** : le champ `updated_at` n'est plus autorisé dans les filtres (migration Pennylane avril 2026). On utilise `date` à la place.

**Commandes :**
```bash
python db_loader.py --full    # Rechargement complet depuis 2026-01-01
python db_loader.py --cron    # Incrémental 10 derniers jours
```

---

### shopify_pennylane.py

Script principal. Pour chaque boutique, récupère les payouts Shopify Payments, détaille les transactions (charges + refunds), matche chaque commande à une facture Pennylane, et crée l'écriture comptable.

**Flux :**
```
Shopify Payouts API        →  liste des versements (payout_id, date, amount)
  └→ Transactions API      →  liste des transactions par payout (charge, refund)
       └→ Orders API        →  order_name par transaction
            └→ DB invoices  →  invoice_number + customer_id
                 └→ DB customers  →  ledger_account_id (compte 411)
                      └→ Pennylane POST /ledger_entries
```

**Auth Shopify** : OAuth client_credentials (`POST /admin/oauth/access_token`). Token caché avec 60s de marge avant expiration.

**API Shopify** :
```
GET /admin/api/2026-01/shopify_payments/payouts.json?date_min=YYYY-MM-DD
GET /admin/api/2026-01/shopify_payments/payouts/{id}/transactions.json
GET /admin/api/2026-01/orders/{id}.json
```

**Types de transaction** :
- `Payments::Charge` → vente (crédit compte client, débit frais)
- `Payments::Refund` → remboursement (débit compte client, crédit frais)
- Autres (payout, dispute, adjustment) → ignorés silencieusement

**Structure d'une écriture (journal ENCSP)** :

```
Label: "Versement Shopify Payments 2026-04-07 [LFC] 3572.66€"

  411INTERNET (trésorerie)     D: 3572.66    C: 0.00     ← montant net du payout
  411CLIENT1                   D: 0.00       C: 168.99   ← vente client 1
  627001                       D: 5.25       C: 0.00     ← frais client 1
  411CLIENT2                   D: 0.00       C: 536.04   ← vente client 2
  627001                       D: 7.22       C: 0.00     ← frais client 2
  ...
  411CLIENT_REFUND             D: 50.92      C: 0.00     ← remboursement
  627001 (écart)               D: X.XX       C: 0.00     ← ajustement si écart

  Validation : sum(D) == sum(C) ± 0.01
```

**Mode cron** : récupère tous les payouts depuis J-3, sans date max. Inclut les payouts `scheduled` (programmés) et `paid` (déposés).

**Anti-doublon** :
1. Table `processed_payouts` — skip si `(store_name, payout_id)` déjà présent
2. `ledger_entry_exists(date, label, journal_id)` — vérifie via API Pennylane avant création
3. Le montant est inclus dans le label pour différencier les payouts multiples du même jour/boutique

**Commandes :**
```bash
python shopify_pennylane.py --cron                   # Production
python shopify_pennylane.py --date 2026-04-07        # Date spécifique
python shopify_pennylane.py --date 2026-04-07 --test # Simulation (pas de création)
python shopify_pennylane.py --date 2026-04-07 --force # Ignore processed_payouts
python shopify_pennylane.py --from-date 2026-04-01 --date 2026-04-07  # Rattrapage
python shopify_pennylane.py --store LFC --date 2026-04-07  # Une seule boutique
```

---

### klarna_pennylane.py

Même logique que Shopify mais pour les settlements Klarna.

**Auth** : Basic auth (username/password par boutique).

**API Klarna** :
```
GET https://api.klarna.com/settlements/v1/payouts?start_date=X&end_date=Y
GET https://api.klarna.com/settlements/v1/transactions?payment_reference={ref}
```

**Types de transaction** : `SALE`, `RETURN`, `FEE` (avec TVA).

**Structure d'une écriture :**
```
Label: "Versement Klarna 2026-04-07 - LFC"

  411KLARNA (trésorerie)       D: {net}      C: 0.00
  411CLIENT                    D: 0.00       C: {sale}    ← montant vente
  627001                       D: {fee}      C: 0.00      ← frais HT
  44566                        D: {tva}      C: 0.00      ← TVA sur frais
```

**Boutiques** : LFC (K6272251), TAR (K6684056).

---

### mollie_pennylane.py

Même logique pour les settlements Mollie.

**Auth** : Bearer token (`MOLLIE_OAUTH_TOKEN`).

**API Mollie** :
```
GET https://api.mollie.com/v2/settlements?limit=50
GET https://api.mollie.com/v2/settlements/{id}/payments?limit=250
```

**Matching** : `payment.metadata.shopify_payment_id` → table `shopify_orders` → `order_name` → table `invoices`.

**Calcul des frais** : proportionnel au montant (`fee_ratio = (brut - net) / brut`).

**Compte fallback** : `411NA` si le client n'a pas de compte 411 dans Pennylane.

**Boutiques** : LFC, HET, TAR, RED, COCO, LOV, RETE.

---

### solde_411.py

Apure automatiquement les micro-écarts (< 0.03€) sur les comptes clients 411.

Pour chaque compte 411 ayant un solde résiduel inférieur au seuil :
- Solde débiteur → débit 658 (charges diverses) + crédit 411
- Solde créditeur → crédit 758 (produits divers) + débit 411

**Journal** : OD (Opérations Diverses).

---

### sync_shopify_orders.py

Alimente la table `shopify_orders` pour le matching Mollie. Pour chaque boutique, récupère les commandes payées via Mollie et stocke le mapping `payment_id → order_name`.

---

## Comptes Pennylane

| Compte | Usage |
|--------|-------|
| `411INTERNET` | Transit Shopify Payments |
| `411KLARNA` | Transit Klarna |
| `411MOLLIE` | Transit Mollie |
| `411NA` | Fallback client non-mappé (Mollie) |
| `411XXX` (par client) | Comptes clients individuels |
| `627001` | Frais de paiement |
| `44566` | TVA sur frais (Klarna uniquement) |
| `658` | Charges diverses (apurement écarts) |
| `758` | Produits divers (apurement écarts) |

**Journaux** : `ENCSP` (encaissements), `OD` (opérations diverses).

---

## Boutiques

| Code | Shopify Store | Klarna | Mollie |
|------|--------------|--------|--------|
| LFC | mon-filet-de-camouflage.myshopify.com | K6272251 | oui |
| RED | red-de-camuflaje.myshopify.com | — | oui |
| HET | het-camouflagenet.myshopify.com | — | oui |
| MTC/COCO | coconets.myshopify.com | — | oui |
| MO | mon-ombrage.myshopify.com | — | — |
| RETE | rete-mimetica.myshopify.com | — | oui |
| TZ/TAR | tarnnetz.myshopify.com | K6684056 | oui |
| LVO | le-filet-camouflage-1.myshopify.com | — | oui |
| UNIV | univers-camouflage.myshopify.com | — | — |

---

## Variables d'environnement

```bash
# Pennylane
PENNYLANE_TOKEN

# Base de données
DATABASE_URL               # postgresql://user:pass@host:5432/db

# Shopify (un secret par boutique)
SHOPIFY_SECRET_LFC
SHOPIFY_SECRET_RED
SHOPIFY_SECRET_HET
SHOPIFY_SECRET_MTC
SHOPIFY_SECRET_MO
SHOPIFY_SECRET_RETE
SHOPIFY_SECRET_TZ
SHOPIFY_SECRET_LVO
SHOPIFY_SECRET_UNIV

# Klarna
KLARNA_USERNAME_LFC / KLARNA_PASSWORD_LFC
KLARNA_USERNAME_TAR / KLARNA_PASSWORD_TAR

# Mollie
MOLLIE_OAUTH_TOKEN

# Telegram (optionnel, alertes erreurs uniquement)
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

---

## API externes

| Service | Version | Base URL | Auth |
|---------|---------|----------|------|
| Shopify | 2026-01 | `https://{store}/admin/api/2026-01` | OAuth client_credentials |
| Pennylane | v2 | `https://app.pennylane.com/api/external/v2` | Bearer token |
| Klarna | v1 | `https://api.klarna.com/settlements/v1` | Basic auth |
| Mollie | v2 | `https://api.mollie.com/v2` | Bearer token |

**Migration API Pennylane 2026 (avril 2026)** :
- Pagination : `per_page`/`page` remplacés par `limit`/`cursor` (cursor-based). L'ancien format renvoie un 400.
- Filtres : `updated_at` supprimé, utiliser `date`
- Réponses : les résultats sont dans la clé `"items"`, avec `"has_more"` et `"next_cursor"` pour la pagination
- Pas d'endpoint DELETE pour les écritures comptables
- Rate limit : 429, géré avec retry exponentiel (backoff) dans tous les scripts

---

## Protection anti-doublons

Trois niveaux de protection :

1. **Table `processed_*`** (PostgreSQL) — chaque payout/settlement traité avec succès est enregistré. Les runs suivants le sautent (sauf `--force`).

2. **`ledger_entry_exists()`** (API Pennylane) — avant toute création, vérifie si une écriture avec le même label + date + journal existe déjà. Le label inclut le montant pour différencier les payouts multiples du même jour.
   - **Pagination complète** : parcourt toutes les pages via cursor (pas de limite à 100 résultats)
   - **Fail-safe** : si l'API Pennylane ne répond pas (erreur, timeout, rate limit après 3 retries), l'écriture est **bloquée** (non créée). Principe : en cas de doute, ne pas créer.
   - **Retry automatique** : backoff exponentiel sur les erreurs 429 (rate limit)

3. **Validation d'équilibre** — toute écriture est vérifiée `sum(débits) == sum(crédits)` avant envoi. Rejetée si déséquilibrée.

---

## Notifications Telegram

Les scripts n'envoient de notification **qu'en cas d'erreur**. Aucun message sur les runs réussis. Format :

```
🚨 Shopify → Pennylane | 2026-04-04 → ∞
✅ 8 versement(s)
❌ 2 erreur(s)

❌ [LFC] LFC30259 pas de facture; LFC30258 pas de facture
✅ [RED] 2026-04-05 — 357.09€
...
```

---

## Debugging

```bash
# Vérifier les factures en base
python db_loader.py --full

# Tester un payout sans créer d'écriture
python shopify_pennylane.py --date 2026-04-07 --test

# Forcer le retraitement (ignore processed_payouts, PAS ledger_entry_exists)
python shopify_pennylane.py --date 2026-04-07 --force

# Lister les doublons dans Pennylane
python cleanup_duplicates.py --from 2026-04-01 --to 2026-04-07

# Supprimer un payout de processed_payouts pour le retraiter
python -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor()
cur.execute('DELETE FROM processed_payouts WHERE payout_id=%s', ('PAYOUT_ID',))
conn.commit(); conn.close()
"
```

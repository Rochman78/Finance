# Mollie → PennyLane — Cron automatique

Job cron qui s'exécute chaque nuit à 1h UTC, récupère les nouveaux versements Mollie des dernières 48h, et crée les écritures comptables correspondantes dans PennyLane.

## Flux

```
Cron 1h UTC
  → GET Mollie /v2/settlements (48h, status=paidout)
    → GET Mollie /v2/settlements/{id}/payments
      → Filtre anti-doublon (PostgreSQL)
        → Shopify : résolution commande via description Mollie
          → PennyLane : recherche facture par numéro commande
            → PennyLane : compte auxiliaire client (411XXXXXX)
              → POST PennyLane : 3 lignes d'écriture
                → Sauvegarde en base (status: success/error/skipped)
```

## Écritures générées (3 lignes par paiement)

| Compte      | Débit        | Crédit       |
|-------------|-------------|--------------|
| 411MOLLIE   | montant net  | —            |
| 411XXXXXX   | —            | montant brut |
| 627001      | frais Mollie | —            |

La ligne 627001 est omise si les frais sont nuls.

---

## Déploiement sur Render

### 1. Créer la base PostgreSQL

1. Sur [render.com](https://render.com) → **New → PostgreSQL**
2. Choisir le Free tier
3. Copier la **Internal Database URL** (sera utilisée comme `DATABASE_URL`)

### 2. Créer le Cron Job

1. **New → Cron Job**
2. Connecter le repo GitHub (`Rochman78/Finance`)
3. Définir le **Root Directory** : `mollie-pennylane`
4. **Build Command** : `npm install`
5. **Command** : `node index.js`
6. **Schedule** : `0 1 * * *` (1h UTC chaque nuit)
7. Laisser `RUN_ONCE=true` dans les env vars (Render Cron Job gère lui-même le scheduling)

> **Alternative** : créer un **Web Service** (toujours allumé) avec le scheduler interne node-cron.
> Dans ce cas, laisser `RUN_ONCE=false`.

### 3. Variables d'environnement à renseigner

| Variable              | Description                               |
|-----------------------|-------------------------------------------|
| `MOLLIE_API_KEY`      | Clé API Mollie (live_...)                 |
| `SHOPIFY_STORE_URL`   | URL du store (sans https://)              |
| `SHOPIFY_ACCESS_TOKEN`| Token d'accès Shopify (shpat_...)         |
| `PENNYLANE_API_KEY`   | Token Bearer PennyLane                    |
| `DATABASE_URL`        | Fourni automatiquement par Render         |
| `MODE_TEST`           | `true` pour simuler, `false` pour prod    |
| `RUN_ONCE`            | `true` pour Render Cron Job natif         |

### 4. Premier démarrage

La table `processed_settlements` est créée automatiquement au premier run.

Vérifier dans les logs Render :
```
[DB] Migrations OK — table processed_settlements prête
[Mollie] X settlement(s) paidout dans les dernières 48h
```

---

## Développement local

```bash
cd mollie-pennylane
npm install
cp .env.example .env
# Renseigner les variables dans .env
# MODE_TEST=true pour ne pas créer de vraies écritures
node index.js
```

## Structure

```
mollie-pennylane/
├── index.js                   # Entry point — cron ou run-once
├── jobs/
│   └── processMollie.js       # Logique principale
├── services/
│   ├── mollieService.js       # API Mollie
│   ├── shopifyService.js      # API Shopify
│   └── pennylaneService.js    # API PennyLane
├── db/
│   ├── client.js              # Pool PostgreSQL
│   └── migrations.js          # CREATE TABLE au boot
├── utils/
│   └── logger.js
└── .env.example
```

## Anti-doublon

La colonne `mollie_id` a une contrainte `UNIQUE`. Un paiement déjà traité est systématiquement ignoré (`status: skipped`), même si le job tourne deux fois.

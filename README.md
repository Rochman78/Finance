# Shopify → Google Sheets Daily Report

Chaque matin à 6h UTC (7h/8h Paris selon l'heure d'été), un job GitHub Actions récupère le **CA HT de la veille** pour chaque boutique Shopify et l'écrit dans une nouvelle colonne d'un Google Sheet.

---

## Résultat attendu

```
          A         B           C           D
1      Boutique  2026-03-04  2026-03-05  2026-03-06
2      LFC        4250.00     3890.00     5120.00
3      LVO         980.00     1200.00      750.00
4      UNI           0.00      320.00        0.00
5      TAR        2100.00     1850.00     2400.00
6      HET           0.00        0.00      150.00
7      RED         430.00        0.00      680.00
8      COCO          0.00        0.00        0.00
9      MON           0.00        0.00        0.00
10     RETE          0.00        0.00        0.00
11     TOTAL      =SUM(...)   =SUM(...)   =SUM(...)
```

Chaque run ajoute une colonne à droite. Si la date est déjà présente, le script s'arrête proprement (`already processed`).

---

## Mise en place (étape par étape)

### 1. Créer un projet Google Cloud et activer l'API Sheets

1. Aller sur [console.cloud.google.com](https://console.cloud.google.com)
2. Créer un nouveau projet (ou en sélectionner un existant)
3. Menu **APIs & Services → Bibliothèque**
4. Rechercher **Google Sheets API** et cliquer sur **Activer**

### 2. Créer un compte de service et télécharger la clé JSON

1. Menu **APIs & Services → Identifiants**
2. Cliquer **+ Créer des identifiants → Compte de service**
3. Donner un nom (ex. `sheets-reporter`), valider
4. Dans la liste des comptes de service, cliquer sur le compte créé
5. Onglet **Clés → Ajouter une clé → Créer une nouvelle clé → JSON**
6. Télécharger le fichier `.json` — il contient `client_email` et `private_key`

### 3. Partager le Google Sheet avec le service account

1. Ouvrir le Google Sheet cible
2. Cliquer sur **Partager**
3. Saisir l'email du service account (champ `client_email` du JSON, format `xxx@yyy.iam.gserviceaccount.com`)
4. Attribuer le rôle **Éditeur**
5. Valider — pas besoin d'envoyer de notification

### 4. Récupérer l'ID du Google Sheet

L'ID se trouve dans l'URL :
```
https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit
```
Copier la partie `<SHEET_ID>`.

### 5. Ajouter les secrets GitHub

Dans le dépôt → **Settings → Secrets and variables → Actions → New repository secret**

Créer un secret pour chaque variable listée dans `.env.example` :

| Secret | Valeur |
|---|---|
| `SHOPIFY_LFC_URL` | `https://lfc.myshopify.com` |
| `SHOPIFY_LFC_TOKEN` | Token Admin API de la boutique |
| … | … (même chose pour chaque boutique) |
| `GOOGLE_SHEET_ID` | ID du Google Sheet |
| `GOOGLE_SERVICE_ACCOUNT_EMAIL` | `client_email` du JSON |
| `GOOGLE_PRIVATE_KEY` | `private_key` du JSON (conserver les lignes `-----BEGIN/END-----`) |

> **Astuce `GOOGLE_PRIVATE_KEY`** : copier la valeur du champ `private_key` du JSON telle quelle dans le secret GitHub. Les `\n` seront interprétés automatiquement par le script.

### 6. Tester manuellement

1. Aller dans **Actions** du dépôt
2. Sélectionner le workflow **Daily Shopify Report**
3. Cliquer **Run workflow → Run workflow**
4. Vérifier les logs et le Google Sheet

---

## Structure du projet

```
/
├── .github/
│   └── workflows/
│       └── daily-report.yml      # Workflow GitHub Actions (cron 6h UTC)
├── jobs/
│   └── dailyReport.js            # Orchestrateur principal
├── services/
│   ├── shopifyService.js         # Appels API Shopify + pagination
│   └── googleSheetsService.js    # Lecture/écriture Sheets (ancrage dynamique)
├── config/
│   └── stores.js                 # Liste des boutiques
├── .env.example                  # Variables d'environnement à renseigner
└── package.json
```

---

## Robustesse

| Cas | Comportement |
|---|---|
| Boutique sans token/URL | Écrit `0`, log `[WARN]`, continue |
| Erreur API Shopify | Écrit `null`, log `[ERROR]`, continue les autres boutiques |
| Date déjà dans le sheet | Log `already processed`, arrêt propre (idempotent) |
| Sheet vierge | Initialise automatiquement les labels (Boutique, LFC … TOTAL) |
| Pagination Shopify (`Link: rel="next"`) | Boucle jusqu'à récupérer toutes les pages |

---

## Timezone

Toutes les dates sont calculées en **Europe/Paris**. Une commande passée à 23h30 heure française est incluse dans le CA de ce jour, même si UTC affiche déjà le lendemain.

---

## Développement local

```bash
# Copier et remplir les variables
cp .env.example .env

# Installer les dépendances
npm install

# Lancer manuellement
node jobs/dailyReport.js
```

Vous aurez besoin de [dotenv](https://www.npmjs.com/package/dotenv) ou d'exporter les variables manuellement pour un test local.

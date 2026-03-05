'use strict';

require('dotenv').config();

const { runMigrations } = require('./db/migrations');
const { processMollie } = require('./jobs/processMollie');
const logger = require('./utils/logger');

// ---------------------------------------------------------------------------
// Configuration des 7 boutiques Mollie
// ---------------------------------------------------------------------------
const STORES = [
  {
    name: 'LFC',
    mollieKey:    process.env.MOLLIE_API_KEY_LFC,
    shopifyUrl:   'mon-filet-de-camouflage.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_LFC,
  },
  {
    name: 'HET',
    mollieKey:    process.env.MOLLIE_API_KEY_HET,
    shopifyUrl:   'het-camouflagenet.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_HET,
  },
  {
    name: 'TAR',
    mollieKey:    process.env.MOLLIE_API_KEY_TAR,
    shopifyUrl:   'tarnnetz.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_TAR,
  },
  {
    name: 'RED',
    mollieKey:    process.env.MOLLIE_API_KEY_RED,
    shopifyUrl:   'red-de-camuflaje.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_RED,
  },
  {
    name: 'COCO',
    mollieKey:    process.env.MOLLIE_API_KEY_COCO,
    shopifyUrl:   'coconets.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_COCO,
  },
  {
    name: 'LOV',
    mollieKey:    process.env.MOLLIE_API_KEY_LOV,
    shopifyUrl:   'le-filet-camouflage-1.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_LOV,
  },
  {
    name: 'RETE',
    mollieKey:    process.env.MOLLIE_API_KEY_RETE,
    shopifyUrl:   'rete-mimetica.myshopify.com',
    shopifyToken: process.env.SHOPIFY_ACCESS_TOKEN_RETE,
  },
];

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  await runMigrations();

  logger.info(`Mollie → PennyLane — ${STORES.length} boutiques à traiter`);

  for (const store of STORES) {
    if (!store.mollieKey || !store.shopifyToken) {
      logger.warn(`[${store.name}] Clés manquantes — boutique ignorée`);
      continue;
    }
    try {
      await processMollie(store);
    } catch (err) {
      logger.error(`[${store.name}] Erreur fatale: ${err.message}`);
    }
  }

  logger.info('Toutes les boutiques traitées.');
  process.exit(0);
}

main().catch(err => {
  logger.error('Erreur fatale au démarrage:', err.message);
  process.exit(1);
});

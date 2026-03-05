'use strict';

const axios = require('axios');
const logger = require('../utils/logger');

const API_VERSION = '2026-01';

// Préfixes de numéros de commande connus (à adapter si besoin)
const ORDER_PREFIXES = ['LFC', 'RED', 'HET', 'MTC', 'MO', 'RETE', 'TZ', 'LVO', 'UNIV', 'HC', 'RDC', 'COCO'];

function client(storeUrl, token) {
  return axios.create({
    baseURL: `https://${storeUrl}/admin/api/${API_VERSION}`,
    headers: {
      'X-Shopify-Access-Token': token,
      'Content-Type': 'application/json',
    },
    timeout: 15000,
  });
}

/**
 * Tente d'extraire un numéro de commande Shopify depuis la description Mollie.
 * La plupart des plugins Mollie envoient quelque chose comme :
 *   - "#LFC29292" ou "LFC29292"
 *   - "Order LFC29292"
 *   - "Commande #LFC29292"
 *   - "rh54kduEJHN559dfbqjihOW3u"  ← pas un numéro de commande
 */
function extractOrderName(description) {
  if (!description) return null;

  // Pattern : préfixe lettres + 3-6 chiffres (avec ou sans #)
  const pattern = new RegExp(`#?(${ORDER_PREFIXES.join('|')})(\\d{3,6})`, 'i');
  const match = description.match(pattern);
  if (match) return `${match[1].toUpperCase()}${match[2]}`;

  return null;
}

/**
 * Cherche une commande Shopify par son nom (ex: LFC29292).
 * Retourne { name, billingName } ou null.
 */
async function findOrderByName(orderName, storeUrl, token) {
  try {
    const { data } = await client(storeUrl, token).get('/orders.json', {
      params: {
        name: `#${orderName}`,
        status: 'any',
        fields: 'id,name,billing_address',
        limit: 5,
      },
    });

    const orders = data.orders ?? [];
    if (orders.length === 0) return null;

    const order = orders[0];
    const billingName = order.billing_address?.company || order.billing_address?.name || 'Client inconnu';
    return { name: orderName, billingName };
  } catch (err) {
    logger.warn(`[Shopify] Erreur recherche commande ${orderName}: ${err.message}`);
    return null;
  }
}

/**
 * Résout la commande depuis la description Mollie.
 * Stratégie 1 : extraire le numéro de commande depuis la description
 * Stratégie 2 : chercher dans les commandes récentes via transaction authorization
 */
async function resolveOrderFromPayment(molliePayment, storeUrl, token) {
  const description = molliePayment.description || '';
  const mollieId = molliePayment.id;

  // Stratégie 1 : le numéro de commande est dans la description
  const orderName = extractOrderName(description);
  if (orderName) {
    const order = await findOrderByName(orderName, storeUrl, token);
    if (order) {
      logger.info(`[Shopify] Commande trouvée via description: ${orderName} → ${order.billingName}`);
      return order;
    }
  }

  // Stratégie 2 : scanner les commandes récentes (fenêtre 72h) et
  // chercher celle dont la transaction authorization = mollieId ou description
  logger.info(`[Shopify] Stratégie 2 — scan des commandes récentes pour ${mollieId}`);
  try {
    const since = new Date(Date.now() - 72 * 60 * 60 * 1000).toISOString();
    const { data } = await client(storeUrl, token).get('/orders.json', {
      params: {
        status: 'any',
        created_at_min: since,
        fields: 'id,name,billing_address',
        limit: 250,
      },
    });

    for (const order of data.orders ?? []) {
      // Récupérer les transactions de la commande
      const { data: txData } = await client(storeUrl, token).get(`/orders/${order.id}/transactions.json`, {
        params: { fields: 'id,authorization,gateway' },
      });

      const match = (txData.transactions ?? []).some(
        tx => tx.authorization === mollieId || tx.authorization === description
      );

      if (match) {
        const billingName = order.billing_address?.company || order.billing_address?.name || 'Client inconnu';
        const name = order.name.replace('#', '');
        logger.info(`[Shopify] Commande trouvée via transaction: ${name} → ${billingName}`);
        return { name, billingName };
      }
    }
  } catch (err) {
    logger.warn(`[Shopify] Erreur stratégie 2 pour ${mollieId}: ${err.message}`);
  }

  logger.warn(`[Shopify] Aucune commande trouvée pour description="${description}" (mollieId=${mollieId})`);
  return null;
}

module.exports = { resolveOrderFromPayment };

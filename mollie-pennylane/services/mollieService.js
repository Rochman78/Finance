'use strict';

const axios = require('axios');
const logger = require('../utils/logger');

const BASE = 'https://api.mollie.com/v2';

function client(apiKey) {
  return axios.create({
    baseURL: BASE,
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    timeout: 15000,
  });
}

/**
 * Récupère les settlements des dernières 48h avec statut "paidout".
 * Mollie ne permet pas de filtrer par date via query param sur /settlements,
 * on pagine et on s'arrête quand settledAt sort de la fenêtre.
 */
async function getRecentSettlements(windowHours = 48, apiKey) {
  const cutoff = new Date(Date.now() - windowHours * 60 * 60 * 1000);
  const settlements = [];
  let url = '/settlements?limit=50';

  while (url) {
    const { data } = await client(apiKey).get(url);
    const items = data._embedded?.settlements ?? [];

    let reachedOld = false;
    for (const s of items) {
      if (s.status !== 'paidout') continue;

      const settledAt = new Date(s.settledAt);
      if (settledAt < cutoff) {
        reachedOld = true;
        break;
      }
      settlements.push(s);
    }

    if (reachedOld || !data._links?.next?.href) break;

    // next href est absolu (https://api.mollie.com/v2/settlements?...)
    // on extrait le path+query
    const nextUrl = new URL(data._links.next.href);
    url = nextUrl.pathname.replace('/v2', '') + nextUrl.search;
  }

  logger.info(`[Mollie] ${settlements.length} settlement(s) paidout dans les dernières ${windowHours}h`);
  return settlements;
}

/**
 * Récupère tous les paiements d'un settlement (pagination complète).
 * Seuls les paiements "paid" ou "paidout" avec amount > 0 sont retournés.
 */
async function getSettlementPayments(settlementId, apiKey) {
  const payments = [];
  let url = `/settlements/${settlementId}/payments?limit=250`;

  while (url) {
    const { data } = await client(apiKey).get(url);
    const items = data._embedded?.payments ?? [];
    payments.push(...items);

    if (!data._links?.next?.href) break;
    const nextUrl = new URL(data._links.next.href);
    url = nextUrl.pathname.replace('/v2', '') + nextUrl.search;
  }

  // Garder uniquement les paiements réels (pas les remboursements / chargebacks)
  const realPayments = payments.filter(p => {
    const amount = parseFloat(p.amount?.value ?? '0');
    return amount > 0 && ['paid', 'paidout'].includes(p.status);
  });

  logger.info(`[Mollie] Settlement ${settlementId} : ${realPayments.length} paiement(s) réel(s) (total brut inclus refunds: ${payments.length})`);
  return realPayments;
}

module.exports = { getRecentSettlements, getSettlementPayments };

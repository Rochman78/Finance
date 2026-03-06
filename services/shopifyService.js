'use strict';

const fetch = require('node-fetch');

/**
 * Fetch all paid orders for a store on a given day (Europe/Paris timezone).
 * Handles Shopify cursor-based pagination via Link headers.
 *
 * @param {string} storeUrl   - Base store URL, e.g. https://xxx.myshopify.com
 * @param {string} token      - Shopify Admin API access token
 * @param {string} dateMin    - ISO datetime string for start of day (Paris)
 * @param {string} dateMax    - ISO datetime string for end of day (Paris)
 * @returns {Promise<Array>}  - Array of order objects with total_price & total_tax
 */
async function fetchOrdersForDay(storeUrl, token, dateMin, dateMax) {
  const baseUrl = (storeUrl.startsWith('http') ? storeUrl : `https://${storeUrl}`).replace(/\/$/, '');
  const allOrders = [];

  let url =
    `${baseUrl}/admin/api/2023-10/orders.json` +
    `?status=any` +
    `&financial_status=paid` +
    `&created_at_min=${encodeURIComponent(dateMin)}` +
    `&created_at_max=${encodeURIComponent(dateMax)}` +
    `&fields=total_price,total_tax` +
    `&limit=250`;

  while (url) {
    const response = await fetch(url, {
      headers: {
        'X-Shopify-Access-Token': token,
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      const body = await response.text();
      throw new Error(`Shopify API error ${response.status}: ${body}`);
    }

    const data = await response.json();
    allOrders.push(...(data.orders || []));

    // Follow cursor pagination
    const linkHeader = response.headers.get('link') || '';
    const nextMatch = linkHeader.match(/<([^>]+)>;\s*rel="next"/);
    url = nextMatch ? nextMatch[1] : null;
  }

  return allOrders;
}

/**
 * Calculate revenue excl. tax (CA HT) from an array of orders.
 *
 * @param {Array} orders
 * @returns {number} rounded to 2 decimal places
 */
function computeCAHT(orders) {
  const total = orders.reduce((sum, order) => {
    return sum + parseFloat(order.total_price) - parseFloat(order.total_tax);
  }, 0);
  return Math.round(total * 100) / 100;
}

/**
 * Fetch the CA HT for a single store on a given day.
 * Returns 0 (and logs a warning) if credentials are missing.
 * Returns null (and logs the error) if the API call fails.
 *
 * @param {{ name: string, urlEnv: string, tokenEnv: string }} store
 * @param {string} dateMin
 * @param {string} dateMax
 * @returns {Promise<number|null>}
 */
async function getStoreCAHT(store, dateMin, dateMax) {
  const storeUrl = process.env[store.urlEnv];
  const token    = process.env[store.tokenEnv];

  if (!token || !storeUrl) {
    console.warn(`[WARN] Missing credentials for store ${store.name} — writing 0`);
    return 0;
  }

  try {
    const orders = await fetchOrdersForDay(storeUrl, token, dateMin, dateMax);
    const caHT   = computeCAHT(orders);
    console.log(`[INFO] ${store.name}: ${orders.length} order(s) → CA HT = ${caHT}`);
    return caHT;
  } catch (err) {
    console.error(`[ERROR] ${store.name}: ${err.message}`);
    return null;
  }
}

module.exports = { getStoreCAHT };

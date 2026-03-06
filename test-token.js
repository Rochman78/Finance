'use strict';

// Usage: SHOPIFY_URL=xxx.myshopify.com SHOPIFY_TOKEN=shpss_xxx node test-token.js
const fetch = require('node-fetch');

const storeUrl = process.env.SHOPIFY_URL;
const token    = process.env.SHOPIFY_TOKEN;

if (!storeUrl || !token) {
  console.error('Usage: SHOPIFY_URL=xxx.myshopify.com SHOPIFY_TOKEN=shpss_xxx node test-token.js');
  process.exit(1);
}

const baseUrl = `https://${storeUrl.replace(/^https?:\/\//, '').replace(/\/$/, '')}`;
const url = `${baseUrl}/admin/api/2023-10/orders.json?status=any&financial_status=paid&limit=1&fields=id,total_price`;

console.log(`Testing: ${baseUrl}`);

fetch(url, {
  headers: { 'X-Shopify-Access-Token': token }
}).then(async res => {
  const body = await res.json();
  if (res.ok) {
    console.log(`✓ SUCCESS — ${body.orders.length} order(s) returned`);
  } else {
    console.error(`✗ ERROR ${res.status}:`, JSON.stringify(body));
  }
}).catch(err => {
  console.error('✗ Network error:', err.message);
});

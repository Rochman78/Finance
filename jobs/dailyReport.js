'use strict';

const { DateTime } = require('luxon');
const { STORES }   = require('../config/stores');
const { getStoreCAHT }       = require('../services/shopifyService');
const { writeDailyReport }   = require('../services/googleSheetsService');

const TIMEZONE = 'Europe/Paris';

/**
 * Compute the date range for "yesterday" in Europe/Paris timezone.
 * Returns ISO strings with timezone offset that Shopify will parse correctly.
 */
function getYesterdayRange() {
  const yesterday = DateTime.now().setZone(TIMEZONE).minus({ days: 1 });

  const dateMin = yesterday.startOf('day').toISO();   // e.g. 2026-03-05T00:00:00+01:00
  const dateMax = yesterday.endOf('day').toISO();     // e.g. 2026-03-05T23:59:59.999+01:00
  const dateLabel = yesterday.toISODate();             // e.g. 2026-03-05

  return { dateMin, dateMax, dateLabel };
}

async function main() {
  console.log('[INFO] Daily Shopify → Google Sheets report starting');

  const { dateMin, dateMax, dateLabel } = getYesterdayRange();
  console.log(`[INFO] Fetching data for ${dateLabel} (${dateMin} → ${dateMax})`);

  // Fetch CA HT for every store (in parallel, errors are contained per-store)
  const results = await Promise.all(
    STORES.map(store => getStoreCAHT(store, dateMin, dateMax))
  );

  const storeNames = STORES.map(s => s.name);

  // Log summary
  storeNames.forEach((name, i) => {
    console.log(`[SUMMARY] ${name}: ${results[i] === null ? 'ERROR (null)' : results[i]}`);
  });

  // Write to Google Sheets
  await writeDailyReport(dateLabel, storeNames, results);

  console.log('[INFO] Report complete');
}

main().catch(err => {
  console.error('[FATAL]', err);
  process.exit(1);
});

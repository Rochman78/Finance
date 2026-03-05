'use strict';

require('dotenv').config();

const cron = require('node-cron');
const { runMigrations } = require('./db/migrations');
const { processMollie } = require('./jobs/processMollie');
const logger = require('./utils/logger');

async function main() {
  // Crée la table si elle n'existe pas encore
  await runMigrations();

  // --- Mode one-shot (ex: test manuel ou Render Cron Job natif) ---
  // Si RUN_ONCE=true, on exécute une fois et on sort.
  if (process.env.RUN_ONCE === 'true') {
    logger.info('Mode RUN_ONCE activé — exécution unique');
    await processMollie();
    process.exit(0);
  }

  // --- Mode cron interne (Web Service Render avec scheduler embarqué) ---
  // Schedule : 0 1 * * *  → tous les jours à 1h00 UTC
  const schedule = process.env.CRON_SCHEDULE ?? '0 1 * * *';
  logger.info(`Cron planifié : "${schedule}" (UTC)`);

  cron.schedule(schedule, async () => {
    await processMollie();
  }, { timezone: 'UTC' });

  logger.info('Service démarré. En attente du prochain déclenchement...');

  // Garde le process en vie (nécessaire pour le scheduler interne)
  process.on('SIGTERM', () => {
    logger.info('SIGTERM reçu — arrêt propre');
    process.exit(0);
  });
}

main().catch(err => {
  logger.error('Erreur fatale au démarrage:', err.message);
  process.exit(1);
});

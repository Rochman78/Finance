'use strict';

const pool = require('./client');
const logger = require('../utils/logger');

const SQL = `
  CREATE TABLE IF NOT EXISTS processed_settlements (
    id              SERIAL PRIMARY KEY,
    mollie_id       VARCHAR(255) UNIQUE NOT NULL,
    payment_ref     VARCHAR(255),
    order_name      VARCHAR(50),
    invoice_number  VARCHAR(100),
    amount          DECIMAL(10,2),
    processed_at    TIMESTAMP DEFAULT NOW(),
    status          VARCHAR(50)   -- 'success' | 'error' | 'skipped'
  );

  CREATE INDEX IF NOT EXISTS idx_processed_settlements_mollie_id
    ON processed_settlements(mollie_id);
`;

async function runMigrations() {
  try {
    await pool.query(SQL);
    logger.info('[DB] Migrations OK — table processed_settlements prête');
  } catch (err) {
    logger.error('[DB] Erreur migration:', err.message);
    throw err;
  }
}

module.exports = { runMigrations };

'use strict';

const axios = require('axios');
const logger = require('../utils/logger');

const BASE = 'https://app.pennylane.com/api/external/v2';

// Cache local pour éviter les appels répétés sur le même run
const _cache = {
  accounts: {},     // number → id
  journals: null,   // array
  customers: {},    // pennylaneCustomerId → { name, accountNumber, accountId }
};

function client() {
  return axios.create({
    baseURL: BASE,
    headers: {
      Authorization: `Bearer ${process.env.PENNYLANE_API_KEY}`,
      'Content-Type': 'application/json',
    },
    timeout: 15000,
  });
}

// ---------------------------------------------------------------------------
// Comptes comptables
// ---------------------------------------------------------------------------

async function getAccountId(accountNumber) {
  if (_cache.accounts[accountNumber]) return _cache.accounts[accountNumber];

  const filter = JSON.stringify([{ field: 'number', operator: 'eq', value: accountNumber }]);
  const { data } = await client().get('/ledger_accounts', { params: { filter, per_page: 5 } });

  const items = data.items ?? [];
  if (items.length === 0) {
    logger.warn(`[PennyLane] Compte introuvable: ${accountNumber}`);
    return null;
  }

  _cache.accounts[accountNumber] = items[0].id;
  return items[0].id;
}

// ---------------------------------------------------------------------------
// Journaux
// ---------------------------------------------------------------------------

async function getJournalId(code) {
  if (!_cache.journals) {
    const { data } = await client().get('/journals', { params: { per_page: 100 } });
    _cache.journals = data.items ?? [];
  }

  const journal = _cache.journals.find(j => j.code?.toUpperCase() === code.toUpperCase());
  if (!journal) {
    logger.error(`[PennyLane] Journal introuvable: ${code}`);
    return null;
  }
  return journal.id;
}

// ---------------------------------------------------------------------------
// Factures clients
// ---------------------------------------------------------------------------

/**
 * Cherche la facture PennyLane liée à une commande Shopify.
 * Cherche d'abord dans special_mention, puis dans label.
 * Retourne { invoiceNumber, customerId, customerName } ou null.
 */
async function findInvoiceByOrderName(orderName) {
  // Essai 1 : special_mention contient le numéro de commande
  for (const field of ['special_mention', 'label']) {
    try {
      const filter = JSON.stringify([
        { field, operator: 'contains', value: orderName },
      ]);
      const { data } = await client().get('/customer_invoices', {
        params: { filter, per_page: 5 },
      });

      const items = data.items ?? [];
      if (items.length > 0) {
        const inv = items[0];
        const customer = inv.customer ?? {};
        logger.info(`[PennyLane] Facture trouvée (via ${field}): ${inv.invoice_number} pour commande ${orderName}`);
        return {
          invoiceNumber: inv.invoice_number,
          customerId: customer.id ?? customer.source_id,
          customerName: customer.name ?? 'Client inconnu',
        };
      }
    } catch (err) {
      logger.warn(`[PennyLane] Erreur recherche facture (field=${field}): ${err.message}`);
    }
  }

  logger.warn(`[PennyLane] Aucune facture trouvée pour commande ${orderName}`);
  return null;
}

// ---------------------------------------------------------------------------
// Clients — numéro de compte auxiliaire
// ---------------------------------------------------------------------------

/**
 * Récupère le numéro de compte comptable (411XXXXXX) d'un client PennyLane.
 */
async function getCustomerAccountNumber(customerId) {
  if (_cache.customers[customerId]) return _cache.customers[customerId];

  try {
    const { data } = await client().get(`/customers/${customerId}`);
    const customer = data.customer ?? data;
    const ledgerAccount = customer.ledger_account ?? {};
    const accountNumber = ledgerAccount.number ?? null;
    const accountId = ledgerAccount.id ?? null;
    const name = customer.name ?? 'Client inconnu';

    _cache.customers[customerId] = { name, accountNumber, accountId };
    return _cache.customers[customerId];
  } catch (err) {
    logger.warn(`[PennyLane] Erreur récupération client ${customerId}: ${err.message}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Création d'écriture comptable
// ---------------------------------------------------------------------------

/**
 * Crée une écriture comptable dans PennyLane.
 *
 * @param {object} params
 * @param {string} params.date           - YYYY-MM-DD
 * @param {string} params.label          - libellé global
 * @param {string} params.journalCode    - ex: ENCSP
 * @param {object[]} params.lines        - lignes d'écriture
 * @param {string}   params.piece        - ex: ENCSP-F-2026-02-24-8897
 */
async function createLedgerEntry({ date, label, journalCode, lines, piece }) {
  const journalId = await getJournalId(journalCode);
  if (!journalId) throw new Error(`Journal ${journalCode} introuvable`);

  // Résolution des account IDs pour chaque ligne
  const resolvedLines = [];
  for (const line of lines) {
    let accountId = line.accountId;

    if (!accountId && line.accountNumber) {
      accountId = await getAccountId(line.accountNumber);
    }

    if (!accountId) {
      throw new Error(`Compte introuvable: ${line.accountNumber}`);
    }

    resolvedLines.push({
      ledger_account_id: accountId,
      debit:  line.debit  != null ? String(parseFloat(line.debit).toFixed(2))  : '0.00',
      credit: line.credit != null ? String(parseFloat(line.credit).toFixed(2)) : '0.00',
      label: line.label ?? label,
    });
  }

  // Vérification équilibre débit/crédit
  const totalDebit  = resolvedLines.reduce((s, l) => s + parseFloat(l.debit),  0);
  const totalCredit = resolvedLines.reduce((s, l) => s + parseFloat(l.credit), 0);
  if (Math.abs(totalDebit - totalCredit) > 0.01) {
    throw new Error(`Écriture déséquilibrée: débit=${totalDebit.toFixed(2)} crédit=${totalCredit.toFixed(2)}`);
  }

  const payload = {
    date,
    label,
    journal_id: journalId,
    ledger_entry_lines: resolvedLines,
    ...(piece ? { reference: piece } : {}),
  };

  if (process.env.MODE_TEST === 'true') {
    logger.info(`[PennyLane] [MODE TEST] Simulation écriture: ${label} | piece=${piece}`);
    resolvedLines.forEach(l =>
      logger.info(`  → Compte ${l.ledger_account_id}  D:${l.debit}  C:${l.credit}`)
    );
    return { id: 'TEST', status: 'simulated' };
  }

  const { data } = await client().post('/ledger_entries', payload);
  logger.info(`[PennyLane] Écriture créée (id=${data.id ?? data.ledger_entry?.id})`);
  return data;
}

module.exports = {
  getAccountId,
  findInvoiceByOrderName,
  getCustomerAccountNumber,
  createLedgerEntry,
};

'use strict';

const pool = require('../db/client');
const mollie = require('../services/mollieService');
const shopify = require('../services/shopifyService');
const pennylane = require('../services/pennylaneService');
const logger = require('../utils/logger');

const JOURNAL_CODE    = 'ENCSP';
const COMPTE_MOLLIE   = '411MOLLIE';
const COMPTE_FRAIS    = '627001';
const COMPTE_CLIENT_FALLBACK = '411NA';
const WINDOW_HOURS    = 48;

// ---------------------------------------------------------------------------
// Persistence
// ---------------------------------------------------------------------------

async function isAlreadyProcessed(mollieId) {
  const { rows } = await pool.query(
    'SELECT id FROM processed_settlements WHERE mollie_id = $1',
    [mollieId]
  );
  return rows.length > 0;
}

async function saveResult({ mollieId, paymentRef, orderName, invoiceNumber, amount, status }) {
  await pool.query(
    `INSERT INTO processed_settlements
       (mollie_id, payment_ref, order_name, invoice_number, amount, status)
     VALUES ($1, $2, $3, $4, $5, $6)
     ON CONFLICT (mollie_id) DO UPDATE SET status = EXCLUDED.status`,
    [mollieId, paymentRef, orderName, invoiceNumber, amount, status]
  );
}

// ---------------------------------------------------------------------------
// Traitement d'un paiement individuel
// ---------------------------------------------------------------------------

async function processPayment(payment) {
  const mollieId   = payment.id;
  const description = payment.description ?? '';
  const amount      = parseFloat(payment.amount?.value ?? '0');
  const settlAmt    = parseFloat(payment.settlementAmount?.value ?? payment.amount?.value ?? '0');
  const frais       = parseFloat((amount - settlAmt).toFixed(2));
  const paymentDate = (payment.createdAt ?? '').slice(0, 10); // YYYY-MM-DD

  logger.info(`\n--- Paiement ${mollieId} | ${amount}€ (net: ${settlAmt}€, frais: ${frais}€)`);

  // 1. Anti-doublon
  if (await isAlreadyProcessed(mollieId)) {
    logger.info(`  → Déjà traité, skip`);
    return { status: 'skipped' };
  }

  // 2. Résolution Shopify
  const shopifyOrder = await shopify.resolveOrderFromPayment(payment);
  if (!shopifyOrder) {
    await saveResult({ mollieId, paymentRef: description, amount, status: 'error' });
    return { status: 'error', reason: 'Commande Shopify introuvable' };
  }

  const { name: orderName, billingName } = shopifyOrder;

  // 3. Résolution PennyLane — facture
  const invoice = await pennylane.findInvoiceByOrderName(orderName);
  if (!invoice) {
    await saveResult({ mollieId, paymentRef: description, orderName, amount, status: 'error' });
    return { status: 'error', reason: `Facture PennyLane introuvable pour ${orderName}` };
  }

  const { invoiceNumber, customerId, customerName } = invoice;
  const piece = `${JOURNAL_CODE}-${invoiceNumber}`;

  // 4. Résolution du compte auxiliaire client
  let clientAccountNumber = COMPTE_CLIENT_FALLBACK;
  let clientAccountId = null;

  if (customerId) {
    const customerInfo = await pennylane.getCustomerAccountNumber(customerId);
    if (customerInfo?.accountNumber) {
      clientAccountNumber = customerInfo.accountNumber;
      clientAccountId = customerInfo.accountId ?? null;
    }
  }

  if (clientAccountNumber === COMPTE_CLIENT_FALLBACK) {
    logger.warn(`  → Compte client introuvable pour ${customerName} — utilisation de ${COMPTE_CLIENT_FALLBACK}`);
  }

  // 5. Libellé
  const libelle = `${customerName} - ${orderName}`;

  // 6. Construction des 3 lignes d'écriture
  const lines = [
    // Ligne 1 : 411MOLLIE débit net
    {
      accountNumber: COMPTE_MOLLIE,
      debit: settlAmt,
      credit: 0,
      label: libelle,
    },
    // Ligne 2 : compte client crédit brut
    {
      accountNumber: clientAccountNumber,
      accountId: clientAccountId,
      debit: 0,
      credit: amount,
      label: libelle,
    },
  ];

  // Ligne 3 : frais Mollie uniquement si > 0
  if (frais > 0.001) {
    lines.push({
      accountNumber: COMPTE_FRAIS,
      debit: frais,
      credit: 0,
      label: libelle,
    });
  }

  // 7. Création de l'écriture
  try {
    await pennylane.createLedgerEntry({
      date: paymentDate,
      label: libelle,
      journalCode: JOURNAL_CODE,
      lines,
      piece,
    });
  } catch (err) {
    logger.error(`  → Erreur création écriture: ${err.message}`);
    await saveResult({ mollieId, paymentRef: description, orderName, invoiceNumber, amount, status: 'error' });
    return { status: 'error', reason: err.message };
  }

  // 8. Marquer comme traité
  await saveResult({ mollieId, paymentRef: description, orderName, invoiceNumber, amount, status: 'success' });
  logger.info(`  → OK : ${libelle} | ${amount}€ | piece=${piece}`);
  return { status: 'success', orderName, invoiceNumber, amount };
}

// ---------------------------------------------------------------------------
// Job principal
// ---------------------------------------------------------------------------

async function processMollie() {
  const startedAt = new Date();
  logger.info('========================================');
  logger.info('Mollie → PennyLane — démarrage du job');
  logger.info(`Fenêtre : ${WINDOW_HOURS}h | Mode test : ${process.env.MODE_TEST === 'true' ? 'OUI' : 'NON'}`);
  logger.info('========================================');

  let totalSuccess = 0;
  let totalError   = 0;
  let totalSkipped = 0;

  try {
    const settlements = await mollie.getRecentSettlements(WINDOW_HOURS);

    if (settlements.length === 0) {
      logger.info('Aucun settlement paidout dans les dernières 48h.');
      return;
    }

    for (const settlement of settlements) {
      logger.info(`\nSettlement ${settlement.id} | ${settlement.amount?.value}€ | ${settlement.settledAt}`);

      let payments;
      try {
        payments = await mollie.getSettlementPayments(settlement.id);
      } catch (err) {
        logger.error(`Erreur récupération paiements du settlement ${settlement.id}: ${err.message}`);
        totalError++;
        continue;
      }

      for (const payment of payments) {
        try {
          const result = await processPayment(payment);
          if      (result.status === 'success') totalSuccess++;
          else if (result.status === 'skipped') totalSkipped++;
          else                                  totalError++;
        } catch (err) {
          logger.error(`Erreur inattendue sur paiement ${payment.id}: ${err.message}`);
          totalError++;
          // Tentative de sauvegarde de l'erreur en base
          try {
            await saveResult({
              mollieId: payment.id,
              paymentRef: payment.description,
              amount: parseFloat(payment.amount?.value ?? '0'),
              status: 'error',
            });
          } catch (_) { /* ignore */ }
        }
      }
    }
  } catch (err) {
    logger.error(`Erreur fatale du job: ${err.message}`);
  }

  const durationMs = Date.now() - startedAt.getTime();
  logger.info('========================================');
  logger.info(`Job terminé en ${(durationMs / 1000).toFixed(1)}s`);
  logger.info(`  Succès : ${totalSuccess}`);
  logger.info(`  Erreurs: ${totalError}`);
  logger.info(`  Skipped: ${totalSkipped}`);
  logger.info('========================================');
}

module.exports = { processMollie };

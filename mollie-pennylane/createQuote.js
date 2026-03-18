#!/usr/bin/env node
'use strict';

/**
 * Script autonome pour créer un devis PennyLane.
 *
 * Utilisable par un agent Claude qui génère le chiffrage puis appelle ce script :
 *
 *   node createQuote.js '{
 *     "client": "Dupont SAS",
 *     "date": "2026-03-18",
 *     "deadline": "2026-04-18",
 *     "subject": "Devis prestation Mars 2026",
 *     "lines": [
 *       { "label": "Audit initial", "quantity": 1, "unitPrice": "1500.00", "vatRate": "FR_200" },
 *       { "label": "Développement", "quantity": 5, "unit": "day", "unitPrice": "800.00", "vatRate": "FR_200" }
 *     ]
 *   }'
 *
 * Ou via stdin :
 *   echo '{ ... }' | node createQuote.js
 *
 * Variables d'environnement requises : PENNYLANE_API_KEY
 * Optionnel : MODE_TEST=true pour simuler sans créer dans PennyLane
 */

require('dotenv').config();

const { findCustomer, createQuote } = require('./services/pennylaneQuoteService');
const logger = require('./utils/logger');

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

async function main() {
  let raw = process.argv[2];
  if (!raw) {
    if (process.stdin.isTTY) {
      console.error('Usage: node createQuote.js \'<JSON>\' ou echo \'<JSON>\' | node createQuote.js');
      process.exit(1);
    }
    raw = await readStdin();
  }

  let input;
  try {
    input = JSON.parse(raw);
  } catch (err) {
    console.error('Erreur JSON:', err.message);
    process.exit(1);
  }

  // --- Résolution du client ---
  let customerId = input.customerId;

  if (!customerId && input.client) {
    logger.info(`Recherche du client "${input.client}" dans PennyLane…`);
    const matches = await findCustomer(input.client);
    if (matches.length === 0) {
      console.error(`Aucun client trouvé pour "${input.client}". Créez-le dans PennyLane ou passez customerId.`);
      process.exit(1);
    }
    customerId = matches[0].id;
    logger.info(`Client trouvé: ${matches[0].name} (id=${customerId})`);
    if (matches.length > 1) {
      logger.warn(`Attention: ${matches.length} clients correspondent — le premier est utilisé.`);
    }
  }

  if (!customerId) {
    console.error('Veuillez fournir "client" (nom) ou "customerId" (ID PennyLane).');
    process.exit(1);
  }

  // --- Construction des lignes ---
  const lines = (input.lines ?? []).map((l) => ({
    label: l.label,
    quantity: l.quantity ?? 1,
    raw_currency_unit_price: l.unitPrice ?? l.raw_currency_unit_price,
    vat_rate: l.vatRate ?? l.vat_rate ?? 'FR_200',
    unit: l.unit ?? 'piece',
    description: l.description,
    product_id: l.productId ?? l.product_id,
    section_rank: l.sectionRank ?? l.section_rank,
    discount: l.discount,
  }));

  // --- Création du devis ---
  const result = await createQuote({
    date: input.date ?? new Date().toISOString().slice(0, 10),
    deadline: input.deadline,
    customerId,
    currency: input.currency ?? 'EUR',
    language: input.language ?? 'fr_FR',
    subject: input.subject,
    freeText: input.freeText,
    description: input.description,
    specialMention: input.specialMention,
    externalRef: input.externalRef,
    discount: input.discount,
    sections: input.sections,
    lines,
  });

  // --- Output JSON pour l'agent ---
  const output = {
    success: true,
    quoteId: result.id,
    quoteNumber: result.quote_number,
    status: result.status,
    amount: result.amount ?? result.currency_amount,
    pdfUrl: result.public_file_url,
  };

  console.log(JSON.stringify(output, null, 2));
}

main().catch((err) => {
  console.error(JSON.stringify({ success: false, error: err.message }));
  process.exit(1);
});

#!/usr/bin/env node
'use strict';

/**
 * Script autonome pour créer un devis PennyLane.
 *
 * Utilisable par un agent Claude qui génère le chiffrage puis appelle ce script :
 *
 *   node createQuote.js '{
 *     "customer": {
 *       "type": "individual",
 *       "firstName": "Mathieu",
 *       "lastName": "BAJARD",
 *       "address": { "address": "26 impasse de l amenier", "postalCode": "05230", "city": "Chorges" }
 *     },
 *     "subject": "Devis filet camouflage",
 *     "lines": [
 *       { "label": "Filet polyester sable 0.75x0.95m", "quantity": 2, "unitPrice": "45.00", "vatRate": "FR_200", "unit": "m2" }
 *     ]
 *   }'
 *
 * Le script :
 *   1. Cherche le client par nom + adresse dans PennyLane
 *   2. Si non trouvé, le crée automatiquement (particulier ou professionnel)
 *   3. Crée le devis avec les lignes fournies
 *   4. Retourne un JSON avec quoteId, quoteNumber, pdfUrl
 *
 * Customer types :
 *   - "individual" (particulier) : firstName, lastName requis
 *   - "company" (professionnel) : name requis, vatNumber optionnel
 *
 * Variables d'environnement requises : PENNYLANE_API_KEY
 * Optionnel : MODE_TEST=true pour simuler sans créer dans PennyLane
 */

require('dotenv').config();

const { findOrCreateCustomer, createQuote } = require('./services/pennylaneQuoteService');
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

  if (!customerId && input.customer) {
    const c = input.customer;
    customerId = await findOrCreateCustomer({
      type: c.type ?? 'individual',
      firstName: c.firstName,
      lastName: c.lastName,
      name: c.name,
      email: c.email,
      phone: c.phone,
      vatNumber: c.vatNumber,
      address: c.address,
    });
  }

  // Rétrocompatibilité : "client" = recherche par nom simple
  if (!customerId && input.client) {
    customerId = await findOrCreateCustomer({
      type: 'individual',
      name: input.client,
    });
  }

  if (!customerId) {
    console.error('Veuillez fournir "customer" (objet client) ou "customerId" (ID PennyLane).');
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

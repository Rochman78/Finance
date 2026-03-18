#!/usr/bin/env node
'use strict';

/**
 * Script autonome pour créer un devis PennyLane.
 *
 * Utilisable par un agent Claude qui génère le chiffrage puis appelle ce script.
 *
 * Exemple d'input JSON :
 *
 *   {
 *     "store": "LFC",
 *     "customer": {
 *       "type": "individual",
 *       "firstName": "Mathieu",
 *       "lastName": "BAJARD",
 *       "email": "matthieu.bajard@gmail.com",
 *       "phone": "0666893138",
 *       "address": { "address": "26 impasse de l'amendier", "postalCode": "05230", "city": "Chorges" }
 *     },
 *     "subject": "Devis filet polyester sable 0.75x0.95m",
 *     "lines": [
 *       {
 *         "type": "product",
 *         "label": "SABLE - 0.75x0.95 m - Filet de camouflage renforce corde de 6mm",
 *         "description": "Quantité : 2 | Total m² : 1.43 | Délai de production + livraison : environ 14 jours",
 *         "quantity": 1.43,
 *         "unitPrice": "45.00",
 *         "unit": "m2"
 *       },
 *       {
 *         "type": "transport",
 *         "label": "Transport sur mesure",
 *         "unitPrice": "19.99"
 *       },
 *       {
 *         "type": "transport_discount",
 *         "label": "Remise transport sur mesure",
 *         "unitPrice": "-19.99"
 *       }
 *     ]
 *   }
 *
 * Line types :
 *   - "product"            → rattaché au product_id 14369303 (filet template), surcharge label/qty/prix
 *   - "transport"          → ligne libre transport
 *   - "transport_discount" → ligne libre remise transport (prix négatif)
 *   - "accessory"          → ligne libre accessoire
 *   - "free"               → ligne libre quelconque
 *
 * Stores : LFC, HET, LVO, COCO, MON, RED, RETE, TAR, UNI
 *
 * Variables d'environnement requises : PENNYLANE_API_KEY
 * Optionnel : MODE_TEST=true pour simuler sans créer dans PennyLane
 */

require('dotenv').config();

const { findOrCreateCustomer, createQuote } = require('./services/pennylaneQuoteService');
const logger = require('./utils/logger');

// ---------------------------------------------------------------------------
// Mapping des boutiques → quote_template_id PennyLane
// ---------------------------------------------------------------------------

const STORE_TEMPLATES = {
  HET:  257162,   // Het Camouflage Net - DEVIS
  LFC:  253634,   // Le Filet de Camouflage - DEVIS
  LVO:  877143,   // LVO devis
  COCO: 257180,   // Ma toile coco - DEVIS
  MON:  883869,   // MON OMBRAGE Devis
  RED:  257168,   // Red de Camuflaje - DEVIS
  RETE: 861190,   // RETE devis
  TAR:  257174,   // Tarnnetz - DEVIS
  UNI:  883875,   // UNIVERS devis
};

// ---------------------------------------------------------------------------
// Product IDs templates (on surcharge label/qty/prix/description à chaque devis)
// ---------------------------------------------------------------------------

const PRODUCT_ID_FILET = 14369303;    // **** - *x* m - Filet... → quantité en m²
const PRODUCT_ID_GENERIC = 16822267;  // **** - Produit / Accessoire / Transport → quantité en pièce

// ---------------------------------------------------------------------------

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

  // --- Résolution de la boutique → template ---
  const store = (input.store ?? 'LFC').toUpperCase();
  const quoteTemplateId = STORE_TEMPLATES[store];
  if (!quoteTemplateId) {
    console.error(`Boutique inconnue: "${store}". Valeurs acceptées: ${Object.keys(STORE_TEMPLATES).join(', ')}`);
    process.exit(1);
  }
  logger.info(`Boutique: ${store} → template ${quoteTemplateId}`);

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
  const lines = (input.lines ?? []).map((l) => {
    const lineType = (l.type ?? 'free').toLowerCase();
    const line = {
      label: l.label,
      quantity: l.quantity ?? 1,
      raw_currency_unit_price: l.unitPrice ?? l.raw_currency_unit_price,
      vat_rate: l.vatRate ?? l.vat_rate ?? 'FR_200',
      unit: l.unit ?? 'piece',
      description: l.description,
      section_rank: l.sectionRank ?? l.section_rank,
      discount: l.discount,
    };

    // Rattacher au bon produit template selon le type de ligne
    if (lineType === 'product') {
      line.product_id = l.productId ?? l.product_id ?? PRODUCT_ID_FILET;
    } else {
      line.product_id = l.productId ?? l.product_id ?? PRODUCT_ID_GENERIC;
    }

    return line;
  });

  // --- Deadline par défaut : +30 jours ---
  let deadline = input.deadline;
  if (!deadline) {
    const d = new Date();
    d.setDate(d.getDate() + 30);
    deadline = d.toISOString().slice(0, 10);
  }

  // --- Création du devis ---
  const result = await createQuote({
    date: input.date ?? new Date().toISOString().slice(0, 10),
    deadline,
    customerId,
    quoteTemplateId,
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

'use strict';

const axios = require('axios');
const logger = require('../utils/logger');

const BASE = 'https://app.pennylane.com/api/external/v2';

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
// Recherche de client par nom ou email
// ---------------------------------------------------------------------------

/**
 * Recherche un client dans PennyLane.
 * Filtres supportés par l'API v2 : id, customer_type, ledger_account_id, name,
 * external_reference, reg_no, emails.
 * Seul l'opérateur "eq" est supporté pour "name" (pas "contains").
 */
async function findCustomer(search) {
  // Recherche exacte par nom
  const filter = JSON.stringify([
    { field: 'name', operator: 'eq', value: search },
  ]);
  try {
    const { data } = await client().get('/customers', {
      params: { filter, per_page: 10 },
    });
    if ((data.items ?? []).length > 0) return data.items;
  } catch (err) {
    logger.warn(`[PennyLane] Recherche exacte échouée: ${err.message}`);
  }

  // Recherche par email si le search ressemble à un email
  if (search.includes('@')) {
    const emailFilter = JSON.stringify([
      { field: 'emails', operator: 'eq', value: search },
    ]);
    try {
      const { data } = await client().get('/customers', {
        params: { filter: emailFilter, per_page: 10 },
      });
      if ((data.items ?? []).length > 0) return data.items;
    } catch (err) {
      logger.warn(`[PennyLane] Recherche par email échouée: ${err.message}`);
    }
  }

  return [];
}

// ---------------------------------------------------------------------------
// Création de client
// ---------------------------------------------------------------------------

/**
 * Crée un client dans PennyLane.
 *
 * @param {object} params
 * @param {string}  params.type        - "individual" ou "company"
 * @param {string}  [params.firstName] - Prénom (particulier)
 * @param {string}  [params.lastName]  - Nom (particulier)
 * @param {string}  [params.name]      - Raison sociale (professionnel)
 * @param {string}  [params.email]     - Email
 * @param {string}  [params.phone]     - Téléphone
 * @param {string}  [params.vatNumber] - Numéro de TVA (professionnel)
 * @param {object}  [params.address]   - { address, postalCode, city, countryAlpha2 }
 */
async function createCustomer({
  type = 'individual',
  firstName,
  lastName,
  name,
  email,
  phone,
  vatNumber,
  address,
}) {
  const payload = { customer_type: type };

  if (type === 'individual') {
    if (firstName) payload.first_name = firstName;
    if (lastName) payload.last_name = lastName;
  } else {
    if (name) payload.name = name;
    if (vatNumber) payload.vat_number = vatNumber;
  }

  if (email) payload.emails = [email];
  if (phone) payload.phone = phone;

  if (address) {
    const addr = {
      address: address.address ?? '',
      postal_code: address.postalCode ?? '',
      city: address.city ?? '',
      country_alpha2: address.countryAlpha2 ?? 'FR',
    };
    payload.billing_address = addr;
    payload.delivery_address = addr;
  }

  const { data } = await client().post('/customers', payload);
  logger.info(`[PennyLane] Client créé: ${data.name} (id=${data.id})`);
  return data;
}

/**
 * Recherche un client par nom. Si non trouvé, le crée automatiquement.
 * Retourne l'ID du client.
 */
async function findOrCreateCustomer(customerInfo) {
  const searchName = customerInfo.name
    || `${customerInfo.firstName ?? ''} ${customerInfo.lastName ?? ''}`.trim();

  if (!searchName) throw new Error('Nom du client requis');

  // Recherche par nom
  const matches = await findCustomer(searchName);

  // Vérification par adresse si plusieurs résultats
  if (matches.length > 0 && customerInfo.address) {
    const byAddress = matches.find((c) => {
      const addr = c.billing_address ?? {};
      const inputCity = (customerInfo.address.city ?? '').toLowerCase();
      const inputPostal = customerInfo.address.postalCode ?? '';
      return (addr.city ?? '').toLowerCase() === inputCity
        || (addr.postal_code ?? '') === inputPostal;
    });
    if (byAddress) {
      logger.info(`[PennyLane] Client trouvé par nom + adresse: ${byAddress.name} (id=${byAddress.id})`);
      return byAddress.id;
    }
  }

  if (matches.length > 0) {
    logger.info(`[PennyLane] Client trouvé par nom: ${matches[0].name} (id=${matches[0].id})`);
    return matches[0].id;
  }

  // Pas trouvé → création
  logger.info(`[PennyLane] Client "${searchName}" non trouvé, création en cours…`);
  const created = await createCustomer(customerInfo);
  return created.id;
}

// ---------------------------------------------------------------------------
// Création de devis
// ---------------------------------------------------------------------------

/**
 * Crée un devis (quote) dans PennyLane via l'API v2.
 *
 * @param {object} params
 * @param {string}   params.date             - Date du devis (YYYY-MM-DD)
 * @param {string}   params.deadline         - Date de validité (YYYY-MM-DD)
 * @param {number}   params.customerId       - ID client PennyLane
 * @param {string}   [params.currency]       - Devise (défaut: EUR)
 * @param {string}   [params.language]       - Langue du PDF (défaut: fr_FR)
 * @param {string}   [params.subject]        - Objet du devis
 * @param {string}   [params.freeText]       - Texte libre en bas du PDF
 * @param {string}   [params.description]    - Description affichée sur le PDF
 * @param {string}   [params.specialMention] - Mention spéciale
 * @param {string}   [params.externalRef]    - Référence externe (ex: numéro de projet)
 * @param {object}   [params.discount]       - { type: "absolute"|"percentage", value: "25" }
 * @param {object[]} [params.sections]       - Sections de lignes
 * @param {object[]} params.lines            - Lignes du devis
 *
 * Chaque ligne (params.lines) peut contenir :
 *   - label            (string)  ex: "Prestation de conseil"
 *   - quantity          (number)  ex: 2
 *   - raw_currency_unit_price (string)  ex: "150.00"
 *   - vat_rate          (string)  ex: "FR_200" (20%), "FR_100" (10%), "FR_055" (5.5%), "FR_021" (2.1%)
 *   - unit              (string)  ex: "piece", "hour", "day"
 *   - description       (string)  description de la ligne
 *   - product_id        (number)  si un produit PennyLane existe, il remplit auto les champs
 *   - section_rank      (number)  rang de la section parente
 *   - discount          (object)  { type, value }
 *   - ledger_account_id (number)  compte de vente (optionnel)
 */
async function createQuote({
  date,
  deadline,
  customerId,
  quoteTemplateId,
  currency = 'EUR',
  language = 'fr_FR',
  subject,
  freeText,
  description,
  specialMention,
  externalRef,
  discount,
  sections,
  lines,
}) {
  if (!customerId) throw new Error('customerId est requis');
  if (!lines || lines.length === 0) throw new Error('Au moins une ligne est requise');

  const payload = {
    date,
    deadline,
    customer_id: customerId,
    currency,
    language,
  };

  if (quoteTemplateId) payload.quote_template_id = quoteTemplateId;
  if (subject) payload.pdf_invoice_subject = subject;
  if (freeText) payload.pdf_invoice_free_text = freeText;
  if (description) payload.pdf_description = description;
  if (specialMention) payload.special_mention = specialMention;
  if (externalRef) payload.external_reference = externalRef;
  if (discount) payload.discount = discount;

  if (sections && sections.length > 0) {
    payload.invoice_line_sections = sections.map((s, i) => ({
      title: s.title,
      description: s.description ?? '',
      rank: s.rank ?? i + 1,
    }));
  }

  payload.invoice_lines = lines.map((line) => {
    const l = { label: line.label, quantity: line.quantity ?? 1 };
    if (line.raw_currency_unit_price != null) l.raw_currency_unit_price = String(line.raw_currency_unit_price);
    if (line.vat_rate) l.vat_rate = line.vat_rate;
    if (line.unit) l.unit = line.unit;
    if (line.description) l.description = line.description;
    if (line.product_id) l.product_id = line.product_id;
    if (line.section_rank) l.section_rank = line.section_rank;
    if (line.discount) l.discount = line.discount;
    if (line.ledger_account_id) l.ledger_account_id = line.ledger_account_id;
    return l;
  });

  if (process.env.MODE_TEST === 'true') {
    logger.info(`[PennyLane] [MODE TEST] Simulation devis pour client ${customerId}`);
    payload.invoice_lines.forEach((l) =>
      logger.info(`  → ${l.label} x${l.quantity} @ ${l.raw_currency_unit_price ?? '(produit)'} ${l.vat_rate ?? ''}`)
    );
    return { id: 'TEST', quote_number: 'TEST-0001', status: 'simulated', public_file_url: null };
  }

  const { data } = await client().post('/quotes', payload);
  logger.info(`[PennyLane] Devis créé: ${data.quote_number} (id=${data.id}) — PDF: ${data.public_file_url ?? 'en cours'}`);
  return data;
}

// ---------------------------------------------------------------------------
// Récupération d'un devis existant (avec URL du PDF)
// ---------------------------------------------------------------------------

async function getQuote(quoteId) {
  const { data } = await client().get(`/quotes/${quoteId}`);
  return data;
}

// ---------------------------------------------------------------------------
// Liste des devis
// ---------------------------------------------------------------------------

async function listQuotes({ status, customerId, page = 1, perPage = 25 } = {}) {
  const filters = [];
  if (status) filters.push({ field: 'status', operator: 'eq', value: status });
  if (customerId) filters.push({ field: 'customer_id', operator: 'eq', value: String(customerId) });

  const params = { page, per_page: perPage };
  if (filters.length > 0) params.filter = JSON.stringify(filters);

  const { data } = await client().get('/quotes', { params });
  return data;
}

// ---------------------------------------------------------------------------
// Créer une facture à partir d'un devis accepté
// ---------------------------------------------------------------------------

async function createInvoiceFromQuote(quoteId, { finalize = false } = {}) {
  const { data } = await client().post('/customer_invoices/create_from_quote', {
    quote_id: quoteId,
    finalize,
  });
  logger.info(`[PennyLane] Facture créée depuis devis ${quoteId}: ${data.invoice_number ?? data.id}`);
  return data;
}

module.exports = {
  findCustomer,
  createCustomer,
  findOrCreateCustomer,
  createQuote,
  getQuote,
  listQuotes,
  createInvoiceFromQuote,
};

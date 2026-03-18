'use strict';

require('dotenv').config();

const express = require('express');
const { findOrCreateCustomer, createQuote } = require('./services/pennylaneQuoteService');
const logger = require('./utils/logger');

const app = express();
app.use(express.json());

// ---------------------------------------------------------------------------
// API Key middleware
// ---------------------------------------------------------------------------

function requireApiKey(req, res, next) {
  const key = req.headers['x-api-key'];
  if (!process.env.API_SECRET_KEY) {
    return res.status(500).json({ success: false, error: 'API_SECRET_KEY non configurée sur le serveur' });
  }
  if (!key || key !== process.env.API_SECRET_KEY) {
    return res.status(401).json({ success: false, error: 'Clé API invalide ou manquante' });
  }
  next();
}

// ---------------------------------------------------------------------------
// Mapping boutiques → quote_template_id PennyLane
// ---------------------------------------------------------------------------

const STORE_TEMPLATES = {
  HET:  257162,
  LFC:  253634,
  LVO:  877143,
  COCO: 257180,
  MON:  883869,
  RED:  257168,
  RETE: 861190,
  TAR:  257174,
  UNI:  883875,
};

const PRODUCT_ID_FILET = 14369303;
const PRODUCT_ID_GENERIC = 16822267;

// ---------------------------------------------------------------------------
// POST /api/create-quote
// ---------------------------------------------------------------------------

app.post('/api/create-quote', requireApiKey, async (req, res) => {
  try {
    const input = req.body;

    // --- Boutique → template ---
    const store = (input.store ?? 'LFC').toUpperCase();
    const quoteTemplateId = STORE_TEMPLATES[store];
    if (!quoteTemplateId) {
      return res.status(400).json({
        success: false,
        error: `Boutique inconnue: "${store}". Valeurs acceptées: ${Object.keys(STORE_TEMPLATES).join(', ')}`,
      });
    }

    // --- Client ---
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

    if (!customerId) {
      return res.status(400).json({
        success: false,
        error: 'Veuillez fournir "customer" (objet client) ou "customerId" (ID PennyLane).',
      });
    }

    // --- Lignes ---
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

      if (lineType === 'product') {
        line.product_id = l.productId ?? l.product_id ?? PRODUCT_ID_FILET;
      } else {
        line.product_id = l.productId ?? l.product_id ?? PRODUCT_ID_GENERIC;
      }

      return line;
    });

    // --- Deadline ---
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

    res.json({
      success: true,
      quoteId: result.id,
      quoteNumber: result.quote_number,
      status: result.status,
      amount: result.amount ?? result.currency_amount,
      pdfUrl: result.public_file_url,
    });
  } catch (err) {
    logger.error(`[API] Erreur création devis: ${err.message}`);
    res.status(500).json({ success: false, error: err.message });
  }
});

// ---------------------------------------------------------------------------
// Health check
// ---------------------------------------------------------------------------

app.get('/health', (_req, res) => {
  res.json({ status: 'ok' });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
  logger.info(`Serveur devis démarré sur le port ${PORT}`);
});

'use strict';

const { google } = require('googleapis');

const SHEET_NAME  = 'Sheet1';   // Default tab name — change if needed
const ANCHOR_TEXT = 'Boutique'; // Cell that anchors the whole table

/**
 * Build an authenticated Google Sheets client using a service account.
 */
function buildSheetsClient() {
  const privateKey = (process.env.GOOGLE_PRIVATE_KEY || '').replace(/\\n/g, '\n');

  const auth = new google.auth.GoogleAuth({
    credentials: {
      client_email: process.env.GOOGLE_SERVICE_ACCOUNT_EMAIL,
      private_key:  privateKey,
    },
    scopes: ['https://www.googleapis.com/auth/spreadsheets'],
  });

  return google.sheets({ version: 'v4', auth });
}

/**
 * Convert a 0-based column index to an A1 column letter (A, B, … Z, AA, …).
 * @param {number} index
 * @returns {string}
 */
function colIndexToLetter(index) {
  let letter = '';
  let n = index;
  while (n >= 0) {
    letter = String.fromCharCode((n % 26) + 65) + letter;
    n = Math.floor(n / 26) - 1;
  }
  return letter;
}

/**
 * Read all values from row 1 of the sheet to locate the "Boutique" anchor.
 * Returns the 0-based column index, or -1 if not found.
 *
 * @param {object} sheets   - Authenticated Sheets client
 * @param {string} sheetId
 * @returns {Promise<number>}
 */
async function findAnchorColumn(sheets, sheetId) {
  const response = await sheets.spreadsheets.values.get({
    spreadsheetId: sheetId,
    range: `${SHEET_NAME}!1:1`,
  });
  const row = (response.data.values || [[]])[0] || [];
  const idx = row.findIndex(cell => cell === ANCHOR_TEXT);
  return idx; // -1 if not found
}

/**
 * Initialize the sheet structure if it is still empty:
 *   Col A, Row 1  → "Boutique"
 *   Col A, Rows 2-11 → store names + "TOTAL"
 *
 * @param {object} sheets
 * @param {string} sheetId
 * @param {string[]} storeNames  - Ordered list of store names
 */
async function initSheet(sheets, sheetId, storeNames) {
  console.log('[INFO] Sheet is empty — initialising structure');
  const values = [
    [ANCHOR_TEXT],
    ...storeNames.map(n => [n]),
    ['TOTAL'],
  ];

  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId,
    range: `${SHEET_NAME}!A1`,
    valueInputOption: 'RAW',
    requestBody: { values },
  });
}

/**
 * Scan row 1 to the right of the anchor column and return the 0-based index
 * of the first empty cell (= next available date column).
 * Also checks whether targetDate is already present (idempotency guard).
 *
 * @param {object} sheets
 * @param {string} sheetId
 * @param {number} anchorColIdx  - 0-based column index of "Boutique"
 * @param {string} targetDate    - Date string to check (YYYY-MM-DD)
 * @returns {Promise<number|null>} - 0-based col index, or null if already done
 */
async function findNextColumn(sheets, sheetId, anchorColIdx, targetDate) {
  // Read a wide slice of row 1 starting from the anchor
  const startCol = colIndexToLetter(anchorColIdx);
  const endCol   = colIndexToLetter(anchorColIdx + 500); // generous upper bound

  const response = await sheets.spreadsheets.values.get({
    spreadsheetId: sheetId,
    range: `${SHEET_NAME}!${startCol}1:${endCol}1`,
  });
  const row = (response.data.values || [[]])[0] || [];

  // row[0] is "Boutique", dates start at row[1]
  for (let i = 1; i < row.length; i++) {
    if (row[i] === targetDate) {
      console.log(`[INFO] Date ${targetDate} already in sheet — nothing to do`);
      return null; // already processed
    }
    if (!row[i]) {
      // First empty cell after anchor = next column
      return anchorColIdx + i;
    }
  }

  // All scanned cells are filled → append after the last one
  return anchorColIdx + row.length;
}

/**
 * Write a complete daily column to the sheet.
 *
 * @param {object}   sheets
 * @param {string}   sheetId
 * @param {number}   colIdx       - 0-based column index to write into
 * @param {string}   date         - YYYY-MM-DD header
 * @param {(number|null)[]} caValues - CA HT per store (same order as storeNames)
 * @param {string[]} storeNames
 */
async function writeColumn(sheets, sheetId, colIdx, date, caValues, storeNames) {
  const col     = colIndexToLetter(colIdx);
  const dataRow = 2;                          // stores start on row 2
  const lastDataRow = dataRow + storeNames.length - 1;
  const totalRow    = lastDataRow + 1;        // TOTAL row

  // Build value array: date header + one value per store
  const storeValues = caValues.map(v =>
    v === null ? [null] : [v]
  );

  // Write date header
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId,
    range: `${SHEET_NAME}!${col}1`,
    valueInputOption: 'RAW',
    requestBody: { values: [[date]] },
  });

  // Write store CA HT values
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId,
    range: `${SHEET_NAME}!${col}${dataRow}:${col}${lastDataRow}`,
    valueInputOption: 'RAW',
    requestBody: { values: storeValues },
  });

  // Write SUM formula for TOTAL row
  const sumFormula = `=SUM(${col}${dataRow}:${col}${lastDataRow})`;
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId,
    range: `${SHEET_NAME}!${col}${totalRow}`,
    valueInputOption: 'USER_ENTERED',
    requestBody: { values: [[sumFormula]] },
  });

  console.log(`[INFO] Column ${col} written — date: ${date}, total: ${sumFormula}`);
}

/**
 * Main entry point: write one day's worth of data into the sheet.
 *
 * @param {string}   date        - YYYY-MM-DD (J-1)
 * @param {string[]} storeNames  - Ordered list of store names
 * @param {(number|null)[]} caValues - CA HT per store (same order)
 */
async function writeDailyReport(date, storeNames, caValues) {
  const sheetId = process.env.GOOGLE_SHEET_ID;
  const sheets  = buildSheetsClient();

  // 1. Find anchor column
  let anchorColIdx = await findAnchorColumn(sheets, sheetId);

  // 2. Init sheet if anchor is missing
  if (anchorColIdx === -1) {
    await initSheet(sheets, sheetId, storeNames);
    anchorColIdx = 0; // "Boutique" was just written to column A (index 0)
  }

  // 3. Find next available column (idempotency check included)
  const nextColIdx = await findNextColumn(sheets, sheetId, anchorColIdx, date);
  if (nextColIdx === null) {
    // Already processed — exit cleanly
    return;
  }

  // 4. Write the column
  await writeColumn(sheets, sheetId, nextColIdx, date, caValues, storeNames);
}

module.exports = { writeDailyReport };

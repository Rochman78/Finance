'use strict';

const LEVELS = { info: 'INFO', warn: 'WARN', error: 'ERROR' };

function format(level, ...args) {
  const ts = new Date().toISOString();
  const msg = args.map(a => (typeof a === 'object' ? JSON.stringify(a, null, 2) : String(a))).join(' ');
  return `[${ts}] [${LEVELS[level]}] ${msg}`;
}

const logger = {
  info:  (...args) => console.log(format('info', ...args)),
  warn:  (...args) => console.warn(format('warn', ...args)),
  error: (...args) => console.error(format('error', ...args)),
};

module.exports = logger;

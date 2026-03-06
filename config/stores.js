'use strict';

/**
 * List of Shopify stores.
 * urlEnv  : name of the env var holding the store URL (e.g. https://xxx.myshopify.com)
 * tokenEnv: name of the env var holding the Admin API access token
 */
const STORES = [
  { name: 'LFC',  urlEnv: 'SHOPIFY_LFC_URL',  tokenEnv: 'SHOPIFY_LFC_TOKEN'  },
  { name: 'LVO',  urlEnv: 'SHOPIFY_LVO_URL',  tokenEnv: 'SHOPIFY_LVO_TOKEN'  },
  { name: 'UNI',  urlEnv: 'SHOPIFY_UNI_URL',  tokenEnv: 'SHOPIFY_UNI_TOKEN'  },
  { name: 'TAR',  urlEnv: 'SHOPIFY_TAR_URL',  tokenEnv: 'SHOPIFY_TAR_TOKEN'  },
  { name: 'HET',  urlEnv: 'SHOPIFY_HET_URL',  tokenEnv: 'SHOPIFY_HET_TOKEN'  },
  { name: 'RED',  urlEnv: 'SHOPIFY_RED_URL',  tokenEnv: 'SHOPIFY_RED_TOKEN'  },
  { name: 'COCO', urlEnv: 'SHOPIFY_COCO_URL', tokenEnv: 'SHOPIFY_COCO_TOKEN' },
  { name: 'MON',  urlEnv: 'SHOPIFY_MON_URL',  tokenEnv: 'SHOPIFY_MON_TOKEN'  },
  { name: 'RETE', urlEnv: 'SHOPIFY_RETE_URL', tokenEnv: 'SHOPIFY_RETE_TOKEN' },
];

module.exports = { STORES };

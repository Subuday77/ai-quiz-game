/* Synchronous head script: mark non-English pages before the first paint.
   The English HTML remains available to crawlers and visitors with JS off. */
(() => {
  'use strict';
  const supported = new Set(['en', 'ru', 'de', 'fr', 'es', 'it', 'pt', 'he', 'ar', 'ja', 'zh']);
  const fromUrl = new URLSearchParams(location.search).get('lang');
  const preferred = (fromUrl || (navigator.language || 'en').split('-')[0]).toLowerCase();
  const code = supported.has(preferred) ? preferred : 'en';
  document.documentElement.dataset.siteLang = code;
  if (code === 'en') return;
  document.documentElement.classList.add('site-localizing');
  document.documentElement.lang = code;
  document.documentElement.dir = code === 'he' || code === 'ar' ? 'rtl' : 'ltr';
})();

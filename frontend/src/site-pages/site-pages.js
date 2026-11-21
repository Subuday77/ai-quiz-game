/* Standalone, crawlable public pages. The English HTML is the accessible
   fallback; translated copy comes from the same API/dictionary as Angular. */
(() => {
  'use strict';
  const allowed = new Set(['en','ru','de','fr','es','it','pt','he','ar','ja','zh']);
  const params = new URLSearchParams(location.search);
  const requested = (params.get('lang') || navigator.language.split('-')[0] || 'en').toLowerCase();
  const lang = allowed.has(requested) ? requested : 'en';
  let words = {};
  const t = key => words[key] || document.querySelector('[data-i18n="' + key + '"]')?.textContent || key;

  document.querySelectorAll('a[href^="/"]').forEach(link => {
    const url = new URL(link.href);
    if (lang !== 'en' && ['/about','/contact'].includes(url.pathname)) {
      url.searchParams.set('lang', lang);
      link.href = url.pathname + url.search;
    }
  });

  if (lang !== 'en') {
    fetch('/api/localization/dictionary/' + encodeURIComponent(lang), { cache: 'no-store' })
      .then(res => { if (!res.ok) throw Error('Dictionary unavailable'); return res.json(); })
      .then(data => {
        if (data.language_code !== lang || data.fallback || data.missing_count || !data.dictionary) return;
        words = data.dictionary;
        document.querySelectorAll('[data-i18n]').forEach(el => {
          const replacement = words[el.dataset.i18n];
          if (typeof replacement === 'string') el.textContent = replacement;
        });
        document.documentElement.lang = lang;
        document.documentElement.dir = lang === 'he' || lang === 'ar' ? 'rtl' : 'ltr';
        document.title = (document.querySelector('h1')?.textContent || document.title) + ' | AI Quiz Game';
      }).catch(() => {/* English is available without the localization API. */});
  }

  const form = document.getElementById('contact-form');
  if (!form) return;
  const status = document.getElementById('contact-status');
  const button = document.getElementById('contact-submit');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity() || button.disabled) return;
    button.disabled = true;
    status.textContent = t('site.contactSending');
    status.removeAttribute('data-error');
    const payload = Object.fromEntries(new FormData(form));
    try {
      const response = await fetch('/api/contact', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        status.textContent = t(response.status === 429 ? 'site.contactRateLimit' : 'site.contactFailed');
        status.dataset.error = 'true';
        return;
      }
      form.reset();
      status.textContent = t('site.contactSent');
      status.dataset.error = 'false';
    } catch {
      status.textContent = t('site.contactFailed');
      status.dataset.error = 'true';
    } finally {
      button.disabled = false;
    }
  });
})();

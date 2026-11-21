/* Standalone, crawlable public pages. The English HTML is the accessible
   fallback; translated copy comes from the same API/dictionary as Angular. */
(() => {
  'use strict';
  const allowed = new Set(['en','ru','de','fr','es','it','pt','he','ar','ja','zh']);
  const params = new URLSearchParams(location.search);
  const requested = (params.get('lang') || navigator.language.split('-')[0] || 'en').toLowerCase();
  const lang = allowed.has(requested) ? requested : 'en';
  let words = {};
  // The public form remains readable and usable in English even when the
  // optional dictionary API has not responded (or is temporarily unavailable).
  const englishStatus = {
    'site.contactSending': 'Sending your message…',
    'site.contactSent': 'Thanks! Your message has been sent.',
    'site.contactFailed': "We couldn't send your message. Please try again later.",
    'site.contactRateLimit': 'Too many messages sent. Please try again later.',
  };
  const t = key => words[key] || englishStatus[key] ||
    document.querySelector('[data-i18n="' + key + '"]')?.textContent || key;

  document.querySelectorAll('a[href^="/"]').forEach(link => {
    const url = new URL(link.href);
    if (lang !== 'en' && ['/about','/contact'].includes(url.pathname)) {
      url.searchParams.set('lang', lang);
      link.href = url.pathname + url.search;
    }
  });

  if (lang !== 'en') {
    const loadDictionary = async attempt => {
      try {
        const res = await fetch('/api/localization/dictionary/' + encodeURIComponent(lang), {
          cache: 'no-store',
        });
        if (!res.ok) return;
        const data = await res.json();
        if (data.language_code !== lang || !data.dictionary) return;
        if (data.fallback || data.missing_count > 0) {
          // Newly introduced English keys may still be translating. Follow
          // the game's bounded polling convention, without a visible flash.
          if (attempt < 8) setTimeout(() => loadDictionary(attempt + 1), 2500);
          return;
        }
        words = data.dictionary;
        document.querySelectorAll('[data-i18n]').forEach(el => {
          const replacement = words[el.dataset.i18n];
          if (typeof replacement === 'string') el.textContent = replacement;
        });
        document.documentElement.lang = lang;
        document.documentElement.dir = lang === 'he' || lang === 'ar' ? 'rtl' : 'ltr';
        document.title = (document.querySelector('h1')?.textContent || document.title) + ' | AI Quiz Game';
      } catch {/* English is available without the localization API. */}
    };
    loadDictionary(0);
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

/* Standalone, crawlable public pages. The English HTML is the accessible
   fallback; translated copy comes from the same API/dictionary as Angular. */
(() => {
  'use strict';
  // The blocking head script sets data-site-lang and hides untranslated HTML
  // before first paint. This deferred script resolves one complete dictionary
  // and then reveals the page in a single turn, just like the Angular client.
  const lang = document.documentElement.dataset.siteLang || 'en';
  const requiredKeys = [...document.querySelectorAll('[data-i18n]')].map(el => el.dataset.i18n);
  const cacheKey = 'ai-quiz-public-site-v1-' + lang;
  let words = {};
  let settled = false;
  // The public form remains readable and usable in English if the
  // translation service is unavailable.
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
    if (lang !== 'en' && ['/about', '/contact'].includes(url.pathname)) {
      url.searchParams.set('lang', lang);
      link.href = url.pathname + url.search;
    }
  });

  if (lang !== 'en') {
    let deadline;
    const revealEnglish = () => {
      if (settled) return;
      settled = true; // Ignore late network responses after fallback.
      clearTimeout(deadline);
      document.documentElement.lang = 'en';
      document.documentElement.dir = 'ltr';
      document.documentElement.classList.remove('site-localizing');
    };
    const applyCompleteDictionary = dictionary => {
      if (settled || !dictionary || !requiredKeys.every(key =>
        typeof dictionary[key] === 'string' && dictionary[key].trim())) return false;
      words = dictionary;
      // Mutate all visible localized text before revealing anything.
      document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = words[el.dataset.i18n];
      });
      document.title = (document.querySelector('h1')?.textContent || document.title) + ' | AI Quiz Game';
      settled = true;
      clearTimeout(deadline);
      document.documentElement.classList.remove('site-localizing');
      return true;
    };

    // Reuse completed text across About/Contact navigation in the same tab.
    // Keys are UI-only public copy, never login or account data.
    try {
      const cached = JSON.parse(sessionStorage.getItem(cacheKey) || 'null');
      if (cached && cached.expires > Date.now()) applyCompleteDictionary(cached.words);
    } catch {/* Browsers may block sessionStorage. Normal fetch still works. */}

    if (!settled) {
      // Never display English for a moment and then replace it with Russian.
      // A failure or prolonged outage instead reveals the English fallback
      // once, and late results are intentionally ignored.
      deadline = setTimeout(revealEnglish, 25000);
      const loadDictionary = async attempt => {
        if (settled) return;
        try {
          const response = await fetch('/api/localization/dictionary/' + encodeURIComponent(lang), {
            cache: 'no-store',
          });
          if (!response.ok) return revealEnglish();
          const data = await response.json();
          if (settled) return;
          if (data.language_code !== lang || !data.dictionary) return revealEnglish();
          if (data.fallback || data.missing_count > 0) {
            if (attempt < 8) setTimeout(() => loadDictionary(attempt + 1), 2500);
            else revealEnglish();
            return;
          }
          if (!applyCompleteDictionary(data.dictionary)) return revealEnglish();
          try {
            const publicCopy = Object.fromEntries(Object.entries(data.dictionary)
              .filter(([key]) => key.startsWith('site.')));
            sessionStorage.setItem(cacheKey, JSON.stringify({
              expires: Date.now() + 15 * 60 * 1000, words: publicCopy,
            }));
          } catch {/* Translation display never depends on storage. */}
        } catch {
          revealEnglish();
        }
      };
      loadDictionary(0);
    }
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

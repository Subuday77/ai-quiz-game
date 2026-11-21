import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const source = name => readFileSync(new URL('../src/site-pages/' + name, import.meta.url), 'utf8');
const prepaint = source('site-pages-preload.js');
const localizedPage = source('site-pages.js');

function harness({ lang = 'ru', fetchImpl, cached } = {}) {
  const classes = new Set();
  const elements = [
    { dataset: { i18n: 'site.aboutTitle' }, textContent: 'About AI Quiz Game' },
    { dataset: { i18n: 'site.aboutLead' }, textContent: 'A curious mind' },
  ];
  const stored = new Map();
  if (cached) stored.set('ai-quiz-public-site-v1-' + lang, JSON.stringify(cached));
  const timers = [];
  const root = {
    dataset: {},
    lang: 'en',
    dir: 'ltr',
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    },
  };
  const document = {
    documentElement: root,
    title: 'About Us | AI Quiz Game',
    querySelectorAll: selector => {
      if (selector === '[data-i18n]') return elements;
      if (selector === 'a[href^="/"]') return [];
      throw Error('Unexpected selector: ' + selector);
    },
    querySelector: selector => {
      if (selector === 'h1') return elements[0];
      return null;
    },
    getElementById: () => null,
  };
  const context = {
    document,
    location: { search: '?lang=' + lang },
    navigator: { language: 'en-US' },
    URL, URLSearchParams, Date,
    sessionStorage: {
      getItem: key => stored.get(key) ?? null,
      setItem: (key, value) => stored.set(key, value),
    },
    fetch: fetchImpl ?? (async () => { throw Error('Network unavailable'); }),
    setTimeout: (callback, delay) => {
      const timer = { callback, delay };
      timers.push(timer);
      return timer;
    },
    clearTimeout: timer => {
      if (timer) timer.cleared = true;
    },
  };
  const runHead = () => runInNewContext(prepaint, context);
  const runPage = () => runInNewContext(localizedPage, context);
  return { root, classes, elements, stored, timers, document, runHead, runPage };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const ru = { 'site.aboutTitle': 'О нас', 'site.aboutLead': 'Любознательность — это здорово' };

test('prepaint locale gate is loaded synchronously before CSS on both public pages', () => {
  for (const name of ['about', 'contact']) {
    const html = source(name + '.html');
    const script = '<script src="/site-pages-preload.js"></script>';
    assert.ok(html.includes(script), name + ': blocking preload missing');
    assert.ok(html.indexOf(script) < html.indexOf('<link rel="stylesheet"'), name + ': preload after CSS');
    assert.ok(html.indexOf(script) < html.indexOf('<body>'), name + ': preload must execute in head');
    assert.doesNotMatch(script, /\bdefer\b|\basync\b/);
    assert.match(html, /<html lang="en">/, 'English HTML still crawlable without JavaScript');
  }
  const css = source('site-pages.css');
  assert.match(css, /html\.site-localizing \.info-shell \{\s*visibility: hidden;/);
  assert.match(css, /html\.site-localizing body::before/);
  assert.match(css, /prefers-reduced-motion: reduce/);
});

test('Russian page stays hidden until complete dictionary replaces all text at once', async () => {
  let resolveNetwork;
  const h = harness({ fetchImpl: () => new Promise(resolve => { resolveNetwork = resolve; }) });
  h.runHead();
  assert.equal(h.root.lang, 'ru');
  assert.equal(h.classes.has('site-localizing'), true, 'English content hidden before first paint');
  h.runPage();
  assert.equal(h.elements[0].textContent, 'About AI Quiz Game');
  assert.equal(h.elements[1].textContent, 'A curious mind');
  assert.equal(h.classes.has('site-localizing'), true);
  resolveNetwork({
    ok: true,
    json: async () => ({ language_code: 'ru', dictionary: ru, fallback: false, missing_count: 0 }),
  });
  await flush();
  assert.deepEqual(h.elements.map(x => x.textContent), Object.values(ru));
  assert.equal(h.root.lang, 'ru');
  assert.equal(h.classes.has('site-localizing'), false);
  assert.match(h.document.title, /^О нас/);
  assert.ok(h.stored.has('ai-quiz-public-site-v1-ru'), 'successful public copy reused next time');
});

test('a completed cached locale is applied before any network request', () => {
  let calls = 0;
  const h = harness({
    fetchImpl: () => { calls++; return Promise.reject(Error('unexpected fetch')); },
    cached: { expires: Date.now() + 60000, words: ru },
  });
  h.runHead();
  h.runPage();
  assert.equal(calls, 0);
  assert.equal(h.classes.has('site-localizing'), false);
  assert.deepEqual(h.elements.map(x => x.textContent), Object.values(ru));
});

test('incomplete dictionary does not show a partially translated page', async () => {
  const h = harness({
    fetchImpl: async () => ({
      ok: true,
      json: async () => ({
        language_code: 'ru', dictionary: { 'site.aboutTitle': 'О нас' },
        fallback: false, missing_count: 0,
      }),
    }),
  });
  h.runHead();
  h.runPage();
  await flush();
  assert.equal(h.classes.has('site-localizing'), false);
  assert.deepEqual(h.elements.map(x => x.textContent), ['About AI Quiz Game', 'A curious mind']);
  assert.equal(h.root.lang, 'en');
});

test('timeout falls back once; late translated responses cannot cause English flash', async () => {
  let resolveNetwork;
  const h = harness({ fetchImpl: () => new Promise(resolve => { resolveNetwork = resolve; }) });
  h.runHead();
  h.runPage();
  const deadline = h.timers.find(x => x.delay === 25000);
  assert.ok(deadline);
  deadline.callback();
  assert.equal(h.classes.has('site-localizing'), false);
  assert.equal(h.root.lang, 'en');
  resolveNetwork({
    ok: true,
    json: async () => ({ language_code: 'ru', dictionary: ru, fallback: false, missing_count: 0 }),
  });
  await flush();
  assert.equal(h.elements[0].textContent, 'About AI Quiz Game');
  assert.equal(h.root.lang, 'en');
});

test('English page never enters the loading gate', () => {
  const h = harness({ lang: 'en' });
  h.runHead();
  h.runPage();
  assert.equal(h.root.lang, 'en');
  assert.equal(h.classes.has('site-localizing'), false);
});

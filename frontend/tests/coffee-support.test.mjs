import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../src/app/app.component.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../src/app/app.component.css', import.meta.url), 'utf8');
const component = readFileSync(new URL('../src/app/app.component.ts', import.meta.url), 'utf8');
const english = JSON.parse(readFileSync(new URL('../../backend/localization/en.json', import.meta.url), 'utf8'));

test('About moves beside setup title, coffee replaces its old location', () => {
  const setupStart = html.indexOf('class="setup-heading"');
  const about = html.indexOf('(click)="openAbout()"', setupStart);
  const headingEnd = html.indexOf('</div>', setupStart);
  const intro = html.indexOf("locale.t('hero.intro')", headingEnd);
  const coffee = html.indexOf('class="coffee-support"', intro);
  const setup = html.indexOf('class="setup"', coffee);
  assert.ok(setupStart >= 0 && about > setupStart && about < headingEnd);
  assert.ok(coffee > intro && coffee < setup);
  assert.match(html.slice(coffee, setup), /locale\.t\('support\.coffeePrompt'\)/);
  assert.match(html.slice(coffee, setup), /locale\.t\('support\.buyCoffee'\)/);
  assert.match(html.slice(coffee, setup), /rel="noopener noreferrer"/);
  assert.match(html.slice(coffee, setup), /disabled/);
});

test('Coffee UI uses canonical localization and a fail-closed profile link', () => {
  for (const key of ['support.coffeePrompt', 'support.buyCoffee', 'support.comingSoon']) {
    assert.ok(english[key], key);
  }
  assert.match(component, /donationUrl = '';/);
  assert.match(component, /buymeacoffee/);
  assert.match(css, /@media \(max-width: 640px\)/);
  assert.match(css, /\.setup-heading/);
  assert.match(css, /\.coffee-support/);
});

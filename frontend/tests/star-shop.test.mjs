import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const ts = read('../src/app/app.component.ts');
const html = read('../src/app/app.component.html');
const css = read('../src/app/app.component.css');
const english = JSON.parse(read('../../backend/localization/en.json'));

test('Shop catalog is loaded from backend and not a client-priced payment', () => {
  assert.match(ts, /this\.http\.get<StarShopCatalog>\('\/api\/v2\/star-shop'\)/);
  assert.match(ts, /shop\.packages|starShop\.packages/);
  assert.match(ts, /!this\.starShop\.verified_email/);
  assert.match(ts, /this\.starShop\.packages\.includes\(pack\)/);
  assert.match(ts, /window\.open\(pack\.checkout_url, '_blank', 'noopener,noreferrer'\)/);
  assert.doesNotMatch(ts, /\/api\/v2\/star-shop\/credit/);
  assert.doesNotMatch(html, /https:\/\/buymeacoffee\.com\/subuday\/e\//);
});

test('Shop has five responsive pack buttons, no checkout until ready and verified email', () => {
  assert.match(html, /\*ngFor="let pack of catalog\.packages"/);
  assert.match(html, /\[disabled\]="!catalog\.ready \|\| !pack\.checkout_url"/);
  assert.match(html, /starShop\?\.verified_email/);
  assert.match(html, /locale\.t\('shop\.sameEmailRule'\)/);
  assert.match(html, /locale\.t\('shop\.premiumDiscount'\)/);
  assert.doesNotMatch(html, /premium_coupon|discountCode|enterCode/);
  assert.match(html, /locale\.t\('shop\.notReady'\)/);
  assert.match(html, /\(click\)="openStarShop\(\)"/);
  assert.match(css, /grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(css, /@media \(max-width: 680px\)/);
});

test('Shop and reduced About reward use canonical English dictionary', () => {
  const keys = [
    'open', 'title', 'intro', 'email', 'verifyEmail', 'premiumDiscount',
    'loading', 'failed', 'notReady', 'sameEmailRule', 'refreshBalance',
  ];
  for (const name of keys) {
    assert.ok(english['shop.' + name], name + ' missing from English dictionary');
    assert.match(html + ts, new RegExp('shop\\.' + name));
  }
  assert.match(ts, /readonly aboutReward: StarCounts = \{ bronze: 5, silver: 4, gold: 3, platinum: 2 \}/);
  assert.match(ts, /this\.starShop = null;/);
});

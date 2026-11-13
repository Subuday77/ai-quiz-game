import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../src/app/localization/about-copy.ts', import.meta.url), 'utf8');
const component = readFileSync(new URL('../src/app/app.component.ts', import.meta.url), 'utf8');
const html = readFileSync(new URL('../src/app/app.component.html', import.meta.url), 'utf8');
const english = JSON.parse(readFileSync(new URL('../../backend/localization/en.json', import.meta.url), 'utf8'));

function aboutSections() {
  const at = source.indexOf('export const ABOUT_SECTIONS');
  assert.notEqual(at, -1);
  const start = source.indexOf(' = ', at) + 3;
  const end = source.indexOf(';\n', start);
  assert.ok(start > 2 && end > start);
  return JSON.parse(source.slice(start, end));
}

test('About copy uses canonical English keys for all languages', () => {
  const sections = aboutSections();
  assert.equal(sections.length, 6);
  for (const section of sections) {
    assert.ok(english[section.heading], 'missing English title: ' + section.heading);
    for (const block of section.blocks) {
      assert.equal(typeof english[block.key], 'string', 'missing English copy: ' + block.key);
      assert.ok(english[block.key].trim(), 'empty English copy: ' + block.key);
    }
  }
  for (const k of ['about.gift.p1', 'about.gift.p8', 'about.gift.p11',
                   'about.gift.platinum', 'about.gift.gold',
                   'about.gift.silver', 'about.gift.bronze']) {
    assert.ok(english[k], 'missing gift copy: ' + k);
  }
  assert.ok(!source.includes('APPROVED_ABOUT_RU'));
  assert.ok(!component.includes('APPROVED_ABOUT_RU'));
  assert.ok(!component.includes("this.locale.code === 'ru'"));
  assert.ok(html.includes('locale.t(block.key)'));
  assert.ok(html.includes('locale.t(section.heading)'));
  assert.ok(!html.includes('aboutText('));
});

test('one-time reading gift is after the story, not in the introduction', () => {
  assert.ok(html.includes('aboutSections'));
  assert.ok(html.indexOf('aboutSections') < html.indexOf('about.giftTitle'));
  assert.ok(html.includes('aboutReward[tier.key]'));
  assert.ok(html.includes('about.gift.p8'));
  assert.ok(html.includes('about.giftClaim'));
  assert.ok(!html.includes('about.draftNotice'));
});

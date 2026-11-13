import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('../src/app/localization/about-copy.ts', import.meta.url), 'utf8');
const html = readFileSync(new URL('../src/app/app.component.html', import.meta.url), 'utf8');
const english = JSON.parse(readFileSync(new URL('../../backend/localization/en.json', import.meta.url), 'utf8'));

function jsonExport(name) {
  const at = source.indexOf('export const ' + name);
  assert.notEqual(at, -1, 'missing export ' + name);
  const start = source.indexOf(' = ', at) + 3;
  const end = source.indexOf(';\n', start);
  assert.ok(start > 2 && end > start, 'unexpected export syntax: ' + name);
  return JSON.parse(source.slice(start, end));
}
const ru = jsonExport('APPROVED_ABOUT_RU');
const sections = jsonExport('ABOUT_SECTIONS');

test('approved Russian About text is stable and backed by English keys', () => {
  assert.equal(sections.length, 6);
  for (const [key, value] of Object.entries(ru)) {
    assert.equal(typeof english[key], 'string', 'missing English source key ' + key);
    assert.ok(value && value.trim(), 'empty approved Russian text: ' + key);
  }
  for (const section of sections) {
    assert.ok(ru[section.heading]);
    for (const block of section.blocks) assert.ok(ru[block.key], block.key);
  }
  assert.equal(ru['about.s1.h'], 'Думаешь, ты умнее искусственного интеллекта?');
  assert.equal(ru['about.s6.p7'], 'И помни: победа над искусственным интеллектом особенно приятна, когда для неё пришлось использовать собственный.');
  assert.equal(ru['about.gift.p8'], 'Правда, есть одно условие: подарок можно получить только один раз на аккаунт.');
  assert.match(ru['about.giftTitle'], /дочитал до сюда/);
});

test('reading gift appears after the story and is one-time', () => {
  assert.ok(html.includes('aboutSections'));
  assert.ok(html.indexOf('aboutSections') < html.indexOf("about.giftTitle"));
  assert.ok(html.includes('aboutReward[tier.key]'));
  assert.ok(html.includes("about.gift.p8"));
  assert.ok(html.includes("about.giftClaim"));
  assert.ok(!html.includes('about.draftNotice'));
});

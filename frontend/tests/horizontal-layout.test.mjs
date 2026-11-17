// Structural regression checks for the responsive desktop game layout.
// These tests use the real Angular template and CSS without a browser or backend.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
const styles = readFileSync(join(root, 'src/app/app.component.css'), 'utf8');

const stageStart = template.indexOf('<div class="game-stage"');
const railStart = template.indexOf('<aside class="game-sidebar"');
const railEnd = template.indexOf('</aside>', railStart);
const stage = template.slice(stageStart, railStart);
const rail = template.slice(railStart, railEnd);

test('desktop game stage precedes a separate account/language/star sidebar', () => {
  assert.ok(stageStart >= 0 && railStart > stageStart && railEnd > railStart);
  assert.match(template, /<div class="game-layout" dir="ltr">/);
  assert.match(template, /<aside class="game-sidebar" \[attr\.dir\]="rtl \? 'rtl' : 'ltr'">/);
  for (const cls of ['account-controls', 'ui-language-control', 'star-dashboard']) {
    assert.ok(rail.includes('class="' + cls + '"') || rail.includes('class="' + cls + ' '),
      cls + ' belongs in the right rail');
    assert.ok(!stage.includes('class="' + cls + '"') && !stage.includes('class="' + cls + ' '),
      cls + ' must not appear above the gameplay');
  }
  assert.ok(stage.includes('class="card"'), 'the actual game is the primary content');
  assert.ok(stage.includes('class="helper-tablet"'), 'active helper remains next to the question');
});

test('existing localization and economy actions survive template-only relocation', () => {
  for (const action of [
    'setUiLanguage($event)', 'openAuthDialog(\'login\')', 'logout()',
    "exchangeBetween('bronze', 'silver')", "exchangeBetween('silver', 'bronze')",
    "exchangeBetween('silver', 'gold')", "exchangeBetween('gold', 'silver')",
    'premiumGoldToPlatinum()', 'openAppealHistory()'
  ]) {
    assert.ok(rail.includes(action), action + ' must remain connected');
  }
  for (const action of ['answer(choice)', 'removeOptions(1)', 'startGame()']) {
    assert.ok(stage.includes(action), action + ' must remain in gameplay');
  }
  assert.match(rail, /locale\.t\('account\.interfaceLanguage'\)/);
  assert.match(rail, /locale\.t\('stars\.' \+ tier\.key\)/);
});

test('layout has a wide desktop grid and narrow viewport fallback', () => {
  assert.match(styles, /\.game-layout\s*\{\s*display:\s*grid;/);
  assert.match(styles, /grid-template-columns:\s*minmax\(0, 1fr\) minmax\(304px, 344px\)/);
  assert.match(styles, /@media \(max-width: 1080px\)[\s\S]*?\.game-layout\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(styles, /@media \(max-width: 680px\)/);
  assert.match(styles, /\.game-sidebar \.star-items-exchange\s*\{/);
});

test('both star panels use same-size full-width rows; exchange controls bridge tiers', () => {
  assert.match(rail, /<div class="star-items">/);
  assert.match(rail, /<div class="star-items star-items-exchange">/);
  assert.doesNotMatch(rail, /wallet-wide/);
  assert.match(styles, /\.game-sidebar \.star-items \{\s*display: flex;\s*flex-direction: column;/);
  assert.match(styles, /\.game-sidebar \.star-item \{[^}]*width: 100%;[^}]*min-height: 52px;/s);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \{[^}]*display: flex;[^}]*flex-direction: column;/s);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \.exchange-pair \{[^}]*position: relative;[^}]*margin-block: -19px -16px;/s);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \.exchange-pair \{[^}]*width: 100%;[^}]*margin-inline: 0;/s);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \.exchange-pair \{[^}]*justify-content: center;/s);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \.exchange-pair \{[^}]*flex-direction: row;/s);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \.exchange-pair \{[^}]*z-index: 3;/s);
  assert.match(rail, /class="exchange-pair gold"/);
  assert.match(rail, /class="exchange-pair" \[ngClass\]="tier\.key"/);
  assert.match(rail, /exchangeBetween\('bronze', 'silver'\)/);
  assert.match(rail, /exchangeBetween\('silver', 'bronze'\)/);
  assert.match(rail, /exchangeBetween\('silver', 'gold'\)/);
  assert.match(rail, /exchangeBetween\('gold', 'silver'\)/);
  assert.match(rail, /premiumGoldToPlatinum\(\)/);
  assert.equal((rail.match(/<span aria-hidden="true">↓<\/span>/g) || []).length, 3);
  assert.equal((rail.match(/<span aria-hidden="true">↑<\/span>/g) || []).length, 2);
  assert.match(styles, /\.game-sidebar \.star-items-exchange \.exchange-pair \{[^}]*gap: 34px;/s);
});

test('game stage stretches to sidebar height on wide layouts', () => {
  assert.match(styles, /\.game-layout \{\s*display: grid;[\s\S]*?align-items: stretch;/);
  assert.match(styles, /\.game-stage \{ display: flex; flex-direction: column; \}/);
  assert.match(styles, /\.game-stage \.card \{ flex: 1 1 auto; \}/);
});

test('player-facing locale dictionary does not mention database implementation', () => {
  const en = JSON.parse(readFileSync(join(root, '../backend/localization/en.json'), 'utf8'));
  assert.match(en['stars.account'], /your account/);
  assert.ok(!Object.values(en).some(value =>
    typeof value === 'string' && /postgresql/i.test(value)
  ));
  assert.doesNotMatch(en['footer.note'], /postgresql/i);
});

test('appeal quota is absent from the star sidebar and kept in complaint history', () => {
  assert.doesNotMatch(rail, /appeal-allowance-inline|appeals\.freeCount|openAppealRules\(\)/);
  const source = readFileSync(join(root, 'src/app/app.component.ts'), 'utf8');
  const globalStyles = readFileSync(join(root, 'src/styles.css'), 'utf8');
  const history = source.slice(source.indexOf('async openAppealHistory()'),
    source.indexOf('async openAccountSettings()'));
  assert.match(history, /\/api\/v2\/appeals\/allowance/);
  assert.match(history, /'appeals\.freeQuota'/);
  assert.match(history, /appeal-history-quota/);
  assert.match(history, /<dialog id="appeal-history-rules-dialog"/);
  assert.match(history, /rulesDialog\.showModal\(\)/);
  assert.match(history, /rulesDialog\?\.close\(\)/);
  assert.match(history, /allowEscapeKey: \(\) => !rulesDialog\?\.open/);
  assert.match(globalStyles, /\.appeal-history-rules-dialog::backdrop/);
  assert.match(globalStyles, /\.appeal-history-rules-close/);
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const angular = JSON.parse(readFileSync(new URL('../angular.json', import.meta.url), 'utf8'));
const html = readFileSync(new URL('../src/index.html', import.meta.url), 'utf8');
const svg = readFileSync(new URL('../src/favicon.svg', import.meta.url), 'utf8');
const dockerfile = readFileSync(new URL('../Dockerfile', import.meta.url), 'utf8');

test('favicon is linked and included in both Angular and Docker builds', () => {
  assert.ok(angular.projects['ai-quiz-frontend'].architect.build.options.assets.includes('src/favicon.svg'));
  assert.match(html, /<link rel="icon" type="image\/svg\+xml" href="favicon\.svg\?v=2" \/>/);
  assert.match(dockerfile, /COPY src\/ \.\/src\//);
  assert.match(svg, /<svg\s[^>]*viewBox="0 0 64 64"/);
  assert.match(svg, /<\/svg>/);
  assert.ok(!svg.includes('<script'), 'favicon must be a static SVG');
});

test('favicon uses a single high-contrast question mark, without tiny ornaments', () => {
  assert.match(svg, /stroke="#ffdb72"/);
  assert.match(svg, /stroke-width="9"/);
  assert.match(svg, /<circle cx="32" cy="54" r="5"/);
  assert.doesNotMatch(svg, /spark|#8ce9ef/);
});

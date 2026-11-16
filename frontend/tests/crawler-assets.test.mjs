import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
const robots = read('../src/robots.txt');
const sitemap = read('../src/sitemap.xml');
const config = JSON.parse(read('../angular.json'));
const nginx = read('../nginx.conf');
const index = read('../src/index.html');

test('public crawler files are copied into Angular distribution', () => {
  const assets = config.projects['ai-quiz-frontend'].architect.build.options.assets;
  for (const path of ['src/robots.txt', 'src/sitemap.xml']) {
    assert.ok(assets.includes(path), path + ' is missing from build assets');
  }
});

test('robots.txt permits public pages but keeps API out of search indexes', () => {
  assert.match(robots, /^User-agent: \*$/m);
  assert.match(robots, /^Allow: \/$/m);
  assert.match(robots, /^Disallow: \/api\/$/m);
  assert.match(robots, /^Sitemap: https:\/\/squizthebot\.com\/sitemap\.xml$/m);
  assert.doesNotMatch(robots, /<html/i);
});

test('sitemap contains only public canonical homepage, not API or private URLs', () => {
  assert.match(sitemap, /<urlset\b[^>]*xmlns="http:\/\/www\.sitemaps\.org\/schemas\/sitemap\/0\.9"/);
  assert.match(sitemap, /<loc>https:\/\/squizthebot\.com\/<\/loc>/);
  assert.doesNotMatch(sitemap, /<loc>[^<]*\/api\//i);
  assert.equal((sitemap.match(/<loc>/g) || []).length, 1);
  assert.match(index, /<link rel="canonical" href="https:\/\/squizthebot\.com\/"\s*\/>/);
});

test('crawler files never fall back to Angular index.html at Nginx', () => {
  assert.match(nginx, /location = \/robots\.txt \{[^}]*try_files \$uri =404;/s);
  assert.match(nginx, /location = \/sitemap\.xml \{[^}]*try_files \$uri =404;/s);
  assert.match(nginx, /location \/ \{[^}]*try_files \$uri \$uri\/ \/index\.html;/s);
});

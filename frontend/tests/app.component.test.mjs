// Offline Angular component tests with mocked HTTP and virtual RxJS time.
// No browser, PostgreSQL or paid AI calls are required.
import '@angular/compiler';
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import ts from 'typescript';
import { HttpErrorResponse } from '@angular/common/http';
import { of, Subject, throwError } from 'rxjs';
import { TestScheduler } from 'rxjs/testing';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const temp = mkdtempSync(join(root, '.quiz-tests-'));
after(() => rmSync(temp, { recursive: true, force: true }));
const source = readFileSync(join(root, 'src/app/app.component.ts'), 'utf8');
const english = JSON.parse(readFileSync(join(root, '../backend/localization/en.json'), 'utf8'));
const compilerOptions = {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
  experimentalDecorators: true, useDefineForClassFields: false,
};
mkdirSync(join(temp, 'localization'));
const localeSource = readFileSync(join(root, 'src/app/localization/store.ts'), 'utf8');
writeFileSync(join(temp, 'localization/store.mjs'),
  ts.transpileModule(localeSource, { compilerOptions }).outputText);
const resetSource = readFileSync(join(root, 'src/app/localization/reset-copy.ts'), 'utf8');
writeFileSync(join(temp, 'localization/reset-copy.mjs'),
  ts.transpileModule(resetSource, { compilerOptions }).outputText);
const componentSource = source
  .replace("from './localization/store';", "from './localization/store.mjs';")
  .replace("from './localization/reset-copy';", "from './localization/reset-copy.mjs';");
const compiled = ts.transpileModule(componentSource, { compilerOptions }).outputText;
writeFileSync(join(temp, 'app.component.mjs'), compiled);
const { AppComponent } = await import(pathToFileURL(join(temp, 'app.component.mjs')).href);
const { LocalizationStore } = await import(pathToFileURL(join(temp, 'localization/store.mjs')).href);
const { RESET_COPY, RESET_LANGUAGE_NAMES } = await import(
  pathToFileURL(join(temp, 'localization/reset-copy.mjs')).href,
);

const account = {
  id: 'user-1', username: 'Alice', display_name: 'Alice',
  preferred_language: 'English', ui_language: 'en', theme_key: 'default',
};
const question = {
  question_id: 'same-question', number: 5, difficulty: 4,
  question: 'Offline question', answers: { A: 'a', B: 'b', C: 'c', D: 'd' },
  bonus: false,
};

function setup({ warning = 8000, timeout = 45000, settingsError = false,
                  signedIn = true, starAwards = {}, answered = 0, correct = 0, devReset = false } = {}) {
  const requests = [];
  const posts = [];
  const patches = [];
  const profile = {
    games_played: 1, total_score: 0, best_round_score: 0,
    wallet_balance: 0, star_awards: { ...starAwards }, achievements: [],
    answered_questions: answered, correct_answers: correct,
  };
  const http = {
    get(url) {
      if (url === '/api/client-config') {
        return settingsError ? throwError(() => new Error('offline')) : of({
          long_wait_warning_ms: warning, request_timeout_ms: timeout,
        });
      }
      if (url.startsWith('/api/localization/dictionary/')) {
        const code = url.split('/').pop();
        return of({ language_code: code, dictionary: english,
          source_version: 'test', fallback: false, missing_count: 0 });
      }
      if (url === '/api/localization/reset-capability') {
        return of({ enabled: devReset });
      }
      if (url === '/api/auth/me') {
        return signedIn ? of({ ...account }) : throwError(() => new HttpErrorResponse({ status: 401 }));
      }
      if (url === '/api/profile/me/stats') {
        return of({ ...profile, star_awards: { ...profile.star_awards } });
      }
      const subject = new Subject();
      requests.push({ url, subject });
      return subject.asObservable();
    },
    post(url, body) {
      const subject = new Subject();
      posts.push({ url, body, subject });
      return subject.asObservable();
    },
    patch(url, body) {
      const subject = new Subject();
      patches.push({ url, body, subject });
      return subject.asObservable();
    },
  };
  const app = new AppComponent(http);
  app.ngOnInit();
  app.gameId = 'existing-game';
  const scheduler = new TestScheduler((actual, expected) => assert.deepEqual(actual, expected));
  return { app, http, requests, posts, patches, scheduler, profile };
}

function succeed(request, value = question) {
  request.subject.next(value);
  request.subject.complete();
}

test('checks session and loads all-time stars from PostgreSQL stats API', () => {
  const { app } = setup({ starAwards: { bronze: 4, gold: 2 } });
  assert.equal(app.account.username, 'Alice');
  assert.deepEqual(app.totalStars, { bronze: 4, silver: 0, gold: 2, platinum: 0 });
  assert.equal(app.authLoading, false);
  assert.equal(app.gamesPlayed, 1);
});

test('lifetime count includes only correct answers, not incorrect attempts', () => {
  const { app } = setup({ answered: 13, correct: 11 });
  assert.equal(app.correctAnswers, 11);
  assert.equal(app.gamesPlayed, 1);
  assert.equal(app.answeredQuestions, undefined);
});

test('password length hint is only used when registering', () => {
  assert.match(source, /hint\('quiz-auth-password', 'auth.passwordHint'\)/);
  assert.match(source, /if \(isRegister\) \{/);
  assert.doesNotMatch(source, /maxlength="256" placeholder="At least 8 characters"/);
});

test('guest cannot create an unprotected v2 game', () => {
  const { app, posts } = setup({ signedIn: false });
  app.gameId = '';
  let modalOpened = false;
  app.openAuthDialog = async () => { modalOpened = true; };
  app.startGame();
  assert.equal(modalOpened, true);
  assert.equal(app.account, null);
  assert.equal(posts.length, 0);
});

test('long-wait warning appears at 8 seconds and clears on success', () => {
  const { app, requests, scheduler } = setup();
  scheduler.run(() => {
    app.loadQuestion();
    scheduler.schedule(() => assert.equal(app.longWait, false), 7999);
    scheduler.schedule(() => assert.equal(app.longWait, true), 8001);
    scheduler.schedule(() => succeed(requests[0]), 9000);
  });
  assert.equal(app.current.question_id, question.question_id);
  assert.equal(app.loading, false);
  assert.equal(app.questionLoading, false);
  assert.equal(app.longWait, false);
  assert.equal(app.error, '');
  assert.equal(requests[0].url, '/api/v2/games/existing-game/question');
});

test('45-second timeout clears loading and offers retry without a new game', () => {
  const { app, requests, posts, scheduler } = setup();
  scheduler.run(() => app.loadQuestion());
  assert.equal(scheduler.frame, 45000);
  assert.equal(app.loading, false);
  assert.equal(app.longWait, false);
  assert.equal(app.canRetryQuestion, true);
  assert.match(app.locale.t(app.error), /may still be preparing/i);
  assert.equal(requests[0].subject.observed, false);
  scheduler.run(() => {
    app.retryQuestion();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].url, requests[0].url);
    assert.equal(posts.length, 0);
    succeed(requests[1]);
  });
  assert.equal(app.current.question_id, 'same-question');
  assert.equal(app.error, '');
  assert.equal(app.canRetryQuestion, false);
});

test('backend and connection failures clear loading and allow a retry', () => {
  for (const status of [0, 502, 503]) {
    const { app, requests, scheduler } = setup();
    scheduler.run(() => {
      app.loadQuestion();
      requests[0].subject.error(new HttpErrorResponse({ status }));
    });
    assert.equal(app.loading, false);
    assert.equal(app.questionLoading, false);
    assert.equal(app.longWait, false);
    assert.equal(app.canRetryQuestion, true);
    assert.match(app.locale.t(app.error), /retry/i);
    scheduler.run(() => { app.retryQuestion(); succeed(requests[1]); });
    assert.equal(app.screen, 'question');
    assert.equal(app.loading, false);
  }
});

test('expired session resets private player state', () => {
  const { app, requests, scheduler } = setup({ starAwards: { bronze: 2 } });
  scheduler.run(() => {
    app.loadQuestion();
    requests[0].subject.error(new HttpErrorResponse({ status: 401 }));
  });
  assert.equal(app.account, null);
  assert.equal(app.gameId, '');
  assert.equal(app.totalStars.bronze, 0);
  assert.equal(app.canRetryQuestion, false);
  assert.match(app.locale.t(app.error), /session expired/i);
});

test('rapid clicks do not generate parallel questions', () => {
  const { app, requests, scheduler } = setup();
  scheduler.run(() => {
    app.loadQuestion();
    app.loadQuestion();
    app.retryQuestion();
    app.continue();
    app.startGame();
    assert.equal(requests.length, 1);
    succeed(requests[0]);
  });
});

test('answering uses v2 and refreshes server-authoritative rewards', () => {
  const { app, requests, posts, scheduler, profile } = setup();
  app.progress = 4;
  app.screen = 'feedback';
  app.feedback = { phase: 'main', correct: true, correct_answer: 'B', correct_main_answers: 4,
    achievement: null };
  scheduler.run(() => app.loadQuestion());
  assert.equal(app.progress, 4);
  assert.equal(app.currentStars.bronze, 0);
  scheduler.run(() => { app.retryQuestion(); succeed(requests[1]); });
  profile.star_awards.bronze = 3; // The database, not the browser, is authoritative.
  scheduler.run(() => {
    app.answer('B');
    app.answer('B');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/v2/games/existing-game/answer');
    succeed(posts[0], { phase: 'main', correct: true, correct_answer: 'B',
      correct_main_answers: 5, achievement: 'bronze' });
    app.answer('B');
  });
  assert.equal(app.progress, 5);
  assert.equal(app.currentStars.bronze, 1);
  assert.equal(app.totalStars.bronze, 3);
  assert.equal(posts.length, 1);
});

test('logout revokes session and clears server-derived counters in UI', () => {
  const { app, posts } = setup({ starAwards: { gold: 5 } });
  app.logout();
  assert.equal(posts[0].url, '/api/auth/logout');
  succeed(posts[0], undefined);
  assert.equal(app.account, null);
  assert.equal(app.totalStars.gold, 0);
  assert.equal(app.screen, 'setup');
});

test('server-provided warning and timeout configuration takes effect', () => {
  const { app, scheduler } = setup({ warning: 1000, timeout: 3000 });
  scheduler.run(() => {
    app.loadQuestion();
    scheduler.schedule(() => assert.equal(app.longWait, true), 1001);
  });
  assert.equal(scheduler.frame, 3000);
  assert.equal(app.loading, false);
});

test('failed settings request keeps finite defaults', () => {
  const { app, scheduler } = setup({ settingsError: true });
  assert.equal(app.configLoading, false);
  scheduler.run(() => app.loadQuestion());
  assert.equal(scheduler.frame, 45000);
  assert.equal(app.loading, false);
});

test('destroying the component cancels warning and HTTP subscriptions', () => {
  const { app, requests, scheduler } = setup();
  scheduler.run(() => {
    app.loadQuestion();
    scheduler.schedule(() => app.ngOnDestroy(), 1000);
  });
  assert.equal(requests[0].subject.observed, false);
  assert.equal(app.loading, false);
  assert.equal(app.longWait, false);
});

test('starting a game hands loading state to generation and preserves its game ID', () => {
  const { app, requests, posts, scheduler } = setup();
  app.gameId = '';
  scheduler.run(() => {
    app.startGame();
    assert.equal(app.loading, true);
    assert.equal(posts[0].url, '/api/v2/games');
    succeed(posts[0], { game_id: 'created-game', language: 'English', phase: 'main',
      correct_main_answers: 0 });
    assert.equal(app.loading, true);
    assert.equal(requests[0].url, '/api/v2/games/created-game/question');
    requests[0].subject.error(new Error('offline'));
    app.retryQuestion();
    succeed(requests[1]);
  });
  assert.equal(posts.length, 1);
  assert.equal(app.loading, false);
  assert.equal(app.gameId, 'created-game');
});

test('restores unfinished round on login without creating another game', () => {
  const previous = globalThis.localStorage;
  const saved = new Map([['ai-quiz-game:active-round:v2:user-1', 'saved-game-id']]);
  globalThis.localStorage = {
    getItem: key => saved.get(key) ?? null,
    setItem: (key, value) => saved.set(key, value),
    removeItem: key => saved.delete(key),
  };
  try {
    const { app, requests, posts } = setup();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/v2/games/saved-game-id');
    assert.equal(app.restoringGame, true);
    app.gameId = '';
    app.startGame();
    assert.equal(posts.length, 0);
    succeed(requests[0], { game_id: 'saved-game-id', language: 'German',
      phase: 'main', correct_main_answers: 7 });
    assert.equal(app.restoringGame, false);
    assert.equal(app.resumeAvailable, true);
    assert.equal(app.gameId, 'saved-game-id');
    assert.equal(app.progress, 7);
    assert.equal(app.currentStars.bronze, 1);
    app.startGame();
    assert.equal(requests[1].url, '/api/v2/games/saved-game-id/question');
    succeed(requests[1]);
  } finally {
    if (previous === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous;
  }
});

test('setup and answer requests have timeouts without automatic POST retries', () => {
  for (const action of ['startGame', 'answer']) {
    const { app, posts, scheduler } = setup();
    if (action === 'startGame') app.gameId = '';
    else { app.current = question; app.screen = 'question'; }
    scheduler.run(() => app[action]('B'));
    assert.equal(app.loading, false);
    assert.equal(posts.length, 1);
    assert.equal(app.canRetryQuestion, false);
    assert.notEqual(app.error, '');
  }
});

test('template wires login, PostgreSQL rewards and question retries', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /openAuthDialog\('login'\)/);
  assert.match(template, /openAuthDialog\('register'\)/);
  assert.match(template, /role="status" \*ngIf="questionLoading && longWait"/);
  assert.match(template, /\*ngIf="canRetryQuestion && !duplicateExhausted"[\s\S]*?\(click\)="retryQuestion\(\)"/);
  assert.match(template, /stars\.allTime/);
  assert.doesNotMatch(source, /local-star-totals|storeStars|readStoredStars/);
  assert.match(source, /\/api\/profile\/me\/stats/);
});


test('SweetAlert2 dependency, pinned lockfile and styles are synchronized', () => {
  const packageJson = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
  const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
  const angular = JSON.parse(readFileSync(join(root, 'angular.json'), 'utf8'));
  assert.equal(packageJson.dependencies.sweetalert2, '11.26.25');
  assert.equal(lock.packages[''].dependencies.sweetalert2, packageJson.dependencies.sweetalert2);
  assert.equal(lock.packages['node_modules/sweetalert2'].version, packageJson.dependencies.sweetalert2);
  assert.ok(angular.projects['ai-quiz-frontend'].architect.build.options.styles
    .includes('node_modules/sweetalert2/dist/sweetalert2.min.css'));
});

test('UI languages are shown by native names, never sent to localizer', () => {
  const { app } = setup();
  const names = Object.fromEntries(app.uiLanguages.map(item => [item.code, item.nativeName]));
  assert.equal(Object.keys(names).length, 11);
  assert.equal(names.en, 'English');
  assert.equal(names.de, 'Deutsch');
  assert.equal(names.ru, 'Русский');
  assert.equal(names.he, 'עברית');
  assert.equal(names.ar, 'العربية');
  assert.equal(names.ja, '日本語');
  assert.equal(names.zh, '中文');
  assert.equal(Object.values(english).includes('Deutsch'), false);
});

test('changing interface language never changes question language', () => {
  const { app, patches } = setup();
  app.selectedLanguage = 'Russian';
  app.setUiLanguage('he');
  assert.equal(app.locale.code, 'he');
  assert.equal(app.rtl, true);
  assert.equal(app.selectedLanguage, 'Russian');
  assert.equal(patches[0].url, '/api/localization/preference');
  assert.deepEqual(patches[0].body, { language_code: 'he' });
  succeed(patches[0], { language_code: 'he' });
  assert.equal(app.account.ui_language, 'he');
  app.selectedLanguage = 'German';
  assert.equal(app.locale.code, 'he');
});

test('UI source dictionary contains every explicit static translation key', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const referenced = [...source.matchAll(/(?:this\.)?locale\.t\('([^']+)'\)/g),
    ...template.matchAll(/locale\.t\('([^']+)'\)/g),
    ...source.matchAll(/this\.fail\('([^']+)'\)/g)];
  for (const match of referenced) {
    if (match[1].endsWith('.')) continue;
    assert.ok(Object.hasOwn(english, match[1]), 'Missing English key: ' + match[1]);
  }
});


test('language changes are atomic: no English flash while another translation loads', () => {
  const german = new Subject();
  const http = {
    get(url) {
      if (url.endsWith('/en')) return of({
        language_code: 'en', dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0,
      });
      if (url.endsWith('/de')) return german.asObservable();
      throw new Error('Unexpected request: ' + url);
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  assert.equal(store.t('hero.ready'), 'Ready to play?');
  assert.equal(store.loading, false);

  store.load('de');
  assert.equal(store.code, 'en');
  assert.equal(store.requestedCode, 'de');
  assert.equal(store.loading, true);
  assert.equal(store.t('hero.ready'), 'Ready to play?');

  const translated = { ...english, 'hero.ready': 'Bereit zum Spielen?' };
  german.next({ language_code: 'de', dictionary: translated, source_version: 'v1',
    fallback: false, missing_count: 0 });
  german.complete();
  assert.equal(store.loading, false);
  assert.equal(store.code, 'de');
  assert.equal(store.t('hero.ready'), 'Bereit zum Spielen?');
  store.destroy();
});

test('German to French uses no intermediate English for ready cached locales', () => {
  const french = new Subject();
  const germanDictionary = { ...english, 'hero.ready': 'Bereit zum Spielen?' };
  const frenchDictionary = { ...english, 'hero.ready': 'Prêt à jouer ?' };
  const http = {
    get(url) {
      if (url.endsWith('/en')) return of({
        language_code: 'en', dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0,
      });
      if (url.endsWith('/de')) return of({
        language_code: 'de', dictionary: germanDictionary,
        source_version: 'v1', fallback: false, missing_count: 0,
      });
      if (url.endsWith('/fr')) return french.asObservable();
      throw new Error('Unexpected request: ' + url);
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('de');
  assert.equal(store.t('hero.ready'), 'Bereit zum Spielen?');

  store.load('fr');
  assert.equal(store.loading, true);
  assert.equal(store.code, 'de');
  assert.equal(store.t('hero.ready'), 'Bereit zum Spielen?');
  french.next({ language_code: 'fr', dictionary: frenchDictionary,
    source_version: 'v1', fallback: false, missing_count: 0 });
  french.complete();
  assert.equal(store.loading, false);
  assert.equal(store.code, 'fr');
  assert.equal(store.t('hero.ready'), 'Prêt à jouer ?');
  store.destroy();
});

test('incomplete translations keep prior screen and spinner until ready', () => {
  const requests = [];
  const http = {
    get(url) {
      if (url.endsWith('/en')) return of({ language_code: 'en', dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0 });
      const subject = new Subject();
      requests.push(subject);
      return subject.asObservable();
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('fr');
  requests[0].next({ language_code: 'fr', dictionary: english, source_version: 'v1',
    fallback: true, missing_count: 10 });
  requests[0].complete();
  assert.equal(store.loading, true);
  assert.equal(store.code, 'en');
  assert.equal(store.t('hero.ready'), 'Ready to play?');
  store.destroy(); // Cancels the scheduled retry; no paid calls leak from tests.
});

test('failed translation keeps the previous language without saving preference', () => {
  const requests = [];
  const http = {
    get(url) {
      if (url.endsWith('/en')) return of({ language_code: 'en', dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0 });
      const request = new Subject();
      requests.push(request);
      return request.asObservable();
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  let success = true;
  store.load('fr', (value) => { success = value; });
  requests[0].error(new Error('offline'));
  assert.equal(success, false);
  assert.equal(store.loading, false);
  assert.equal(store.code, 'en');
  assert.equal(store.requestedCode, 'en');
  assert.equal(store.fallback, true);
  store.destroy();
});

test('loading overlay contains rotating captions in eleven native languages', () => {
  const { app } = setup();
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.equal(app.localizationStatus.length, 11);
  assert.ok(app.localizationStatus.some(x => x.text.includes('Идёт перевод сайта')));
  assert.ok(app.localizationStatus.some(x => x.text.includes('正在翻译网站')));
  assert.ok(app.localizationStatus.some(x => x.rtl));
  assert.match(template, /class="localization-overlay"/);
  assert.match(template, /class="localization-phrase"/);
  assert.match(template, /\*ngIf="locale.loading"/);
});

test('reset control is visible only when backend enables development mode', () => {
  const { app } = setup();
  assert.equal(app.resetEnabled, false);
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /\*ngIf="account && resetEnabled"/);
  const enabled = setup({ devReset: true });
  assert.equal(enabled.app.resetEnabled, true);
  assert.equal(enabled.app.locale.code, 'en');
});


test('loading caption always remains visible and switches every two seconds', () => {
  const englishResult = { language_code: 'en', dictionary: english,
    source_version: 'v1', fallback: false, missing_count: 0 };
  const pending = new Subject();
  const store = new LocalizationStore({
    get(url) { return url.endsWith('/en') ? of(englishResult) : pending.asObservable(); },
  });
  const scheduler = new TestScheduler((actual, expected) => assert.deepEqual(actual, expected));
  scheduler.run(() => {
    store.load('en');
    store.load('de');
    assert.equal(store.messageIndex, 0);
    scheduler.schedule(() => assert.equal(store.messageIndex, 1), 2001);
    scheduler.schedule(() => assert.equal(store.messageIndex, 2), 4001);
    scheduler.schedule(() => {
      pending.next({ language_code: 'de',
        dictionary: { ...english, 'hero.ready': 'Bereit zum Spielen?' },
        source_version: 'v1', fallback: false, missing_count: 0 });
      pending.complete();
      assert.equal(store.loading, false);
      assert.equal(store.code, 'de');
      store.destroy();
    }, 4100);
  });
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const css = readFileSync(join(root, 'src/app/app.component.css'), 'utf8');
  assert.match(template, /localizationStatus\[locale\.messageIndex\]\.text/);
  assert.equal([...template.matchAll(/class="localization-phrase"/g)].length, 1);
  assert.match(css, /\.localization-phrase\s*\{[^}]*opacity:\s*1/s);
  assert.doesNotMatch(css, /@keyframes locale-phrase-cycle|@keyframes locale-phrase-crossfade/);
});

test('question language options are localized labels but API uses original English names', () => {
  const { app } = setup();
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const expected = ['English', 'Russian', 'Hebrew', 'German', 'French', 'Spanish', 'Italian'];
  assert.deepEqual(app.languages, expected);
  assert.match(template, /\[value\]="language"/);
  assert.match(template, /locale\.t\('questionLanguage\.' \+ language\)/);
  for (const name of expected) {
    assert.equal(english['questionLanguage.' + name], name);
  }
  // Interface language dropdown must still use native names.
  assert.match(template, /\{\{ language\.nativeName \}\}/);
});

test('developer reset button, all dialog text and choices stay English', () => {
  const { app } = setup({ devReset: true });
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.equal(app.resetCopy.button, 'Reset translations (dev)');
  assert.equal(RESET_COPY.confirm, 'Remove saved translations');
  assert.equal(RESET_COPY.cancel, 'Cancel');
  assert.match(RESET_COPY.explain, /cost money/);
  assert.equal(RESET_LANGUAGE_NAMES.de, 'German');
  assert.equal(RESET_LANGUAGE_NAMES.ru, 'Russian');
  assert.equal(RESET_LANGUAGE_NAMES.he, 'Hebrew');
  assert.match(template, /resetCopy\.button/);
  assert.doesNotMatch(source, /locale\.(?:t|format)\('localization\.reset/);
  assert.deepEqual(Object.keys(english).filter(k => k.startsWith('localization.reset')), []);
  assert.equal(app.locale.code, 'en');
});

test('spinner continues rotating under reduced-motion setting', () => {
  const css = readFileSync(join(root, 'src/app/app.component.css'), 'utf8');
  assert.match(css, /@keyframes locale-orbit/);
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*?)\n\}/)?.[1];
  assert.ok(reduced, 'Reduced-motion styles should exist');
  assert.doesNotMatch(reduced, /animation:\s*none|display:\s*none/);
  assert.match(reduced, /animation-duration:\s*6s/);
  assert.match(reduced, /animation-duration:\s*8s/);
});


test('RAM cache switches among complete locales instantly without HTTP or spinner', () => {
  const calls = [];
  const words = { en: 'Ready to play?', de: 'Bereit zum Spielen?', fr: 'Prêt à jouer ?' };
  const http = {
    get(url) {
      const code = url.split('/').pop();
      calls.push(code);
      return of({ language_code: code, dictionary: { ...english, 'hero.ready': words[code] },
        source_version: 'v1', fallback: false, missing_count: 0 });
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('de');
  store.load('fr');
  assert.deepEqual(calls, ['en', 'de', 'fr']);
  assert.equal(store.t('hero.ready'), words.fr);
  let callbackSuccess = false;
  store.load('de', success => { callbackSuccess = success; });
  assert.equal(callbackSuccess, true);
  assert.equal(store.loading, false);
  assert.equal(store.code, 'de');
  assert.equal(store.t('hero.ready'), words.de);
  store.load('fr');
  store.load('en');
  assert.deepEqual(calls, ['en', 'de', 'fr']);
  assert.equal(store.loading, false);
  assert.equal(store.t('hero.ready'), words.en);
  store.destroy();
});

test('a newly created localization store fetches fresh dictionaries after page reload', () => {
  const calls = [];
  const http = {
    get(url) {
      const code = url.split('/').pop();
      calls.push(code);
      return of({ language_code: code, dictionary: english,
        source_version: 'latest', fallback: false, missing_count: 0 });
    },
  };
  const oldPage = new LocalizationStore(http);
  oldPage.load('en');
  oldPage.load('de');
  oldPage.load('en');
  oldPage.destroy();

  const newPage = new LocalizationStore(http);
  newPage.load('en');
  newPage.load('de');
  assert.deepEqual(calls, ['en', 'de', 'en', 'de']);
  newPage.destroy();
});

test('reset invalidates selected cached locale but preserves unrelated ones', () => {
  const calls = [];
  const http = {
    get(url) {
      const code = url.split('/').pop();
      calls.push(code);
      return of({ language_code: code, dictionary: { ...english, 'hero.ready': code },
        source_version: String(calls.length), fallback: false, missing_count: 0 });
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('de');
  store.load('fr');
  store.invalidate('de');
  store.load('de');
  store.load('fr');
  store.load('de');
  assert.deepEqual(calls, ['en', 'de', 'fr', 'de']);
  assert.equal(store.loading, false);
  assert.equal(store.code, 'de');
  assert.match(source, /this\.locale\.invalidate\(selected\)/);
  store.destroy();
});

test('reset all and forced refresh bypass stale RAM dictionaries', () => {
  const calls = [];
  const http = {
    get(url) {
      const code = url.split('/').pop();
      calls.push(code);
      return of({ language_code: code, dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0 });
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('de');
  store.load('fr');
  store.invalidate('all');
  store.load('de'); // Must fetch fresh copy, even if it is currently displayed.
  store.load('fr');
  store.load('fr', undefined, true); // Explicit refresh also bypasses cache.
  assert.deepEqual(calls, ['en', 'de', 'fr', 'de', 'fr', 'fr']);
  store.destroy();
});

test('cache hit cancels a pending translation and ignores its late response', () => {
  const pending = new Subject();
  const calls = [];
  const http = {
    get(url) {
      const code = url.split('/').pop();
      calls.push(code);
      if (code === 'de') return pending.asObservable();
      return of({ language_code: code, dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0 });
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('de');
  assert.equal(store.loading, true);
  store.load('en'); // English is already in RAM; no request or overlay.
  assert.equal(store.loading, false);
  assert.equal(store.code, 'en');
  assert.equal(pending.observed, false);
  pending.next({ language_code: 'de', dictionary: { ...english, 'hero.ready': 'Stale' },
    source_version: 'v1', fallback: false, missing_count: 0 });
  pending.complete();
  assert.equal(store.code, 'en');
  assert.equal(store.t('hero.ready'), 'Ready to play?');
  assert.deepEqual(calls, ['en', 'de']);
  store.destroy();
});

test('failed translations never enter the RAM cache', () => {
  let attempts = 0;
  const http = {
    get(url) {
      const code = url.split('/').pop();
      if (code === 'en') return of({ language_code: code, dictionary: english,
        source_version: 'v1', fallback: false, missing_count: 0 });
      attempts++;
      return throwError(() => new Error('Offline'));
    },
  };
  const store = new LocalizationStore(http);
  store.load('en');
  store.load('de');
  assert.equal(store.loading, false);
  assert.equal(store.code, 'en');
  store.load('de');
  assert.equal(store.loading, false);
  assert.equal(store.code, 'en');
  assert.equal(attempts, 2);
  store.destroy();
});


test('duplicate exhaustion presents a translated recovery panel, retry keeps the game', () => {
  const { app, requests, posts, scheduler } = setup();
  app.screen = 'feedback';
  app.feedback = { phase: 'main', correct: true, correct_main_answers: 7 };
  app.current = question;
  app.progress = 7;
  scheduler.run(() => {
    app.loadQuestion();
    requests[0].subject.error(new HttpErrorResponse({
      status: 409, error: { detail: { code: 'duplicate_exhausted' } },
    }));
  });
  assert.equal(app.duplicateExhausted, true);
  assert.equal(app.error, 'game.duplicateExhausted');
  assert.match(app.locale.t(app.error), /unique question/i);
  assert.equal(app.canRetryQuestion, true);
  assert.equal(app.screen, 'question');
  assert.equal(app.current, null);
  assert.equal(app.feedback, null);
  assert.equal(app.gameId, 'existing-game');
  assert.equal(app.progress, 7);
  assert.equal(posts.length, 0);
  scheduler.run(() => {
    app.retryQuestion();
    assert.equal(app.duplicateExhausted, false);
    assert.equal(requests.length, 2);
    assert.equal(requests[1].url, requests[0].url);
    succeed(requests[1]);
  });
  assert.equal(app.error, '');
  assert.equal(app.screen, 'question');
  assert.equal(app.gameId, 'existing-game');
  assert.equal(app.canRetryQuestion, false);
  assert.equal(posts.length, 0);
});

test('Back to menu preserves the unfinished game and Resume uses the same ID', () => {
  const { app, requests, posts, scheduler } = setup();
  app.progress = 11;
  scheduler.run(() => {
    app.loadQuestion();
    requests[0].subject.error(new HttpErrorResponse({
      status: 409, error: { detail: { code: 'duplicate_exhausted' } },
    }));
  });
  app.returnToMenuAfterDuplicate();
  assert.equal(app.screen, 'setup');
  assert.equal(app.resumeAvailable, true);
  assert.equal(app.gameId, 'existing-game');
  assert.equal(app.progress, 11);
  assert.equal(app.error, '');
  assert.equal(app.duplicateExhausted, false);
  assert.equal(app.canRetryQuestion, false);
  assert.equal(posts.length, 0);
  scheduler.run(() => {
    app.startGame();
    assert.equal(requests[1].url, requests[0].url);
    succeed(requests[1]);
  });
  assert.equal(app.gameId, 'existing-game');
  assert.equal(app.progress, 11);
  assert.equal(posts.length, 0);
});

test('only the dedicated duplicate_exhausted error triggers the special panel', () => {
  for (const error of [
    { status: 409, error: { detail: 'Game state changed' } },
    { status: 502, error: { detail: { code: 'different_error' } } },
    { status: 503, error: { detail: 'AI unavailable' } },
  ]) {
    const { app, requests, scheduler } = setup();
    scheduler.run(() => {
      app.loadQuestion();
      requests[0].subject.error(new HttpErrorResponse(error));
    });
    assert.equal(app.duplicateExhausted, false);
    assert.notEqual(app.error, 'game.duplicateExhausted');
    assert.equal(app.canRetryQuestion, true);
  }
});

test('duplicate recovery shows two accessible translated actions, not generic retry', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /class="duplicate-recovery" \*ngIf="duplicateExhausted" role="alert"/);
  assert.match(template, /locale\.t\('game\.duplicateExhausted'\)/);
  assert.match(template, /\(click\)="retryQuestion\(\)"[\s\S]*?game\.duplicateRetry/);
  assert.match(template, /\(click\)="returnToMenuAfterDuplicate\(\)"[\s\S]*?game\.returnToMenu/);
  assert.match(template, /\*ngIf="canRetryQuestion && !duplicateExhausted"/);
  for (const key of ['game.duplicateExhausted', 'game.duplicateRetry', 'game.returnToMenu']) {
    assert.ok(english[key], key + ' must be in the AI localization source');
  }
});

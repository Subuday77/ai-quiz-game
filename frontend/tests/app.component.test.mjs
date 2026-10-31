// Offline Angular component tests with mocked HTTP and virtual RxJS time.
// No browser, PostgreSQL or paid AI calls are required.
import '@angular/compiler';
import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
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
const compiled = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
  experimentalDecorators: true, useDefineForClassFields: false,
} }).outputText;
writeFileSync(join(temp, 'app.component.mjs'), compiled);
const { AppComponent } = await import(pathToFileURL(join(temp, 'app.component.mjs')).href);

const account = {
  id: 'user-1', username: 'Alice', display_name: 'Alice',
  preferred_language: 'English', theme_key: 'default',
};
const question = {
  question_id: 'same-question', number: 5, difficulty: 4,
  question: 'Offline question', answers: { A: 'a', B: 'b', C: 'c', D: 'd' },
  bonus: false,
};

function setup({ warning = 8000, timeout = 45000, settingsError = false,
                 signedIn = true, starAwards = {} } = {}) {
  const requests = [];
  const posts = [];
  const profile = {
    games_played: 1, total_score: 0, best_round_score: 0,
    wallet_balance: 0, star_awards: { ...starAwards }, achievements: [],
    answered_questions: 0, correct_answers: 0,
  };
  const http = {
    get(url) {
      if (url === '/api/client-config') {
        return settingsError ? throwError(() => new Error('offline')) : of({
          long_wait_warning_ms: warning, request_timeout_ms: timeout,
        });
      }
      if (url === '/api/auth/me') {
        return signedIn ? of(account) : throwError(() => new HttpErrorResponse({ status: 401 }));
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
  };
  const app = new AppComponent(http);
  app.ngOnInit();
  app.gameId = 'existing-game';
  const scheduler = new TestScheduler((actual, expected) => assert.deepEqual(actual, expected));
  return { app, http, requests, posts, scheduler, profile };
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
  assert.match(app.error, /may still be preparing/i);
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
    assert.match(app.error, /retry/i);
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
  assert.match(app.error, /session expired/i);
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
  assert.match(template, /\*ngIf="canRetryQuestion"[\s\S]*?\(click\)="retryQuestion\(\)"/);
  assert.match(template, /ALL TIME/);
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

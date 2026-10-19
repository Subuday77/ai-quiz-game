// Offline component tests with mocked HTTP and virtual RxJS time. No browser or server.
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
const question = { question_id: 'same-question', number: 5, difficulty: 4,
  question: 'Offline question', answers: { A: 'a', B: 'b', C: 'c', D: 'd' }, bonus: false };

function setup({ warning = 8000, timeout = 45000, settingsError = false } = {}) {
  const requests = [];
  const posts = [];
  const http = {
    get(url) {
      if (url === '/api/client-config') {
        return settingsError ? throwError(() => new Error('offline')) : of({
          long_wait_warning_ms: warning, request_timeout_ms: timeout,
        });
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
  return { app, http, requests, posts, scheduler };
}

function succeed(request, value = question) {
  request.subject.next(value);
  request.subject.complete();
}

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

test('backend and connection failures clear loading and allow a working retry', () => {
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

test('question retry preserves progress and rewards; answering awards once', () => {
  const { app, requests, posts, scheduler } = setup();
  app.progress = 4;
  app.screen = 'feedback';
  app.feedback = { phase: 'main', correct: true, correct_answer: 'B', correct_main_answers: 4,
    achievement: null };
  scheduler.run(() => app.loadQuestion());
  assert.equal(app.progress, 4);
  assert.equal(app.currentStars.bronze, 0);
  scheduler.run(() => { app.retryQuestion(); succeed(requests[1]); });
  assert.equal(app.progress, 4);
  assert.equal(app.currentStars.bronze, 0);
  scheduler.run(() => {
    app.answer('B');
    app.answer('B');
    assert.equal(posts.length, 1);
    succeed(posts[0], { phase: 'main', correct: true, correct_answer: 'B',
      correct_main_answers: 5, achievement: 'bronze' });
    app.answer('B');
  });
  assert.equal(app.progress, 5);
  assert.equal(app.currentStars.bronze, 1);
  assert.equal(app.totalStars.bronze, 1);
  assert.equal(posts.length, 1);
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

test('starting a game hands loading state to generation and recovery uses its game ID', () => {
  const { app, requests, posts, scheduler } = setup();
  app.gameId = '';
  scheduler.run(() => {
    app.startGame();
    assert.equal(app.loading, true);
    succeed(posts[0], { game_id: 'created-game', language: 'English', phase: 'main',
      correct_main_answers: 0 });
    assert.equal(app.loading, true);
    assert.equal(requests[0].url, '/api/games/created-game/question');
    requests[0].subject.error(new Error('offline'));
    app.retryQuestion();
    succeed(requests[1]);
  });
  assert.equal(posts.length, 1);
  assert.equal(app.loading, false);
  assert.equal(app.gameId, 'created-game');
});

test('setup and answer requests also have finite timeouts without automatic POST retries', () => {
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

test('template exposes warning status and wires the Retry button', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /role="status" \*ngIf="questionLoading && longWait"/);
  assert.match(template, /\*ngIf="canRetryQuestion" \(click\)="retryQuestion\(\)"/);
});

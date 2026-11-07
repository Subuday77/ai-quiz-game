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
const testComponents = [];
after(() => {
  // Mock component instances own RxJS subscriptions and timer-based i18n polls.
  // Dispose every instance so Node can exit naturally after all assertions.
  for (const app of testComponents) app.ngOnDestroy();
  rmSync(temp, { recursive: true, force: true });
});
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

function setup({ warning = 15000, timeout = 45000, settingsError = false,
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
      if (url === '/api/v2/stars') {
        return of({ bronze: 0, silver: 0, gold: 0, platinum: 0 });
      }
      if (/^\/api\/v2\/games\/[^/]+\/categories$/.test(url)) {
        return of({
          allowed: ['Ancient history', 'Biology', 'Physics', 'Poetry'],
          current: null, alternatives: ['Biology', 'Physics', 'Poetry'],
          changes_used: 0, manual_used: 0,
          can_change: true, can_choose: true, can_exclude: true,
          minimum_allowed: 16,
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
    patch(url, body) {
      const subject = new Subject();
      patches.push({ url, body, subject });
      return subject.asObservable();
    },
  };
  const app = new AppComponent(http);
  testComponents.push(app);
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

test('long-wait warning appears at 15 seconds and clears on success', () => {
  const { app, requests, scheduler } = setup();
  scheduler.run(() => {
    app.loadQuestion();
    scheduler.schedule(() => assert.equal(app.longWait, false), 14999);
    scheduler.schedule(() => assert.equal(app.longWait, true), 15001);
    scheduler.schedule(() => succeed(requests[0]), 16000);
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

test('failed first generated question shows Retry only, never the setup or exclusions', () => {
  // Regression: POST /games succeeded, but the first GET /question returned 502.
  // The run must remain allocated; a retry is the ONLY ordinary action.
  const { app, requests, posts, scheduler } = setup();
  app.gameId = '';
  app.screen = 'setup';
  scheduler.run(() => {
    app.startGame();
    assert.equal(posts.length, 1);
    succeed(posts[0], { game_id: 'started-round', language: 'Russian',
      phase: 'main', correct_main_answers: 0 });
    assert.equal(app.screen, 'question');
    assert.equal(app.current, null);
    assert.equal(app.showCategoryExclusions, false);
    assert.equal(requests.length, 1);
    requests[0].subject.error(new HttpErrorResponse({ status: 502 }));
    assert.equal(app.screen, 'question');
    assert.equal(app.current, null);
    assert.equal(app.feedback, null);
    assert.equal(app.loading, false);
    assert.equal(app.canRetryQuestion, true);
    assert.equal(app.duplicateExhausted, false);
    assert.equal(app.gameId, 'started-round');
    assert.equal(app.progress, 0);
    assert.equal(posts.length, 1); // No second game created.
    app.retryQuestion();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].url, '/api/v2/games/started-round/question');
    succeed(requests[1], { ...question, number: 1, difficulty: 0 });
  });
  assert.equal(app.screen, 'question');
  assert.equal(app.current.number, 1);
  assert.equal(app.canRetryQuestion, false);
  assert.equal(app.error, '');
  assert.equal(posts.length, 1);
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  // Setup and category actions are under screen=setup, inaccessible on failure.
  assert.match(template, /<div \*ngIf="screen === 'setup' && !bonusDecisionAvailable" class="setup">/);
  assert.match(template, /<div class="question-actions" \*ngIf="canRetryQuestion && !duplicateExhausted">/);
});

test('failed later question discards stale feedback and keeps the original game', () => {
  const { app, requests, posts, scheduler } = setup();
  app.screen = 'feedback';
  app.progress = 8;
  app.current = question;
  app.feedback = { phase: 'main', correct: true, correct_answer: 'B',
    correct_main_answers: 8, achievement: null };
  scheduler.run(() => {
    app.continue();
    assert.equal(app.screen, 'question');
    assert.equal(app.current, null);
    assert.equal(app.feedback, null);
    requests[0].subject.error(new HttpErrorResponse({ status: 503 }));
    assert.equal(app.screen, 'question');
    assert.equal(app.canRetryQuestion, true);
    assert.equal(app.progress, 8);
    app.retryQuestion();
    succeed(requests[1], { ...question, number: 9, difficulty: 8 });
  });
  assert.equal(app.current.number, 9);
  assert.equal(app.gameId, 'existing-game');
  assert.equal(posts.length, 0);
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

test('next-question button switches to generation label until the response arrives', () => {
  const { app, requests, scheduler } = setup();
  app.screen = 'feedback';
  app.feedback = { phase: 'main', correct: true, correct_answer: 'B',
    correct_main_answers: 2, achievement: null };
  assert.equal(app.feedbackButtonLabelKey, 'game.nextQuestion');
  scheduler.run(() => {
    app.continue();
    assert.equal(app.loading, true);
    assert.equal(app.feedbackButtonLabelKey, 'game.loading');
    assert.equal(requests[0].url, '/api/v2/games/existing-game/question');
    succeed(requests[0]);
  });
  assert.equal(app.loading, false);
  assert.equal(app.screen, 'question');
});


test('failed question hides Next and offers one Retry without losing progress', () => {
  const { app, requests, scheduler } = setup();
  app.screen = 'feedback';
  app.progress = 10;
  app.feedback = { phase: 'main', correct: true, correct_answer: 'B',
    correct_main_answers: 10, achievement: 'silver' };
  scheduler.run(() => {
    app.continue();
    assert.equal(requests.length, 1);
    requests[0].subject.error(new HttpErrorResponse({ status: 502 }));
    assert.equal(app.canRetryQuestion, true);
    app.continue(); // Old "Next question" cannot issue an extra request.
    assert.equal(requests.length, 1);
    assert.equal(app.progress, 10);
    app.retryQuestion();
    assert.equal(requests.length, 2);
    succeed(requests[1]);
  });
  assert.equal(app.screen, 'question');
  assert.equal(app.canRetryQuestion, false);
  assert.equal(app.progress, 10);
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /\*ngIf="!canRetryQuestion && !duplicateExhausted && !bonusDecisionAvailable"/);
});

test('bonus choice is required before issuing Q16; failed loading still permits retry', () => {
  const { app, requests, posts, scheduler } = setup();
  app.screen = 'feedback';
  app.progress = 15;
  app.bonusDecisionAvailable = true;
  app.feedback = { phase: 'bonus', correct: true, correct_answer: 'B',
    correct_main_answers: 15, achievement: 'gold' };
  app.currentStars.gold = 1;
  app.continue();
  assert.equal(requests.length, 0); // Cannot bypass the explicit gold decision.
  scheduler.run(() => {
    // Keep both subscription and completion on the same virtual scheduler.
    // Crossing to real time leaves RxJS's timeout interval alive after tests.
    app.acceptBonus();
    assert.equal(posts[0].url, '/api/v2/games/existing-game/bonus/accept');
    succeed(posts[0], { phase: 'bonus', correct_main_answers: 15 });
    assert.equal(app.bonusDecisionAvailable, false);
    assert.equal(requests[0].url, '/api/v2/games/existing-game/question');
    requests[0].subject.error(new HttpErrorResponse({ status: 503 }));
    assert.equal(app.canRetryQuestion, true);
    assert.equal(app.progress, 15);
    app.continue();
    assert.equal(requests.length, 1);
    app.retryQuestion();
    assert.equal(requests.length, 2);
    succeed(requests[1], { ...question, number: 16, difficulty: 15, bonus: true });
  });
  assert.equal(app.current.bonus, true);
  assert.equal(app.canRetryQuestion, false);
  assert.equal(app.currentStars.gold, 1);
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.doesNotMatch(template, /changeBonusTopic|game.changeBonusTopic/);
});

test('bonus chooser network errors do not issue a question or charge again', () => {
  for (const error of [
    new HttpErrorResponse({ status: 0 }), new Error('connection error'),
  ]) {
    const { app, requests, posts } = setup();
    app.bonusDecisionAvailable = true;
    app.progress = 15;
    app.screen = 'feedback';
    app.feedback = { phase: 'bonus', correct: true, correct_answer: 'B',
      correct_main_answers: 15, achievement: 'gold' };
    app.acceptBonus();
    assert.equal(posts.length, 1);
    posts[0].subject.error(error);
    assert.equal(requests.length, 0);
    assert.equal(app.bonusDecisionAvailable, true);
    assert.equal(app.error, 'helper.bonusDecisionFailed');
  }
});

test('results button keeps results label and does not generate a new question', () => {
  const { app, requests } = setup();
  app.screen = 'feedback';
  app.feedback = { phase: 'completed', correct: true, correct_answer: 'B',
    correct_main_answers: 15, achievement: 'gold' };
  assert.equal(app.feedbackButtonLabelKey, 'game.seeResults');
  app.continue();
  assert.equal(app.screen, 'done');
  assert.equal(requests.length, 0);
});

test('current question uses the active slot, including failed Q8 and bonus Q16', () => {
  const { app } = setup();
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.equal(english['game.progress'], 'CURRENT QUESTION');
  assert.match(template, /currentQuestionNumber/);
  assert.match(template, /currentQuestionTotal/);
  app.screen = 'question';
  app.current = null;
  app.progress = 0;
  assert.equal(app.currentQuestionNumber, 1); // First question, not 0.
  app.progress = 7;
  assert.equal(app.currentQuestionNumber, 8); // Next question is being generated.
  app.current = { ...question, number: 8 };
  assert.equal(app.currentQuestionNumber, 8);
  app.screen = 'done';
  app.progress = 8; // Six correct, one skipped, one incorrect.
  assert.equal(app.currentQuestionNumber, 8);
  assert.equal(app.currentQuestionTotal, 15);
  app.current = { ...question, number: 16, bonus: true };
  assert.equal(app.currentQuestionNumber, 16);
  assert.equal(app.currentQuestionTotal, 16);
  app.current = null; // Recovered terminal state.
  assert.equal(app.currentQuestionNumber, 8);
});

test('normal Start game immediately requests Q1 without requiring a second click', () => {
  const { app, requests, posts, scheduler } = setup();
  app.gameId = '';
  scheduler.run(() => {
    app.startGame();
    assert.equal(app.loading, true);
    assert.equal(posts[0].url, '/api/v2/games');
    succeed(posts[0], { game_id: 'created-game', language: 'English',
      phase: 'main', correct_main_answers: 0 });
    assert.equal(app.gameId, 'created-game');
    assert.equal(app.showCategoryExclusions, false);
    assert.equal(requests[0].url, '/api/v2/games/created-game/question');
    assert.equal(app.loading, true);
    succeed(requests[0]);
  });
  assert.equal(posts.length, 1);
  assert.equal(app.loading, false);
  assert.equal(app.gameId, 'created-game');
  assert.equal(app.screen, 'question');
});

test('after paying for category exclusions the question language remains editable', () => {
  const { app, requests, posts, patches, scheduler } = setup();
  app.gameId = '';
  scheduler.run(() => {
    app.openCategoryExclusions();
    succeed(posts[0], { game_id: 'paid-game', language: 'English',
      phase: 'main', correct_main_answers: 0 });
    assert.equal(app.categoryInfo.can_exclude, true);
    assert.equal(app.canEditPregameLanguage, true);
    // The server may also report no more exclusions possible (only 16 left).
    app.categoryInfo = {
      ...app.categoryInfo, allowed: ['Biology', 'Physics'],
      can_exclude: false, current: null,
    };
    app.spendableStars.silver = 0;
    app.showCategoryExclusions = false;
    assert.equal(app.canEditPregameLanguage, true);
    app.changeQuestionLanguage('Russian');
    assert.equal(patches.length, 1);
    assert.equal(patches[0].url, '/api/v2/games/paid-game/language');
    assert.deepEqual(patches[0].body, { language: 'Russian' });
    assert.equal(app.languageSaving, true);
    app.startGame(); // Must wait for PostgreSQL to confirm the language.
    assert.equal(requests.length, 0);
    succeed(patches[0], { game_id: 'paid-game', language: 'Russian',
      phase: 'main', correct_main_answers: 0 });
    assert.equal(app.languageSaving, false);
    assert.equal(app.gameId, 'paid-game');
    assert.equal(app.selectedLanguage, 'Russian');
    assert.equal(app.categoryInfo.allowed.length, 2);
    assert.equal(app.spendableStars.silver, 0);
    app.startGame();
    assert.equal(requests[0].url, '/api/v2/games/paid-game/question');
    succeed(requests[0]);
  });
  assert.equal(app.screen, 'question');
  assert.equal(posts.length, 1); // No new game and no second payment.
});

test('failed pregame language save retains purchased exclusions and original language', () => {
  const { app, posts, patches, scheduler } = setup();
  app.gameId = '';
  scheduler.run(() => {
    app.openCategoryExclusions();
    succeed(posts[0], { game_id: 'paid-game', language: 'English',
      phase: 'main', correct_main_answers: 0 });
    app.categoryInfo = { ...app.categoryInfo,
      allowed: ['Biology', 'Physics'], current: null, can_exclude: false,
    };
    app.changeQuestionLanguage('Russian');
    patches[0].subject.error(new HttpErrorResponse({ status: 503 }));
  });
  assert.equal(app.languageSaving, false);
  assert.equal(app.selectedLanguage, 'English');
  assert.equal(app.gameId, 'paid-game');
  assert.deepEqual(app.categoryInfo.allowed, ['Biology', 'Physics']);
  assert.equal(app.error, 'game.languageUpdateFailed');
  assert.equal(app.canEditPregameLanguage, true);
});

test('visible pregame exclusion button creates empty round and defers Q1 until Start questions', () => {
  const { app, requests, posts, scheduler } = setup();
  app.gameId = '';
  assert.equal(app.setupButtonLabelKey, 'game.start');
  scheduler.run(() => {
    app.openCategoryExclusions();
    assert.equal(app.loading, true);
    assert.equal(posts[0].url, '/api/v2/games');
    succeed(posts[0], { game_id: 'created-game', language: 'English',
      phase: 'main', correct_main_answers: 0 });
    assert.equal(app.gameId, 'created-game');
    assert.equal(app.loading, false);
    assert.equal(app.screen, 'setup');
    assert.equal(app.showCategoryExclusions, true);
    assert.equal(app.categoryInfo.can_exclude, true);
    assert.equal(app.setupButtonLabelKey, 'game.start');
    assert.equal(requests.length, 0);
    app.startGame();
    assert.equal(app.showCategoryExclusions, false);
    assert.equal(app.loading, true);
    assert.equal(requests[0].url, '/api/v2/games/created-game/question');
    succeed(requests[0]);
  });
  assert.equal(posts.length, 1);
  assert.equal(app.loading, false);
  assert.equal(app.screen, 'question');
});

test('adjacent star arrows exchange exactly one bundle in either direction', () => {
  const { app, posts } = setup();
  app.spendableStars = { bronze: 10, silver: 1, gold: 0, platinum: 0 };
  app.exchangeBetween('bronze', 'silver');
  assert.equal(posts[0].url, '/api/v2/stars/exchange');
  assert.deepEqual({ source: posts[0].body.source, target: posts[0].body.target,
    amount: posts[0].body.amount },
    { source: 'bronze', target: 'silver', amount: 10 });
  succeed(posts[0], { bronze: 0, silver: 2, gold: 0, platinum: 0 });
  assert.equal(app.perksBusy, false);
  assert.equal(app.spendableStars.silver, 2);
  app.exchangeBetween('silver', 'bronze');
  assert.deepEqual({ source: posts[1].body.source, target: posts[1].body.target,
    amount: posts[1].body.amount },
    { source: 'silver', target: 'bronze', amount: 1 });
  succeed(posts[1], { bronze: 10, silver: 1, gold: 0, platinum: 0 });
  assert.equal(app.spendableStars.bronze, 10);
  app.exchangeBetween('bronze', 'gold'); // Non-adjacent exchanges are forbidden in UI.
  assert.equal(posts.length, 2);
});

test('setup button shows loading during resume and start again after failed creation', () => {
  const { app, requests, posts, scheduler } = setup();
  app.gameQuestionIssued = true;
  assert.equal(app.setupButtonLabelKey, 'game.resume');
  scheduler.run(() => {
    app.startGame();
    assert.equal(app.setupButtonLabelKey, 'game.loading');
    succeed(requests[0]);
  });
  assert.equal(app.screen, 'question');
  app.gameId = '';
  app.gameQuestionIssued = false;
  app.screen = 'setup';
  assert.equal(app.setupButtonLabelKey, 'game.start');
  scheduler.run(() => {
    app.startGame();
    assert.equal(app.setupButtonLabelKey, 'game.loading');
    posts[0].subject.error(new Error('offline'));
  });
  assert.equal(app.loading, false);
  assert.equal(app.setupButtonLabelKey, 'game.start');
});

test('reload restores a paid helper before Q1 without buying another or allocating a new run', () => {
  const previous = globalThis.localStorage;
  const key = 'ai-quiz-game:active-round:v2:user-1';
  const saved = new Map([[key, 'paid-pregame']]);
  globalThis.localStorage = {
    getItem: k => saved.get(k) ?? null,
    setItem: (k, value) => saved.set(k, value),
    removeItem: k => saved.delete(k),
  };
  const helper = {
    id: 'persisted-helper', name: 'Maria Sample', profession: 'Historian',
    erudition: 67, strong_topics: ['History'], weak_topics: ['Sports'],
    consultations_remaining: 3, consultations_total: 3, refunded: false, price: 1,
  };
  try {
    const { app, requests, posts, patches, scheduler } = setup();
    app.gameId = ''; // setup() supplies a dummy ID after initialization.
    assert.equal(requests[0].url, '/api/v2/games/paid-pregame');
    succeed(requests[0], {
      game_id: 'paid-pregame', language: 'English', phase: 'main',
      correct_main_answers: 0, main_question_index: 0,
      question_issued: false, helper,
    });
    assert.equal(app.restoringGame, false);
    assert.equal(app.gameId, 'paid-pregame');
    assert.equal(app.resumeCandidate, null);
    assert.equal(app.resumeAvailable, false);
    assert.equal(app.helperCard, helper);
    assert.equal(app.displayedHelperCard, helper);
    assert.equal(app.gameQuestionIssued, false);
    assert.equal(app.setupButtonLabelKey, 'game.start');
    assert.equal(app.canEditPregameLanguage, true);
    assert.equal(app.screen, 'setup');
    assert.equal(app.categoryInfo.can_exclude, true);
    assert.equal(saved.get(key), 'paid-pregame');
    assert.equal(posts.length, 0); // A reload NEVER repurchases the helper.

    // Language remains editable before Q1, even when a helper was already paid for.
    app.changeQuestionLanguage('Russian');
    assert.equal(patches[0].url, '/api/v2/games/paid-pregame/language');
    assert.deepEqual(patches[0].body, { language: 'Russian' });
    succeed(patches[0], {
      game_id: 'paid-pregame', language: 'Russian', phase: 'main',
      correct_main_answers: 0, question_issued: false, helper,
    });
    assert.equal(app.selectedLanguage, 'Russian');

    scheduler.run(() => {
      app.startGame();
      assert.equal(requests[1].url, '/api/v2/games/paid-pregame/question');
      succeed(requests[1], { ...question, number: 1, difficulty: 0 });
    });
    assert.equal(app.gameQuestionIssued, true);
    assert.equal(app.helperCard, helper);
    assert.equal(posts.length, 0);
    const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
    assert.match(template, /class="helper-tablet" \*ngIf="displayedHelperCard as helper"/);
  } finally {
    if (previous === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous;
  }
});

test('saved game with issued question previews paid helper before Resume', () => {
  const previous = globalThis.localStorage;
  const key = 'ai-quiz-game:active-round:v2:user-1';
  const saved = new Map([[key, 'already-started']]);
  globalThis.localStorage = {
    getItem: k => saved.get(k) ?? null,
    setItem: (k, value) => saved.set(k, value),
    removeItem: k => saved.delete(k),
  };
  const helper = {
    id: 'helper-from-db', name: 'Henri Example', profession: 'Chef',
    erudition: 42, strong_topics: ['Chemistry'], weak_topics: ['Music'],
    consultations_remaining: 2, consultations_total: 3, refunded: false, price: 1,
  };
  try {
    const { app, requests, posts, scheduler } = setup();
    app.gameId = '';
    succeed(requests[0], {
      game_id: 'already-started', language: 'French', phase: 'main',
      correct_main_answers: 3, main_question_index: 4,
      question_issued: true, helper,
    });
    assert.equal(app.gameId, '');
    assert.equal(app.resumeAvailable, true);
    assert.equal(app.helperCard, null); // Preview is not yet the active game.
    assert.equal(app.displayedHelperCard, helper);
    scheduler.run(() => {
      app.startGame();
      assert.equal(app.gameId, 'already-started');
      assert.equal(app.helperCard, helper);
      assert.equal(requests[1].url, '/api/v2/games/already-started/question');
      succeed(requests[1], { ...question, number: 5 });
    });
    assert.equal(posts.length, 0);
  } finally {
    if (previous === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous;
  }
});

test('new-round cancellation and failed POST preserve purchases; success opens clean SETUP', () => {
  const { app, posts, requests, scheduler } = setup();
  const oldHelper = {
    id: 'old-helper', name: 'Old Helper', profession: 'Engineer',
    erudition: 50, strong_topics: [], weak_topics: [],
    consultations_remaining: 2, consultations_total: 3, refunded: false, price: 1,
  };
  app.helperCard = oldHelper;
  app.helperReply = 'Old reply';
  app.helperCallKey = 'old-call';
  app.resumeAvailable = true;
  app.selectedLanguage = 'Spanish';

  app.startFreshGame();
  assert.equal(app.showAbandonConfirm, true);
  assert.equal(posts.length, 0);
  app.cancelNewGame();
  assert.equal(app.showAbandonConfirm, false);
  assert.equal(app.helperCard, oldHelper);

  scheduler.run(() => {
    app.startFreshGame();
    app.confirmNewGame();
    assert.equal(posts[0].url, '/api/v2/games/existing-game/replace');
    assert.deepEqual(posts[0].body, { language: 'Spanish' });
    assert.equal(app.helperCard, oldHelper);
    posts[0].subject.error(new Error('network error'));
  });
  assert.equal(app.helperCard, oldHelper);
  assert.equal(app.error, 'game.replaceFailed');
  assert.equal(app.gameId, 'existing-game');
  scheduler.run(() => {
    app.startFreshGame();
    app.confirmNewGame();
    succeed(posts[1], {
      game_id: 'another-game', language: 'Spanish', phase: 'main',
      correct_main_answers: 0,
    });
  });
  assert.equal(app.gameId, 'another-game');
  assert.equal(app.screen, 'setup');
  assert.equal(app.gameQuestionIssued, false);
  assert.equal(app.categoryInfo.can_exclude, true);
  assert.equal(app.helperCard, null);
  assert.equal(app.displayedHelperCard, null);
  assert.equal(app.helperReply, '');
  assert.equal(app.helperCallKey, '');
  assert.equal(requests.length, 0); // Must not issue Q1 automatically.
});

test('offers a saved round without locking language and resumes only on choice', () => {
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
      phase: 'main', correct_main_answers: 7, question_issued: true });
    assert.equal(app.restoringGame, false);
    assert.equal(app.resumeAvailable, true);
    assert.equal(app.gameId, '');
    assert.equal(app.resumeCandidate.game_id, 'saved-game-id');
    assert.equal(app.progress, 0);
    assert.equal(app.currentStars.bronze, 0);
    assert.equal(app.setupButtonLabelKey, 'game.resume');
    app.selectedLanguage = 'Spanish'; // Available for a fresh game.
    app.startGame();
    assert.equal(app.gameId, 'saved-game-id');
    assert.equal(app.selectedLanguage, 'German'); // Resume uses saved run language.
    assert.equal(app.progress, 7);
    assert.equal(app.currentStars.bronze, 1);
    assert.equal(requests[1].url, '/api/v2/games/saved-game-id/question');
    succeed(requests[1]);
  } finally {
    if (previous === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous;
  }
});

test('saved started round replacement requires warning and opens unstarted setup', () => {
  const previous = globalThis.localStorage;
  const key = 'ai-quiz-game:active-round:v2:user-1';
  const saved = new Map([[key, 'saved-game-id']]);
  globalThis.localStorage = {
    getItem: k => saved.get(k) ?? null,
    setItem: (k, value) => saved.set(k, value),
    removeItem: k => saved.delete(k),
  };
  try {
    const { app, requests, posts, scheduler } = setup();
    app.gameId = '';
    succeed(requests[0], {
      game_id: 'saved-game-id', language: 'German', phase: 'main',
      correct_main_answers: 7, main_question_index: 8, question_issued: true,
    });
    assert.equal(app.resumeAvailable, true);
    assert.equal(saved.get(key), 'saved-game-id');
    app.selectedLanguage = 'Hebrew';
    app.startFreshGame();
    assert.equal(app.showAbandonConfirm, true);
    assert.equal(posts.length, 0);
    app.cancelNewGame();
    assert.equal(app.showAbandonConfirm, false);
    assert.equal(app.resumeCandidate.game_id, 'saved-game-id');
    scheduler.run(() => {
      app.startFreshGame();
      app.confirmNewGame();
      assert.equal(posts.length, 1);
      assert.equal(posts[0].url, '/api/v2/games/saved-game-id/replace');
      assert.equal(posts[0].body.language, 'Hebrew');
      assert.equal(saved.get(key), 'saved-game-id');
      succeed(posts[0], {
        game_id: 'new-game-id', language: 'Hebrew',
        phase: 'main', correct_main_answers: 0, question_issued: false,
      });
    });
    assert.equal(app.gameId, 'new-game-id');
    assert.equal(saved.get(key), 'new-game-id');
    assert.equal(app.resumeAvailable, false);
    assert.equal(app.resumeCandidate, null);
    assert.equal(app.selectedLanguage, 'Hebrew');
    assert.equal(app.progress, 0);
    assert.equal(app.screen, 'setup');
    assert.equal(app.categoryInfo.can_exclude, true);
    assert.equal(requests.length, 1);
  } finally {
    if (previous === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous;
  }
});

test('failed confirmed new-game request keeps the saved started round resumable', () => {
  const { app, posts, scheduler } = setup();
  app.gameId = '';
  app.resumeCandidate = {
    game_id: 'saved-game-id', language: 'Russian',
    phase: 'main', correct_main_answers: 3,
  };
  app.resumeAvailable = true;
  app.selectedLanguage = 'French';
  scheduler.run(() => {
    app.startFreshGame();
    assert.equal(posts.length, 0);
    app.confirmNewGame();
    assert.equal(posts[0].url, '/api/v2/games/saved-game-id/replace');
    assert.equal(posts[0].body.language, 'French');
    posts[0].subject.error(new Error('offline'));
  });
  assert.equal(app.loading, false);
  assert.equal(app.gameId, '');
  assert.equal(app.resumeAvailable, true);
  assert.equal(app.resumeCandidate.game_id, 'saved-game-id');
});

test('closed and missing saved games do not block a new round', () => {
  for (const mode of ['lost', 'completed', 'abandoned', 'missing']) {
    const previous = globalThis.localStorage;
    const key = 'ai-quiz-game:active-round:v2:user-1';
    const saved = new Map([[key, 'saved-game-id']]);
    globalThis.localStorage = {
      getItem: k => saved.get(k) ?? null,
      setItem: (k, value) => saved.set(k, value),
      removeItem: k => saved.delete(k),
    };
    try {
      const { app, requests, posts, scheduler } = setup();
      app.gameId = '';
      if (mode === 'missing') {
        requests[0].subject.error(new HttpErrorResponse({ status: 404 }));
      } else {
        succeed(requests[0], { game_id: 'saved-game-id',
          language: 'English', phase: mode, correct_main_answers: 7 });
      }
      assert.equal(app.resumeCandidate, null);
      assert.equal(app.resumeAvailable, false);
      assert.equal(saved.has(key), false);
      app.selectedLanguage = 'Italian';
      scheduler.run(() => {
        app.startGame();
        assert.equal(posts[0].body.language, 'Italian');
        succeed(posts[0], { game_id: 'new-game-id',
          language: 'Italian', phase: 'main', correct_main_answers: 0 });
        succeed(requests[1], { ...question, number: 1 });
      });
      assert.equal(app.gameId, 'new-game-id');
    } finally {
      if (previous === undefined) delete globalThis.localStorage;
      else globalThis.localStorage = previous;
    }
  }
});

test('saved started round cannot silently open exclusions; after new SETUP it can', () => {
  const { app, posts, scheduler } = setup();
  app.gameId = '';
  app.resumeAvailable = true;
  app.resumeCandidate = {
    game_id: 'old-game-id', language: 'English',
    phase: 'main', correct_main_answers: 4, question_issued: true,
  };
  app.selectedLanguage = 'Spanish';
  app.openCategoryExclusions(); // May not skip confirmation.
  assert.equal(posts.length, 0);
  scheduler.run(() => {
    app.startFreshGame();
    app.confirmNewGame();
    assert.equal(posts[0].url, '/api/v2/games/old-game-id/replace');
    succeed(posts[0], {
      game_id: 'new-game-id', language: 'Spanish',
      phase: 'main', correct_main_answers: 0,
    });
  });
  assert.equal(app.resumeCandidate, null);
  assert.equal(app.resumeAvailable, false);
  assert.equal(app.gameId, 'new-game-id');
  assert.equal(app.screen, 'setup');
  assert.equal(app.showCategoryExclusions, false);
  app.openCategoryExclusions();
  assert.equal(app.showCategoryExclusions, true);
  assert.equal(app.categoryInfo.can_exclude, true);
  assert.equal(posts.length, 1);
});

test('template shows warning, Resume and New, while keeping pregame perks available', () => {
  const html = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(html, /\*ngIf="resumeAvailable" class="primary"[^>]*\(click\)="startGame\(\)"/);
  assert.match(html, /\*ngIf="account && \(gameId \|\| resumeCandidate\)"/);
  assert.match(html, /\(click\)="requestNewGame\(\)"/);
  assert.match(html, /\*ngIf="showAbandonConfirm"/);
  assert.match(html, /\(click\)="confirmNewGame\(\)"/);
  assert.match(html, /\(click\)="cancelNewGame\(\)"/);
  assert.match(html, /\(ngModelChange\)="changeQuestionLanguage\(\$event\)"/);
  assert.match(html, /!canEditPregameLanguage/);
  assert.match(html, /!gameQuestionIssued && \(categoryInfo === null \|\| categoryInfo.can_exclude\)/);
  assert.equal(english['game.startNew'], 'Start a new game →');
  assert.match(english['hero.resumed'], /continue it or start a new game/i);
  assert.match(english['game.abandonWarning'], /all stars you have already earned/i);
  assert.match(english['game.abandonCost'], /not be refunded/i);
});

test('recovery menu requires explicit new game before exposing paid pregame setup', () => {
  const { app, requests, posts, scheduler } = setup();
  app.screen = 'question';
  app.selectedLanguage = 'English';
  app.progress = 3;
  app.returnToMenuAfterDuplicate();
  app.duplicateExhausted = true;
  app.returnToMenuAfterDuplicate();
  assert.equal(app.resumeAvailable, true);
  assert.equal(app.gameId, 'existing-game');
  app.selectedLanguage = 'Italian';
  scheduler.run(() => {
    app.startGame(); // Resume keeps the original language.
    assert.equal(app.selectedLanguage, 'English');
    assert.equal(requests[0].url, '/api/v2/games/existing-game/question');
    succeed(requests[0]);
  });
  app.screen = 'setup';
  app.resumeAvailable = true;
  app.selectedLanguage = 'German';
  app.openCategoryExclusions(); // No silent creation or abandonment.
  assert.equal(posts.length, 0);
  scheduler.run(() => {
    app.startFreshGame();
    app.confirmNewGame();
    assert.equal(posts[0].url, '/api/v2/games/existing-game/replace');
    assert.equal(posts[0].body.language, 'German');
    succeed(posts[0], {
      game_id: 'new-game-id', language: 'German',
      phase: 'main', correct_main_answers: 0,
    });
  });
  assert.equal(app.gameId, 'new-game-id');
  assert.equal(app.screen, 'setup');
  assert.equal(app.showCategoryExclusions, false);
  app.openCategoryExclusions();
  assert.equal(app.showCategoryExclusions, true);
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
  assert.match(template, /locale\.t\(setupButtonLabelKey\)/);
  assert.match(template, /locale\.t\(feedbackButtonLabelKey\)/);
  assert.match(template, /locale\.t\('perks\.balance'\)/);
  assert.match(template, /exchangeBetween\('bronze', 'silver'\)/);
  assert.match(template, /exchangeBetween\('gold', 'silver'\)/);
  assert.doesNotMatch(template, /class="spendable-star-panel"/);
  assert.match(template, /openCategoryExclusions\(\)/);
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

test('all category labels use the shared translated UI dictionary but API identifiers stay canonical', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const categories = Object.keys(english).filter(key => key.startsWith('category.'));
  assert.equal(categories.length, 50);
  for (const key of categories) assert.equal(english[key], key.slice('category.'.length));
  assert.match(template, /locale\.t\('category\.' \+ category\)/);
  assert.match(template, /locale\.t\('category\.' \+ categoryInfo\?\.current\)/);
  assert.ok(source.includes('inputOptions: Object.fromEntries(alternatives.map(category =>'));
  assert.ok(source.includes("[category, this.locale.t('category.' + category)]"));
  assert.doesNotMatch(template, /\{\{ category \}\}/);
});


test('pregame and dialog categories sort alphabetically by displayed localized names', () => {
  const { app } = setup();
  const html = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const original = ['Ancient history', 'Physics', 'Biology'];
  app.categoryInfo = {
    allowed: original, current: null, alternatives: original,
    changes_used: 0, manual_used: 0,
    can_change: true, can_choose: true, can_exclude: true,
    minimum_allowed: 16,
  };
  assert.deepEqual(app.sortedAllowedCategories,
    ['Ancient history', 'Biology', 'Physics']);
  assert.deepEqual(original, ['Ancient history', 'Physics', 'Biology']);
  assert.match(html, /\*ngFor="let category of sortedAllowedCategories"/);
  assert.ok(source.includes('const alternatives = this.sortCategoryLabels(this.categoryInfo?.alternatives ?? [])'));

  // The canonical API slugs remain unchanged; only visible, translated text
  // determines the order. This order also updates when UI language changes.
  app.locale.load('ru');
  const translate = app.locale.t.bind(app.locale);
  const russianLabels = {
    'category.Ancient history': 'Языки',
    'category.Physics': 'Астрономия',
    'category.Biology': 'Биология',
  };
  app.locale.t = key => russianLabels[key] ?? translate(key);
  assert.deepEqual(app.sortedAllowedCategories,
    ['Physics', 'Biology', 'Ancient history']);
  assert.deepEqual(app.sortCategoryLabels(original),
    ['Physics', 'Biology', 'Ancient history']);
  assert.deepEqual(original, ['Ancient history', 'Physics', 'Biology']);
});

test('gold skip shows next-question loading, updates persisted counts and keeps progress separate', () => {
  const { app, posts, requests, scheduler } = setup();
  app.screen = 'question';
  app.current = { ...question };
  app.progress = 4;
  app.gameCorrectAnswers = 3;
  app.spendableStars.gold = 1;
  app.shopOpen = true;
  scheduler.run(() => {
    app.skipCurrentQuestion();
    assert.equal(posts[0].url, '/api/v2/games/existing-game/skip');
    assert.equal(app.current, null);
    assert.equal(app.questionLoading, true);
    assert.equal(app.generatingNextQuestion, true);
    succeed(posts[0], { game_id: 'existing-game', language: 'English',
      phase: 'main', correct_main_answers: 3, main_question_index: 5, skipped_questions: 1 });
    assert.equal(app.progress, 5);
    assert.equal(app.gameCorrectAnswers, 3);
    assert.equal(app.skippedQuestions, 1);
    assert.equal(requests[0].url, '/api/v2/games/existing-game/question');
    assert.equal(app.questionLoading, true);
    succeed(requests[0], { ...question, number: 6 });
  });
  assert.equal(app.questionLoading, false);
  assert.equal(app.generatingNextQuestion, false);
  assert.equal(app.shopOpen, false);
  assert.equal(app.screen, 'question');
});

test('failed paid skip restores question and hides transition spinner', () => {
  const { app, posts, scheduler } = setup();
  app.screen = 'question';
  app.current = { ...question };
  app.spendableStars.gold = 1;
  scheduler.run(() => {
    app.skipCurrentQuestion();
    posts[0].subject.error(new Error('offline'));
  });
  assert.equal(app.current.question_id, question.question_id);
  assert.equal(app.questionLoading, false);
  assert.equal(app.generatingNextQuestion, false);
  assert.equal(app.loading, false);
  assert.equal(app.skippedQuestions, 0);
  assert.equal(app.perksError, 'perks.actionFailed');
});

test('confirmed category change immediately hides the old question and shows loading', () => {
  const { app, posts, scheduler } = setup();
  app.screen = 'question';
  app.current = { ...question };
  app.shopOpen = true;
  app.progress = 4;
  app.spendableStars.silver = 2;
  app.categoryInfo = {
    allowed: ['Biology', 'Physics'], current: 'Biology',
    alternatives: ['Physics'], changes_used: 0, manual_used: 0,
    can_change: true, can_choose: true, can_exclude: false, minimum_allowed: 16,
  };
  app.chosenCategory = 'Physics';
  scheduler.run(() => {
    app.changeCategory('manual');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/v2/games/existing-game/categories/change');
    assert.equal(posts[0].body.question_id, question.question_id);
    assert.equal(posts[0].body.mode, 'manual');
    assert.equal(posts[0].body.category, 'Physics');
    assert.equal(app.current, null);
    assert.equal(app.shopOpen, false);
    assert.equal(app.screen, 'question');
    assert.equal(app.loading, true);
    assert.equal(app.questionLoading, true);
    assert.equal(app.generatingNextQuestion, true);
    app.changeCategory('random'); // One pending purchase at a time.
    assert.equal(posts.length, 1);
    scheduler.schedule(() => assert.equal(app.longWait, false), 14999);
    scheduler.schedule(() => assert.equal(app.longWait, true), 15001);
    scheduler.schedule(() => succeed(posts[0], {
      ...question, question_id: 'changed-question', question: 'New category question',
    }), 15100);
  });
  assert.equal(app.current.question_id, 'changed-question');
  assert.equal(app.loading, false);
  assert.equal(app.questionLoading, false);
  assert.equal(app.generatingNextQuestion, false);
  assert.equal(app.longWait, false);
  assert.equal(app.perksBusy, false);
  assert.equal(app.shopOpen, false);
  assert.equal(app.progress, 4); // Category change does not advance the quiz.
  assert.equal(app.perksError, '');
});

test('failed category change restores the old question, shop and controls', () => {
  const { app, posts, scheduler } = setup();
  app.screen = 'question';
  app.current = { ...question };
  app.shopOpen = true;
  app.spendableStars.silver = 1;
  app.categoryInfo = {
    allowed: ['Biology', 'Physics'], current: 'Biology',
    alternatives: ['Physics'], changes_used: 0, manual_used: 0,
    can_change: true, can_choose: true, can_exclude: false, minimum_allowed: 16,
  };
  scheduler.run(() => {
    app.changeCategory('random');
    assert.equal(posts[0].body.mode, 'random');
    assert.equal(posts[0].body.category, null);
    assert.equal(app.current, null);
    posts[0].subject.error(new Error('offline'));
  });
  assert.equal(app.current.question_id, question.question_id);
  assert.equal(app.shopOpen, true);
  assert.equal(app.loading, false);
  assert.equal(app.questionLoading, false);
  assert.equal(app.generatingNextQuestion, false);
  assert.equal(app.perksBusy, false);
  assert.equal(app.perksError, 'perks.categoryFailed');
  assert.equal(app.spendableStars.silver, 1);
});

test('expired session during category change clears loading for a new login', () => {
  const { app, posts, scheduler } = setup();
  app.screen = 'question';
  app.current = { ...question };
  app.spendableStars.silver = 1;
  app.categoryInfo = {
    allowed: ['Biology', 'Physics'], current: 'Biology',
    alternatives: ['Physics'], changes_used: 0, manual_used: 0,
    can_change: true, can_choose: false, can_exclude: false, minimum_allowed: 16,
  };
  scheduler.run(() => {
    app.changeCategory('random');
    posts[0].subject.error(new HttpErrorResponse({ status: 401 }));
  });
  assert.equal(app.account, null);
  assert.equal(app.screen, 'setup');
  assert.equal(app.loading, false);
  assert.equal(app.questionLoading, false);
  assert.equal(app.generatingNextQuestion, false);
  assert.equal(app.perksBusy, false);
  assert.equal(app.error, 'account.sessionExpired');
});

test('category change timeout restores the question and removes the spinner', () => {
  const { app, posts, scheduler } = setup();
  app.screen = 'question';
  app.current = { ...question };
  app.spendableStars.silver = 1;
  app.categoryInfo = {
    allowed: ['Biology', 'Physics'], current: 'Biology',
    alternatives: ['Physics'], changes_used: 0, manual_used: 0,
    can_change: true, can_choose: false, can_exclude: false, minimum_allowed: 16,
  };
  scheduler.run(() => app.changeCategory('random'));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].subject.observed, false);
  assert.equal(app.current.question_id, question.question_id);
  assert.equal(app.questionLoading, false);
  assert.equal(app.loading, false);
  assert.equal(app.perksBusy, false);
  assert.equal(app.perksError, 'perks.categoryFailed');
});

test('hidden answer preserves the original letter cell rather than compacting the grid', () => {
  const html = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const css = readFileSync(join(root, 'src/app/app.component.css'), 'utf8');
  assert.match(html, /\*ngFor="let choice of choices"/);
  assert.match(html, /\*ngIf="question\.answers\[choice\] as answerText"/);
  assert.match(html, /\{\{ choice \}\}/);
  assert.match(html, /\*ngIf="!question\.answers\[choice\]" class="hidden-answer-slot"/);
  assert.match(css, /\.answers \.hidden-answer-slot\s*\{[^}]*visibility:\s*hidden/s);
});

test('collapsed shop uses tier-colored prices and only opens category changes in a dialog', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  const style = readFileSync(join(root, 'src/app/app.component.css'), 'utf8');
  assert.ok(template.includes('class="shop-toggle"'));
  assert.ok(template.includes('(click)="shopOpen = !shopOpen"'));
  assert.ok(template.includes('*ngIf="shopOpen"'));
  assert.ok(template.includes('openCategoryChangeDialog()'));
  assert.ok(!template.includes('(click)="changeCategory('));
  for (const tier of ['bronze', 'silver', 'gold']) {
    assert.ok(template.includes('class="tier-price ' + tier + '"'));
    assert.ok(style.includes('.tier-price.' + tier));
  }
  for (const key of ['stats.roundCorrect', 'stats.roundSkipped', 'hero.nextQuestion',
    'perks.changeCategory', 'perks.back']) assert.ok(english[key], key);
  assert.ok(template.includes('class="question-generation"'));
  assert.ok(template.includes("locale.format('stats.roundSkipped'"));
  assert.ok(source.includes('if (selected.isDenied) continue'));
});

test('helper setup shows collapsed hiring, two price choices and person silhouette', () => {
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /\(click\)="showHelperHire\(\)"/);
  assert.match(template, /\*ngIf="helperHireOpen"/);
  assert.match(template, /\(click\)="selectRandomHelper\(\)"/);
  assert.match(template, /\(click\)="showChooseHelper\(\)"/);
  assert.match(template, /\*ngIf="helperChooseProfession"/);
  assert.match(template, /class="helper-avatar"/);
  assert.doesNotMatch(template, /☎/);
  assert.doesNotMatch(template, /helper\.shopDescription/);
  assert.match(template, /\*ngIf="!helperReply"/);
  assert.equal(english['helper.call'], 'Ask for help');
});

test('helper card uses localizable templates and does not translate the literal name', () => {
  const { app } = setup();
  const helper = {
    name: 'Мария Смирнова', profession: 'Doctor', gender: 'female',
    strong_topics: ['Biology'], weak_topics: ['Mythology'],
    tagline_id: 1, tagline_high: 'Biology', tagline_low: 'Mythology',
  };
  assert.equal(app.helperProfessionLabel(helper), 'Doctor');
  assert.match(app.helperTagline(helper), /Biology/);
  assert.match(app.helperTagline(helper), /Mythology/);
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /\{\{ helper\.name \}\}/);
  assert.match(template, /helperTagline\(helper\)/);
  assert.doesNotMatch(template, /helper\.tagline \}\}/);
});

test('pre-question hiring state is Start, actual issued Q1 state is Resume', () => {
  const { app } = setup();
  app.helperCard = {
    name: 'Test Person', profession: 'Historian',
    strong_topics: [], weak_topics: [], consultations_remaining: 3,
  };
  app.gameQuestionIssued = false;
  assert.equal(app.setupButtonLabelKey, 'game.start');
  app.gameQuestionIssued = true;
  assert.equal(app.setupButtonLabelKey, 'game.resume');
});

test('successful helper reply hides request button and retry label is context-aware', () => {
  const { app } = setup();
  app.helperCallKey = '';
  app.helperError = '';
  assert.equal(app.helperCallButtonKey, 'helper.call');
  app.helperCallKey = 'previous-request';
  assert.equal(app.helperCallButtonKey, 'helper.recoverCall');
  app.helperError = 'helper.callFailed';
  assert.equal(app.helperCallButtonKey, 'helper.repeatCall');
  const template = readFileSync(join(root, 'src/app/app.component.html'), 'utf8');
  assert.match(template, /\*ngIf="!helperReply"/);
});


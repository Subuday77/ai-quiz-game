import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, firstValueFrom, Subscription, timeout, TimeoutError, timer } from 'rxjs';
import { LocalizationStore, UI_LANGUAGES } from './localization/store';
import { RESET_COPY, RESET_LANGUAGE_NAMES } from './localization/reset-copy';

type Choice = 'A' | 'B' | 'C' | 'D';
type Screen = 'setup' | 'question' | 'feedback' | 'done';
type StarTier = 'bronze' | 'silver' | 'gold' | 'platinum';
type ConvertibleTier = Exclude<StarTier, 'platinum'>;
type StarCounts = Record<StarTier, number>;
type AuthMode = 'login' | 'register';

const GAME_API = '/api/v2/games';
const STARS_API = '/api/v2/stars';
const ACTIVE_GAME_KEY = 'ai-quiz-game:active-round:v2:';
const USERNAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{2,29}$/;
const emptyStars = (): StarCounts => ({ bronze: 0, silver: 0, gold: 0, platinum: 0 });

interface Account {
  id: string;
  username: string;
  display_name: string | null;
  preferred_language: string;
  ui_language: string;
  theme_key: string;
}
interface ProfileStats {
  games_played: number;
  total_score: number;
  best_round_score: number;
  wallet_balance: number;
  star_awards: Record<string, number>;
  achievements: string[];
  answered_questions: number;
  correct_answers: number;
}
interface GameInfo {
  game_id: string;
  language: string;
  phase: string;
  correct_main_answers: number;
}
interface PublicQuestion {
  question_id: string;
  number: number;
  difficulty: number;
  question: string;
  answers: Partial<Record<Choice, string>>;
  bonus: boolean;
  hidden_count: number;
}
interface AnswerFeedback {
  correct: boolean;
  correct_answer: Choice;
  phase: string;
  correct_main_answers: number;
  achievement: string | null;
}

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './app.component.html',
  styleUrls: ['./app.component.css'],
})
export class AppComponent implements OnInit, OnDestroy {
  readonly languages = ['English', 'Russian', 'Hebrew', 'German', 'French', 'Spanish', 'Italian'];
  readonly uiLanguages = UI_LANGUAGES;
  readonly resetCopy = RESET_COPY;
  readonly locale: LocalizationStore;
  // Status text is intentionally shown in multiple native languages while
  // translation runs; it does not depend on a dictionary being available.
  readonly localizationStatus = [
    { text: 'Localizing the website…', rtl: false },
    { text: 'Die Website wird übersetzt…', rtl: false },
    { text: 'Traduction du site en cours…', rtl: false },
    { text: 'Traduciendo el sitio web…', rtl: false },
    { text: 'Traduzione del sito in corso…', rtl: false },
    { text: 'A traduzir o site…', rtl: false },
    { text: 'Идёт перевод сайта…', rtl: false },
    { text: 'האתר עובר תרגום…', rtl: true },
    { text: 'جارٍ ترجمة الموقع…', rtl: true },
    { text: 'サイトを翻訳しています…', rtl: false },
    { text: '正在翻译网站…', rtl: false },
  ];
  readonly choices: Choice[] = ['A', 'B', 'C', 'D'];
  readonly starTiers: { key: StarTier; label: string }[] = [
    { key: 'bronze', label: 'Bronze' },
    { key: 'silver', label: 'Silver' },
    { key: 'gold', label: 'Gold' },
    { key: 'platinum', label: 'Platinum' },
  ];

  account: Account | null = null;
  authLoading = true;
  authBusy = false;
  resetEnabled = false;
  resetBusy = false;
  resetError = '';
  restoringGame = false;
  finishedPhase: 'lost' | 'completed' | null = null;
  statsLoading = false;
  statsError = '';
  gamesPlayed = 0;
  correctAnswers = 0;

  selectedLanguage = 'English';
  screen: Screen = 'setup';
  gameId = '';
  resumeAvailable = false;
  current: PublicQuestion | null = null;
  feedback: AnswerFeedback | null = null;
  loading = false;
  error = '';
  configLoading = true;
  questionLoading = false;
  longWait = false;
  canRetryQuestion = false;
  duplicateExhausted = false;
  private warningMs = 8000;
  private requestTimeoutMs = 45000;
  private readonly subscriptions = new Subscription();
  private statsVersion = 0;
  private restoreVersion = 0;

  progress = 0;
  stars: string[] = [];
  currentStars: StarCounts = emptyStars();
  totalStars: StarCounts = emptyStars();
  spendableStars: StarCounts = emptyStars();
  gameCorrectAnswers = 0;
  perksBusy = false;
  perksError = '';
  readonly exchangeTiers: ConvertibleTier[] = ['bronze', 'silver', 'gold'];
  exchangeSource: ConvertibleTier = 'bronze';
  exchangeTarget: ConvertibleTier = 'silver';
  exchangeAmount = 10;

  constructor(private readonly http: HttpClient) {
    this.locale = new LocalizationStore(http);
  }

  // A game ID is allocated before the first question arrives. While that
  // request is pending, the setup button must not claim the game is resumable.
  get setupButtonLabelKey(): string {
    if (!this.account) return 'game.loginToPlay';
    if (this.loading) return 'game.loading';
    return this.gameId ? 'game.resume' : 'game.start';
  }

  ngOnInit(): void {
    this.locale.load('en');
    this.subscriptions.add(this.http.get<{
      long_wait_warning_ms: number; request_timeout_ms: number;
    }>('/api/client-config').pipe(
      timeout(5000), finalize(() => { this.configLoading = false; }),
    ).subscribe({
      next: (settings) => {
        if (Number.isFinite(settings.long_wait_warning_ms) &&
            Number.isFinite(settings.request_timeout_ms) &&
            settings.long_wait_warning_ms > 0 &&
            settings.request_timeout_ms > settings.long_wait_warning_ms) {
          this.warningMs = settings.long_wait_warning_ms;
          this.requestTimeoutMs = settings.request_timeout_ms;
        }
      },
      error: () => {},
    }));

    // The server owns the opaque session cookie. Never store credentials or
    // an authentication token in localStorage/sessionStorage.
    this.subscriptions.add(this.http.get<Account>('/api/auth/me').pipe(
      timeout(10000), finalize(() => { this.authLoading = false; }),
    ).subscribe({
      next: (account) => this.signedIn(account),
      error: () => { this.account = null; },
    }));
  }

  ngOnDestroy(): void {
    this.subscriptions.unsubscribe();
    this.locale.destroy();
  }

  get rtl(): boolean { return this.locale.rtl; }
  get questionRtl(): boolean { return this.selectedLanguage === 'Hebrew'; }
  get starNames(): string {
    return this.stars.map(key => this.locale.t('stars.' + key)).join(' · ');
  }

  setUiLanguage(code: string): void {
    if (!this.uiLanguages.some(item => item.code === code) || this.locale.loading) return;
    const accountId = this.account?.id;
    // Persist the preference only AFTER the complete translation is ready.
    this.locale.load(code, (successful) => {
      if (!successful) {
        this.error = 'localization.loadFailed';
        return;
      }
      this.error = '';
      if (!accountId || this.account?.id !== accountId || this.account.ui_language === code) return;
      this.subscriptions.add(this.http.patch<{ language_code: string }>(
        '/api/localization/preference', { language_code: code },
      ).pipe(timeout(10000)).subscribe({
        next: (result) => {
          if (this.account?.id === accountId && this.locale.code === result.language_code) {
            this.account.ui_language = result.language_code;
          }
        },
        error: () => { this.error = 'account.languageSaveFailed'; },
      }));
    });
  }

  // The reset button exists only when the development-only backend explicitly
  // allows it. Production defaults to disabled, including API-level checks.
  private discoverResetCapability(accountId: string): void {
    this.resetEnabled = false;
    this.subscriptions.add(this.http.get<{ enabled: boolean }>(
      '/api/localization/reset-capability',
    ).pipe(timeout(5000)).subscribe({
      next: (result) => {
        if (this.account?.id === accountId) this.resetEnabled = result.enabled === true;
      },
      error: () => { this.resetEnabled = false; },
    }));
  }

  async resetTranslations(): Promise<void> {
    if (!this.account || !this.resetEnabled || this.resetBusy || this.locale.loading) return;
    this.resetBusy = true;
    try {
      const { default: Swal } = await import('sweetalert2');
      this.resetError = '';
      const inputOptions: Record<string, string> = { all: RESET_COPY.all };
      for (const language of this.uiLanguages) {
        if (language.code !== 'en') {
          inputOptions[language.code] = RESET_LANGUAGE_NAMES[language.code];
        }
      }
      const decision = await Swal.fire({
        titleText: RESET_COPY.title,
        text: RESET_COPY.explain,
        input: 'select',
        inputOptions,
        inputValue: this.locale.code === 'en' ? 'all' : this.locale.code,
        showCancelButton: true,
        confirmButtonText: RESET_COPY.confirm,
        cancelButtonText: RESET_COPY.cancel,
        confirmButtonColor: '#a14a60',
        background: '#1b1e3e',
        color: '#f1f3ff',
        focusCancel: true,
      });
      if (!decision.isConfirmed || typeof decision.value !== 'string') return;
      const selected = decision.value;
      const result = await firstValueFrom(this.http.post<{ language_code: string; removed_count: number }>(
        '/api/localization/reset', { language_code: selected },
      ).pipe(timeout(10000)));
      await Swal.fire({
        icon: 'success',
        titleText: RESET_COPY.done,
        text: RESET_COPY.result(result.removed_count),
        background: '#1b1e3e',
        color: '#f1f3ff',
        confirmButtonColor: '#8170d8',
      });
      // A reset invalidates both PostgreSQL and any in-memory copies.
      // Otherwise switching back to a previously loaded language could
      // mistakenly show its deleted translation without requesting fresh data.
      this.locale.invalidate(selected);
      if (this.locale.code !== 'en' &&
          (selected === 'all' || selected === this.locale.code)) {
        this.locale.load(this.locale.code, (successful) => {
          if (!successful) this.error = 'localization.loadFailed';
        }, true);
      }
    } catch {
      this.resetError = RESET_COPY.failed;
    } finally {
      this.resetBusy = false;
    }
  }

  get correctAnswerText(): string {
    return this.feedback && this.current
      ? this.current.answers[this.feedback.correct_answer]
      : '';
  }
  get gameEnded(): boolean {
    return this.feedback?.phase === 'completed' || this.feedback?.phase === 'lost';
  }

  get feedbackButtonLabelKey(): string {
    if (this.gameEnded) return 'game.seeResults';
    return this.loading ? 'game.loading' : 'game.nextQuestion';
  }

  // SweetAlert2 is lazy-loaded only when the user opens a sign-in form.
  async openAuthDialog(mode: AuthMode): Promise<void> {
    if (this.authBusy || this.loading || this.authLoading) return;
    this.authBusy = true;
    this.error = '';
    try {
      const { default: Swal } = await import('sweetalert2');
      const isRegister = mode === 'register';
      const result = await Swal.fire({
        titleText: this.locale.t(isRegister ? 'auth.titleRegister' : 'auth.titleLogin'),
        // Labels are populated via textContent, never by interpolating AI text into HTML.
        html:
          '<div class="quiz-auth-fields">' +
            '<label id="quiz-label-username" for="quiz-auth-username"></label>' +
            '<input id="quiz-auth-username" class="swal2-input" type="text" autocomplete="username" maxlength="30">' +
            '<label id="quiz-label-password" for="quiz-auth-password"></label>' +
            '<input id="quiz-auth-password" class="swal2-input" type="password" ' +
              'autocomplete="' + (isRegister ? 'new-password' : 'current-password') + '" maxlength="256">' +
            (isRegister ?
              '<label id="quiz-label-confirm" for="quiz-auth-confirm"></label>' +
              '<input id="quiz-auth-confirm" class="swal2-input" type="password" ' +
                'autocomplete="new-password" maxlength="256">'
              : '') +
          '</div>',
        didOpen: () => {
          const popup = Swal.getPopup();
          const label = (id: string, key: string) => {
            const element = popup?.querySelector<HTMLElement>('#' + id);
            if (element) element.textContent = this.locale.t(key);
          };
          const hint = (id: string, key: string) => {
            const element = popup?.querySelector<HTMLInputElement>('#' + id);
            if (element) element.placeholder = this.locale.t(key);
          };
          label('quiz-label-username', 'auth.username');
          label('quiz-label-password', 'auth.password');
          hint('quiz-auth-username', 'auth.usernamePlaceholder');
          if (isRegister) {
            label('quiz-label-confirm', 'auth.confirm');
            hint('quiz-auth-password', 'auth.passwordHint');
            hint('quiz-auth-confirm', 'auth.repeatPlaceholder');
          }
        },
        customClass: {
          popup: 'quiz-auth-popup',
          confirmButton: 'quiz-auth-confirm',
        },
        background: '#1b1e3e',
        color: '#f1f3ff',
        confirmButtonText: this.locale.t(isRegister ? 'auth.submitRegister' : 'auth.submitLogin'),
        confirmButtonColor: '#8170d8',
        showCancelButton: true,
        cancelButtonText: this.locale.t('auth.cancel'),
        focusConfirm: false,
        showLoaderOnConfirm: true,
        allowOutsideClick: () => !Swal.isLoading(),
        allowEscapeKey: () => !Swal.isLoading(),
        preConfirm: async () => {
          const popup = Swal.getPopup();
          const username = (popup?.querySelector<HTMLInputElement>('#quiz-auth-username')?.value ?? '').trim();
          const password = popup?.querySelector<HTMLInputElement>('#quiz-auth-password')?.value ?? '';
          const confirmation = popup?.querySelector<HTMLInputElement>('#quiz-auth-confirm')?.value ?? '';

          if (!USERNAME_PATTERN.test(username)) {
            Swal.showValidationMessage(this.locale.t('auth.usernameInvalid'));
            return false;
          }
          if (password.length < 8 || password.length > 256) {
            Swal.showValidationMessage(this.locale.t('auth.passwordInvalid'));
            return false;
          }
          if (isRegister && confirmation !== password) {
            Swal.showValidationMessage(this.locale.t('auth.passwordMismatch'));
            return false;
          }
          try {
            return await firstValueFrom(this.http.post<Account>(
              isRegister ? '/api/auth/register' : '/api/auth/login',
              { username, password },
            ).pipe(timeout(15000)));
          } catch (error: unknown) {
            Swal.showValidationMessage(this.locale.t(this.authErrorMessage(error, mode)));
            return false;
          }
        },
      });
      if (result.isConfirmed && result.value) {
        this.signedIn(result.value as Account);
      }
    } catch {
      this.error = 'auth.modalFailed';
    } finally {
      this.authBusy = false;
    }
  }

  logout(): void {
    if (!this.account || this.loading || this.authBusy) return;
    this.authBusy = true;
    this.error = '';
    this.subscriptions.add(this.http.post<void>('/api/auth/logout', {}).pipe(
      timeout(10000), finalize(() => { this.authBusy = false; }),
    ).subscribe({
      next: () => this.clearAccount(),
      error: (err: unknown) => {
        if (err instanceof HttpErrorResponse && err.status === 401) {
          this.clearAccount();
        } else {
          this.error = 'account.logoutFailed';
        }
      },
    }));
  }

  startGame(): void {
    if (this.loading || this.configLoading || this.authLoading || this.authBusy || this.restoringGame) return;
    if (!this.account) {
      void this.openAuthDialog('login');
      return;
    }
    if (this.gameId) {
      this.resumeAvailable = false;
      this.loadQuestion();
      return;
    }
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API, { language: this.selectedLanguage },
    ).pipe(timeout(this.requestTimeoutMs)).subscribe({
      next: (game) => {
        this.gameId = game.game_id;
        this.rememberActiveGame(game.game_id);
        this.stars = [];
        this.currentStars = emptyStars();
        this.progress = 0;
        this.gameCorrectAnswers = 0;
        this.feedback = null;
        this.resumeAvailable = false;
        this.loading = false;
        this.loadQuestion();
      },
      error: (err: unknown) => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.fail('game.startFailed');
      },
    }));
  }

  loadQuestion(): void {
    if (!this.gameId || this.loading || !this.account) return;
    this.loading = true;
    this.questionLoading = true;
    this.longWait = false;
    this.canRetryQuestion = false;
    this.duplicateExhausted = false;
    this.error = '';
    const warning = timer(this.warningMs).subscribe(() => { this.longWait = true; });
    this.subscriptions.add(this.http.get<PublicQuestion>(
      GAME_API + '/' + this.gameId + '/question',
    ).pipe(
      timeout(this.requestTimeoutMs),
      finalize(() => {
        warning.unsubscribe();
        this.loading = false;
        this.questionLoading = false;
        this.longWait = false;
      }),
    ).subscribe({
      next: (question) => {
        this.current = question;
        this.progress = Math.max(this.progress, Math.min(15, question.number - 1));
        this.feedback = null;
        this.screen = 'question';
        this.resumeAvailable = false;
      },
      error: (error: unknown) => {
        if (this.isUnauthorized(error)) {
          this.sessionExpired();
          return;
        }
        this.canRetryQuestion = true;
        if (error instanceof HttpErrorResponse && error.status === 409 &&
            error.error?.detail?.code === 'duplicate_exhausted') {
          // No new question was offered. Keep the server game ID, progress,
          // and saved resume token intact; hide prior round feedback.
          this.duplicateExhausted = true;
          this.current = null;
          this.feedback = null;
          this.screen = 'question';
          this.fail('game.duplicateExhausted');
        } else if (error instanceof TimeoutError) {
          this.fail('game.questionTimeout');
        } else if (error instanceof HttpErrorResponse && error.status === 502) {
          this.fail('game.questionInvalid');
        } else if (error instanceof HttpErrorResponse && error.status === 503) {
          this.fail('game.questionUnavailable');
        } else {
          this.fail('game.questionFailed');
        }
      },
    }));
  }

  retryQuestion(): void {
    if (this.canRetryQuestion && !this.loading) this.loadQuestion();
  }

  returnToMenuAfterDuplicate(): void {
    if (!this.duplicateExhausted || this.loading) return;
    // This is navigation, not game abandonment. Resume continues the same
    // round without losing progress or opening a new game in PostgreSQL.
    this.screen = 'setup';
    this.resumeAvailable = !!this.gameId;
    this.current = null;
    this.feedback = null;
    this.canRetryQuestion = false;
    this.duplicateExhausted = false;
    this.error = '';
  }

  answer(choice: Choice): void {
    if (this.loading || !this.current || this.screen !== 'question' || !this.account) return;
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<AnswerFeedback>(
      GAME_API + '/' + this.gameId + '/answer', {
        question_id: this.current.question_id,
        answer: choice,
      },
    ).pipe(timeout(this.requestTimeoutMs)).subscribe({
      next: (result) => {
        this.feedback = result;
        this.finishedPhase = result.phase === 'lost' || result.phase === 'completed' ? result.phase : null;
        this.progress = Math.min(15, this.current?.number ?? result.correct_main_answers);
        this.gameCorrectAnswers = result.correct_main_answers;
        if (result.achievement && this.isStarTier(result.achievement)) {
          this.stars.push(result.achievement);
          this.currentStars[result.achievement] += 1;
        }
        if (result.phase === 'completed' || result.phase === 'lost') {
          this.forgetActiveGame();
        }
        this.refreshStats(); // All-time rewards always come from PostgreSQL.
        this.screen = 'feedback';
        this.loading = false;
      },
      error: (err: unknown) => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else if (err instanceof HttpErrorResponse && err.status === 409) {
          this.recoverGameAfterConflict();
        } else {
          this.fail('game.answerFailed');
        }
      },
    }));
  }

  continue(): void {
    // Error recovery owns navigation; never silently issue a second request
    // while the Retry action is visible.
    if (this.loading || this.canRetryQuestion) return;
    if (this.gameEnded) {
      this.screen = 'done';
    } else {
      this.loadQuestion();
    }
  }

  restart(): void {
    if (this.loading) return;
    this.forgetActiveGame();
    this.resetGameView();
  }

  private signedIn(account: Account): void {
    if (this.account?.id !== account.id) this.resetGameView();
    this.account = account;
    this.authLoading = false;
    this.locale.load(account.ui_language || 'en');
    this.discoverResetCapability(account.id);
    this.totalStars = emptyStars();
    if (this.languages.includes(account.preferred_language) && !this.gameId) {
      this.selectedLanguage = account.preferred_language;
    }
    this.refreshStats();
    this.refreshSpendableStars();
    this.restoreActiveGame();
  }

  private clearAccount(): void {
    this.statsVersion++;
    this.resetEnabled = false;
    this.restoreVersion++;
    this.restoringGame = false;
    this.account = null;
    this.totalStars = emptyStars();
    this.spendableStars = emptyStars();
    this.gamesPlayed = 0;
    this.correctAnswers = 0;
    this.statsLoading = false;
    this.statsError = '';
    this.resetGameView();
  }

  private sessionExpired(): void {
    this.clearAccount();
    this.error = 'account.sessionExpired';
    this.loading = false;
  }

  private refreshSpendableStars(): void {
    if (!this.account) return;
    const accountId = this.account.id;
    this.subscriptions.add(this.http.get<StarCounts>(STARS_API).pipe(
      timeout(10000),
    ).subscribe({
      next: (values) => {
        if (this.account?.id === accountId) this.spendableStars = values;
      },
      error: () => { this.perksError = 'perks.balanceUnavailable'; },
    }));
  }

  removeOptions(count: 1 | 2): void {
    const current = this.current;
    if (!this.account || !current || current.bonus || this.screen !== 'question' ||
        this.perksBusy || this.loading) return;
    this.perksBusy = true;
    this.perksError = '';
    this.subscriptions.add(this.http.post<PublicQuestion>(
      GAME_API + '/' + this.gameId + '/hints/remove',
      { question_id: current.question_id, count, request_id: crypto.randomUUID() },
    ).pipe(timeout(this.requestTimeoutMs), finalize(() => { this.perksBusy = false; }))
     .subscribe({
      next: (question) => {
        if (this.current?.question_id === current.question_id) this.current = question;
        this.refreshSpendableStars();
      },
      error: (err: unknown) => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.perksError = 'perks.actionFailed';
      },
    }));
  }

  skipCurrentQuestion(): void {
    const current = this.current;
    if (!this.account || !current || current.bonus || this.screen !== 'question' ||
        this.perksBusy || this.loading) return;
    this.perksBusy = true;
    this.loading = true;
    this.perksError = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API + '/' + this.gameId + '/skip',
      { question_id: current.question_id, request_id: crypto.randomUUID() },
    ).pipe(timeout(this.requestTimeoutMs),
           finalize(() => { this.perksBusy = false; this.loading = false; }))
     .subscribe({
      next: (game) => {
        this.progress = Math.min(15, current.number);
        this.gameCorrectAnswers = game.correct_main_answers;
        this.current = null;
        this.refreshSpendableStars();
        this.refreshStats();
        if (game.phase === 'completed') {
          this.finishedPhase = 'completed';
          this.forgetActiveGame();
          this.screen = 'done';
        } else {
          // Finalize loading before asking for the next main slot.
          this.loading = false;
          this.loadQuestion();
        }
      },
      error: (err: unknown) => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else if (err instanceof HttpErrorResponse && err.status === 409) {
          this.perksError = 'perks.actionFailed';
        } else {
          this.perksError = 'perks.actionFailed';
        }
      },
    }));
  }

  exchangeStars(): void {
    const amount = Number(this.exchangeAmount);
    if (!this.account || this.perksBusy || this.exchangeSource === this.exchangeTarget ||
        !Number.isSafeInteger(amount) || amount <= 0) return;
    this.perksBusy = true;
    this.perksError = '';
    this.subscriptions.add(this.http.post<StarCounts>(STARS_API + '/exchange', {
      source: this.exchangeSource, target: this.exchangeTarget,
      amount, request_id: crypto.randomUUID(),
    }).pipe(timeout(15000), finalize(() => { this.perksBusy = false; }))
     .subscribe({
      next: (balances) => { this.spendableStars = balances; },
      error: (err: unknown) => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.perksError = 'perks.exchangeFailed';
      },
    }));
  }

  private refreshStats(): void {
    this.refreshSpendableStars();
    const accountId = this.account?.id;
    if (!accountId) return;
    const version = ++this.statsVersion;
    this.statsLoading = true;
    this.statsError = '';
    this.subscriptions.add(this.http.get<ProfileStats>('/api/profile/me/stats').pipe(
      timeout(10000), finalize(() => {
        if (version === this.statsVersion) this.statsLoading = false;
      }),
    ).subscribe({
      next: (stats) => {
        if (this.account?.id !== accountId || version !== this.statsVersion) return;
        const updated = emptyStars();
        for (const tier of this.starTiers) {
          const value = stats.star_awards[tier.key];
          if (Number.isSafeInteger(value) && value >= 0) updated[tier.key] = value;
        }
        this.totalStars = updated;
        this.gamesPlayed = stats.games_played;
        this.correctAnswers = stats.correct_answers;
      },
      error: (err: unknown) => {
        if (this.account?.id !== accountId || version !== this.statsVersion) return;
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.statsError = 'stats.unavailable';
      },
    }));
  }

  private restoreActiveGame(): void {
    const accountId = this.account?.id;
    if (!accountId) return;
    const savedId = this.readActiveGame(accountId);
    if (!savedId) return;
    const version = ++this.restoreVersion;
    this.restoringGame = true;
    this.subscriptions.add(this.http.get<GameInfo>(GAME_API + '/' + savedId).pipe(
      timeout(10000),
      finalize(() => { if (this.restoreVersion === version) this.restoringGame = false; }),
    ).subscribe({
      next: (run) => {
        if (this.account?.id !== accountId || this.restoreVersion !== version) return;
        if (run.phase !== 'main' && run.phase !== 'bonus') {
          this.removeSavedGame(accountId);
          return;
        }
        this.gameId = run.game_id;
        this.resumeAvailable = true;
        this.selectedLanguage = run.language;
        this.progress = run.correct_main_answers;
        this.gameCorrectAnswers = run.correct_main_answers;
        this.current = null;
        this.feedback = null;
        this.screen = 'setup';
        this.currentStars = {
          bronze: run.correct_main_answers >= 5 ? 1 : 0,
          silver: run.correct_main_answers >= 10 ? 1 : 0,
          gold: run.correct_main_answers >= 15 ? 1 : 0,
          platinum: 0,
        };
        this.stars = this.starTiers.filter((tier) => this.currentStars[tier.key] > 0)
          .map((tier) => tier.key);
      },
      error: (err: unknown) => {
        if (this.account?.id !== accountId || this.restoreVersion !== version) return;
        if (err instanceof HttpErrorResponse && err.status === 404) {
          this.removeSavedGame(accountId);
        } else if (this.isUnauthorized(err)) {
          this.sessionExpired();
        } else {
          this.error = 'game.restoreFailed';
        }
      },
    }));
  }

  private recoverGameAfterConflict(): void {
    const id = this.gameId;
    if (!id) { this.fail('game.noRound'); return; }
    this.subscriptions.add(this.http.get<GameInfo>(GAME_API + '/' + id).pipe(
      timeout(10000),
    ).subscribe({
      next: (run) => {
        this.loading = false;
        this.canRetryQuestion = false;
        this.progress = run.correct_main_answers;
        if (run.phase === 'completed' || run.phase === 'lost') {
          this.forgetActiveGame();
          this.feedback = null;
          this.finishedPhase = run.phase === 'lost' ? 'lost' : 'completed';
          this.current = null;
          this.screen = 'done';
          this.refreshStats();
        } else {
          this.current = null;
          this.feedback = null;
          this.finishedPhase = null;
          this.screen = 'setup';
          this.resumeAvailable = true;
        }
        this.error = 'game.answerAlreadyRecorded';
      },
      error: () => this.fail('game.recoveryFailed'),
    }));
  }

  private resetGameView(): void {
    this.canRetryQuestion = false;
    this.duplicateExhausted = false;
    this.longWait = false;
    this.gameId = '';
    this.resumeAvailable = false;
    this.current = null;
    this.feedback = null;
    this.finishedPhase = null;
    this.error = '';
    this.progress = 0;
    this.gameCorrectAnswers = 0;
    this.spendableStars = emptyStars();
    this.stars = [];
    this.currentStars = emptyStars();
    this.screen = 'setup';
  }

  private rememberActiveGame(id: string): void {
    const userId = this.account?.id;
    if (!userId) return;
    try { localStorage.setItem(ACTIVE_GAME_KEY + userId, id); } catch { /* private mode */ }
  }
  private readActiveGame(userId: string): string | null {
    try { return localStorage.getItem(ACTIVE_GAME_KEY + userId); }
    catch { return null; }
  }
  private removeSavedGame(userId: string): void {
    try { localStorage.removeItem(ACTIVE_GAME_KEY + userId); } catch { /* private mode */ }
  }
  private forgetActiveGame(): void {
    if (this.account) this.removeSavedGame(this.account.id);
  }

  private isStarTier(value: string): value is StarTier {
    return this.starTiers.some((tier) => tier.key === value);
  }

  private isUnauthorized(err: unknown): boolean {
    return err instanceof HttpErrorResponse && err.status === 401;
  }

  private authErrorMessage(error: unknown, mode: AuthMode): string {
    if (error instanceof HttpErrorResponse) {
      if (error.status === 401) return 'auth.invalidCredentials';
      if (error.status === 409 && mode === 'register') return 'auth.usernameTaken';
      if (error.status === 429) return 'auth.rateLimited';
      if (error.status === 422) return 'auth.fieldsInvalid';
      if (error.status === 403) return 'auth.originDenied';
    }
    return 'auth.networkFailed';
  }

  private fail(message: string): void {
    this.error = message;
    this.loading = false;
  }
}

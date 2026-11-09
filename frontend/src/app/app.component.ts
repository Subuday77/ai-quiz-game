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
const PENDING_RESULTS_KEY = 'ai-quiz-game:unviewed-results:v1:';
const HELPER_CALL_KEY = 'ai-quiz-game:helper-call:v1:';
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
  round_stars: StarCounts;
  main_question_index?: number;
  skipped_questions?: number;
  question_issued?: boolean;
  helper?: HelperCard | null;
  replacement_game_id?: string | null;
  bonus_decision_available?: boolean;
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
interface CategoryInfo {
  allowed: string[];
  current: string | null;
  alternatives: string[];
  changes_used: number;
  manual_used: number;
  can_change: boolean;
  can_choose: boolean;
  can_exclude: boolean;
  minimum_allowed: number;
}
interface HelperCard {
  id: string;
  name: string;
  profession: string;
  erudition: number;
  strong_topics: string[];
  weak_topics: string[];
  tagline: string;
  tagline_id?: number;
  tagline_high?: string;
  tagline_low?: string;
  gender?: string;
  nationality?: string;
  consultations_remaining: number;
  consultations_total: number;
  refunded: boolean;
  price: number;
}
interface HelperReply { text: string; helper: HelperCard; }

interface AppealQuote {
  before_answer: boolean;
  deposit_tier: StarTier;
  deposit_amount: number;
  filing_fee_bronze: number;
  free_appeals_remaining: number;
  question_snapshot: {
    question_id: string; question: string;
    answers: Record<Choice, string>; hidden_keys: Choice[];
    number: number; difficulty: number; language: string;
  };
}
interface AppealRecord {
  id: string; question_id: string; question_number: number; language: string;
  status: string; submitted_at: string; closed_at: string | null;
  question_snapshot: AppealQuote['question_snapshot'];
  reasons: string[]; comment: string; source_links: string[];
  verdict: string | null; deposit: { tier: StarTier; amount: number; refunded: boolean };
  filing_fee_bronze: number; compensation: { tier: StarTier; amount: number };
  bonus_wager_refunded: boolean;
  expenses_refunded: { tier: StarTier; amount: number; reason: string }[];
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

  helperCard: HelperCard | null = null;
  showAbandonConfirm = false;
  // A saved but not yet resumed round may own a paid helper. Show its card
  // without activating that round or allowing consultations before Resume.
  get displayedHelperCard(): HelperCard | null {
    return this.helperCard ?? (this.resumeAvailable ? this.resumeCandidate?.helper ?? null : null);
  }
  helperHireOpen = false;
  helperChooseProfession = false;
  gameQuestionIssued = false;
  helperProfessions: string[] = [];
  selectedHelperProfession = '';
  helperBusy = false;
  helperProfessionsLoading = false;
  helperDetailsVisible = false;
  helperReply = '';
  helperError = '';
  helperCallKey = '';
  private helperCallQuestionId = '';
  bonusDecisionAvailable = false;


  appealBusy = false;
  appealMessage = '';
  appealedQuestionIds = new Set<string>();
  readonly appealReasons = [
    'multiple_correct', 'no_correct', 'incorrect_key', 'ambiguous', 'answer_leak'
  ];

  account: Account | null = null;
  authLoading = true;
  authBusy = false;
  resetEnabled = false;
  resetBusy = false;
  resetError = '';
  restoringGame = false;
  finishedPhase: 'lost' | 'completed' | null = null;
  pendingResults: GameInfo | null = null;
  statsLoading = false;
  statsError = '';
  gamesPlayed = 0;
  correctAnswers = 0;

  selectedLanguage = 'English';
  private gameLanguage = '';
  languageSaving = false;
  screen: Screen = 'setup';
  gameId = '';
  resumeAvailable = false;
  // A saved round is only an offer, not the selected game. Keeping it
  // separate lets the player choose a new question language and new game.
  resumeCandidate: GameInfo | null = null;
  private resumeRunLanguage = '';
  current: PublicQuestion | null = null;
  feedback: AnswerFeedback | null = null;
  loading = false;
  error = '';
  configLoading = true;
  questionLoading = false;
  longWait = false;
  canRetryQuestion = false;
  duplicateExhausted = false;
  private warningMs = 15000;
  private requestTimeoutMs = 45000;
  private lateQuestionTimeoutMs = 140000;
  private readonly subscriptions = new Subscription();
  private statsVersion = 0;
  private restoreVersion = 0;

  progress = 0;
  stars: string[] = [];
  currentStars: StarCounts = emptyStars();
  totalStars: StarCounts = emptyStars();
  spendableStars: StarCounts = emptyStars();
  gameCorrectAnswers = 0;
  skippedQuestions = 0;
  shopOpen = false;
  generatingNextQuestion = false;
  perksBusy = false;
  perksError = '';
  showCategoryExclusions = false;
  categoryInfo: CategoryInfo | null = null;
  categoriesToExclude: string[] = [];
  chosenCategory = '';
  categoryLoading = false;

  constructor(private readonly http: HttpClient) {
    this.locale = new LocalizationStore(http);
  }

  // A game ID is allocated before the first question arrives. While that
  // request is pending, the setup button must not claim the game is resumable.
  get currentQuestionNumber(): number {
    // progress tracks answered/skipped slots, not the number on screen.
    // While Q1 is being generated progress is 0, but the current slot is 1.
    // On the results screen retain the final question's number.
    if (this.current) return this.current.number;
    return this.screen === 'done'
      ? Math.max(1, this.progress)
      : Math.min(15, this.progress + 1);
  }

  get currentQuestionTotal(): number {
    return this.current?.bonus ? 16 : 15;
  }

  get canEditPregameLanguage(): boolean {
    // An already-issued question fixes the saved round's language. While the
    // user decides Resume vs New, show that actual language read-only; after
    // confirming New, the fresh unstarted run allows changes again.
    return this.screen === 'setup' && !this.resumeAvailable &&
      !this.resumeCandidate && (!this.gameId || !this.gameQuestionIssued);
  }

  // An excluded-category payment already allocated a game. Update its
  // language in PostgreSQL, not just the dropdown, before Q1 is offered.
  changeQuestionLanguage(language: string): void {
    if (!this.languages.includes(language)) return;
    // Defense in depth: programmatic changes must not make the saved game
    // appear to have another language than its persisted Question rows.
    if (this.resumeAvailable || this.resumeCandidate) {
      this.selectedLanguage = this.resumeCandidate?.language || this.gameLanguage;
      return;
    }
    if (!this.gameId) {
      this.selectedLanguage = language;
      return;
    }
    if (!this.canEditPregameLanguage || !this.account || this.loading ||
        this.languageSaving || this.perksBusy || this.categoryLoading) {
      this.selectedLanguage = this.gameLanguage;
      return;
    }
    if (language === this.gameLanguage) {
      this.selectedLanguage = language;
      return;
    }
    const gameId = this.gameId;
    const accountId = this.account.id;
    this.languageSaving = true;
    this.selectedLanguage = language;
    this.error = '';
    this.subscriptions.add(this.http.patch<GameInfo>(
      GAME_API + '/' + gameId + '/language', { language },
    ).pipe(timeout(10000), finalize(() => {
      if (this.gameId === gameId) this.languageSaving = false;
    })).subscribe({
      next: (game) => {
        if (this.gameId !== gameId || this.account?.id !== accountId) return;
        this.gameLanguage = game.language;
        this.selectedLanguage = game.language;
      },
      error: (err: unknown) => {
        if (this.gameId !== gameId || this.account?.id !== accountId) return;
        if (this.isUnauthorized(err)) this.sessionExpired();
        else {
          this.selectedLanguage = this.gameLanguage;
          this.error = 'game.languageUpdateFailed';
        }
      },
    }));
  }

  get setupButtonLabelKey(): string {
    if (!this.account) return 'game.loginToPlay';
    if (this.loading) return 'game.loading';
    // Buying a helper (or excluding categories) allocates a game ID before Q1.
    // The first question, not the database row, defines the start of play.
    if (this.resumeCandidate) {
      return this.resumeCandidate.question_issued ? 'game.resume' : 'game.start';
    }
    return this.gameQuestionIssued ? 'game.resume' : 'game.start';
  }

  helperProfessionLabel(helper: HelperCard): string {
    return this.locale.t('helper.profession.' + helper.profession);
  }

  helperTagline(helper: HelperCard): string {
    const high = helper.tagline_high || helper.strong_topics[0] || '';
    const low = helper.tagline_low || helper.weak_topics[0] || '';
    const id = Math.min(4, Math.max(1, helper.tagline_id || 1));
    const kind = low ? 'balanced' : 'strong';
    return this.locale.format('helper.phrase.' + id + '.' + kind, {
      strong: this.locale.t('helper.topic.' + high),
      weak: this.locale.t('helper.topic.' + low),
    });
  }

  get helperCallButtonKey(): string {
    if (this.helperError) return 'helper.repeatCall';
    return this.helperCallKey ? 'helper.recoverCall' : 'helper.call';
  }

  showHelperHire(): void {
    this.helperHireOpen = !this.helperHireOpen;
    this.helperChooseProfession = false;
    this.selectedHelperProfession = '';
    this.helperError = '';
  }

  selectRandomHelper(): void {
    this.helperChooseProfession = false;
    this.selectedHelperProfession = '';
    void this.buyHelper();
  }

  showChooseHelper(): void {
    this.helperChooseProfession = true;
    this.selectedHelperProfession = '';
    this.loadHelperProfessions();
  }

  ngOnInit(): void {
    this.locale.load('en');
    this.subscriptions.add(this.http.get<{
      long_wait_warning_ms: number; request_timeout_ms: number;
      late_question_timeout_ms?: number;
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
          if (Number.isFinite(settings.late_question_timeout_ms) &&
              (settings.late_question_timeout_ms ?? 0) >= settings.request_timeout_ms) {
            this.lateQuestionTimeoutMs = settings.late_question_timeout_ms!;
          }
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
      ? (this.current.answers[this.feedback.correct_answer] ?? '')
      : '';
  }
  get gameEnded(): boolean {
    return this.feedback?.phase === 'completed' || this.feedback?.phase === 'lost';
  }

  get feedbackButtonLabelKey(): string {
    if (this.gameEnded) return 'game.seeResults';
    return this.loading ? 'game.loading' : 'game.nextQuestion';
  }


  private escapeAppealHtml(value: unknown): string {
    return String(value ?? '').replace(/[&<>"']/g, char =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as Record<string, string>)[char]);
  }

  async openAppealForm(): Promise<void> {
    if (!this.account || !this.gameId || !this.current || this.appealBusy ||
        this.loading || this.perksBusy ||
        !(this.screen === 'question' ||
          (this.screen === 'feedback' && this.feedback?.phase === 'lost' && !this.feedback.correct)) ||
        this.appealedQuestionIds.has(this.current.question_id)) return;
    this.appealBusy = true;
    const question = this.current;
    const runId = this.gameId;
    const accountId = this.account.id;
    try {
      const { default: Swal } = await import('sweetalert2');
      const quote = await firstValueFrom(this.http.get<AppealQuote>(
        GAME_API + '/' + runId + '/appeals/' + question.question_id + '/quote'
      ).pipe(timeout(12000)));
      if (this.gameId !== runId || this.account?.id !== accountId) return;
      const safe = (value: unknown) => this.escapeAppealHtml(value);
      const snapshot = quote.question_snapshot;
      const options = this.choices.map(key =>
        '<li><strong>' + key + '.</strong> ' + safe(snapshot.answers[key]) + '</li>'
      ).join('');
      const reasonInputs = this.appealReasons.filter(r =>
        r !== 'incorrect_key' || !quote.before_answer
      ).map(r => '<label class="appeal-choice"><input type="checkbox" name="appeal-reason" value="' +
        safe(r) + '"> ' + safe(this.locale.t('appeals.reason.' + r)) + '</label>').join('');
      const feeWarning = quote.filing_fee_bronze > 0
        ? '<p class="appeal-warning">' + safe(this.locale.t('appeals.feeNeverReturned')) +
          '</p><label class="appeal-choice"><input type="checkbox" id="appeal-confirm-fee"> ' +
          safe(this.locale.t('appeals.confirmFee')) + '</label>'
        : '';
      const wagerNotice = question.number === 16
        ? '<p>' + safe(this.locale.t('appeals.bonusSeparate')) + '</p>' : '';
      const hintNotice = question.hidden_count
        ? '<p>' + safe(this.locale.t('appeals.hintsLater')) + '</p>' : '';
      const result = await Swal.fire({
        titleText: this.locale.t('appeals.title'), width: '850px', showCancelButton: true,
        confirmButtonText: this.locale.t('appeals.submit'),
        cancelButtonText: this.locale.t('helper.close'),
        html: '<div class="appeal-form"><p><strong>' + safe(snapshot.question) +
          '</strong></p><ol class="appeal-options">' + options +
          '</ol><div class="appeal-reasons">' + reasonInputs +
          '</div><label for="appeal-comment">' + safe(this.locale.t('appeals.comment')) +
          '</label><textarea id="appeal-comment" maxlength="1000" rows="4"></textarea>' +
          '<label for="appeal-sources">' + safe(this.locale.t('appeals.sources')) +
          '</label><textarea id="appeal-sources" rows="2"></textarea>' +
          '<p>' + safe(this.locale.t('appeals.deposit')) + ': 1 ' +
          safe(this.locale.t('stars.' + quote.deposit_tier)) + '</p>' +
          '<p>' + safe(this.locale.t('appeals.fee')) + ': ' +
          quote.filing_fee_bronze + ' ' + safe(this.locale.t('stars.bronze')) +
          '</p>' + wagerNotice + hintNotice + feeWarning + '</div>',
        preConfirm: () => {
          const popup = Swal.getPopup();
          if (!popup) return false;
          const reasons = Array.from(popup.querySelectorAll<HTMLInputElement>('input[name="appeal-reason"]:checked'))
            .map(input => input.value);
          const comment = popup.querySelector<HTMLTextAreaElement>('#appeal-comment')?.value.trim() || '';
          const links = (popup.querySelector<HTMLTextAreaElement>('#appeal-sources')?.value || '')
            .split(/\r?\n/).map(v => v.trim()).filter(Boolean);
          const confirmFee = !!popup.querySelector<HTMLInputElement>('#appeal-confirm-fee')?.checked;
          if ((!reasons.length && !comment) || links.length > 5 || links.some(l =>
            l.length > 500 || !/^https?:\/\//i.test(l)) ||
            (quote.filing_fee_bronze > 0 && !confirmFee)) {
            Swal.showValidationMessage(this.locale.t('appeals.invalid'));
            return false;
          }
          return { reasons, comment, source_links: links, confirm_nonrefundable_fee: confirmFee,
            expected_fee_bronze: quote.filing_fee_bronze };
        }
      });
      if (!result.isConfirmed || !result.value) return;
      if (this.gameId !== runId || this.account?.id !== accountId) return;
      const storageKey = 'ai-quiz-game:appeal-request:' + accountId + ':' +
        runId + ':' + question.question_id;
      let requestId = '';
      try { requestId = sessionStorage.getItem(storageKey) || ''; } catch { /* private mode */ }
      if (!requestId) {
        requestId = crypto.randomUUID();
        try { sessionStorage.setItem(storageKey, requestId); } catch { /* private mode */ }
      }
      await firstValueFrom(this.http.post<AppealRecord>(
        GAME_API + '/' + runId + '/appeals',
        { ...result.value, question_id: question.question_id, request_id: requestId }
      ).pipe(timeout(15000)));
      try { sessionStorage.removeItem(storageKey); } catch { /* private mode */ }
      this.appealedQuestionIds.add(question.question_id);
      this.appealMessage = this.locale.t('appeals.received');
      this.refreshSpendableStars();
      if (quote.before_answer && this.gameId === runId) {
        this.current = null;
        this.feedback = null;
        this.generatingNextQuestion = true;
        this.loadQuestion();
      }
    } catch (error) {
      if (this.isUnauthorized(error)) this.sessionExpired();
      else this.appealMessage = this.locale.t('appeals.failed');
    } finally {
      this.appealBusy = false;
    }
  }

  async openAppealHistory(): Promise<void> {
    if (!this.account || this.appealBusy) return;
    this.appealBusy = true;
    const accountId = this.account.id;
    try {
      const { default: Swal } = await import('sweetalert2');
      const records = await firstValueFrom(this.http.get<AppealRecord[]>(
        '/api/v2/appeals').pipe(timeout(15000)));
      if (this.account?.id !== accountId) return;
      const safe = (value: unknown) => this.escapeAppealHtml(value);
      const html = records.length ? records.map(item => {
        const answers = this.choices.map(key =>
          '<li><strong>' + key + '</strong>: ' +
          safe(item.question_snapshot.answers[key]) + '</li>').join('');
        const costs = item.expenses_refunded.map(v =>
          safe(v.amount + ' ' + this.locale.t('stars.' + v.tier) + ' (' + v.reason + ')')).join(', ');
        return '<details class="appeal-history-item"><summary>' +
          safe(new Date(item.submitted_at).toLocaleString()) + ' · Q' +
          item.question_number + ' · ' + safe(this.locale.t('appeals.status.' + item.status)) +
          '</summary><p><strong>' + safe(item.question_snapshot.question) +
          '</strong></p><ol>' + answers + '</ol><p>' +
          safe(item.reasons.map(r => this.locale.t('appeals.reason.' + r)).join(', ')) +
          '</p><p>' + safe(item.comment) + '</p><p>' +
          safe(item.verdict || this.locale.t('appeals.notDecided')) + '</p><p>' +
          safe(this.locale.t('appeals.closed')) + ': ' +
          safe(item.closed_at ? new Date(item.closed_at).toLocaleDateString() : '—') +
          '</p><p>' + safe(this.locale.t('appeals.deposit')) + ': ' +
          safe(item.deposit.amount + ' ' + this.locale.t('stars.' + item.deposit.tier)) +
          (item.deposit.refunded ? ' (' + safe(this.locale.t('appeals.refunded')) + ')' : '') +
          '</p><p>' + safe(this.locale.t('appeals.fee')) + ': ' + item.filing_fee_bronze +
          ' ' + safe(this.locale.t('stars.bronze')) +
          '</p><p>' + safe(this.locale.t('appeals.compensation')) + ': ' +
          item.compensation.amount + ' ' + safe(this.locale.t('stars.' + item.compensation.tier)) +
          '</p><p>' + safe(this.locale.t('appeals.wagerRefunded')) + ': ' +
          safe(item.bonus_wager_refunded ? this.locale.t('appeals.yes') : this.locale.t('appeals.no')) +
          '</p><p>' + safe(this.locale.t('appeals.hintRefunds')) + ': ' +
          (costs || '0') + '</p></details>';
      }).join('') : '<p>' + safe(this.locale.t('appeals.none')) + '</p>';
      await Swal.fire({ titleText: this.locale.t('appeals.myComplaints'),
        width: '850px', html: '<div class="appeal-history">' + html + '</div>',
        confirmButtonText: this.locale.t('helper.close') });
    } catch (error) {
      if (this.isUnauthorized(error)) this.sessionExpired();
      else this.appealMessage = this.locale.t('appeals.historyFailed');
    } finally {
      this.appealBusy = false;
    }
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
    if (this.loading || this.perksBusy || this.helperBusy || this.configLoading || this.authLoading ||
        this.authBusy || this.restoringGame || this.categoryLoading ||
        this.languageSaving || this.categoriesToExclude.length > 0 ||
        this.error === 'game.restoreFailed') return;
    if (!this.account) {
      void this.openAuthDialog('login');
      return;
    }
    if (this.resumeCandidate) {
      const run = this.resumeCandidate;
      this.resumeCandidate = null;
      this.gameId = run.game_id;
      this.gameQuestionIssued = run.question_issued ?? (run.main_question_index ?? 0) > 0;
      this.helperCard = run.helper ?? null;
      this.bonusDecisionAvailable = run.bonus_decision_available === true;
      this.gameLanguage = run.language;
      this.selectedLanguage = run.language;
      this.progress = run.main_question_index ?? run.correct_main_answers;
      this.gameCorrectAnswers = run.correct_main_answers;
      this.skippedQuestions = run.skipped_questions ?? 0;
      this.currentStars = {
        bronze: run.correct_main_answers >= 5 ? 1 : 0,
        silver: run.correct_main_answers >= 10 ? 1 : 0,
        gold: run.correct_main_answers >= 15 ? 1 : 0,
        platinum: 0,
      };
      this.stars = this.starTiers.filter(tier => this.currentStars[tier.key] > 0)
        .map(tier => tier.key);
      if (this.bonusDecisionAvailable) {
        this.screen = 'setup';
        this.resumeAvailable = false;
        return;
      }
      this.generatingNextQuestion = this.progress > 0;
      this.loadQuestion();
      return;
    }
    if (this.gameId) {
      // A recovered round might be shown alongside an editable new-game
      // language selector. Resume always restores the original run language.
      if (this.resumeAvailable && this.resumeRunLanguage)
        this.selectedLanguage = this.resumeRunLanguage;
      this.resumeRunLanguage = '';
      this.showCategoryExclusions = false;
      this.resumeAvailable = false;
      if (this.bonusDecisionAvailable) {
        this.screen = 'setup';
        return;
      }
      this.generatingNextQuestion = this.progress > 0;
      this.loadQuestion();
      return;
    }
    this.createGame(false, true);
  }

  startFreshGame(): void {
    this.requestNewGame();
  }

  requestNewGame(): void {
    if (!this.account || !(this.gameId || this.resumeCandidate) ||
        this.loading || this.perksBusy || this.helperBusy || this.configLoading ||
        this.authLoading || this.authBusy || this.restoringGame ||
        this.categoryLoading || this.languageSaving || this.showAbandonConfirm) return;
    this.showAbandonConfirm = true;
  }

  cancelNewGame(): void {
    if (!this.loading) this.showAbandonConfirm = false;
  }

  confirmNewGame(): void {
    if (!this.showAbandonConfirm || !this.account || this.loading ||
        this.perksBusy || this.helperBusy || this.restoringGame) return;
    const oldId = this.gameId || this.resumeCandidate?.game_id;
    if (!oldId) return;
    const accountId = this.account.id;
    const language = this.selectedLanguage;
    this.showAbandonConfirm = false;
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API + '/' + oldId + '/replace', { language },
    ).pipe(timeout(this.requestTimeoutMs), finalize(() => { this.loading = false; }))
      .subscribe({
        next: (game) => {
          if (this.account?.id !== accountId) return;
          this.resetGameView();
          this.gameId = game.game_id;
          this.gameLanguage = game.language;
          this.selectedLanguage = game.language;
          this.rememberActiveGame(game.game_id);
          this.loadCategoryInfo();
          this.refreshSpendableStars();
        },
        error: (err: unknown) => {
          if (this.account?.id !== accountId) return;
          if (this.isUnauthorized(err)) this.sessionExpired();
          else this.error = 'game.replaceFailed';
        },
      }));
  }

  openCategoryExclusions(): void {
    if (this.loading || this.perksBusy || this.configLoading || this.authLoading ||
        this.authBusy || this.restoringGame || this.categoryLoading ||
        this.languageSaving || this.error === 'game.restoreFailed') return;
    if (!this.account) {
      void this.openAuthDialog('login');
      return;
    }
    if (this.resumeAvailable || this.resumeCandidate || this.gameQuestionIssued) return;
    if (!this.gameId) {
      // Allocate an empty run; the player can still hire a helper afterwards.
      this.createGame(true);
      return;
    }
    if (this.categoryInfo && !this.categoryInfo.can_exclude) return;
    this.showCategoryExclusions = !this.showCategoryExclusions;
    if (this.showCategoryExclusions && !this.categoryInfo) this.loadCategoryInfo();
  }

  private createGame(forExclusions: boolean, beginQuestions = false): void {
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API, { language: this.selectedLanguage },
    ).pipe(timeout(this.requestTimeoutMs)).subscribe({
      next: (game) => {
        this.gameId = game.game_id;
        this.clearPendingResults();
        this.gameQuestionIssued = false;
        this.gameLanguage = game.language;
        this.rememberActiveGame(game.game_id);
        this.resumeCandidate = null;
        this.resumeRunLanguage = '';
        // Starting a different game must never carry a helper purchased for
        // the previous run. Only clear it after POST /games succeeds.
        this.helperCard = null;
        this.helperReply = '';
        this.helperError = '';
        this.helperCallKey = '';
        this.helperCallQuestionId = '';
        this.helperDetailsVisible = false;
        this.helperHireOpen = false;
        this.helperChooseProfession = false;
        this.bonusDecisionAvailable = false;
        this.categoryInfo = null;
        this.categoriesToExclude = [];
        this.chosenCategory = '';
        this.stars = [];
        this.currentStars = emptyStars();
        this.progress = 0;
        this.gameCorrectAnswers = 0;
        this.skippedQuestions = 0;
        this.generatingNextQuestion = false;
        this.shopOpen = false;
        this.feedback = null;
        this.resumeAvailable = false;
        this.loading = false;
        this.screen = 'setup';
        this.showCategoryExclusions = forExclusions;
        if (forExclusions || !beginQuestions) this.loadCategoryInfo();
        if (beginQuestions) this.loadQuestion();
      },
      error: (err: unknown) => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else if (err instanceof HttpErrorResponse && err.status === 409) {
          // Another browser may have created an active game meanwhile.
          // Ask the server for that game instead of hiding it behind an error.
          this.loading = false;
          this.restoreActiveGame();
        } else this.fail('game.startFailed');
      },
    }));
  }

  loadQuestion(): void {
    if (!this.gameId || this.loading || !this.account) return;
    // An allocated run is already in progress, even if its very first AI
    // question fails. Never leave the pregame controls visible on GET failure.
    // Also discard stale feedback / answer buttons when loading a new slot.
    this.screen = 'question';
    this.current = null;
    this.feedback = null;
    this.showCategoryExclusions = false;
    this.resumeAvailable = false;
    this.loading = true;
    this.questionLoading = true;
    this.shopOpen = false;
    this.longWait = false;
    this.canRetryQuestion = false;
    this.duplicateExhausted = false;
    this.error = '';
    const warning = timer(this.warningMs).subscribe(() => { this.longWait = true; });
    // Q10–Q16 can try a different vetted category on generation failure.
    const questionTimeoutMs = this.progress >= 9
      ? Math.max(this.lateQuestionTimeoutMs, this.requestTimeoutMs)
      : this.requestTimeoutMs;
    this.subscriptions.add(this.http.get<PublicQuestion>(
      GAME_API + '/' + this.gameId + '/question',
    ).pipe(
      timeout(questionTimeoutMs),
      finalize(() => {
        warning.unsubscribe();
        this.loading = false;
        this.questionLoading = false;
        this.generatingNextQuestion = false;
        this.longWait = false;
      }),
    ).subscribe({
      next: (question) => {
        this.current = question;
        this.gameQuestionIssued = true;
        this.helperReply = '';
        this.helperError = '';
        this.helperCallKey = this.readHelperCallKey(question.question_id);
        this.helperCallQuestionId = question.question_id;
        this.progress = Math.max(this.progress, Math.min(15, question.number - 1));
        this.feedback = null;
        this.screen = 'question';
        this.resumeAvailable = false;
        this.loadCategoryInfo();
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
    if (this.canRetryQuestion && !this.loading) {
      this.generatingNextQuestion = this.progress > 0;
      this.loadQuestion();
    }
  }

  returnToMenuAfterDuplicate(): void {
    if (!this.duplicateExhausted || this.loading) return;
    // This is navigation, not game abandonment. Resume continues the same
    // round without losing progress or opening a new game in PostgreSQL.
    this.screen = 'setup';
    this.resumeAvailable = !!this.gameId;
    this.resumeRunLanguage = this.selectedLanguage;
    this.current = null;
    this.feedback = null;
    this.canRetryQuestion = false;
    this.duplicateExhausted = false;
    this.error = '';
  }

  answer(choice: Choice): void {
    if (this.loading || this.perksBusy || this.appealBusy || !this.current || this.screen !== 'question' || !this.account) return;
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
        this.bonusDecisionAvailable = result.phase === 'bonus';
        if (this.current?.bonus && !result.correct) {
          this.currentStars.gold = Math.max(0, this.currentStars.gold - 1);
          this.stars = this.stars.filter(tier => tier !== 'gold');
        }
        this.finishedPhase = result.phase === 'lost' || result.phase === 'completed' ? result.phase : null;
        this.progress = Math.min(15, this.current?.number ?? result.correct_main_answers);
        this.gameCorrectAnswers = result.correct_main_answers;
        if (result.achievement && this.isStarTier(result.achievement)) {
          this.stars.push(result.achievement);
          this.currentStars[result.achievement] += 1;
        }
        if (result.phase === 'completed' || result.phase === 'lost') {
          // Persist before the user clicks See Results: F5 must not lose access
          // to an already awarded, terminal round.
          this.rememberPendingResults(this.gameId);
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
    if (this.loading || this.canRetryQuestion || this.bonusDecisionAvailable) return;
    if (this.gameEnded) {
      this.clearPendingResults();
      this.screen = 'done';
    } else {
      this.generatingNextQuestion = true;
      this.loadQuestion();
    }
  }

  restart(): void {
    if (this.loading || !this.account || !this.gameId || !this.finishedPhase) return;
    const terminalRun = this.gameId;
    const accountId = this.account.id;
    const language = this.selectedLanguage;
    this.loading = true;
    this.error = '';
    // Play Again is an explicit request for a FRESH empty round. The server
    // makes it idempotent and never discards another run's paid perks silently.
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API + '/' + terminalRun + '/replay', { language },
    ).pipe(timeout(this.requestTimeoutMs), finalize(() => {
      if (this.account?.id === accountId) this.loading = false;
    })).subscribe({
      next: game => {
        if (this.account?.id !== accountId) return;
        this.resetGameView();
        this.gameId = game.game_id;
        this.gameLanguage = game.language;
        this.selectedLanguage = game.language;
        this.rememberActiveGame(game.game_id);
        this.loadCategoryInfo();
        this.refreshSpendableStars();
      },
      error: (err: unknown) => {
        if (this.account?.id !== accountId) return;
        if (this.isUnauthorized(err)) {
          this.sessionExpired();
        } else if (err instanceof HttpErrorResponse && err.status === 409 &&
                   err.error?.detail?.code === 'saved_paid_round') {
          // A forgotten round still owns paid perks. Do not forfeit them:
          // show the existing Resume or confirmed New Game choice.
          this.resetGameView();
          this.restoreActiveGame();
        } else {
          this.error = 'game.startFailed';
        }
      },
    }));
  }

  loadHelperProfessions(): void {
    if (!this.account || this.helperProfessionsLoading || this.helperProfessions.length) return;
    this.helperProfessionsLoading = true;
    this.subscriptions.add(this.http.get<{ professions: string[] }>(
      GAME_API + '/helper/professions',
    ).pipe(timeout(10000), finalize(() => { this.helperProfessionsLoading = false; })).subscribe({
      next: data => { this.helperProfessions = data.professions; },
      error: () => { this.helperProfessions = []; },
    }));
  }

  async buyHelper(): Promise<void> {
    if (!this.account || this.helperCard || this.helperBusy || this.loading ||
        this.restoringGame || this.resumeAvailable || this.resumeCandidate ||
        this.error === 'game.restoreFailed' ||
        this.screen !== 'setup' || this.progress !== 0) return;
    this.helperBusy = true;
    this.helperError = '';
    try {
      if (!this.gameId) {
        const game = await firstValueFrom(this.http.post<GameInfo>(
          GAME_API, { language: this.selectedLanguage },
        ).pipe(timeout(this.requestTimeoutMs)));
        this.gameId = game.game_id;
        this.gameQuestionIssued = false;
        this.gameLanguage = game.language;
        this.rememberActiveGame(game.game_id);
        this.resumeCandidate = null;
        this.categoryInfo = null;
      }
      this.helperCard = await firstValueFrom(this.http.post<HelperCard>(
        GAME_API + '/' + this.gameId + '/helper/purchase',
        { profession: this.selectedHelperProfession || null, ui_language: this.locale.code },
      ).pipe(timeout(12000)));
      this.helperHireOpen = false;
      this.helperChooseProfession = false;
      // The purchase pre-allocates a run. Category exclusion must remain
      // available immediately, without requiring a page reload.
      this.loadCategoryInfo();
      this.refreshStats();
    } catch (error) {
      if (this.isUnauthorized(error)) this.sessionExpired();
      else if (!this.gameId && error instanceof HttpErrorResponse && error.status === 409)
        this.restoreActiveGame();
      else this.helperError = 'helper.purchaseFailed';
    } finally {
      this.helperBusy = false;
    }
  }

  private helperCallStorageKey(questionId: string): string {
    return HELPER_CALL_KEY + this.account?.id + ':' + this.gameId + ':' + questionId;
  }

  private readHelperCallKey(questionId: string): string {
    try { return localStorage.getItem(this.helperCallStorageKey(questionId)) || ''; }
    catch { return ''; }
  }

  consultHelper(): void {
    if (!this.account || !this.gameId || !this.current || this.current.bonus ||
        this.screen !== 'question' || !this.helperCard || this.helperBusy || this.loading ||
        (this.helperCard.consultations_remaining <= 0 && !this.helperCallKey) ||
        !!this.helperReply) return;
    if (this.helperCallQuestionId !== this.current.question_id || !this.helperCallKey) {
      this.helperCallKey = crypto.randomUUID();
      this.helperCallQuestionId = this.current.question_id;
      try {
        localStorage.setItem(this.helperCallStorageKey(this.current.question_id), this.helperCallKey);
      } catch { /* private mode */ }
    }
    this.helperBusy = true;
    this.helperError = '';
    const gameId = this.gameId;
    const questionId = this.current.question_id;
    this.subscriptions.add(this.http.post<HelperReply>(
      GAME_API + '/' + gameId + '/helper/call',
      { question_id: questionId, request_key: this.helperCallKey,
        ui_language: this.locale.code },
    ).pipe(timeout(22000), finalize(() => { this.helperBusy = false; })).subscribe({
      next: result => {
        if (this.gameId !== gameId || this.current?.question_id !== questionId) return;
        this.helperReply = result.text;
        this.helperCard = result.helper;
      },
      error: () => {
        if (this.gameId === gameId && this.current?.question_id === questionId)
          this.helperError = 'helper.callFailed';
      },
    }));
  }

  acceptBonus(): void {
    if (!this.bonusDecisionAvailable || this.loading || !this.account || !this.gameId) return;
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API + '/' + this.gameId + '/bonus/accept', {},
    ).pipe(timeout(12000)).subscribe({
      next: () => {
        this.loading = false;
        this.bonusDecisionAvailable = false;
        this.refreshSpendableStars();
        this.generatingNextQuestion = true;
        this.loadQuestion();
      },
      error: (err: unknown) => {
        this.loading = false;
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.error = 'helper.bonusDecisionFailed';
      },
    }));
  }

  declineBonus(): void {
    if (!this.bonusDecisionAvailable || this.loading || !this.account || !this.gameId) return;
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API + '/' + this.gameId + '/bonus/decline', {},
    ).pipe(timeout(12000)).subscribe({
      next: () => {
        this.loading = false;
        this.bonusDecisionAvailable = false;
        this.finishedPhase = 'completed';
        this.current = null;
        this.feedback = null;
        this.screen = 'done';
        this.forgetActiveGame();
        this.refreshStats();
      },
      error: (err: unknown) => {
        this.loading = false;
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.error = 'helper.bonusDecisionFailed';
      },
    }));
  }

  private signedIn(account: Account): void {
    if (this.account?.id !== account.id) {
      this.resetGameView();
      this.spendableStars = emptyStars();
    }
    this.account = account;
    this.authLoading = false;
    this.locale.load(account.ui_language || 'en');
    this.discoverResetCapability(account.id);
    this.totalStars = emptyStars();
    if (this.languages.includes(account.preferred_language) && !this.gameId) {
      this.selectedLanguage = account.preferred_language;
    }
    this.refreshStats();
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

  get exclusionCost(): number { return this.categoriesToExclude.length * 5; }

  // Sort what the player actually sees, not the canonical English API values.
  // Return a copy: the backend order and category identifiers stay unchanged.
  sortCategoryLabels(categories: readonly string[]): string[] {
    const collator = new Intl.Collator(this.locale.code, { sensitivity: 'base' });
    return [...categories].sort((a, b) =>
      collator.compare(this.locale.t('category.' + a), this.locale.t('category.' + b)) ||
      a.localeCompare(b, 'en'));
  }

  get sortedAllowedCategories(): string[] {
    return this.sortCategoryLabels(this.categoryInfo?.allowed ?? []);
  }

  toggleExcludedCategory(category: string, checked: boolean): void {
    if (this.perksBusy || !this.categoryInfo?.can_exclude) return;
    if (checked && !this.categoriesToExclude.includes(category))
      this.categoriesToExclude = [...this.categoriesToExclude, category];
    else if (!checked)
      this.categoriesToExclude = this.categoriesToExclude.filter(value => value !== category);
  }

  private loadCategoryInfo(): void {
    if (!this.account || !this.gameId) return;
    const gameId = this.gameId;
    this.categoryLoading = true;
    this.subscriptions.add(this.http.get<CategoryInfo>(
      GAME_API + '/' + gameId + '/categories',
    ).pipe(timeout(10000), finalize(() => { this.categoryLoading = false; }))
      .subscribe({
        next: (info) => {
          if (this.gameId !== gameId) return;
          this.categoryInfo = info;
          if (!info.alternatives.includes(this.chosenCategory))
            this.chosenCategory = this.sortCategoryLabels(info.alternatives)[0] ?? '';
          this.categoriesToExclude = this.categoriesToExclude.filter(
            value => info.allowed.includes(value),
          );
        },
        error: (err: unknown) => {
          if (this.isUnauthorized(err)) this.sessionExpired();
          else this.perksError = 'perks.categoriesUnavailable';
        },
      }));
  }

  async excludeCategories(): Promise<void> {
    const info = this.categoryInfo;
    if (!info?.can_exclude || !this.account || !this.gameId ||
        !this.categoriesToExclude.length || this.perksBusy || this.loading ||
        this.exclusionCost > this.spendableStars.silver ||
        info.allowed.length - this.categoriesToExclude.length < 16) return;
    const { default: Swal } = await import('sweetalert2');
    const decision = await Swal.fire({
      titleText: this.locale.t('perks.excludeTitle'),
      text: this.locale.format('perks.excludeConfirm', {
        count: this.categoriesToExclude.length, price: this.exclusionCost,
      }),
      showCancelButton: true,
      confirmButtonText: this.locale.t('perks.confirm'),
      cancelButtonText: this.locale.t('auth.cancel'),
      background: '#1b1e3e', color: '#f1f3ff',
      confirmButtonColor: '#8170d8',
    });
    if (!decision.isConfirmed || this.perksBusy || !this.gameId) return;
    this.perksBusy = true;
    this.perksError = '';
    const gameId = this.gameId;
    this.subscriptions.add(this.http.post<CategoryInfo>(
      GAME_API + '/' + gameId + '/categories/exclude',
      { categories: [...this.categoriesToExclude], request_id: crypto.randomUUID() },
    ).pipe(timeout(15000), finalize(() => { this.perksBusy = false; }))
      .subscribe({
        next: (state) => {
          if (this.gameId !== gameId) return;
          this.categoryInfo = state;
          this.categoriesToExclude = [];
          this.showCategoryExclusions = false;
          this.refreshSpendableStars();
        },
        error: (err: unknown) => {
          if (this.isUnauthorized(err)) this.sessionExpired();
          else this.perksError = 'perks.actionFailed';
        },
      }));
  }

  async openCategoryChangeDialog(): Promise<void> {
    const state = this.categoryInfo;
    if (!state?.can_change || !this.current || this.current.bonus || this.screen !== 'question' ||
        this.loading || this.perksBusy || this.spendableStars.silver < 1) return;
    const { default: Swal } = await import('sweetalert2');
    const style = { background: '#1b1e3e', color: '#f1f3ff', confirmButtonColor: '#8170d8' };
    while (this.current && !this.loading && !this.perksBusy) {
      const choice = await Swal.fire({
        titleText: this.locale.t('perks.changeCategory'),
        text: this.locale.t('perks.currentCategory') + ': ' +
              this.locale.t('category.' + (this.categoryInfo?.current ?? '')),
        showCancelButton: true,
        showDenyButton: !!this.categoryInfo?.can_choose && this.spendableStars.silver >= 2,
        confirmButtonText: this.locale.t('perks.randomCategory') + ' — 1 ' + this.locale.t('stars.silver') + ' ★',
        denyButtonText: this.locale.t('perks.chooseCategory') + ' — 2 ' + this.locale.t('stars.silver') + ' ★',
        cancelButtonText: this.locale.t('auth.cancel'),
        ...style,
      });
      if (choice.isConfirmed) {
        const pay = await Swal.fire({
          titleText: this.locale.t('perks.randomCategory'),
          text: this.locale.t('perks.randomConfirm'),
          showCancelButton: true, showDenyButton: true,
          confirmButtonText: this.locale.t('perks.confirmChange'),
          denyButtonText: this.locale.t('perks.back'),
          cancelButtonText: this.locale.t('auth.cancel'),
          ...style,
        });
        if (pay.isDenied) continue;
        if (pay.isConfirmed) this.changeCategory('random');
        return;
      }
      if (choice.isDenied) {
        const alternatives = this.sortCategoryLabels(this.categoryInfo?.alternatives ?? []);
        const selected = await Swal.fire({
          titleText: this.locale.t('perks.chooseCategory'),
          input: 'select',
          inputLabel: this.locale.t('perks.selectCategory'),
          inputOptions: Object.fromEntries(alternatives.map(category =>
            [category, this.locale.t('category.' + category)])),
          inputValue: alternatives.includes(this.chosenCategory) ? this.chosenCategory : alternatives[0],
          inputValidator: value => alternatives.includes(value) ? undefined : this.locale.t('perks.selectCategory'),
          showCancelButton: true, showDenyButton: true,
          confirmButtonText: this.locale.t('perks.confirmChange') + ' — 2 ' + this.locale.t('stars.silver') + ' ★',
          denyButtonText: this.locale.t('perks.back'),
          cancelButtonText: this.locale.t('auth.cancel'),
          ...style,
        });
        if (selected.isDenied) continue;
        if (selected.isConfirmed && alternatives.includes(String(selected.value))) {
          this.chosenCategory = String(selected.value);
          this.changeCategory('manual');
        }
      }
      return;
    }
  }

  changeCategory(mode: 'random' | 'manual'): void {
    const current = this.current, state = this.categoryInfo;
    if (!this.account || !current || current.bonus || !state ||
        this.screen !== 'question' || this.loading || this.perksBusy ||
        !state.can_change || (mode === 'manual' && !state.can_choose)) return;
    if (mode === 'manual' && !state.alternatives.includes(this.chosenCategory)) return;
    const cost = mode === 'manual' ? 2 : 1;
    if (this.spendableStars.silver < cost) return;
    const gameId = this.gameId;
    const accountId = this.account.id;
    const shopWasOpen = this.shopOpen;
    // The confirmed category change can take as long as question generation.
    // Hide the previous question immediately rather than leaving it actionable
    // behind the closed SweetAlert dialog.
    this.perksBusy = true;
    this.loading = true;
    this.questionLoading = true;
    this.generatingNextQuestion = true;
    this.current = null;
    this.feedback = null;
    this.shopOpen = false;
    this.perksError = '';
    this.longWait = false;
    const warning = timer(this.warningMs).subscribe(() => {
      if (this.gameId === gameId && this.questionLoading) this.longWait = true;
    });
    this.subscriptions.add(this.http.post<PublicQuestion>(
      GAME_API + '/' + gameId + '/categories/change',
      { question_id: current.question_id, mode,
        category: mode === 'manual' ? this.chosenCategory : null,
        request_id: crypto.randomUUID() },
    ).pipe(timeout(current.number >= 13
                      ? Math.max(this.lateQuestionTimeoutMs, this.requestTimeoutMs)
                      : this.requestTimeoutMs),
           finalize(() => {
             warning.unsubscribe();
             // Session expiry clears the account and game, but must still
             // release the pending UI flags for the next login.
             if (this.account && (this.gameId !== gameId || this.account.id !== accountId)) return;
             this.perksBusy = false;
             this.loading = false;
             this.questionLoading = false;
             this.generatingNextQuestion = false;
             this.longWait = false;
           }))
      .subscribe({
        next: (question) => {
          if (this.gameId !== gameId || this.account?.id !== accountId) return;
          this.current = question;
          this.loadCategoryInfo();
          this.refreshSpendableStars();
        },
        error: (err: unknown) => {
          if (this.gameId !== gameId || this.account?.id !== accountId) return;
          if (this.isUnauthorized(err)) {
            this.sessionExpired();
            return;
          }
          // A failed request must not strand the player on an empty question.
          this.current = current;
          this.shopOpen = shopWasOpen;
          this.perksError = 'perks.categoryFailed';
        },
      }));
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
    this.current = null;
    this.generatingNextQuestion = true;
    this.questionLoading = true;
    this.subscriptions.add(this.http.post<GameInfo>(
      GAME_API + '/' + this.gameId + '/skip',
      { question_id: current.question_id, request_id: crypto.randomUUID() },
    ).pipe(timeout(this.requestTimeoutMs),
           finalize(() => { this.perksBusy = false; }))
     .subscribe({
      next: (game) => {
        this.progress = game.main_question_index ?? Math.min(15, current.number);
        this.gameCorrectAnswers = game.correct_main_answers;
        this.skippedQuestions = game.skipped_questions ?? this.skippedQuestions + 1;
        this.current = null;
        this.refreshStats();
        this.refreshSpendableStars();
        if (game.phase === 'completed') {
          this.finishedPhase = 'completed';
          this.forgetActiveGame();
          this.screen = 'done';
          this.questionLoading = false;
          this.generatingNextQuestion = false;
          this.loading = false;
        } else {
          // Finalize loading before asking for the next main slot.
          this.loading = false;
          this.loadQuestion();
        }
      },
      error: (err: unknown) => {
        this.loading = false;
        this.questionLoading = false;
        this.generatingNextQuestion = false;
        this.current = current;
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.perksError = 'perks.actionFailed';
      },
    }));
  }

  exchangeBetween(source: ConvertibleTier, target: ConvertibleTier): void {
    const upward = (source === 'bronze' && target === 'silver') ||
                   (source === 'silver' && target === 'gold');
    const downward = (source === 'silver' && target === 'bronze') ||
                     (source === 'gold' && target === 'silver');
    if (!this.account || this.perksBusy || this.loading || (!upward && !downward)) return;
    const amount = upward ? 10 : 1; // One click = one adjacent-tier exchange.
    if (this.spendableStars[source] < amount) return;
    this.perksBusy = true;
    this.perksError = '';
    this.subscriptions.add(this.http.post<StarCounts>(STARS_API + '/exchange', {
      source, target, amount, request_id: crypto.randomUUID(),
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

  retryRestoreGame(): void {
    if (!this.account || this.restoringGame || this.loading ||
        this.error !== 'game.restoreFailed') return;
    this.error = '';
    this.restoreActiveGame();
  }

  private restoreActiveGame(): void {
    const accountId = this.account?.id;
    if (!accountId) return;
    // Always ask PostgreSQL. Browser storage is only a best-effort cache:
    // it may be missing after switching localhost <-> 127.0.0.1, browsers,
    // cookies, or clearing site data. The authenticated server owns the run.
    const version = ++this.restoreVersion;
    this.restoringGame = true;
    this.subscriptions.add(this.http.get<GameInfo | null>(GAME_API + '/active').pipe(
      timeout(10000),
      finalize(() => { if (this.restoreVersion === version) this.restoringGame = false; }),
    ).subscribe({
      next: (run) => {
        if (this.account?.id !== accountId || this.restoreVersion !== version) return;
        this.error = '';
        if (!run) {
          this.removeSavedGame(accountId);
          this.restorePendingResults(accountId);
          return;
        }
        if (run.phase !== 'main' && run.phase !== 'bonus') {
          this.error = 'game.restoreFailed';
          return;
        }
        this.rememberActiveGame(run.game_id);
        // A game without an issued first question is still in pregame setup.
        // Restore it directly: purchasing a helper allocates and charges this
        // game before Q1, and offering Start New here would strand the purchase.
        if (run.phase === 'main' && run.question_issued === false) {
          this.resetGameView();
          this.gameId = run.game_id;
          this.gameLanguage = run.language;
          this.selectedLanguage = run.language;
          this.gameQuestionIssued = false;
          this.helperCard = run.helper ?? null;
          this.loadCategoryInfo();
          return;
        }
        // Once Q1 has been issued, leave Resume / Start New as an explicit
        // choice; the saved helper is still visible in the setup preview.
        // Clear any old helper/game UI so the card belongs to this run only.
        this.resetGameView();
        this.resumeCandidate = run;
        this.resumeRunLanguage = run.language;
        this.gameLanguage = run.language;
        this.selectedLanguage = run.language;
        this.resumeAvailable = true;
        this.screen = 'setup';
      },
      error: (err: unknown) => {
        if (this.account?.id !== accountId || this.restoreVersion !== version) return;
        // A 404 here is an API mismatch/error, not proof that the saved
        // round disappeared. Never clear its cache on a failed lookup.
        if (this.isUnauthorized(err)) this.sessionExpired();
        else this.error = 'game.restoreFailed';
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
        this.progress = run.main_question_index ?? run.correct_main_answers;
        this.bonusDecisionAvailable = run.bonus_decision_available === true;
        this.gameQuestionIssued = run.question_issued === true;
        this.gameCorrectAnswers = run.correct_main_answers;
        this.skippedQuestions = run.skipped_questions ?? 0;
        if (run.phase === 'completed' || run.phase === 'lost') {
          this.forgetActiveGame();
          this.feedback = null;
          this.finishedPhase = run.phase === 'lost' ? 'lost' : 'completed';
          this.currentStars = { ...run.round_stars };
          this.stars = this.starTiers.filter(t => this.currentStars[t.key] > 0)
            .map(t => t.key);
          this.current = null;
          this.screen = 'done';
          this.refreshStats();
        } else {
          this.current = null;
          this.feedback = null;
          this.finishedPhase = null;
          this.screen = 'setup';
          this.resumeAvailable = true;
          this.resumeRunLanguage = run.language;
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
    this.gameLanguage = '';
    this.languageSaving = false;
    this.resumeAvailable = false;
    this.resumeCandidate = null;
    this.resumeRunLanguage = '';
    this.current = null;
    this.feedback = null;
    this.finishedPhase = null;
    this.appealMessage = '';
    this.appealedQuestionIds.clear();
    this.helperCard = null;
    this.showAbandonConfirm = false;
    this.helperReply = '';
    this.helperError = '';
    this.helperCallKey = '';
    this.helperCallQuestionId = '';
    this.helperHireOpen = false;
    this.helperChooseProfession = false;
    this.gameQuestionIssued = false;
    this.helperDetailsVisible = false;
    this.bonusDecisionAvailable = false;
    this.error = '';
    this.progress = 0;
    this.gameCorrectAnswers = 0;
    this.skippedQuestions = 0;
    this.shopOpen = false;
    this.generatingNextQuestion = false;
    this.stars = [];
    this.currentStars = emptyStars();
    this.categoryInfo = null;
    this.categoriesToExclude = [];
    this.chosenCategory = '';
    this.showCategoryExclusions = false;
    this.screen = 'setup';
  }

  private rememberPendingResults(id: string): void {
    if (!this.account) return;
    try { localStorage.setItem(PENDING_RESULTS_KEY + this.account.id, id); }
    catch { /* private mode */ }
  }

  private clearPendingResults(): void {
    this.pendingResults = null;
    if (!this.account) return;
    try { localStorage.removeItem(PENDING_RESULTS_KEY + this.account.id); }
    catch { /* private mode */ }
  }

  private restorePendingResults(accountId: string): void {
    let savedId: string | null = null;
    try { savedId = localStorage.getItem(PENDING_RESULTS_KEY + accountId); }
    catch { /* private mode */ }
    if (!savedId) return;
    const version = this.restoreVersion;
    this.subscriptions.add(this.http.get<GameInfo>(GAME_API + '/' + encodeURIComponent(savedId)).pipe(
      timeout(10000),
    ).subscribe({
      next: run => {
        if (this.account?.id !== accountId || this.restoreVersion !== version ||
            this.gameId || this.resumeCandidate) return;
        if (run.game_id !== savedId || (run.phase !== 'completed' && run.phase !== 'lost')) {
          this.clearPendingResults();
          return;
        }
        this.pendingResults = run;
      },
      error: (err: unknown) => {
        // Only a confirmed missing/unauthorized run clears the local bookmark.
        // Transient network errors must not erase it.
        if (this.account?.id === accountId && err instanceof HttpErrorResponse &&
            (err.status === 403 || err.status === 404)) this.clearPendingResults();
      },
    }));
  }

  showPendingResults(): void {
    const run = this.pendingResults;
    if (!run || this.loading || this.screen !== 'setup' ||
        (run.phase !== 'completed' && run.phase !== 'lost')) return;
    this.gameId = run.game_id;
    this.gameLanguage = run.language;
    this.selectedLanguage = run.language;
    this.progress = run.main_question_index ?? run.correct_main_answers;
    this.gameCorrectAnswers = run.correct_main_answers;
    this.skippedQuestions = run.skipped_questions ?? 0;
    this.finishedPhase = run.phase;
    // Rehydrate the exact persisted award set (including the Q16 platinum
    // star and a forfeited gold wager), never infer it from 15/15.
    this.currentStars = { ...run.round_stars };
    this.stars = this.starTiers.filter(t => this.currentStars[t.key] > 0)
      .map(t => t.key);
    this.helperCard = run.helper ?? null;
    this.screen = 'done';
    this.clearPendingResults();
  }

  private rememberActiveGame(id: string): void {
    const userId = this.account?.id;
    if (!userId) return;
    try { localStorage.setItem(ACTIVE_GAME_KEY + userId, id); } catch { /* private mode */ }
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

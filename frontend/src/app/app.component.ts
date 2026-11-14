import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, firstValueFrom, Subscription, timeout, TimeoutError, timer } from 'rxjs';
import { LocalizationStore, UI_LANGUAGES } from './localization/store';
import { RESET_COPY, RESET_LANGUAGE_NAMES } from './localization/reset-copy';
import { ABOUT_SECTIONS } from './localization/about-copy';

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
const DRAFT_VOTE_KEY = 'ai-quiz-game:uncommitted-vote:v1:';
const USERNAME_PATTERN = /^[A-Za-z][A-Za-z0-9_]{2,29}$/;
const emptyStars = (): StarCounts => ({ bronze: 0, silver: 0, gold: 0, platinum: 0 });

interface AboutBonusClaim {
  claimed: boolean;
  newly_claimed: boolean;
  balances: StarCounts;
}

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

interface AppealAllowance {
  free_remaining: number;
  free_total: number;
  filed_this_month: number;
  next_filing_fee_bronze: number;
}
interface AppealQuote {
  before_answer: boolean;
  deposit_tier: StarTier;
  deposit_amount: number;
  filing_fee_bronze: number;
  free_appeals_remaining: number;
  free_appeals_total: number;
  free_appeals_refresh_at: string;
  hint_refund_bronze_if_upheld: number;
  loss_compensation_tier_if_upheld: StarTier | null;
  bonus_wager_refund_if_upheld: boolean;
  question_snapshot: {
    question_id: string; question: string;
    answers: Record<Choice, string>; hidden_keys: Choice[];
    number: number; difficulty: number; language: string;
  };
}
interface AppealRecord {
  id: string; question_id: string; question_number: number; language: string;
  status: string; display_status?: string; submitted_at: string; closed_at: string | null;
  question_snapshot: AppealQuote['question_snapshot'];
  reasons: string[]; comment: string; source_links: string[];
  verdict: string | null; deposit: { tier: StarTier; amount: number; refunded: boolean };
  filing_fee_bronze: number; compensation: { tier: StarTier; amount: number };
  selected_key: Choice | null; answer_key: Choice | null;
  bonus_wager_refunded: boolean;
  expenses_refunded: { tier: StarTier; amount: number; reason: string }[];
}

type AppealHistoryStatus = 'all' | 'upheld' | 'rejected' | 'in_review' | 'technical_unresolved';
type AppealHistorySort = 'newest' | 'oldest';

interface AppealHistoryFilters {
  status: AppealHistoryStatus;
  search: string;
  from: string;
  to: string;
  sort: AppealHistorySort;
}

/** Preserve the wallet's distinct refund purposes in the complaint history.
 *  The API sends machine-readable reason codes, not user-facing labels.
 */
export function appealHistoryRefundRows(
  expenses: AppealRecord['expenses_refunded'] | null | undefined,
): { labelKey: string; tier: StarTier; amount: number }[] {
  const reasonLabels: Record<string, string> = {
    hints: 'appeals.hintRefunds',
    helper: 'appeals.helperCompensation',
    unused_helper: 'appeals.unusedHelperRefund',
  };
  const rows: { labelKey: string; tier: StarTier; amount: number }[] = [];
  for (const expense of expenses ?? []) {
    if (!Number.isInteger(expense.amount) || expense.amount <= 0 ||
        !['bronze', 'silver', 'gold', 'platinum'].includes(expense.tier)) continue;
    const labelKey = reasonLabels[expense.reason] ?? 'appeals.otherRefunds';
    const existing = rows.find(row => row.labelKey === labelKey && row.tier === expense.tier);
    if (existing) existing.amount += expense.amount;
    else rows.push({ labelKey, tier: expense.tier, amount: expense.amount });
  }
  return rows;
}

/** Dates in this browser's local timezone; both date endpoints are inclusive. */
function appealLocalDate(value: string): string {
  const time = new Date(value);
  if (!Number.isFinite(time.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return time.getFullYear() + '-' + pad(time.getMonth() + 1) + '-' + pad(time.getDate());
}

/** Four-word preview; full text is still present in the expanded card and tooltip. */
export function appealQuestionExcerpt(value: string): string {
  const words = String(value ?? '').trim().replace(/\s+/g, ' ').split(' ').filter(Boolean);
  if (!words.length) return '—';
  return words.slice(0, 4).join(' ') + (words.length > 4 ? '…' : '');
}

/** Only finalized merits rulings are Judge decisions; other states are statuses. */
export function appealHistoryDecisionPresentation(
  status: string, verdict: string | null, translate: (key: string) => string,
  hiddenUntilGameEnd = false,
): { headingKey: string; message: string } {
  const decided = status === 'upheld' || status === 'rejected';
  const awaiting = status === 'pending' || status === 'retry_pending';
  return {
    headingKey: decided ? 'appeals.historyVerdict' : 'appeals.historyComplaintStatus',
    message: awaiting ? translate('appeals.notDecided') :
      decided && hiddenUntilGameEnd ? translate('appeals.keyHidden') :
      verdict || translate('appeals.notDecided'),
  };
}

/** Use the selected UI language, not the browser's default locale. */
export function formatAppealHistoryDate(value: string, languageCode: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString(languageCode, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }) : '—';
}

/** Pure function shared by UI and regression tests: no network or state mutation. */
export function filterAppealHistory(
  records: AppealRecord[], filters: AppealHistoryFilters,
): AppealRecord[] {
  const query = filters.search.trim().toLocaleLowerCase();
  return records.filter(item => {
    const statusMatches = filters.status === 'all' ||
      (filters.status === 'in_review'
        ? item.status === 'pending' || item.status === 'retry_pending'
        : item.status === filters.status);
    if (!statusMatches) return false;
    if (query && !String(item.question_snapshot?.question ?? '')
      .toLocaleLowerCase().includes(query)) return false;
    const date = appealLocalDate(item.submitted_at);
    if (filters.from && (!date || date < filters.from)) return false;
    if (filters.to && (!date || date > filters.to)) return false;
    return true;
  }).sort((a, b) => {
    const difference = new Date(a.submitted_at).getTime() -
      new Date(b.submitted_at).getTime();
    return (filters.sort === 'oldest' ? difference : -difference) ||
      a.id.localeCompare(b.id);
  });
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


  profileMenuOpen = false;
  appealAllowance: AppealAllowance | null = null;
  appealBusy = false;
  appealMessage = '';
  appealedQuestionIds = new Set<string>();
  readonly appealReasons = [
    'multiple_correct', 'no_correct', 'incorrect_key', 'ambiguous', 'answer_leak'
  ];

  account: Account | null = null;
  authLoading = true;
  authBusy = false;
  readonly aboutSections = ABOUT_SECTIONS;
  aboutOpen = false;
  aboutClaimed = false;
  aboutStatusLoading = false;
  aboutBusy = false;
  aboutError = '';
  readonly aboutReward: StarCounts = { bronze: 50, silver: 40, gold: 30, platinum: 20 };
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
  currentVote: -1 | 0 | 1 = 0;
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

  private restoreLostQuestionForAppeal(runId: string): void {
    if (!this.account || !runId) return;
    const accountId = this.account.id;
    this.subscriptions.add(this.http.get<PublicQuestion>(
      GAME_API + '/' + runId + '/appeals/lost-question'
    ).pipe(timeout(10000)).subscribe({
      next: question => {
        if (this.account?.id === accountId && this.gameId === runId &&
            this.finishedPhase === 'lost' && this.screen === 'done') this.current = question;
      },
      error: () => { /* Already appealed or not eligible; keep the result unchanged. */ },
    }));
  }

  async openAppealForm(): Promise<void> {
    if (!this.account || !this.gameId || !this.current || this.appealBusy ||
        this.loading || this.perksBusy ||
        !(this.screen === 'question' ||
          (this.screen === 'feedback' && !!this.feedback)) ||
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
      const star = (tier: StarTier, count: number) =>
        count + ' ' + this.locale.t('stars.' + tier) + ' ★';
      const snapshot = quote.question_snapshot;
      const options = this.choices.map(key =>
        '<li><strong>' + key + '.</strong> ' + safe(snapshot.answers[key]) + '</li>'
      ).join('');
      const reasons = this.appealReasons.filter(r =>
        r !== 'incorrect_key' || !quote.before_answer
      );
      const reasonsHtml = reasons.map(r =>
        '<label class="appeal-choice"><input type="checkbox" name="appeal-reason" value="' +
        safe(r) + '"> ' + safe(this.locale.t('appeals.reason.' + r)) + '</label>'
      ).join('');
      const feeText = this.locale.t('appeals.filingPrice') + ': ' +
        star('bronze', quote.filing_fee_bronze);
      // UTC calendar months: never shift the quota reset into the prior local day.
      const refreshDate = new Intl.DateTimeFormat('en-GB', {
        timeZone: 'UTC', day: '2-digit', month: '2-digit', year: '2-digit',
      }).format(new Date(quote.free_appeals_refresh_at));
      const quotaText = this.locale.format('appeals.freeQuota', {
        remaining: quote.free_appeals_remaining,
        total: quote.free_appeals_total,
      }) + ' (' + this.locale.format('appeals.freeReset', { date: refreshDate }) + ')';
      const deposit = star(quote.deposit_tier, quote.deposit_amount);
      const totalNow = quote.deposit_tier === 'bronze'
        ? star('bronze', quote.deposit_amount + quote.filing_fee_bronze)
        : deposit + (quote.filing_fee_bronze
            ? ' + ' + star('bronze', quote.filing_fee_bronze) : '');
      const successExtras = [
        quote.hint_refund_bronze_if_upheld > 0
          ? this.locale.t('appeals.hintRefunds') + ': ' +
            star('bronze', quote.hint_refund_bronze_if_upheld) : '',
        quote.loss_compensation_tier_if_upheld
          ? this.locale.t('appeals.compensation') + ': ' +
            star(quote.loss_compensation_tier_if_upheld, 1) : '',
        quote.bonus_wager_refund_if_upheld
          ? this.locale.t('appeals.wagerRefunded') + ': ' + star('gold', 1) : '',
      ].filter(Boolean).map(row => '<p>' + safe(row) + '</p>').join('');
      const costHtml =
        '<section class="appeal-cost"><h3>' + safe(this.locale.t('appeals.costTitle')) +
        '</h3><p><strong>' + safe(this.locale.t('appeals.deposit')) + ':</strong> ' +
        safe(deposit) + '</p><p><strong>' + safe(feeText) + '</strong></p><p><strong>' +
        safe(this.locale.t('appeals.nowDebit')) + ':</strong> ' +
        safe(totalNow) + '</p><p class="appeal-warning">' +
        safe(this.locale.t('appeals.depositRule')) + '</p>' +
        (quote.filing_fee_bronze ? '<p class="appeal-warning">' +
         safe(this.locale.t('appeals.feeNeverReturned')) + '</p>' : '') +
        '</section>';
      const wagerNotice = question.number === 16
        ? '<p>' + safe(this.locale.t('appeals.bonusSeparate')) + '</p>' : '';
      const hintNotice = question.hidden_count
        ? '<p>' + safe(this.locale.t('appeals.hintsLater')) + '</p>' : '';
      let draft: { reasons: string[]; comment: string; source_links: string[] } = {
        reasons: [], comment: '', source_links: [],
      };
      let confirmed = false;
      while (!confirmed) {
        const result = await Swal.fire({
          titleText: this.locale.t('appeals.title'), width: '850px', showCancelButton: true,
          customClass: { popup: 'quiz-appeal-popup' },
          confirmButtonText: this.locale.t('appeals.submit'),
          cancelButtonText: this.locale.t('helper.close'),
          html: '<p class="appeal-quota-header">' + safe(quotaText) + '</p>' +
            '<div class="appeal-scroll-wrap"><div class="appeal-form">' +
            '<p><strong>' + safe(snapshot.question) + '</strong></p>' +
            '<ol class="appeal-options">' + options + '</ol>' +
            '<div class="appeal-reasons">' + reasonsHtml + '</div>' +
            '<label for="appeal-comment">' + safe(this.locale.t('appeals.comment')) +
            '</label><textarea id="appeal-comment" maxlength="1000" rows="4"></textarea>' +
            '<label for="appeal-sources">' + safe(this.locale.t('appeals.sources')) +
            '</label><textarea id="appeal-sources" rows="2"></textarea>' +
            costHtml + wagerNotice + hintNotice + '</div>' +
            '<div class="appeal-scroll-hint" aria-hidden="true">' +
            '<span class="appeal-scroll-chevron"></span>' +
            '<span class="appeal-scroll-chevron"></span></div></div>',
          didOpen: popup => {
            popup.querySelectorAll<HTMLInputElement>('input[name="appeal-reason"]')
              .forEach(input => { input.checked = draft.reasons.includes(input.value); });
            const comment = popup.querySelector<HTMLTextAreaElement>('#appeal-comment');
            if (comment) comment.value = draft.comment;
            const sources = popup.querySelector<HTMLTextAreaElement>('#appeal-sources');
            if (sources) sources.value = draft.source_links.join('\n');
            const scroll = popup.querySelector<HTMLElement>('.appeal-form');
            const hint = popup.querySelector<HTMLElement>('.appeal-scroll-hint');
            if (!scroll || !hint) return;
            const update = () => {
              hint.hidden = scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 8;
            };
            scroll.addEventListener('scroll', update, { passive: true });
            update();
          },
          preConfirm: () => {
            const popup = Swal.getPopup();
            if (!popup) return false;
            const selectedReasons = Array.from(
              popup.querySelectorAll<HTMLInputElement>('input[name="appeal-reason"]:checked')
            ).map(input => input.value);
            const comment = popup.querySelector<HTMLTextAreaElement>('#appeal-comment')?.value.trim() || '';
            const links = (popup.querySelector<HTMLTextAreaElement>('#appeal-sources')?.value || '')
              .split(/\r?\n/).map(v => v.trim()).filter(Boolean);
            if ((!selectedReasons.length && !comment) || links.length > 5 ||
                links.some(l => l.length > 500 || !/^https?:\/\//i.test(l))) {
              Swal.showValidationMessage(this.locale.t('appeals.invalid'));
              return false;
            }
            return { reasons: selectedReasons, comment, source_links: links };
          },
        });
        if (!result.isConfirmed || !result.value) return;
        draft = result.value;
        if (this.gameId !== runId || this.account?.id !== accountId) return;
        const confirmedResult = await Swal.fire({
          titleText: this.locale.t('appeals.confirmTitle'),
          width: '650px', showCancelButton: true, reverseButtons: true,
          customClass: { popup: 'quiz-appeal-popup quiz-appeal-confirm-popup' },
          confirmButtonText: this.locale.t('appeals.confirmAndSend'),
          cancelButtonText: this.locale.t('appeals.back'),
          html: '<div class="appeal-confirm-cost"><p>' +
            safe(this.locale.t('appeals.confirmIntro')) + '</p><p>' +
            safe(this.locale.t('appeals.deposit')) + ': <strong>' + safe(deposit) +
            '</strong></p><p>' + safe(this.locale.t('appeals.fee')) +
            ': <strong>' + safe(star('bronze', quote.filing_fee_bronze)) +
            '</strong></p><p class="appeal-confirm-total">' +
            safe(this.locale.t('appeals.nowDebit')) + ': <strong>' +
            safe(totalNow) + '</strong></p><p class="appeal-confirm-total">' +
            safe(this.locale.t('appeals.ifUpheldRefund')) + ': <strong>' +
            safe(deposit) + '</strong></p>' + successExtras +
            '<p>' + safe(this.locale.t('appeals.hintRefundRule')) + '</p>' +
            '<p class="appeal-warning">' +
            safe(this.locale.t('appeals.rejectedRule')) + '</p>' +
            (quote.filing_fee_bronze > 0 ? '<p class="appeal-warning">' +
             safe(this.locale.t('appeals.feeNeverReturned')) + '</p>' : '') +
            (question.number === 16 ? '<p>' +
             safe(this.locale.t('appeals.bonusSeparate')) + '</p>' : '') +
            '</div>',
        });
        if (confirmedResult.isConfirmed) confirmed = true;
        else if (confirmedResult.dismiss !== Swal.DismissReason.cancel) return;
      }
      if (this.gameId !== runId || this.account?.id !== accountId) return;
      const storageKey = 'ai-quiz-game:appeal-request:' + accountId + ':' +
        runId + ':' + question.question_id;
      let requestId = '';
      try { requestId = sessionStorage.getItem(storageKey) || ''; } catch { /* private mode */ }
      if (!requestId) {
        requestId = crypto.randomUUID();
        try { sessionStorage.setItem(storageKey, requestId); } catch { /* private mode */ }
      }
      const submittedAppeal = await firstValueFrom(this.http.post<AppealRecord>(
        GAME_API + '/' + runId + '/appeals', {
          ...draft, question_id: question.question_id, request_id: requestId,
          expected_fee_bronze: quote.filing_fee_bronze,
          confirm_nonrefundable_fee: true,
        }
      ).pipe(timeout(15000)));
      try { sessionStorage.removeItem(storageKey); } catch { /* private mode */ }
      this.appealedQuestionIds.add(question.question_id);
      this.appealMessage = this.locale.t('appeals.received');
      this.refreshSpendableStars();
      this.refreshAppealAllowance();
      if (submittedAppeal.status === 'pending' || submittedAppeal.status === 'retry_pending') {
        this.watchAppealSettlement(submittedAppeal.id, accountId, runId);
      }
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

  async openAppealRules(): Promise<void> {
    const { default: Swal } = await import('sweetalert2');
    const safe = (value: unknown) => this.escapeAppealHtml(value);
    const blocks = [
      'appeals.rulesWhen', 'appeals.rulesFree', 'appeals.rulesDeposits',
      'appeals.rulesJudge', 'appeals.rulesUpheld', 'appeals.rulesRejected',
      'appeals.rulesTechnical', 'appeals.rulesHistory'
    ].map(key => '<p>' + safe(this.locale.t(key)) + '</p>').join('');
    await Swal.fire({
      titleText: this.locale.t('appeals.rulesTitle'), width: '720px',
      html: '<div class="appeal-history appeal-rules">' + blocks + '</div>',
      confirmButtonText: this.locale.t('helper.close'),
    });
  }

  /** Bounded background check: reflect Judge's eventual wallet changes without F5.
   *  No repeated paid model request: this only reads this player's appeal status.
   */
  private watchAppealSettlement(appealId: string, accountId: string, runId: string): void {
    let polls = 0;
    let inFlight = false;
    const watch = timer(2500, 4000).subscribe(() => {
      if (this.account?.id !== accountId || ++polls > 24) {
        watch.unsubscribe();
        return;
      }
      if (inFlight) return;
      inFlight = true;
      const request = this.http.get<AppealRecord>(
        '/api/v2/appeals/' + encodeURIComponent(appealId),
      ).pipe(timeout(10000), finalize(() => { inFlight = false; })).subscribe({
        next: item => {
          if (this.account?.id !== accountId) { watch.unsubscribe(); return; }
          if (!['upheld', 'rejected', 'technical_unresolved'].includes(item.status)) return;
          watch.unsubscribe();
          this.refreshSpendableStars();
          this.refreshAppealAllowance();
          if (this.gameId === runId &&
              this.appealMessage === this.locale.t('appeals.received')) {
            this.appealMessage = this.locale.t('appeals.status.' + item.status);
          }
        },
        error: err => {
          if (this.isUnauthorized(err)) watch.unsubscribe();
        },
      });
      this.subscriptions.add(request);
    });
    this.subscriptions.add(watch);
  }

  private refreshAppealAllowance(): void {
    const accountId = this.account?.id;
    if (!accountId) { this.appealAllowance = null; return; }
    this.subscriptions.add(this.http.get<AppealAllowance>(
      '/api/v2/appeals/allowance'
    ).pipe(timeout(10000)).subscribe({
      next: data => {
        if (this.account?.id === accountId) this.appealAllowance = data;
      },
      error: () => {
        if (this.account?.id === accountId) this.appealAllowance = null;
      },
    }));
  }

  async openAppealHistory(): Promise<void> {
    if (!this.account || this.appealBusy) return;
    this.profileMenuOpen = false;
    this.appealBusy = true;
    const accountId = this.account.id;
    try {
      const { default: Swal } = await import('sweetalert2');
      const records: AppealRecord[] = [];
      for (let offset = 0; ; offset += 100) {
        const page = await firstValueFrom(this.http.get<AppealRecord[]>(
          '/api/v2/appeals?offset=' + offset + '&limit=100').pipe(timeout(15000)));
        records.push(...page);
        if (page.length < 100) break;
      }
      if (this.account?.id !== accountId) return;
      this.refreshAppealAllowance();
      // History may already show an asynchronously settled appeal, while
      // the top-of-page wallet still reflects the earlier deposit debit.
      this.refreshSpendableStars();
      const safe = (value: unknown) => this.escapeAppealHtml(value);
      const localized = (key: string) => safe(this.locale.t(key));
      const formattedDate = (value: string) =>
        formatAppealHistoryDate(value, this.locale.code);
      const statuses: { value: AppealHistoryStatus; label: string }[] = [
        { value: 'all', label: 'appeals.historyAll' },
        { value: 'upheld', label: 'appeals.status.upheld' },
        { value: 'rejected', label: 'appeals.status.rejected' },
        { value: 'in_review', label: 'appeals.historyInReview' },
        { value: 'technical_unresolved', label: 'appeals.status.technical_unresolved' },
      ];
      const filterHtml =
        '<div class="appeal-history-toolbar" role="search">' +
          '<div class="appeal-history-filter appeal-history-search">' +
            '<label for="appeal-history-search">' + localized('appeals.historySearch') + '</label>' +
            '<input id="appeal-history-search" type="search" autocomplete="off" ' +
              'placeholder="' + localized('appeals.historySearchPlaceholder') + '">' +
          '</div>' +
          '<div class="appeal-history-filter">' +
            '<label for="appeal-history-status">' + localized('appeals.historyStatus') + '</label>' +
            '<select id="appeal-history-status">' +
              statuses.map(item => '<option value="' + item.value + '">' +
                localized(item.label) + '</option>').join('') +
            '</select>' +
          '</div>' +
          '<div class="appeal-history-filter">' +
            '<label for="appeal-history-from">' + localized('appeals.historyFrom') + '</label>' +
            '<input id="appeal-history-from" type="date">' +
          '</div>' +
          '<div class="appeal-history-filter">' +
            '<label for="appeal-history-to">' + localized('appeals.historyTo') + '</label>' +
            '<input id="appeal-history-to" type="date">' +
          '</div>' +
          '<div class="appeal-history-filter">' +
            '<label for="appeal-history-sort">' + localized('appeals.historySort') + '</label>' +
            '<select id="appeal-history-sort">' +
              '<option value="newest">' + localized('appeals.historyNewest') + '</option>' +
              '<option value="oldest">' + localized('appeals.historyOldest') + '</option>' +
            '</select>' +
          '</div>' +
        '</div>';
      const itemsHtml = records.map((item, index) => {
        const question = String(item.question_snapshot?.question ?? '');
        const summary = appealQuestionExcerpt(question);
        const answers = this.choices.map(key => {
          const selected = item.selected_key === key;
          const correct = item.answer_key === key;
          const marker = [
            selected ? this.locale.t('appeals.yourAnswer') : '',
            correct ? this.locale.t('appeals.gameAnswer') : '',
          ].filter(Boolean).join(' · ');
          return '<li class="' + (selected ? 'appeal-option-player ' : '') +
            (correct ? 'appeal-option-key' : '') + '"><strong>' + key +
            '</strong>: ' + safe(item.question_snapshot?.answers?.[key]) +
            (marker ? ' <span class="appeal-answer-tag">(' + safe(marker) + ')</span>' : '') +
            '</li>';
        }).join('');
        const displayedStatus = item.display_status || item.status;
        const allowedStatus = ['upheld', 'rejected', 'pending', 'retry_pending',
          'technical_unresolved'].includes(displayedStatus) ? displayedStatus : 'unknown';
        const decision = appealHistoryDecisionPresentation(
          item.status, item.verdict, key => this.locale.t(key),
          item.before_answer && item.answer_key === null);
        const field = (labelKey: string, value: string) =>
          '<p class="appeal-history-field"><span class="appeal-history-field-name">' +
          localized(labelKey) + ':</span> ' + value + '</p>';
        const fieldRefund = (labelKey: string, tier: StarTier, amount: number) =>
          field(labelKey, safe('+' + amount + ' ' + this.locale.t('stars.' + tier)));
        const refundFields = appealHistoryRefundRows(item.expenses_refunded).map(row =>
          fieldRefund(row.labelKey, row.tier, row.amount)).join('');
        return '<details class="appeal-history-item" data-history-index="' + index + '">' +
          '<summary title="' + safe(question) + '">' +
            '<span class="appeal-history-heading">' +
              '<span class="appeal-history-question-preview" dir="auto">' + safe(summary) + '</span>' +
              '<span class="appeal-history-date">' + safe(formattedDate(item.submitted_at)) + '</span>' +
            '</span>' +
            '<span class="appeal-history-status appeal-history-status-' + allowedStatus + '">' +
              localized('appeals.status.' + allowedStatus) + '</span>' +
            '<span class="appeal-history-chevron" aria-hidden="true"></span>' +
          '</summary>' +
          '<div class="appeal-history-detail">' +
            '<p class="appeal-history-full-question" dir="auto">' + safe(question) + '</p>' +
            '<ol class="appeal-options">' + answers + '</ol>' +
            '<div class="appeal-history-info-grid">' +
              field('appeals.yourAnswer', safe(item.selected_key || this.locale.t('appeals.notAnswered'))) +
              field('appeals.gameAnswer', safe(item.answer_key || this.locale.t('appeals.keyHidden'))) +
              field('appeals.historyReasons', safe(item.reasons.map(r =>
                this.locale.t('appeals.reason.' + r)).join(', ') || '—')) +
              field('appeals.closed', safe(item.closed_at ? formattedDate(item.closed_at) : '—')) +
            '</div>' +
            (item.comment ? '<div class="appeal-history-note"><strong>' +
              localized('appeals.historyComment') + '</strong><p dir="auto">' +
              safe(item.comment) + '</p></div>' : '') +
            (item.source_links.length ? '<div class="appeal-history-note"><strong>' +
              localized('appeals.historySources') + '</strong><p>' +
              safe(item.source_links.join(' | ')) + '</p></div>' : '') +
            '<div class="appeal-history-verdict">' +
              '<div class="appeal-history-verdict-header"><strong>' +
                localized(decision.headingKey) + '</strong>' +
                '<span class="appeal-history-status appeal-history-status-' + allowedStatus + '">' +
                  localized('appeals.status.' + allowedStatus) + '</span>' +
              '</div><p dir="auto">' +
              safe(decision.message) + '</p></div>' +
            '<div class="appeal-history-economy">' +
              field('appeals.deposit',
                safe(item.deposit.amount + ' ' + this.locale.t('stars.' + item.deposit.tier)) +
                ' (' + safe(this.locale.t(item.deposit.refunded ? 'appeals.refunded' :
                  item.status === 'rejected' ? 'appeals.depositForfeited' :
                  'appeals.depositPending')) + ')') +
              field('appeals.fee',
                safe(item.filing_fee_bronze + ' ' + this.locale.t('stars.bronze'))) +
              field('appeals.lossCompensation',
                safe(item.compensation.amount + ' ' +
                  this.locale.t('stars.' + item.compensation.tier))) +
              (item.question_number === 16
                ? field('appeals.wagerRefunded',
                  safe(this.locale.t(item.bonus_wager_refunded ? 'appeals.yes' : 'appeals.no')))
                : '') +
              refundFields +
            '</div>' +
          '</div></details>';
      }).join('');
      const body = filterHtml +
        '<div class="appeal-history-meta">' +
          '<p class="appeal-history-count" id="appeal-history-count" role="status" aria-live="polite"></p>' +
          '<button id="appeal-history-reset" class="appeal-history-reset" type="button">' +
            localized('appeals.historyReset') + '</button>' +
        '</div>' +
        '<div class="appeal-scroll-wrap appeal-history-scroll-wrap">' +
          '<div class="appeal-history" id="appeal-history-list">' + itemsHtml + '</div>' +
          '<p class="appeal-history-empty" id="appeal-history-empty" hidden></p>' +
          '<div class="appeal-scroll-hint" aria-hidden="true" hidden>' +
            '<span class="appeal-scroll-chevron"></span>' +
            '<span class="appeal-scroll-chevron"></span>' +
          '</div>' +
        '</div>';

      await Swal.fire({
        titleText: this.locale.t('appeals.myComplaints'),
        width: '940px', html: body,
        customClass: { popup: 'quiz-appeal-popup quiz-appeal-history-popup' },
        confirmButtonText: this.locale.t('helper.close'),
        didOpen: popup => {
          const list = popup.querySelector<HTMLElement>('#appeal-history-list');
          const search = popup.querySelector<HTMLInputElement>('#appeal-history-search');
          const status = popup.querySelector<HTMLSelectElement>('#appeal-history-status');
          const from = popup.querySelector<HTMLInputElement>('#appeal-history-from');
          const to = popup.querySelector<HTMLInputElement>('#appeal-history-to');
          const sort = popup.querySelector<HTMLSelectElement>('#appeal-history-sort');
          const count = popup.querySelector<HTMLElement>('#appeal-history-count');
          const reset = popup.querySelector<HTMLButtonElement>('#appeal-history-reset');
          const empty = popup.querySelector<HTMLElement>('#appeal-history-empty');
          const hint = popup.querySelector<HTMLElement>('.appeal-scroll-hint');
          if (!list || !search || !status || !from || !to || !sort || !count || !reset || !empty || !hint) return;
          const nodes = new Map(records.map((item, index) => [
            item.id, list.querySelector<HTMLElement>('[data-history-index="' + index + '"]'),
          ]));
          const updateScroll = () => {
            const scrollable = list.scrollHeight > list.clientHeight + 4;
            hint.hidden = !scrollable ||
              list.scrollTop + list.clientHeight >= list.scrollHeight - 8;
          };
          const update = () => {
            const filters: AppealHistoryFilters = {
              status: status.value as AppealHistoryStatus,
              search: search.value, from: from.value, to: to.value,
              sort: sort.value as AppealHistorySort,
            };
            reset.disabled = !filters.search && filters.status === 'all' &&
              !filters.from && !filters.to && filters.sort === 'newest';
            const matches = filterAppealHistory(records, filters);
            const ids = new Set(matches.map(item => item.id));
            for (const [id, node] of nodes) {
              if (node) node.hidden = !ids.has(id);
            }
            for (const item of matches) {
              const node = nodes.get(item.id);
              if (node) list.appendChild(node); // preserves expanded <details>.
            }
            count.textContent = this.locale.format('appeals.historyCount', {
              shown: matches.length, total: records.length,
            });
            empty.hidden = matches.length !== 0;
            empty.textContent = this.locale.t(
              records.length ? 'appeals.historyNoMatches' : 'appeals.none');
            list.scrollTop = 0;
            updateScroll();
          };
          reset.addEventListener('click', () => {
            search.value = '';
            status.value = 'all';
            from.value = '';
            to.value = '';
            sort.value = 'newest';
            update();
          });
          search.addEventListener('input', update);
          for (const input of [status, from, to, sort]) {
            input.addEventListener('change', update);
          }
          list.addEventListener('scroll', updateScroll, { passive: true });
          list.addEventListener('toggle', updateScroll, true);
          update();
        },
      });
    } catch (error) {
      if (this.isUnauthorized(error)) this.sessionExpired();
      else this.appealMessage = this.locale.t('appeals.historyFailed');
    } finally {
      this.appealBusy = false;
    }
  }

  async openAccountSettings(): Promise<void> {
    if (!this.account || this.authBusy || this.authLoading || this.loading) return;
    const accountId = this.account.id;
    this.authBusy = true;
    this.profileMenuOpen = false;
    try {
      const { default: Swal } = await import('sweetalert2');
      const selection = await Swal.fire({
        titleText: this.locale.t('settings.title'),
        text: this.locale.t('settings.description'),
        customClass: { popup: 'quiz-auth-popup quiz-settings-popup' },
        background: '#1b1e3e', color: '#f1f3ff',
        showDenyButton: true,
        showCancelButton: true,
        confirmButtonText: this.locale.t('settings.changeUsername'),
        denyButtonText: this.locale.t('settings.changePassword'),
        cancelButtonText: this.locale.t('auth.cancel'),
        confirmButtonColor: '#8170d8', denyButtonColor: '#626ba1',
      });
      if (this.account?.id !== accountId) return;
      if (selection.isConfirmed) await this.changeAccountUsername();
      else if (selection.isDenied) await this.changeAccountPassword();
    } catch {
      this.error = 'settings.openFailed';
    } finally {
      this.authBusy = false;
    }
  }

  private async changeAccountUsername(): Promise<void> {
    if (!this.account) return;
    const { default: Swal } = await import('sweetalert2');
    const accountId = this.account.id;
    const result = await Swal.fire({
      titleText: this.locale.t('settings.changeUsername'),
      html: '<div class="quiz-auth-fields">' +
        '<label id="settings-username-label" for="settings-username-input"></label>' +
        '<input id="settings-username-input" class="swal2-input" type="text" ' +
        'autocomplete="username" maxlength="30"></div>',
      didOpen: () => {
        const popup = Swal.getPopup();
        const label = popup?.querySelector<HTMLElement>('#settings-username-label');
        if (label) label.textContent = this.locale.t('auth.username');
        const input = popup?.querySelector<HTMLInputElement>('#settings-username-input');
        if (input) input.value = this.account?.username || '';
      },
      customClass: { popup: 'quiz-auth-popup quiz-settings-popup' },
      background: '#1b1e3e', color: '#f1f3ff',
      confirmButtonColor: '#8170d8',
      confirmButtonText: this.locale.t('settings.saveUsername'),
      cancelButtonText: this.locale.t('auth.cancel'), showCancelButton: true,
      focusConfirm: false, showLoaderOnConfirm: true,
      allowOutsideClick: () => !Swal.isLoading(),
      preConfirm: async () => {
        const username = (Swal.getPopup()?.querySelector<HTMLInputElement>(
          '#settings-username-input')?.value || '').trim();
        if (!USERNAME_PATTERN.test(username)) {
          Swal.showValidationMessage(this.locale.t('auth.usernameInvalid'));
          return false;
        }
        try {
          return await firstValueFrom(this.http.patch<Account>(
            '/api/auth/username', { username },
          ).pipe(timeout(15000)));
        } catch (error: unknown) {
          if (this.isUnauthorized(error)) {
            this.sessionExpired();
            return false;
          }
          const key = error instanceof HttpErrorResponse && error.status === 409
            ? 'auth.usernameTaken' : 'settings.updateFailed';
          Swal.showValidationMessage(this.locale.t(key));
          return false;
        }
      },
    });
    if (!result.isConfirmed || !result.value || this.account?.id !== accountId) return;
    // Keep the same user id, active round, awards and local storage bookmarks.
    this.account = result.value as Account;
    await Swal.fire({
      titleText: this.locale.t('settings.usernameChanged'),
      icon: 'success', background: '#1b1e3e', color: '#f1f3ff',
      confirmButtonText: this.locale.t('settings.done'),
      confirmButtonColor: '#8170d8',
    });
  }

  private async changeAccountPassword(): Promise<void> {
    if (!this.account) return;
    const { default: Swal } = await import('sweetalert2');
    const accountId = this.account.id;
    const fields: { id: string; label: string; autocomplete: string }[] = [
      { id: 'settings-old-password', label: 'settings.currentPassword', autocomplete: 'current-password' },
      { id: 'settings-new-password', label: 'settings.newPassword', autocomplete: 'new-password' },
      { id: 'settings-repeat-password', label: 'settings.repeatPassword', autocomplete: 'new-password' },
    ];
    const html = '<div class="quiz-auth-fields">' + fields.map(field =>
      '<label id="' + field.id + '-label" for="' + field.id + '"></label>' +
      '<input id="' + field.id + '" class="swal2-input" type="password" ' +
      'autocomplete="' + field.autocomplete + '" maxlength="256">'
    ).join('') + '</div>';
    const result = await Swal.fire({
      titleText: this.locale.t('settings.changePassword'),
      html,
      didOpen: () => {
        const popup = Swal.getPopup();
        for (const field of fields) {
          const label = popup?.querySelector<HTMLElement>('#' + field.id + '-label');
          if (label) label.textContent = this.locale.t(field.label);
        }
      },
      customClass: { popup: 'quiz-auth-popup quiz-settings-popup' },
      background: '#1b1e3e', color: '#f1f3ff',
      confirmButtonColor: '#8170d8',
      confirmButtonText: this.locale.t('settings.savePassword'),
      cancelButtonText: this.locale.t('auth.cancel'), showCancelButton: true,
      focusConfirm: false, showLoaderOnConfirm: true,
      allowOutsideClick: () => !Swal.isLoading(),
      preConfirm: async () => {
        const value = (id: string) =>
          Swal.getPopup()?.querySelector<HTMLInputElement>('#' + id)?.value || '';
        const current_password = value('settings-old-password');
        const new_password = value('settings-new-password');
        const confirm_password = value('settings-repeat-password');
        if (!current_password) {
          Swal.showValidationMessage(this.locale.t('settings.currentRequired'));
          return false;
        }
        if (new_password.length < 8 || new_password.length > 256) {
          Swal.showValidationMessage(this.locale.t('auth.passwordInvalid'));
          return false;
        }
        if (new_password !== confirm_password) {
          Swal.showValidationMessage(this.locale.t('auth.passwordMismatch'));
          return false;
        }
        if (new_password === current_password) {
          Swal.showValidationMessage(this.locale.t('settings.passwordSame'));
          return false;
        }
        try {
          await firstValueFrom(this.http.patch<void>(
            '/api/auth/password', { current_password, new_password, confirm_password },
          ).pipe(timeout(15000)));
          return true;
        } catch (error: unknown) {
          const detail = error instanceof HttpErrorResponse ? error.error?.detail : null;
          if (this.isUnauthorized(error) && detail !== 'Current password is incorrect') {
            this.sessionExpired();
            return false;
          }
          let key = 'settings.updateFailed';
          if (detail === 'Current password is incorrect') key = 'settings.wrongPassword';
          else if (detail === 'Password login not configured') key = 'settings.passwordUnavailable';
          else if (error instanceof HttpErrorResponse && error.status === 429)
            key = 'auth.rateLimited';
          Swal.showValidationMessage(this.locale.t(key));
          return false;
        }
      },
    });
    if (!result.isConfirmed || !result.value || this.account?.id !== accountId) return;
    await Swal.fire({
      titleText: this.locale.t('settings.passwordChanged'),
      text: this.locale.t('settings.otherSessionsRevoked'),
      icon: 'success', background: '#1b1e3e', color: '#f1f3ff',
      confirmButtonText: this.locale.t('settings.done'),
      confirmButtonColor: '#8170d8',
    });
  }

  openAbout(): void {
    this.aboutOpen = true;
    this.aboutError = '';
    this.aboutClaimed = false;
    if (!this.account) return;
    const accountId = this.account.id;
    this.aboutStatusLoading = true;
    this.subscriptions.add(this.http.get<{ claimed: boolean }>(
      STARS_API + '/about-bonus'
    ).pipe(timeout(10000), finalize(() => {
      if (this.account?.id === accountId) this.aboutStatusLoading = false;
    })).subscribe({
      next: state => {
        if (this.account?.id === accountId && this.aboutOpen)
          this.aboutClaimed = state.claimed;
      },
      error: () => {
        if (this.account?.id === accountId && this.aboutOpen)
          this.aboutError = 'about.statusFailed';
      },
    }));
  }

  closeAbout(): void {
    if (this.aboutBusy) return;
    this.aboutOpen = false;
    this.aboutError = '';
  }

  async registerFromAbout(): Promise<void> {
    this.closeAbout();
    await this.openAuthDialog('register');
  }

  claimAboutBonus(): void {
    if (!this.account || !this.aboutOpen || this.aboutBusy ||
        this.aboutStatusLoading || this.aboutClaimed || this.aboutError) return;
    const accountId = this.account.id;
    this.aboutBusy = true;
    this.subscriptions.add(this.http.post<AboutBonusClaim>(
      STARS_API + '/about-bonus', {}
    ).pipe(timeout(15000), finalize(() => {
      if (this.account?.id === accountId) this.aboutBusy = false;
    })).subscribe({
      next: result => {
        if (this.account?.id !== accountId) return;
        this.aboutClaimed = result.claimed;
        this.spendableStars = result.balances;
        this.refreshStats();
      },
      error: err => {
        if (this.isUnauthorized(err)) this.sessionExpired();
        else if (this.account?.id === accountId)
          this.aboutError = 'about.claimFailed';
      },
    }));
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
            (isRegister ? '<div id="quiz-username-suggestions" class="quiz-username-suggestions" role="status"></div>' : '') +
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
            popup?.querySelector<HTMLInputElement>('#quiz-auth-username')?.addEventListener('input', () => {
              popup?.querySelector<HTMLElement>('#quiz-username-suggestions')?.replaceChildren();
            });
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
            if (isRegister && error instanceof HttpErrorResponse && error.status === 409) {
              const host = Swal.getPopup()?.querySelector<HTMLElement>('#quiz-username-suggestions');
              if (host) {
                host.replaceChildren();
                try {
                  const response = await firstValueFrom(this.http.get<{ suggestions: string[] }>(
                    '/api/auth/username-suggestions', { params: { username } }
                  ).pipe(timeout(10000)));
                  const popup = Swal.getPopup();
                  if (popup && host.isConnected && response.suggestions.length) {
                    const caption = document.createElement('p');
                    caption.textContent = this.locale.t('auth.suggestionsIntro');
                    host.appendChild(caption);
                    for (const suggestion of response.suggestions) {
                      const button = document.createElement('button');
                      button.type = 'button';
                      button.className = 'quiz-username-option';
                      button.textContent = suggestion;
                      button.addEventListener('click', () => {
                        const field = popup.querySelector<HTMLInputElement>('#quiz-auth-username');
                        if (field) {
                          field.value = suggestion;
                          field.focus();
                        }
                        host.replaceChildren();
                        Swal.resetValidationMessage();
                      });
                      host.appendChild(button);
                    }
                  }
                } catch {
                  // Registration still reports the original collision;
                  // name suggestions are an optional enhancement.
                }
              }
            }
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
    this.currentVote = 0;
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
        this.currentVote = this.restoreDraftVote(question.question_id);
        this.appealMessage = ''; // Keep the notice while preparing; clear only on the next actual question.
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

  private draftVoteKey(questionId: string): string {
    return DRAFT_VOTE_KEY + this.account?.id + ':' + this.gameId + ':' + questionId;
  }

  private restoreDraftVote(questionId: string): -1 | 0 | 1 {
    try {
      const raw = localStorage.getItem(this.draftVoteKey(questionId));
      return raw === '-1' ? -1 : raw === '1' ? 1 : 0;
    } catch {
      return 0;
    }
  }

  private clearDraftVote(questionId: string): void {
    try { localStorage.removeItem(this.draftVoteKey(questionId)); }
    catch { /* local storage is optional */ }
  }

  voteQuestion(value: -1 | 1): void {
    // A choice on an unanswered card is provisional. No HTTP call, no
    // reputation change, no Judge referral until answer succeeds.
    if (!this.current || !this.gameId || !this.account ||
        this.loading || this.appealBusy || this.perksBusy ||
        this.screen !== 'question') return;
    this.currentVote = this.currentVote === value ? 0 : value;
    try {
      const key = this.draftVoteKey(this.current.question_id);
      if (this.currentVote === 0) localStorage.removeItem(key);
      else localStorage.setItem(key, String(this.currentVote));
    } catch { /* refresh may forget local choice, never creates a DB vote */ }
  }

  answer(choice: Choice): void {
    if (this.loading || this.perksBusy || this.appealBusy || !this.current || this.screen !== 'question' || !this.account) return;
    const submittedQuestionId = this.current.question_id;
    const submittedVote = this.currentVote;
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<AnswerFeedback>(
      GAME_API + '/' + this.gameId + '/answer', {
        question_id: submittedQuestionId,
        answer: choice,
        vote: submittedVote,
      },
    ).pipe(timeout(this.requestTimeoutMs)).subscribe({
      next: (result) => {
        this.clearDraftVote(submittedQuestionId);
        this.currentVote = 0;
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
        this.helperCard.refunded ||
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
    this.aboutClaimed = false;
    this.authLoading = false;
    this.locale.load(account.ui_language || 'en');
    this.discoverResetCapability(account.id);
    this.totalStars = emptyStars();
    if (this.languages.includes(account.preferred_language) && !this.gameId) {
      this.selectedLanguage = account.preferred_language;
    }
    this.refreshStats();
    this.refreshAppealAllowance();
    this.restoreActiveGame();
  }

  private clearAccount(): void {
    this.statsVersion++;
    this.appealAllowance = null;
    this.resetEnabled = false;
    this.restoreVersion++;
    this.restoringGame = false;
    this.account = null;
    this.aboutOpen = false;
    this.aboutClaimed = false;
    this.aboutStatusLoading = false;
    this.aboutBusy = false;
    this.aboutError = '';
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
    this.currentVote = 0;
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
    this.profileMenuOpen = false;
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
    if (run.phase === 'lost') this.restoreLostQuestionForAppeal(run.game_id);
    // Preserve the bookmark so an F5 does not erase the losing-card appeal path.
    if (run.phase !== 'lost') this.clearPendingResults();
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

import { CommonModule } from '@angular/common';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, OnDestroy, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, Subscription, timeout, TimeoutError, timer } from 'rxjs';

type Choice = 'A' | 'B' | 'C' | 'D';
type Screen = 'setup' | 'question' | 'feedback' | 'done';
type StarTier = 'bronze' | 'silver' | 'gold' | 'platinum';
type StarCounts = Record<StarTier, number>;

const emptyStars = (): StarCounts => ({ bronze: 0, silver: 0, gold: 0, platinum: 0 });
const STAR_STORAGE_KEY = 'ai-quiz-game:local-star-totals:v1';

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
  answers: Record<Choice, string>;
  bonus: boolean;
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
  readonly choices: Choice[] = ['A', 'B', 'C', 'D'];
  readonly starTiers: { key: StarTier; label: string }[] = [
    { key: 'bronze', label: 'Bronze' },
    { key: 'silver', label: 'Silver' },
    { key: 'gold', label: 'Gold' },
    { key: 'platinum', label: 'Platinum' },
  ];
  selectedLanguage = 'English';
  screen: Screen = 'setup';
  gameId = '';
  current: PublicQuestion | null = null;
  feedback: AnswerFeedback | null = null;
  loading = false;
  error = '';
  configLoading = true;
  questionLoading = false;
  longWait = false;
  canRetryQuestion = false;
  private warningMs = 8000;
  private requestTimeoutMs = 45000;
  private readonly subscriptions = new Subscription();
  progress = 0;
  stars: string[] = [];
  currentStars: StarCounts = emptyStars();
  totalStars: StarCounts = this.readStoredStars();

  constructor(private readonly http: HttpClient) {}

  ngOnInit(): void {
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
      // Keep finite defaults when running against an older or unavailable backend.
      error: () => {},
    }));
  }

  ngOnDestroy(): void {
    this.subscriptions.unsubscribe();
  }

  get rtl(): boolean { return this.selectedLanguage === 'Hebrew'; }
  get correctAnswerText(): string {
    return this.feedback && this.current
      ? this.current.answers[this.feedback.correct_answer]
      : '';
  }
  get gameEnded(): boolean {
    return this.feedback?.phase === 'completed' || this.feedback?.phase === 'lost';
  }

  startGame(): void {
    if (this.loading || this.configLoading) return;
    if (this.gameId) { this.loadQuestion(); return; }
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<GameInfo>(
      '/api/games', { language: this.selectedLanguage },
    ).pipe(timeout(this.requestTimeoutMs)).subscribe({
      next: (game) => {
        this.gameId = game.game_id;
        this.stars = [];
        this.currentStars = emptyStars();
        this.progress = 0;
        this.feedback = null;
        this.loading = false;
        this.loadQuestion();
      },
      error: () => this.fail('Could not start a game. Check the connection and try again.'),
    }));
  }

  loadQuestion(): void {
    if (!this.gameId || this.loading) return;
    this.loading = true;
    this.questionLoading = true;
    this.longWait = false;
    this.canRetryQuestion = false;
    this.error = '';
    const warning = timer(this.warningMs).subscribe(() => { this.longWait = true; });
    this.subscriptions.add(this.http.get<PublicQuestion>(
      '/api/games/' + this.gameId + '/question',
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
        this.feedback = null;
        this.screen = 'question';
      },
      error: (error: unknown) => {
        this.canRetryQuestion = true;
        if (error instanceof TimeoutError) {
          this.fail('The question is taking too long. It may still be preparing. Retry to retrieve it.');
        } else if (error instanceof HttpErrorResponse && error.status === 502) {
          this.fail('The AI could not prepare a valid question. Please retry.');
        } else if (error instanceof HttpErrorResponse && error.status === 503) {
          this.fail('The question service is unavailable. Wait a moment and retry.');
        } else {
          this.fail('Could not retrieve the question. Check your connection and retry.');
        }
      },
    }));
  }

  retryQuestion(): void {
    if (this.canRetryQuestion && !this.loading) this.loadQuestion();
  }

  answer(choice: Choice): void {
    if (this.loading || !this.current || this.screen !== 'question') return;
    this.loading = true;
    this.error = '';
    this.subscriptions.add(this.http.post<AnswerFeedback>('/api/games/' + this.gameId + '/answer', {
      question_id: this.current.question_id,
      answer: choice,
    }).pipe(timeout(this.requestTimeoutMs)).subscribe({
      next: (result) => {
        this.feedback = result;
        this.progress = result.correct_main_answers;
        if (result.achievement && this.isStarTier(result.achievement)) {
          this.stars.push(result.achievement);
          this.currentStars[result.achievement] += 1;
          this.totalStars[result.achievement] += 1;
          this.storeStars();
        }
        this.screen = 'feedback';
        this.loading = false;
      },
      error: () => this.fail('Could not confirm your answer. It may already have been recorded. Please try again.'),
    }));
  }

  continue(): void {
    if (this.loading) return;
    if (this.gameEnded) {
      this.screen = 'done';
    } else {
      this.loadQuestion();
    }
  }

  restart(): void {
    if (this.loading) return;
    this.canRetryQuestion = false;
    this.longWait = false;
    this.gameId = '';
    this.current = null;
    this.feedback = null;
    this.error = '';
    this.progress = 0;
    this.stars = [];
    this.currentStars = emptyStars();
    this.screen = 'setup';
  }

  // Prototype-only display cache: the backend still decides which stars were earned.
  // This local browser total is NOT an authoritative balance for purchases.
  private isStarTier(value: string): value is StarTier {
    return this.starTiers.some((tier) => tier.key === value);
  }

  private readStoredStars(): StarCounts {
    const result = emptyStars();
    try {
      const saved = JSON.parse(localStorage.getItem(STAR_STORAGE_KEY) ?? 'null');
      if (!saved || typeof saved !== 'object') return result;
      for (const key of Object.keys(result) as StarTier[]) {
        const count = saved[key];
        if (Number.isSafeInteger(count) && count >= 0) result[key] = count;
      }
    } catch {
      // Local storage can be unavailable in private browsing or restricted contexts.
    }
    return result;
  }

  private storeStars(): void {
    try {
      localStorage.setItem(STAR_STORAGE_KEY, JSON.stringify(this.totalStars));
    } catch {
      // The quiz still works if the browser cannot persist display-only totals.
    }
  }

  private fail(message: string): void {
    this.error = message;
    this.loading = false;
  }
}

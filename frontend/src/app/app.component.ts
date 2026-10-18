import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';

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
export class AppComponent {
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
  progress = 0;
  stars: string[] = [];
  currentStars: StarCounts = emptyStars();
  totalStars: StarCounts = this.readStoredStars();

  constructor(private readonly http: HttpClient) {}

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
    if (this.loading) return;
    this.loading = true;
    this.error = '';
    this.http.post<GameInfo>('/api/games', { language: this.selectedLanguage }).subscribe({
      next: (game) => {
        this.gameId = game.game_id;
        this.stars = [];
        this.currentStars = emptyStars();
        this.progress = 0;
        this.feedback = null;
        this.loadQuestion();
      },
      error: () => this.fail('Could not start a game. Is FastAPI running on port 8000?'),
    });
  }

  loadQuestion(): void {
    if (!this.gameId) return;
    this.loading = true;
    this.error = '';
    this.http.get<PublicQuestion>('/api/games/' + this.gameId + '/question').subscribe({
      next: (question) => {
        this.current = question;
        this.feedback = null;
        this.screen = 'question';
        this.loading = false;
      },
      error: () => this.fail('Could not generate a question. Check your AI provider and backend logs.'),
    });
  }

  answer(choice: Choice): void {
    if (this.loading || !this.current || this.screen !== 'question') return;
    this.loading = true;
    this.error = '';
    this.http.post<AnswerFeedback>('/api/games/' + this.gameId + '/answer', {
      question_id: this.current.question_id,
      answer: choice,
    }).subscribe({
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
      error: () => this.fail('Could not submit your answer. Please try again.'),
    });
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

import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Component } from '@angular/core';
import { FormsModule } from '@angular/forms';

type Choice = 'A' | 'B' | 'C' | 'D';
type Screen = 'setup' | 'question' | 'feedback' | 'done';

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
  selectedLanguage = 'English';
  screen: Screen = 'setup';
  gameId = '';
  current: PublicQuestion | null = null;
  feedback: AnswerFeedback | null = null;
  loading = false;
  error = '';
  progress = 0;
  stars: string[] = [];

  constructor(private readonly http: HttpClient) {}

  get rtl(): boolean { return this.selectedLanguage === 'Hebrew'; }
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
        if (result.achievement) this.stars.push(result.achievement);
        this.screen = 'feedback';
        this.loading = false;
      },
      error: () => this.fail('Could not submit your answer. Please try again.'),
    });
  }

  continue(): void {
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
    this.screen = 'setup';
  }

  private fail(message: string): void {
    this.error = message;
    this.loading = false;
  }
}

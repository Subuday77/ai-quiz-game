/** Angular localization client. English source stays on FastAPI.
 * Native language names are never translated.
 * FastAPI owns revision comparison, AI calls and PostgreSQL caching.
 */
import { HttpClient } from '@angular/common/http';
import { Subscription, timeout, timer } from 'rxjs';

export interface UiLanguage {
  code: string;
  nativeName: string;
}
export const UI_LANGUAGES: readonly UiLanguage[] = [
  { code: 'en', nativeName: 'English' },
  { code: 'de', nativeName: 'Deutsch' },
  { code: 'fr', nativeName: 'Français' },
  { code: 'es', nativeName: 'Español' },
  { code: 'it', nativeName: 'Italiano' },
  { code: 'pt', nativeName: 'Português' },
  { code: 'ru', nativeName: 'Русский' },
  { code: 'he', nativeName: 'עברית' },
  { code: 'ar', nativeName: 'العربية' },
  { code: 'ja', nativeName: '日本語' },
  { code: 'zh', nativeName: '中文' },
];

interface DictionaryResponse {
  language_code: string;
  dictionary: Record<string, string>;
  source_version: string;
  fallback: boolean;
  missing_count: number;
}

const isSupported = (code: string): boolean => UI_LANGUAGES.some((item) => item.code === code);

export class LocalizationStore {
  // 'code' and 'dictionary' are a committed, visible pair. Until a complete
  // translation arrives, both remain unchanged: no intermediate English flash.
  code = 'en';
  requestedCode = 'en';
  loading = false;
  fallback = false;
  // A single, always-visible status caption cycles independently of CSS.
  messageIndex = 0;
  private messageCycle: Subscription | null = null;
  private dictionary: Record<string, string> = {};
  private requestVersion = 0;
  private active: Subscription | null = null;
  private readonly maxFallbackPolls = 12;
  private readonly fallbackPollMs = 2500;

  constructor(private readonly http: HttpClient) {}

  get rtl(): boolean {
    return this.code === 'he' || this.code === 'ar';
  }

  private startMessageCycle(): void {
    this.stopMessageCycle();
    this.messageIndex = 0;
    this.messageCycle = timer(2000, 2000).subscribe(() => {
      if (this.loading) this.messageIndex = (this.messageIndex + 1) % UI_LANGUAGES.length;
    });
  }

  private stopMessageCycle(): void {
    this.messageCycle?.unsubscribe();
    this.messageCycle = null;
  }

  load(code: string, onSettled?: (successful: boolean) => void, force = false): void {
    if (!isSupported(code)) code = 'en';
    if (this.code === code && Object.keys(this.dictionary).length > 0 &&
        !this.loading && !force) {
      onSettled?.(true);
      return;
    }

    // Cancel any previous pending dictionary/poll. A late response can never
    // overwrite a newer choice, and the old UI keeps its own dictionary/dir.
    this.requestedCode = code;
    this.loading = true;
    this.fallback = false;
    this.startMessageCycle();
    const version = ++this.requestVersion;
    this.active?.unsubscribe();
    const job = new Subscription();
    this.active = job;

    const fail = (): void => {
      if (version !== this.requestVersion || job.closed) return;
      this.loading = false;
      this.stopMessageCycle();
      this.fallback = true;
      this.requestedCode = this.code;
      onSettled?.(false);
    };

    const request = (pollCount: number): void => {
      if (job.closed || version !== this.requestVersion) return;
      const pending = this.http.get<DictionaryResponse>(
        '/api/localization/dictionary/' + encodeURIComponent(code),
      ).pipe(timeout(55000)).subscribe({
        next: (result) => {
          if (version !== this.requestVersion || job.closed) return;
          if (result.language_code !== code || !result.dictionary) {
            fail();
            return;
          }
          // When another worker is translating or the Localizer returned a
          // partial/English fallback, hold the old UI and poll a bounded time.
          if (result.fallback || result.missing_count > 0) {
            if (pollCount >= this.maxFallbackPolls) {
              fail();
            } else {
              job.add(timer(this.fallbackPollMs).subscribe(() => request(pollCount + 1)));
            }
            return;
          }

          // Commit new language + content together in one Angular turn.
          this.dictionary = { ...result.dictionary };
          this.code = code;
          this.requestedCode = code;
          this.fallback = false;
          this.loading = false;
          this.stopMessageCycle();
          onSettled?.(true);
        },
        error: fail,
      });
      job.add(pending);
    };
    request(0);
  }

  t(key: string): string {
    return this.dictionary[key] ?? key.split('.').pop()!.replace(/([A-Z])/g, ' $1').trim();
  }

  format(key: string, args: Record<string, string | number>): string {
    return this.t(key).replace(/\{\{([a-zA-Z0-9_]+)\}\}/g,
      (original, keyName: string) => Object.prototype.hasOwnProperty.call(args, keyName)
        ? String(args[keyName]) : original);
  }

  destroy(): void {
    this.stopMessageCycle();
    this.active?.unsubscribe();
    this.requestVersion++;
  }
}

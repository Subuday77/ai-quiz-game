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
  // Per-app-session RAM cache. Only complete, validated server dictionaries
  // enter it. A new browser page creates a new store and revalidates via API.
  private readonly ready = new Map<string, Record<string, string>>();
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

  /** Evict a cached language after a successful server-side translation reset.
   * 'all' clears the session cache; the canonical English source remains in
   * PostgreSQL/API and can be fetched again if needed.
   */
  invalidate(code: string | 'all'): void {
    if (code === 'all') this.ready.clear();
    else this.ready.delete(code);
  }

  load(code: string, onSettled?: (successful: boolean) => void, force = false): void {
    if (!isSupported(code)) code = 'en';
    // A reset or explicit refresh must never re-use a pre-reset dictionary.
    if (force) this.ready.delete(code);
    const cached = force ? undefined : this.ready.get(code);
    if (cached) {
      // A cache hit is a synchronous, atomic switch: no HTTP request, no
      // overlay, and no flash of another language or text direction.
      ++this.requestVersion;
      this.active?.unsubscribe();
      this.stopMessageCycle();
      this.dictionary = cached;
      this.code = code;
      this.requestedCode = code;
      this.loading = false;
      this.fallback = false;
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

          // Only complete server dictionaries enter the RAM cache.
          // Commit language and strings atomically in the same Angular turn.
          const completed = { ...result.dictionary };
          this.ready.set(code, completed);
          this.dictionary = completed;
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
    this.ready.clear();
    this.requestVersion++;
  }
}

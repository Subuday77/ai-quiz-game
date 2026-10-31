"""Angular localization client — the source English dictionary stays on FastAPI.

Native language names are fixed labels and MUST never go through AI translation.
The server handles revision comparison, paid translation calls and PostgreSQL caching.
"""
import { HttpClient } from '@angular/common/http';
import { Subscription, timeout } from 'rxjs';

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
  code = 'en';
  loading = true;
  fallback = false;
  private dictionary: Record<string, string> = {};
  private requestVersion = 0;
  private active: Subscription | null = null;

  constructor(private readonly http: HttpClient) {}

  get rtl(): boolean {
    return this.code === 'he' || this.code === 'ar';
  }

  load(code: string): void {
    if (!isSupported(code)) code = 'en';
    this.code = code;
    this.loading = true;
    this.fallback = false;
    const version = ++this.requestVersion;
    this.active?.unsubscribe();
    this.active = this.http.get<DictionaryResponse>(
      '/api/localization/dictionary/' + encodeURIComponent(code),
    ).pipe(timeout(55000)).subscribe({
      next: (result) => {
        if (version !== this.requestVersion || result.language_code !== code) return;
        this.dictionary = result.dictionary;
        this.fallback = result.fallback;
        this.loading = false;
      },
      error: () => {
        if (version !== this.requestVersion) return;
        // Keep last known strings when the API is down.
        this.loading = false;
        this.fallback = true;
      },
    });
  }

  t(key: string): string {
    // Missing strings are supplied as English by FastAPI. During first load,
    // display a neutral readable label rather than literal dotted API keys.
    return this.dictionary[key] ?? key.split('.').pop()!.replace(/([A-Z])/g, ' $1').trim();
  }

  format(key: string, args: Record<string, string | number>): string {
    return this.t(key).replace(/\{\{([a-zA-Z0-9_]+)\}\}/g,
      (original, keyName: string) => Object.prototype.hasOwnProperty.call(args, keyName)
        ? String(args[keyName]) : original);
  }

  destroy(): void {
    this.active?.unsubscribe();
  }
}

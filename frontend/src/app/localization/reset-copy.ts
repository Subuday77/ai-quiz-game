/** English-only developer reset messages. Not part of en.json and never
 * sent to the AI Localizer or affected by the selected UI language.
 */
export const RESET_COPY = {
  button: 'Reset translations (dev)',
  title: 'Remove saved translations?',
  explain: 'The site saves translations in a shared database. Resetting a language deletes its saved translations for everyone. When that language is opened again, AI Localizer will translate it again, which can cost money. User accounts, questions, and game results will NOT be deleted.',
  all: 'All translated languages',
  confirm: 'Remove saved translations',
  cancel: 'Cancel',
  done: 'Translations removed',
  result: (count: number): string =>
    'Removed ' + count + ' saved strings. The next visit will request fresh translations.',
  failed: 'Could not reset translations. Please try again.',
} as const;

/** The reset dialog is English-only, including the selectable language names.
 * The ordinary interface-language dropdown still uses each native name.
 */
export const RESET_LANGUAGE_NAMES: Readonly<Record<string, string>> = {
  de: 'German',
  fr: 'French',
  es: 'Spanish',
  it: 'Italian',
  pt: 'Portuguese',
  ru: 'Russian',
  he: 'Hebrew',
  ar: 'Arabic',
  ja: 'Japanese',
  zh: 'Chinese',
};

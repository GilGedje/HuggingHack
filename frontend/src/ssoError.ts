/**
 * The sign-in page's message for a single sign-on failure. The callback reports
 * it in the address (`#/?sso_error=...`), which anyone can write into a link, so
 * the value only ever picks one of these fixed messages and is never shown.
 */
const MESSAGES: Record<string, string> = {
  setup_required: 'Create the owner account with a password first; then single sign-on is available.',
  expired: 'This sign-in link expired or was already used. Try again.',
  other_browser: 'Sign-in must finish in the same browser that started it. Try again.',
  refused: 'The identity provider refused the sign-in.',
  timeout: 'The sign-in took too long. Try again.',
  no_code: 'The identity provider did not return a sign-in code.',
  provider: 'The identity provider could not be reached or sent an answer HuggingHack could not verify.',
  missing_groups: 'The identity provider did not send your groups, so membership could not be checked.',
  not_allowed: 'Your account is not in a group allowed to use HuggingHack.',
  account: 'Your HuggingHack account could not be created or is disabled. Ask an administrator.',
}

const GENERIC = 'Single sign-on did not finish. Try again, or ask an administrator if it keeps happening.'

/** Older servers send the sentence itself; the fixed ones map to their code. */
const SENTENCES: Array<[RegExp, string]> = [
  [/^Create the owner account with a password first\b/, 'setup_required'],
  [/^This sign-in link expired or was already used\. Try again\.$/, 'expired'],
  [/^Sign-in must finish in the same browser that started it\. Try again\.$/, 'other_browser'],
  [/^The identity provider refused the sign-in( \([a-z_]+\))?\.$/, 'refused'],
  [/^The sign-in took too long\. Try again\.$/, 'timeout'],
  [/^The identity provider did not return a sign-in code\.$/, 'no_code'],
  [/^Your account is not in a group allowed to use HuggingHack\.$/, 'not_allowed'],
]

export function ssoErrorMessage(value: string): string {
  const code = value.trim()
  if (!code) return ''
  if (Object.hasOwn(MESSAGES, code)) return MESSAGES[code]
  const known = SENTENCES.find(([pattern]) => pattern.test(code))
  return known ? MESSAGES[known[1]] : GENERIC
}

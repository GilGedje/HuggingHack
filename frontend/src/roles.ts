import type { Role } from './types'

export const ROLE_LABELS: Record<Role, string> = { admin: 'Administrator', member: 'Member', viewer: 'Viewer' }

/** What each server role may do, in the words a confirmation uses. */
const ROLE_POWERS: Record<Role, string> = {
  admin:
    'Administrators manage every account, organization, storage location, and runtime, and can see and pull every repository, private ones included.',
  member: 'Members upload models and change their own repositories, rescan storage, and download from Hugging Face.',
  viewer: 'Viewers only browse, save, and pull models; they cannot upload or change anything.',
}

/** The question asked before an account's server role changes. Anything that
 * gives or takes away administrator rights is marked as dangerous. */
export function roleConfirmation(username: string, from: Role, to: Role) {
  const label = ROLE_LABELS[to].toLowerCase()
  const article = to === 'admin' ? 'an' : 'a'
  const lost =
    from === 'admin'
      ? ' They lose the admin pages and access to private repositories that are not theirs or their organizations’.'
      : ''
  return {
    eyebrow: 'Change role',
    title: `Make ${username} ${article} ${label}?`,
    message: `${ROLE_POWERS[to]}${lost}`,
    confirmLabel: `Make ${label}`,
    danger: from === 'admin' || to === 'admin',
  }
}

/** The question asked before an account is disabled. */
export function disableConfirmation(username: string) {
  return {
    eyebrow: 'Disable account',
    title: `Disable ${username}?`,
    message:
      'They are signed out everywhere and cannot sign in, and their API tokens stop working until the account is enabled again. Their repositories stay.',
    confirmLabel: 'Disable account',
    danger: true,
  }
}

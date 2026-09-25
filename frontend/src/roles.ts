import type { OrganizationMember, OrganizationRole, Role } from './types'

export const ROLE_LABELS: Record<Role, string> = { admin: 'Administrator', member: 'Member', viewer: 'Viewer' }

export const ORG_ROLE_LABELS: Record<OrganizationRole, string> = {
  admin: 'Admin',
  write: 'Write',
  read: 'Read',
}

/** Who holds each organization role, as a noun for running text. */
const ORG_ROLE_NOUNS: Record<OrganizationRole, string> = { admin: 'an admin', write: 'a writer', read: 'a reader' }

const ORG_ROLE_POWERS: Record<OrganizationRole, string> = {
  admin: 'Admins manage members and settings, change visibility, and delete repositories.',
  write: 'Writers create repositories, upload changes, and see private ones.',
  read: 'Readers see and pull the repositories shared with the organization.',
}

/** An organization role as it acts: Viewers on the server only read, whatever
 * role the organization gave them (the server's `effective_org_role`). */
export function effectiveOrgRole(role: OrganizationRole, serverRole?: Role | null): OrganizationRole {
  return serverRole === 'viewer' ? 'read' : role
}

/** Admins who can act as one, as the server counts them (`ORG_ROLE_ACTS`):
 * disabled accounts and server Viewers keep their row but do not count. */
export function actingOrgAdmins(members: OrganizationMember[]): number {
  return members.filter((member) => member.role === 'admin' && !member.disabled && member.server_role !== 'viewer').length
}

/** The question asked before a member's organization role changes, or before
 * someone joins as an admin. Anything that gives or takes away admin rights is
 * marked as dangerous. */
export function orgRoleConfirmation(username: string, organization: string, from: OrganizationRole | null, to: OrganizationRole) {
  const noun = ORG_ROLE_NOUNS[to]
  const lost =
    from === 'admin'
      ? ' They stop managing its members and settings.'
      : from === 'write' && to === 'read'
        ? ' They can no longer upload to it or see its private repositories.'
        : ''
  const already = from ? `${username} is already a member, as ${ORG_ROLE_LABELS[from]}. ` : ''
  return {
    eyebrow: from ? 'Change role' : 'Add member',
    title: from ? `Make ${username} ${noun} of ${organization}?` : `Add ${username} to ${organization} as ${noun}?`,
    message: `${already}${ORG_ROLE_POWERS[to]}${lost}`,
    confirmLabel: from ? `Make ${noun.split(' ')[1]}` : `Add as ${noun.split(' ')[1]}`,
    danger: from === 'admin' || to === 'admin',
  }
}

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

import type { Visibility } from './types'

export const VISIBILITIES: Visibility[] = ['private', 'organization', 'public']

const LABELS: Record<Visibility, string> = {
  private: 'Private',
  organization: 'Organization',
  public: 'Public',
}

export function visibilityLabel(visibility: Visibility): string {
  return LABELS[visibility] || visibility
}

/** Who can see a repository at this visibility; `organization` is the owning
 * organization's name, or null for a personal repository. */
export function visibilityAudience(visibility: Visibility, organization: string | null): string {
  if (visibility === 'public') return 'Every account, plus anonymous pulls from vLLM and the hf CLI'
  if (!organization) return visibility === 'private' ? 'Only you and server admins' : 'Only for organization repositories'
  return visibility === 'private' ? `Admins and writers of ${organization}, and server admins` : `Every member of ${organization}`
}

/** Organization visibility needs an organization to be about. */
export function visibilityAllowed(visibility: Visibility, organization: string | null): boolean {
  return visibility !== 'organization' || Boolean(organization)
}

/** The question asked before a repository's visibility changes: who will see it
 * afterwards, in words. Going public is marked as dangerous. */
export function visibilityConfirmation(repoId: string, next: Visibility, organization: string | null) {
  const audience = visibilityAudience(next, organization)
  if (next === 'public') {
    return {
      eyebrow: 'Change visibility',
      title: `Make ${repoId} public?`,
      message:
        'Every account on this server will be able to find and read it, and vLLM, git, and the hf CLI ' +
        'will pull it without a token. Anyone who can reach the server can download its files.',
      confirmLabel: 'Make public',
      danger: true,
    }
  }
  return {
    eyebrow: 'Change visibility',
    title: next === 'private' ? `Make ${repoId} private?` : `Share ${repoId} with ${organization} only?`,
    message: `Who can see it afterwards: ${audience[0].toLowerCase()}${audience.slice(1)}. Everyone else loses access, including pulls without a token.`,
    confirmLabel: next === 'private' ? 'Make private' : 'Share with organization',
  }
}

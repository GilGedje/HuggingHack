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
  if (!organization) return visibility === 'private' ? 'Only you' : 'Only for organization repositories'
  return visibility === 'private' ? `Admins and writers of ${organization}` : `Every member of ${organization}`
}

/** Organization visibility needs an organization to be about. */
export function visibilityAllowed(visibility: Visibility, organization: string | null): boolean {
  return visibility !== 'organization' || Boolean(organization)
}

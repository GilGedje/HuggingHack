import { createContext, useContext, type ReactNode } from 'react'
import type { User } from './types'

interface Access {
  user: User
  capabilities: Set<string>
  can: (capability: string) => boolean
  refresh: () => void
}

const AccessContext = createContext<Access | null>(null)

/** Capabilities come from the server, which enforces the same table. */
export function AccessProvider({
  user,
  capabilities,
  refresh,
  children,
}: {
  user: User
  capabilities: string[]
  refresh: () => void
  children: ReactNode
}) {
  const set = new Set(capabilities)
  return (
    <AccessContext.Provider value={{ user, capabilities: set, can: (item) => set.has(item), refresh }}>
      {children}
    </AccessContext.Provider>
  )
}

export function useAccess(): Access {
  const value = useContext(AccessContext)
  if (!value) throw new Error('useAccess must be used inside AccessProvider')
  return value
}

export const ADMIN_CAPABILITIES = ['users.manage', 'orgs.manage', 'storage.view', 'settings.view', 'runtimes.use']

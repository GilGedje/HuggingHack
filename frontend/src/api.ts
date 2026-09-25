import type {
  AccountOverview,
  AccountSession,
  AdminOrganizationPage,
  AdminOrganizationQuery,
  AdminUserPage,
  AdminUserQuery,
  ApiToken,
  Organization,
  OrganizationDetails,
  OrganizationRole,
  UploadNamespace,
  PermissionMatrix,
  ServerSettings,
  UserPreferences,
  AuthStatus,
  ChangeSession,
  CommitDetail,
  CommitSummary,
  Collection,
  Health,
  LibraryModelDetails,
  LibrarySearchResult,
  LocalModel,
  LocalModelDetails,
  OwnedRepository,
  RuntimeJob,
  RuntimeTarget,
  SavedModel,
  StorageOption,
  StorageOverview,
  User,
} from './types'

let csrfToken: string | null = null

function applyAuth(status: AuthStatus): AuthStatus {
  csrfToken = status.csrf_token
  return status
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && typeof init.body === 'string' && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (csrfToken && init?.method && !['GET', 'HEAD'].includes(init.method)) {
    headers.set('X-CSRF-Token', csrfToken)
  }
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event('hugginghack:unauthorized'))
    throw new Error(payload.detail || `Request failed with status ${response.status}`)
  }
  return payload as T
}

const repoPath = (repoId: string) =>
  repoId
    .split('/')
    .map((part) => encodeURIComponent(part))
    .join('/')

export const api = {
  authStatus: () => request<AuthStatus>('/api/auth/status').then(applyAuth),
  setup: (payload: { username: string; display_name: string; password: string }) =>
    request<AuthStatus>('/api/auth/setup', {
      method: 'POST',
      body: JSON.stringify(payload),
    }).then(applyAuth),
  login: (payload: { username: string; password: string }) =>
    request<AuthStatus>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify(payload),
    }).then(applyAuth),
  logout: () =>
    request<{ status: string }>('/api/auth/logout', { method: 'POST' }).finally(() => {
      csrfToken = null
    }),
  users: () => request<{ items: User[] }>('/api/users'),
  createUser: (payload: {
    username: string
    display_name: string
    password: string
    role?: 'admin' | 'member' | 'viewer'
    email?: string
    organizations?: Array<{ organization: string; role: OrganizationRole }>
  }) => request<User>('/api/users', { method: 'POST', body: JSON.stringify(payload) }),
  account: () => request<AccountOverview>('/api/account'),
  updateProfile: (payload: { display_name: string; email?: string | null }) =>
    request<User>('/api/account/profile', { method: 'PATCH', body: JSON.stringify(payload) }),
  updatePreferences: (payload: Partial<Record<keyof UserPreferences, string | null>>) =>
    request<UserPreferences>('/api/account/preferences', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  sessions: () => request<{ items: AccountSession[] }>('/api/account/sessions'),
  revokeSession: (sessionId: string) =>
    request<{ status: string }>(`/api/account/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),
  revokeOtherSessions: () =>
    request<{ status: string }>('/api/account/sessions/revoke-others', { method: 'POST' }),
  tokens: () => request<{ items: ApiToken[] }>('/api/account/tokens'),
  createToken: (payload: { name: string; scope: 'read' | 'write'; expires_in_days: number | null }) =>
    request<ApiToken>('/api/account/tokens', { method: 'POST', body: JSON.stringify(payload) }),
  deleteToken: (tokenId: string) =>
    request<{ status: string }>(`/api/account/tokens/${encodeURIComponent(tokenId)}`, {
      method: 'DELETE',
    }),
  adminUsers: (query: AdminUserQuery) => {
    const params = new URLSearchParams({ page: String(query.page), per_page: String(query.per_page), sort: query.sort })
    if (query.q.trim()) params.set('q', query.q.trim())
    if (query.role) params.set('role', query.role)
    if (query.status) params.set('status', query.status)
    return request<AdminUserPage>(`/api/admin/users?${params.toString()}`)
  },
  adminUpdateUser: (
    userId: string,
    payload: { role?: string; disabled?: boolean; display_name?: string; email?: string | null },
  ) =>
    request<User>(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  adminResetPassword: (userId: string, newPassword: string) =>
    request<{ status: string }>(`/api/admin/users/${encodeURIComponent(userId)}/password`, {
      method: 'POST',
      body: JSON.stringify({ new_password: newPassword }),
    }),
  adminRevoke: (userId: string, payload: { sessions: boolean; tokens: boolean }) =>
    request<{ status: string }>(`/api/admin/users/${encodeURIComponent(userId)}/revoke`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  adminDeleteUser: (userId: string) =>
    request<{ status: string }>(`/api/admin/users/${encodeURIComponent(userId)}`, {
      method: 'DELETE',
    }),
  organizations: () => request<{ items: Organization[] }>('/api/organizations'),
  adminOrganizations: (query: AdminOrganizationQuery) => {
    const params = new URLSearchParams({ page: String(query.page), per_page: String(query.per_page), sort: query.sort })
    if (query.q.trim()) params.set('q', query.q.trim())
    if (query.filter) params.set('filter', query.filter)
    return request<AdminOrganizationPage>(`/api/admin/organizations?${params.toString()}`)
  },
  organization: (name: string) =>
    request<OrganizationDetails>(`/api/organizations/${encodeURIComponent(name)}`),
  createOrganization: (payload: { name: string; display_name?: string; description?: string }) =>
    request<OrganizationDetails>('/api/organizations', { method: 'POST', body: JSON.stringify(payload) }),
  updateOrganization: (name: string, payload: { display_name?: string; description?: string }) =>
    request<OrganizationDetails>(`/api/organizations/${encodeURIComponent(name)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteOrganization: (name: string) =>
    request<{ status: string }>(`/api/organizations/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  setOrganizationMember: (name: string, username: string, role: string) =>
    request<OrganizationDetails>(
      `/api/organizations/${encodeURIComponent(name)}/members/${encodeURIComponent(username)}`,
      { method: 'PUT', body: JSON.stringify({ role }) },
    ),
  removeOrganizationMember: (name: string, username: string) =>
    request<OrganizationDetails>(
      `/api/organizations/${encodeURIComponent(name)}/members/${encodeURIComponent(username)}`,
      { method: 'DELETE' },
    ),
  uploadNamespaces: () => request<{ items: UploadNamespace[] }>('/api/uploads/namespaces'),
  permissions: () => request<PermissionMatrix>('/api/admin/permissions'),
  serverSettings: () => request<ServerSettings>('/api/admin/server'),
  changePassword: (payload: { current_password: string; new_password: string }) =>
    request<{ status: string }>('/api/account/password', {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  health: () => request<Health>('/api/health'),
  libraryModels: (params: URLSearchParams) =>
    request<LibrarySearchResult>(`/api/library/models?${params.toString()}`),
  libraryModelDetails: (repoId: string) =>
    request<LibraryModelDetails>(`/api/library/models/${repoPath(repoId)}`),
  updateModelHardware: (repoId: string, hardware: string[]) =>
    request<{ repo_id: string; hardware: string[] }>(
      `/api/library/hardware?repo_id=${encodeURIComponent(repoId)}`,
      { method: 'PUT', body: JSON.stringify({ hardware }) },
    ),
  localModels: (query = '') =>
    request<{ items: LocalModel[]; count: number; total_bytes: number }>(
      `/api/local-models?query=${encodeURIComponent(query)}`,
    ),
  scanLocalModels: () =>
    request<{ count: number; models: LocalModel[]; scanned_at: string }>(
      '/api/local-models/scan',
      { method: 'POST' },
    ),
  localModelDetails: (repoId: string) =>
    request<LocalModelDetails>(`/api/local-models/${repoPath(repoId)}`),
  restoreLocalModel: (repoId: string) =>
    request<LocalModelDetails>(`/api/local-models/${repoPath(repoId)}/restore`, {
      method: 'POST',
    }),
  evictLocalModelCache: (repoId: string) =>
    request<{ status: string; model: LocalModel }>(
      `/api/local-models/${repoPath(repoId)}/cache`,
      { method: 'DELETE' },
    ),
  storageTargets: () => request<StorageOverview>('/api/storage/targets'),
  storageOptions: () =>
    request<{ default: string; items: StorageOption[] }>('/api/storage/options'),
  runtimeTargets: () => request<{ items: RuntimeTarget[] }>('/api/runtimes'),
  runtimeJobs: (limit = 100) =>
    request<{ items: RuntimeJob[]; active: number }>(
      `/api/runtime-jobs?limit=${encodeURIComponent(limit)}`,
    ),
  runtimeJob: (jobId: string) =>
    request<RuntimeJob>(`/api/runtime-jobs/${encodeURIComponent(jobId)}`),
  loadRuntime: (
    targetId: string,
    payload: {
      repo_id: string
      runtime_model_name?: string
      source_file?: string
    },
  ) =>
    request<RuntimeJob>(`/api/runtimes/${encodeURIComponent(targetId)}/load`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  collections: () => request<{ items: Collection[] }>('/api/collections'),
  createCollection: (payload: { name: string; description?: string }) =>
    request<Collection>('/api/collections', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  deleteCollection: (collectionId: string) =>
    request<{ status: string }>(`/api/collections/${encodeURIComponent(collectionId)}`, {
      method: 'DELETE',
    }),
  savedModels: (query = '', collectionId = '') => {
    const params = new URLSearchParams()
    if (query) params.set('query', query)
    if (collectionId) params.set('collection_id', collectionId)
    return request<{ items: SavedModel[]; count: number }>(
      `/api/saved-models?${params.toString()}`,
    )
  },
  saveModel: (payload: {
    repo_id: string
    note?: string
    collection_ids?: string[]
    metadata?: Record<string, unknown>
  }) =>
    request<SavedModel>('/api/saved-models', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  unsaveModel: (repoId: string) =>
    request<{ status: string }>(`/api/saved-models/${repoPath(repoId)}`, {
      method: 'DELETE',
    }),
  uploadRepositories: () =>
    request<{ items: OwnedRepository[] }>('/api/uploads/repositories'),
  createUploadRepository: (payload: {
    slug: string
    description?: string
    visibility?: 'private' | 'shared'
    storage_target?: string
    namespace?: string
  }) =>
    request<OwnedRepository>('/api/uploads/repositories', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateUploadRepository: (
    repoId: string,
    payload: { description: string; visibility: 'private' | 'shared' },
  ) =>
    request<OwnedRepository>(
      `/api/uploads/repositories?repo_id=${encodeURIComponent(repoId)}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    ),
  finalizeUploadRepository: (repoId: string) =>
    request<OwnedRepository>(
      `/api/uploads/repositories/finalize?repo_id=${encodeURIComponent(repoId)}`,
      { method: 'POST' },
    ),
  deleteUploadRepository: (repoId: string, confirmation: string) =>
    request<{ status: string }>(
      `/api/uploads/repositories?repo_id=${encodeURIComponent(repoId)}`,
      {
        method: 'DELETE',
        body: JSON.stringify({ confirmation }),
      },
    ),
  uploadFile: (
    repoId: string,
    filePath: string,
    file: File,
    chunkBytes: number,
    onProgress: (uploaded: number) => void,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({ repo_id: repoId, path: filePath })
    return uploadResumable(
      `/api/uploads/repositories/files/status?${params.toString()}`,
      `/api/uploads/repositories/files?${params.toString()}`,
      filePath,
      file,
      chunkBytes,
      onProgress,
      signal,
    )
  },
  finalizeUpload: (repoId: string, payload: { message?: string; description?: string }) =>
    request<OwnedRepository>(
      `/api/uploads/repositories/finalize?repo_id=${encodeURIComponent(repoId)}`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  startChange: (repoId: string) =>
    request<ChangeSession>('/api/repos/changes', {
      method: 'POST',
      body: JSON.stringify({ repo_id: repoId }),
    }),
  uploadChangeFile: (
    sessionId: string,
    filePath: string,
    file: File,
    chunkBytes: number,
    onProgress: (uploaded: number) => void,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({ path: filePath })
    const base = `/api/repos/changes/${encodeURIComponent(sessionId)}/files`
    return uploadResumable(
      `${base}/status?${params.toString()}`,
      `${base}?${params.toString()}`,
      filePath,
      file,
      chunkBytes,
      onProgress,
      signal,
    )
  },
  commitChange: (
    sessionId: string,
    payload: { message: string; description?: string; deletions?: string[] },
  ) =>
    request<{ commit: CommitSummary | null; model: unknown }>(
      `/api/repos/changes/${encodeURIComponent(sessionId)}/commit`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  abortChange: (sessionId: string) =>
    request<{ status: string }>(`/api/repos/changes/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),
  commits: (repoId: string, limit = 50, offset = 0) =>
    request<{ items: CommitSummary[]; total: number }>(
      `/api/library/commits?${new URLSearchParams({
        repo_id: repoId,
        limit: String(limit),
        offset: String(offset),
      }).toString()}`,
    ),
  commit: (repoId: string, commitId: string) =>
    request<CommitDetail>(
      `/api/library/commit?${new URLSearchParams({ repo_id: repoId, commit_id: commitId }).toString()}`,
    ),
  fileUrl: (repoId: string, path: string) =>
    `/api/library/file?${new URLSearchParams({ repo_id: repoId, path }).toString()}`,
}

async function uploadResumable(
  statusUrl: string,
  putUrl: string,
  filePath: string,
  file: File,
  chunkBytes: number,
  onProgress: (uploaded: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  const status = await request<{ offset: number; complete: boolean }>(statusUrl, { signal })
  if (status.complete && status.offset === file.size) {
    onProgress(file.size)
    return
  }
  if (status.complete || status.offset > file.size) {
    throw new Error(`A different completed or partial file already exists at ${filePath}.`)
  }
  let offset = status.offset
  onProgress(offset)
  do {
    const chunk = file.slice(offset, Math.min(file.size, offset + chunkBytes))
    const headers = new Headers({
      'Content-Type': 'application/octet-stream',
      'Upload-Offset': String(offset),
      'Upload-Length': String(file.size),
    })
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
    const response = await fetch(putUrl, {
      method: 'PUT',
      credentials: 'same-origin',
      headers,
      body: chunk,
      signal,
    })
    const result = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(result.detail || `Upload failed with status ${response.status}`)
    }
    offset = result.offset
    onProgress(offset)
    if (file.size === 0) break
  } while (offset < file.size)
}

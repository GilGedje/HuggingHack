export interface StorageHealth {
  path: string
  total_bytes: number
  used_bytes: number
  free_bytes: number
  writable: boolean
}

export interface ObjectStorageHealth {
  backend: 'filesystem' | 's3'
  enabled: boolean
  connected: boolean
  bucket?: string | null
  prefix?: string | null
  endpoint?: string | null
  error?: string | null
}

export interface Health {
  status: string
  app: string
  version: string
  database_backend: 'sqlite' | 'postgresql'
  storage: StorageHealth
  object_storage: ObjectStorageHealth
  hf_token_configured: boolean
  hf_endpoint: string
  accounts_enabled: boolean
  upload_chunk_bytes: number
  max_upload_size_bytes: number
  runtime_target_count: number
  runtime_api_token_configured: boolean
  hub_api_enabled?: boolean
  public_url?: string | null
}

export type Role = 'admin' | 'member' | 'viewer'

export interface UserPreferences {
  theme?: 'system' | 'light' | 'dark'
  catalog_sort?: 'updated' | 'name' | 'size' | 'parameters'
  default_storage_target?: string
}

export interface User {
  id: string
  username: string
  display_name: string
  role: Role
  created_at: string
  updated_at?: string
  email?: string | null
  disabled?: boolean
  last_login_at?: string | null
  preferences?: UserPreferences
  auth_provider?: string
}

export interface AuthStatus {
  accounts_enabled: boolean
  oidc?: { enabled: boolean; name: string }
  setup_required: boolean
  user: User | null
  capabilities: string[]
  csrf_token: string | null
}

export interface AccountOverview {
  user: User
  capabilities: Array<{ id: string; description: string }>
  accounts_enabled: boolean
  local_password: boolean
  repositories: string[]
  organizations: Array<{ id: string; name: string; display_name: string; role: OrganizationRole }>
  saved_count: number
}

export interface AccountSession {
  id: string
  created_at: string
  expires_at: string
  last_seen_at?: string | null
  user_agent?: string | null
  ip?: string | null
  current: boolean
}

export interface ApiToken {
  id: string
  name: string
  prefix: string
  scope: 'read' | 'write'
  created_at: string
  last_used_at?: string | null
  expires_at?: string | null
  token?: string
}

export interface AdminUser extends User {
  sessions: number
  tokens: number
  repositories: number
}

export type AdminUserSort = 'role' | 'name' | 'last_login' | 'newest'

export interface AdminUserQuery {
  q: string
  role: '' | Role
  status: '' | 'active' | 'disabled'
  sort: AdminUserSort
  page: number
  per_page: number
}

export interface AdminUserPage {
  items: AdminUser[]
  total: number
  page: number
  per_page: number
  pages: number
  counts: Record<'all' | Role | 'active' | 'disabled', number>
  accounts_enabled: boolean
}

export interface PermissionMatrix {
  roles: Array<{ id: Role; description: string; capabilities: string[] }>
  capabilities: Array<{ id: string; description: string }>
}

export interface ServerSettings {
  app: string
  version: string
  accounts: { enabled: boolean; secure_cookies: boolean; session_ttl_hours: number }
  database: { backend: 'sqlite' | 'postgresql'; target: string }
  storage: {
    model_path: string
    data_path: string
    default_target: string
    targets: Array<StorageTargetSummary>
  }
  uploads: { chunk_mb: number; max_file_gb: number }
  pulls: { hub_api_enabled: boolean; public_url?: string | null }
  sso: {
    enabled: boolean
    provider_name: string
    issuer?: string | null
    client_id?: string | null
    client_secret_configured: boolean
    scopes: string
    default_role: string
    allowed_groups: string[]
    redirect_url?: string | null
  }
  hugging_face: {
    downloads_enabled: boolean
    endpoint: string
    token_configured: boolean
    max_concurrent_downloads: number
    workers_per_download: number
  }
  runtimes: { targets: RuntimeTarget[]; api_token_configured: boolean }
}

export interface StorageTargetSummary {
  id: string
  name: string
  kind: 'filesystem' | 's3'
  bucket?: string | null
  prefix?: string | null
  endpoint?: string | null
  region?: string | null
  path?: string | null
  credentials_configured: boolean
}

export interface HubFile {
  path: string
  size: number
  blob_id?: string | null
}

export interface HubModel {
  id: string
  author?: string | null
  pipeline_tag?: string | null
  library_name?: string | null
  tags: string[]
  downloads: number
  downloads_all_time: number
  likes: number
  trending_score: number
  last_modified?: string | null
  created_at?: string | null
  private: boolean
  gated: boolean | string
  sha?: string | null
  license?: string | null
  parameter_count?: number | null
  local?: boolean
  saved?: boolean
}

export interface HubModelDetails extends HubModel {
  revision: string
  files: HubFile[]
  total_bytes: number
  security_status?: unknown
  source_url: string
  model_card?: string | null
}

export type ModelFormat = 'safetensors' | 'gguf' | 'pytorch' | 'onnx' | 'tensorflow' | 'flax'

export interface LibraryModel {
  id: string
  author?: string | null
  pipeline_tag?: string | null
  library_name?: string | null
  tags: string[]
  license?: string | null
  parameter_count?: number | null
  formats: ModelFormat[]
  apps: string[]
  size_bytes: number
  file_count: number
  last_modified?: string | null
  downloaded_at?: string | null
  revision?: string | null
  sha?: string | null
  managed: boolean
  storage_backend: 'filesystem' | 's3'
  storage_target: string
  cached: boolean
  saved: boolean
}

export interface LibraryFacets {
  tasks: string[][]
  libraries: string[][]
  apps: string[][]
}

export interface LibrarySearchResult {
  items: LibraryModel[]
  count: number
  total: number
  total_bytes: number
  facets: LibraryFacets
}

export interface LibraryModelDetails extends LibraryModel {
  files: LibraryFile[]
  latest_commit?: CommitSummary | null
  commit_count: number
  can_edit: boolean
  visibility: 'public' | 'shared' | 'private'
  description: string
  organization?: { name: string; display_name: string } | null
  total_bytes: number
  truncated: boolean
  unsafe_file_count: number
  local_path: string
  remote_uri?: string | null
  source_url?: string | null
  model_card?: string | null
}

export type DownloadMode = 'full' | 'safetensors' | 'gguf' | 'metadata' | 'custom'

export interface DownloadJob {
  id: string
  repo_id: string
  revision: string
  status: 'queued' | 'preparing' | 'downloading' | 'complete' | 'failed' | 'cancelled'
  total_bytes: number
  downloaded_bytes: number
  progress: number
  speed_bps: number
  error?: string | null
  target_path?: string | null
  payload: {
    allow_patterns?: string[]
    ignore_patterns?: string[]
    mode?: DownloadMode
  }
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
  completed_at?: string | null
}

export interface LocalModel {
  repo_id: string
  relative_path: string
  size_bytes: number
  file_count: number
  modified_at: string
  downloaded_at?: string | null
  revision?: string | null
  sha?: string | null
  pipeline_tag?: string | null
  library_name?: string | null
  license?: string | null
  tags: string[]
  config: Record<string, unknown>
  source_url?: string | null
  managed: boolean
  storage_backend: 'filesystem' | 's3'
  cached: boolean
  remote_uri?: string | null
}

export interface LocalFile {
  path: string
  size: number
  modified_at: string
  unsafe_serialization: boolean
}

export interface LocalModelDetails {
  model: LocalModel
  files: LocalFile[]
  unsafe_file_count: number
  truncated: boolean
}

export interface RuntimeTarget {
  id: string
  name: string
  kind: 'ollama' | 'vllm'
  base_url: string
  remote_model_root?: string | null
  authenticated: boolean
  transfer_mode: 'blob-upload' | 'shared-path'
  keep_alive?: string | number | null
}

export interface RuntimeJob {
  id: string
  target_id: string
  target_name: string
  target_kind: 'ollama' | 'vllm'
  repo_id: string
  runtime_model_name: string
  source_file?: string | null
  status: 'queued' | 'preparing' | 'transferring' | 'loading' | 'ready' | 'failed'
  total_bytes: number
  processed_bytes: number
  progress: number
  message: string
  error?: string | null
  created_at: string
  updated_at: string
  completed_at?: string | null
}

export interface Collection {
  id: string
  user_id: string
  name: string
  description: string
  model_count: number
  created_at: string
  updated_at: string
}

export interface SavedModel {
  id: string
  repo_id: string
  note: string
  metadata: {
    author?: string | null
    pipeline_tag?: string | null
    library_name?: string | null
    license?: string | null
    parameter_count?: number | null
    last_modified?: string | null
    local?: boolean
  }
  collections: string[]
  created_at: string
  updated_at: string
}

export interface OwnedRepository {
  id: string
  owner_id: string
  organization_id?: string | null
  organization_name?: string | null
  owner_username: string
  owner_display_name: string
  repo_id: string
  description: string
  visibility: 'private' | 'shared'
  status: 'uploading' | 'ready'
  size_bytes?: number | null
  file_count?: number | null
  modified_at?: string | null
  created_at: string
  updated_at: string
  /** Your role on it: admins manage visibility and deletion, writers upload. */
  my_role: 'admin' | 'write'
}

export interface StorageModel {
  repo_id: string
  size_bytes: number
  file_count: number
  parameter_count?: number | null
  formats: ModelFormat[]
  cached: boolean
  storage_backend: 'filesystem' | 's3'
  modified_at?: string | null
  visibility: 'public' | 'shared' | 'private'
}

export interface StorageCapacity {
  total_bytes: number
  used_bytes: number
  free_bytes: number
}

export interface StorageTarget {
  id: string
  name: string
  kind: 'filesystem' | 's3'
  bucket?: string | null
  prefix?: string | null
  endpoint?: string | null
  region?: string | null
  path?: string | null
  default: boolean
  connected: boolean
  error?: string | null
  model_count: number
  total_bytes: number
  cached_count: number
  models: StorageModel[]
  capacity?: StorageCapacity | null
}

export interface StorageOverview {
  default: string
  cache: StorageCapacity & { path: string; model_count: number; model_bytes: number }
  targets: StorageTarget[]
  conflicts: Array<{ repo_id: string; kept_target: string; skipped_target: string }>
}

export interface StorageOption {
  id: string
  name: string
  kind: 'filesystem' | 's3'
}

export interface CommitSummary {
  id: string
  repo_id: string
  sequence: number
  parent_id?: string | null
  author_id?: string | null
  author_name: string
  message: string
  description: string
  created_at: string
  summary: { added: number; modified: number; deleted: number }
}

export interface CommitChange {
  path: string
  change: 'added' | 'modified' | 'deleted'
  old_size?: number | null
  new_size?: number | null
  binary: boolean
  diff: string[] | null
  truncated?: boolean
  additions?: number
  deletions?: number
}

export interface CommitDetail extends CommitSummary {
  changes: CommitChange[]
}

export interface ChangeSession {
  id: string
  repo_id: string
  user_id: string
  created_at: string
}

export interface LibraryFile extends HubFile {
  last_commit?: { id: string; message: string; created_at: string } | null
}

export type OrganizationRole = 'admin' | 'write' | 'read'

export interface Organization {
  id: string
  name: string
  display_name: string
  description: string
  created_at: string
  updated_at: string
  member_count?: number
  repository_count?: number
  my_role?: OrganizationRole | null
}

export type AdminOrganizationFilter = '' | 'with_repositories' | 'empty' | 'mine'
export type AdminOrganizationSort = 'name' | 'newest' | 'repositories' | 'members'

export interface AdminOrganizationQuery {
  q: string
  filter: AdminOrganizationFilter
  sort: AdminOrganizationSort
  page: number
  per_page: number
}

export interface AdminOrganizationPage {
  items: Organization[]
  total: number
  page: number
  per_page: number
  pages: number
  counts: Record<'all' | 'with_repositories' | 'empty' | 'mine', number>
}

export interface OrganizationMember {
  id: string
  username: string
  display_name: string
  role: OrganizationRole
  server_role: Role
  disabled: boolean
  joined_at: string
}

export interface OrganizationDetails extends Organization {
  can_manage: boolean
  can_upload: boolean
  members: OrganizationMember[]
}

export interface UploadNamespace {
  name: string
  kind: 'user' | 'organization'
  display_name: string
}

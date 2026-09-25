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

export interface DatabaseHealth {
  backend: 'sqlite' | 'postgresql'
  connected: boolean
  error: string | null
}

/** Everyone gets status, app, and version; signed-in accounts get the upload and
 * pull settings; server details need `settings.view`. */
export interface Health {
  status: string
  app: string
  version: string
  database_backend?: 'sqlite' | 'postgresql'
  database?: DatabaseHealth
  storage?: StorageHealth
  object_storage?: ObjectStorageHealth
  hf_token_configured?: boolean
  hf_endpoint?: string
  accounts_enabled?: boolean
  upload_chunk_bytes?: number
  max_upload_size_bytes?: number
  runtime_target_count?: number
  runtime_api_token_configured?: boolean
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
  /** When the profile picture was last set; none means initials. */
  avatar_updated_at?: string | null
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

/** One account as an administrator sees it; tokens carry only their prefix. */
export interface AdminUserDetail {
  user: User
  /** Signs in through the identity provider, which owns the name, email, and password. */
  external: boolean
  accounts_enabled: boolean
  local_password: boolean
  organizations: AccountOverview['organizations']
  repositories: string[]
  sessions: AccountSession[]
  tokens: ApiToken[]
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
    /** Where the site keeps its own files: SYSTEM_STORAGE_TARGET. */
    system: { target: string; location: string; remote: boolean }
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

export type ModelFormat = 'safetensors' | 'gguf' | 'pytorch' | 'onnx' | 'tensorflow' | 'flax'

export type BaseModelRelation = 'quantized' | 'finetune' | 'adapter' | 'merge'

export interface LibraryModel {
  id: string
  author?: string | null
  /** The owner's profile picture, when the user or organization has one. */
  author_avatar?: string | null
  /** The model this one was made from, as its card or a correction names it. */
  base_model?: string | null
  base_model_relation?: BaseModelRelation | null
  pipeline_tag?: string | null
  library_name?: string | null
  tags: string[]
  license?: string | null
  parameter_count?: number | null
  /** Weight number format: bf16, fp8, nvfp4, and a few others. */
  precision?: string | null
  /** Hardware ids the model is tagged as running on. */
  hardware: string[]
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
  /** Only for accounts that may see storage locations. */
  storage_target?: string
  cached: boolean
  saved: boolean
}

/** How many visible models match each filter value. */
export interface LibraryFacets {
  tasks: Record<string, number>
  precision: Record<string, number>
  /** Every known hardware tag, in display order: [id, label, count]. */
  hardware: Array<[string, string, number]>
}

export interface LibrarySearchResult {
  items: LibraryModel[]
  count: number
  total: number
  total_bytes: number
  facets: LibraryFacets
}

/** Where a model comes from and what was made from it, as far as the viewer may see. */
export interface ModelTree {
  base: {
    id: string
    relation: BaseModelRelation
    /** In the library and visible to you; otherwise only its name is known. */
    in_library: boolean
    organization?: { name: string; display_name: string } | null
  } | null
  children: Partial<Record<BaseModelRelation, Array<{ id: string; precision?: string | null; parameter_count?: number | null }>>>
}

export interface LibraryModelDetails extends LibraryModel {
  model_tree: ModelTree
  files: LibraryFile[]
  latest_commit?: CommitSummary | null
  commit_count: number
  can_edit: boolean
  /** May rename, transfer, change visibility, and delete it. */
  can_manage: boolean
  /** Deployment config revisions recorded for it. */
  config_count: number
  /** Has an owner (an upload, or a model assigned to one); others are public. */
  owned: boolean
  hardware_options: Array<[string, string]>
  listing: ModelListing
  visibility: Visibility
  /** Display name of the storage location that holds the model; only for people who may view storage. */
  storage_target_name?: string
  description: string
  organization?: { name: string; display_name: string } | null
  total_bytes: number
  truncated: boolean
  unsafe_file_count: number
  local_path?: string
  remote_uri?: string | null
  source_url?: string | null
  model_card?: string | null
  /** The README was longer than the server shows; the rest is in the file itself. */
  model_card_truncated?: boolean
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
  visibility: Visibility
  status: 'uploading' | 'ready'
  size_bytes?: number | null
  file_count?: number | null
  modified_at?: string | null
  created_at: string
  updated_at: string
  /** Your role on it: admins manage visibility and deletion, writers upload. */
  my_role: 'admin' | 'write'
  /** Listing corrections already saved, so a resumed upload keeps them. */
  listing_overrides?: ListingOverrides
  /** Finished, but the last scan found none of its files in storage. */
  missing?: boolean
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
  visibility: Visibility
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
  /** Who may put new repositories here; empty means every uploader. */
  grants: StorageGrant[]
}

export type StorageMoveStatus =
  | 'queued' | 'copying' | 'verifying' | 'switching' | 'draining' | 'cleaning' | 'done' | 'failed' | 'cancelled'

/** A model moving between storage locations: copy, verify, switch, then remove the old copy. */
export interface StorageMove {
  id: string
  repo_id: string
  source_target: string
  destination_target: string
  keep_local: boolean
  status: StorageMoveStatus
  message: string
  error?: string | null
  total_bytes: number
  copied_bytes: number
  verified_bytes: number
  file_count: number
  /** Downloads of the old copy still running; it is removed when they end. */
  active_reads: number
  created_at: string
  updated_at: string
  switched_at?: string | null
  finished_at?: string | null
}

export interface StorageGrant {
  kind: 'user' | 'organization'
  id: string
  name: string
  display_name: string
}

export interface StorageOverview {
  default: string
  cache: StorageCapacity & { path: string; model_count: number; model_bytes: number }
  targets: StorageTarget[]
  conflicts: Array<{ repo_id: string; kept_target: string; skipped_target: string }>
  /** The site's own folder (profile pictures, git mirrors) and whether it answered. */
  system: { target: string; name: string; location: string; remote: boolean; ok: boolean; error?: string | null }
}

export interface StorageOption {
  id: string
  name: string
  kind: 'filesystem' | 's3'
  /** Only some users and organizations may upload here. */
  restricted: boolean
  /** Reserved for the owner being uploaded as. */
  dedicated: boolean
  /** Free space on local disk; null for buckets. */
  free_bytes: number | null
}

/** Private: the owner (an organization's admins and writers). Organization: every
 * member. Public: every account, plus anonymous Hugging Face clients. */
export type Visibility = 'private' | 'organization' | 'public'

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

export type MetricDirection = 'higher' | 'lower' | null

export interface ConfigMetric {
  id: string
  label: string
  unit: string
  better: MetricDirection
  group: 'speed' | 'latency' | 'capacity' | 'speculative' | 'context'
}

export interface CustomMetric {
  name: string
  value: number
  unit: string
  better: MetricDirection
}

/** Numbers measured while a config ran, with the test's context. */
export interface ConfigResults {
  values: Record<string, number>
  hardware: string | null
  vllm_version: string | null
  custom: CustomMetric[]
  notes: string
}

export interface ConfigRevision {
  id: string
  sequence: number
  parent_id: string | null
  message: string
  description: string
  author_name: string
  created_at: string
  file_count: number
  summary: { added: number; modified: number; deleted: number }
  results: ConfigResults
  results_updated_at: string | null
  results_updated_by: string | null
}

export interface ConfigRevisionDetail extends ConfigRevision {
  files: Array<{ path: string; size: number; content: string }>
  changes: CommitChange[]
}

export interface ConfigListing {
  items: ConfigRevision[]
  metrics: ConfigMetric[]
  hardware: Array<[string, string]>
  can_edit: boolean
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
  avatar_updated_at?: string | null
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
  /** Only for organization admins and account managers. */
  server_role?: Role
  disabled?: boolean
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
  avatar_updated_at?: string | null
}

/** The fields of a model's listing that people may correct. */
export interface ListingFields {
  pipeline_tag: string | null
  precision: string | null
  parameter_count: number | null
  library_name: string | null
  license: string | null
  tags: string[]
  base_model: string | null
  base_model_relation: BaseModelRelation | null
}

export type ListingOverrides = Partial<ListingFields>

/** How a model is listed: what its files say, what people changed, and the result. */
export interface ModelListing {
  detected: Partial<ListingFields> & { model_type?: string | null; formats?: string[] }
  overrides: ListingOverrides
  /** The task shown, or null when only config.model_type is known. */
  listed_task: string | null
  /** The size the model is known by: its name's when that agrees with the count. */
  nominal_parameters: number | null
  precisions: string[]
}

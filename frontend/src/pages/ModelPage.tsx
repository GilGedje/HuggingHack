import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  Boxes,
  ChevronRight,
  Cloud,
  Download,
  File,
  Folder,
  GitBranch,
  GitCommitHorizontal,
  HardDrive,
  Heart,
  History,
  LoaderCircle,
  LockKeyhole,
  Rocket,
  ShieldCheck,
  UploadCloud,
  Users,
} from 'lucide-react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useAccess } from '../access'
import { api } from '../api'
import { ModelActions, ModelCardDocument } from '../components/ModelDetails'
import { GgufInspector } from '../components/GgufInspector'
import { formatLabels } from '../components/RepositoryRows'
import { UploadChangeDialog } from '../components/UploadChangeDialog'
import { CopyButton, UseModelDialog, type UseModelMode } from '../components/UseModel'
import type {
  CommitDetail,
  CommitSummary,
  LibraryFile,
  LibraryModelDetails,
  StorageOption,
} from '../types'
import { formatBytes, formatNumber, initials, relativeTime, taskLabel } from '../utils'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

function shortId(id: string): string {
  return id.slice(0, 7)
}

function CommitSummaryBadges({ commit }: { commit: CommitSummary }) {
  const { added, modified, deleted } = commit.summary
  return (
    <span className="commit-badges">
      {added > 0 && <span className="added">+{added}</span>}
      {modified > 0 && <span className="modified">~{modified}</span>}
      {deleted > 0 && <span className="deleted">−{deleted}</span>}
    </span>
  )
}

function LatestCommitBar({ model }: { model: LibraryModelDetails }) {
  const commit = model.latest_commit
  if (!commit) return null
  return (
    <div className="latest-commit-bar">
      <span className="commit-author">{commit.author_name}</span>
      <Link to={`/models/${model.id}/commit/${commit.id}`} className="commit-message">
        {commit.message}
      </Link>
      <code>{shortId(commit.id)}</code>
      <span className="commit-time">{relativeTime(commit.created_at)}</span>
      <Link to={`/models/${model.id}/commits`} className="commit-history-link">
        <History size={14} /> {formatNumber(model.commit_count)} commit{model.commit_count === 1 ? '' : 's'}
      </Link>
    </div>
  )
}

interface TreeRow {
  name: string
  path: string
  folder: boolean
  size: number
  lastCommit?: LibraryFile['last_commit']
}

function treeRows(files: LibraryFile[], directory: string): TreeRow[] {
  const prefix = directory ? `${directory}/` : ''
  const folders = new Map<string, TreeRow>()
  const rows: TreeRow[] = []
  for (const file of files) {
    if (!file.path.startsWith(prefix)) continue
    const rest = file.path.slice(prefix.length)
    const [head, ...tail] = rest.split('/')
    if (tail.length) {
      const folder = folders.get(head) || {
        name: head,
        path: `${prefix}${head}`,
        folder: true,
        size: 0,
        lastCommit: null,
      }
      folder.size += file.size
      if (
        file.last_commit
        && (!folder.lastCommit || file.last_commit.created_at > folder.lastCommit.created_at)
      ) {
        folder.lastCommit = file.last_commit
      }
      folders.set(head, folder)
    } else {
      rows.push({ name: head, path: file.path, folder: false, size: file.size, lastCommit: file.last_commit })
    }
  }
  return [
    ...[...folders.values()].sort((left, right) => left.name.localeCompare(right.name)),
    ...rows.sort((left, right) => left.name.localeCompare(right.name)),
  ]
}

function FilesSection({
  model,
  onUpload,
}: {
  model: LibraryModelDetails
  onUpload: () => void
}) {
  const [searchParams, setSearchParams] = useSearchParams()
  const directory = (searchParams.get('path') || '').replace(/^\/+|\/+$/g, '')
  const rows = useMemo(() => treeRows(model.files, directory), [model.files, directory])
  const crumbs = directory ? directory.split('/') : []

  function open(path: string) {
    const next = new URLSearchParams(searchParams)
    if (path) next.set('path', path)
    else next.delete('path')
    setSearchParams(next)
  }

  return (
    <section className="model-files" aria-label="Files and versions">
      <LatestCommitBar model={model} />
      <div className="file-browser-toolbar">
        <nav className="file-breadcrumbs" aria-label="Folder">
          <button type="button" onClick={() => open('')}>
            {model.id.split('/')[1]}
          </button>
          {crumbs.map((crumb, index) => (
            <span key={crumbs.slice(0, index + 1).join('/')}>
              <ChevronRight size={13} />
              <button type="button" onClick={() => open(crumbs.slice(0, index + 1).join('/'))}>
                {crumb}
              </button>
            </span>
          ))}
        </nav>
        {model.can_edit && (
          <button type="button" className="secondary-button compact" onClick={onUpload}>
            <UploadCloud size={15} /> Upload changes
          </button>
        )}
      </div>
      <div className="file-browser" role="table" aria-label="Repository files">
        {directory && (
          <div className="file-browser-row" role="row">
            <button type="button" className="file-browser-name" onClick={() => open(crumbs.slice(0, -1).join('/'))}>
              <ArrowLeft size={15} /> ..
            </button>
          </div>
        )}
        {rows.map((row) => (
          <div className="file-browser-row" role="row" key={row.path}>
            {row.folder ? (
              <button type="button" className="file-browser-name" onClick={() => open(row.path)}>
                <Folder size={15} /> {row.name}
              </button>
            ) : (
              <span className="file-browser-name">
                <File size={15} /> <span title={row.path}>{row.name}</span>
              </span>
            )}
            <span className="file-browser-size">{formatBytes(row.size)}</span>
            <span className="file-browser-commit">
              {row.lastCommit ? (
                <Link to={`/models/${model.id}/commit/${row.lastCommit.id}`}>{row.lastCommit.message}</Link>
              ) : (
                '—'
              )}
            </span>
            <span className="file-browser-time">
              {row.lastCommit ? relativeTime(row.lastCommit.created_at) : ''}
            </span>
            <span className="file-browser-action">
              {!row.folder && (
                <a href={api.fileUrl(model.id, row.path)} aria-label={`Download ${row.path}`} title="Download">
                  <Download size={14} />
                </a>
              )}
            </span>
          </div>
        ))}
        {rows.length === 0 && <div className="empty-compact">This folder is empty.</div>}
      </div>
      {model.truncated && (
        <p className="file-browser-note">Only the first {model.files.length} files are listed.</p>
      )}
    </section>
  )
}

function groupByDay(commits: CommitSummary[]): Array<[string, CommitSummary[]]> {
  const groups = new Map<string, CommitSummary[]>()
  for (const commit of commits) {
    const day = new Date(commit.created_at).toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
    groups.set(day, [...(groups.get(day) || []), commit])
  }
  return [...groups.entries()]
}

function CommitsSection({ model }: { model: LibraryModelDetails }) {
  const [commits, setCommits] = useState<CommitSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(
    (offset: number) => {
      setLoading(true)
      api
        .commits(model.id, 50, offset)
        .then((payload) => {
          setCommits((current) => (offset ? [...current, ...payload.items] : payload.items))
          setTotal(payload.total)
        })
        .catch((reason) => setError(reason.message))
        .finally(() => setLoading(false))
    },
    [model.id],
  )

  useEffect(() => {
    load(0)
  }, [load, model.latest_commit?.id])

  return (
    <section className="model-commits" aria-label="Commit history">
      <div className="section-heading-line">
        <div>
          <span className="eyebrow">main</span>
          <h2>Commit history</h2>
        </div>
        <span>{formatNumber(total)} commits</span>
      </div>
      {error && <div className="inline-error">{error}</div>}
      {groupByDay(commits).map(([day, items]) => (
        <div className="commit-day" key={day}>
          <h3>
            <GitCommitHorizontal size={14} /> Commits on {day}
          </h3>
          <ul>
            {items.map((commit) => (
              <li key={commit.id}>
                <div className="commit-row-main">
                  <Link to={`/models/${model.id}/commit/${commit.id}`}>{commit.message}</Link>
                  <small>
                    {commit.author_name} committed {relativeTime(commit.created_at)}
                  </small>
                </div>
                <CommitSummaryBadges commit={commit} />
                <span className="commit-row-id">
                  <code>{shortId(commit.id)}</code>
                  <CopyButton text={commit.id} label="Copy full commit id" />
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {loading && (
        <div className="drawer-loading compact">
          <LoaderCircle size={20} className="spin" /> Loading commits…
        </div>
      )}
      {!loading && commits.length < total && (
        <button type="button" className="secondary-button commit-more" onClick={() => load(commits.length)}>
          Load older commits
        </button>
      )}
      {!loading && !error && commits.length === 0 && (
        <div className="empty-compact">No commits yet. Rescan storage to record the first one.</div>
      )}
    </section>
  )
}

function diffClass(line: string): string {
  if (line.startsWith('+++') || line.startsWith('---')) return 'meta'
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return ''
}

function CommitSection({ model, commitId }: { model: LibraryModelDetails; commitId: string }) {
  const [commit, setCommit] = useState<CommitDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    setCommit(null)
    setError('')
    api
      .commit(model.id, commitId)
      .then(setCommit)
      .catch((reason) => setError(reason.message))
  }, [commitId, model.id])

  if (error) return <div className="inline-error">{error}</div>
  if (!commit) {
    return (
      <div className="drawer-loading">
        <LoaderCircle size={22} className="spin" /> Loading commit…
      </div>
    )
  }
  return (
    <section className="commit-view" aria-label={`Commit ${shortId(commit.id)}`}>
      <Link to={`/models/${model.id}/commits`} className="text-link">
        <ArrowLeft size={14} /> All commits
      </Link>
      <header className="commit-view-header">
        <h2>{commit.message}</h2>
        {commit.description && <p className="commit-description">{commit.description}</p>}
        <div className="commit-view-meta">
          <span>
            <strong>{commit.author_name}</strong> committed {relativeTime(commit.created_at)}
          </span>
          <span className="commit-row-id">
            commit <code>{commit.id}</code>
            <CopyButton text={commit.id} label="Copy commit id" />
          </span>
          {commit.parent_id && (
            <span>
              parent{' '}
              <Link to={`/models/${model.id}/commit/${commit.parent_id}`}>
                <code>{shortId(commit.parent_id)}</code>
              </Link>
            </span>
          )}
          <CommitSummaryBadges commit={commit} />
        </div>
      </header>
      {commit.changes.map((change) => (
        <article className={`commit-file ${change.change}`} key={change.path}>
          <header>
            <span className={`commit-change-kind ${change.change}`}>{change.change}</span>
            <code>{change.path}</code>
            <span className="commit-file-stats">
              {change.binary ? (
                <>
                  {change.old_size != null && formatBytes(change.old_size)}
                  {change.old_size != null && change.new_size != null && ' → '}
                  {change.new_size != null && formatBytes(change.new_size)}
                </>
              ) : (
                <>
                  <span className="added">+{change.additions || 0}</span>{' '}
                  <span className="deleted">−{change.deletions || 0}</span>
                </>
              )}
            </span>
          </header>
          {change.binary ? (
            <p className="commit-binary">
              Binary or large file; contents are not kept in history.
            </p>
          ) : (
            <pre className="commit-diff">
              {(change.diff || []).map((line, index) => (
                <span key={index} className={diffClass(line)}>
                  {line || ' '}
                  {'\n'}
                </span>
              ))}
              {change.truncated && <span className="hunk">… diff truncated</span>}
            </pre>
          )}
        </article>
      ))}
    </section>
  )
}

export function ModelPage({ onToast }: { onToast: ToastHandler }) {
  const { can } = useAccess()
  const params = useParams()
  const repoId = `${params.owner}/${params.name}`
  const rest = params['*'] || ''
  const [searchParams, setSearchParams] = useSearchParams()
  const navigate = useNavigate()
  const [model, setModel] = useState<LibraryModelDetails | null>(null)
  const [error, setError] = useState('')
  const [targets, setTargets] = useState<StorageOption[]>([])
  const [saving, setSaving] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  const section = rest.startsWith('commit/')
    ? 'commit'
    : rest === 'commits'
      ? 'commits'
      : rest === 'tree'
        ? 'files'
        : rest === 'gguf'
          ? 'gguf'
          : 'card'
  const useMode: UseModelMode | null =
    searchParams.get('local-app') === 'vllm'
      ? 'vllm'
      : searchParams.get('clone') === 'true'
        ? 'clone'
        : null
  const uploading = searchParams.get('upload') === '1'

  useEffect(() => {
    let ignore = false
    setError('')
    api
      .libraryModelDetails(repoId)
      .then((payload) => {
        if (!ignore) setModel(payload)
      })
      .catch((reason) => {
        if (!ignore) setError(reason.message)
      })
    return () => {
      ignore = true
    }
  }, [repoId, reloadKey])

  useEffect(() => {
    api.storageOptions().then((payload) => setTargets(payload.items)).catch(() => undefined)
    const refresh = (event: Event) => {
      if ((event as CustomEvent<string>).detail === repoId) setReloadKey((value) => value + 1)
    }
    window.addEventListener('hugginghack:repository-changed', refresh)
    return () => window.removeEventListener('hugginghack:repository-changed', refresh)
  }, [repoId])

  const setParam = useCallback(
    (changes: Record<string, string | null>) => {
      setSearchParams((current) => {
        const next = new URLSearchParams(current)
        for (const [key, value] of Object.entries(changes)) {
          if (value === null) next.delete(key)
          else next.set(key, value)
        }
        return next
      })
    },
    [setSearchParams],
  )

  async function toggleSaved() {
    if (!model) return
    setSaving(true)
    try {
      if (model.saved) await api.unsaveModel(model.id)
      else {
        await api.saveModel({
          repo_id: model.id,
          metadata: {
            author: model.author,
            pipeline_tag: model.pipeline_tag,
            library_name: model.library_name,
            license: model.license,
            parameter_count: model.parameter_count,
            last_modified: model.last_modified,
            local: true,
          },
        })
      }
      setModel({ ...model, saved: !model.saved })
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Unable to update saved models', 'error')
    } finally {
      setSaving(false)
    }
  }

  if (error) {
    return (
      <div className="standard-page">
        <div className="empty-state spacious">
          <Boxes size={34} />
          <h2>{repoId}</h2>
          <p>{error}</p>
          <button className="secondary-button" onClick={() => navigate('/models')}>
            <ArrowLeft size={15} /> Back to models
          </button>
        </div>
      </div>
    )
  }
  if (!model) {
    return (
      <div className="drawer-loading">
        <LoaderCircle size={24} className="spin" /> Reading the local library…
      </div>
    )
  }

  const [owner, name] = model.id.split('/')
  const target = targets.find((item) => item.id === model.storage_target)
  const remoteOnly = model.storage_backend === 's3' && !model.cached
  const ggufFiles = model.files.filter((file) => file.path.toLowerCase().endsWith('.gguf'))
  const vllm = model.apps.includes('vllm')
  const base = `/models/${model.id}`
  const tabs = [
    { id: 'card', label: 'Model card', to: base },
    { id: 'files', label: 'Files and versions', to: `${base}/tree` },
    { id: 'commits', label: 'Commits', to: `${base}/commits`, count: model.commit_count },
    ...(ggufFiles.length ? [{ id: 'gguf', label: 'GGUF', to: `${base}/gguf`, count: ggufFiles.length }] : []),
  ]

  return (
    <div className="model-page">
      <header className="model-hero">
        <div className="model-hero-inner">
          <div className="model-title-row">
            <span className="model-avatar" aria-hidden="true">{initials(model.id)}</span>
            <h1>
              <Link
                to={model.organization ? `/orgs/${model.organization.name}` : `/models?search=${encodeURIComponent(owner)}`}
                className="model-owner"
                title={model.organization ? model.organization.display_name : undefined}
              >
                {owner}
              </Link>
              <span className="model-slash">/</span>
              <span className="model-name">{name}</span>
            </h1>
            <CopyButton text={model.id} label="Copy model name" />
            <button
              type="button"
              className={model.saved ? 'model-like saved' : 'model-like'}
              onClick={toggleSaved}
              disabled={saving}
            >
              <Heart size={14} fill={model.saved ? 'currentColor' : 'none'} />
              {model.saved ? 'Saved' : 'Save'}
            </button>
          </div>
          <div className="drawer-tags model-hero-tags">
            {model.pipeline_tag && <span className="task-tag">{taskLabel(model.pipeline_tag)}</span>}
            {model.formats.map((format) => (
              <span key={format}>{formatLabels[format] || format}</span>
            ))}
            {model.library_name && !model.formats.includes(model.library_name as never) && (
              <span>{model.library_name}</span>
            )}
            {model.parameter_count ? <span>{formatNumber(model.parameter_count)} params</span> : null}
            {model.license && <span>License: {model.license}</span>}
            {model.visibility !== 'public' && (
              <span>
                {model.visibility === 'private' ? <LockKeyhole size={11} /> : <Users size={11} />}{' '}
                {model.visibility}
              </span>
            )}
            <span className="local-badge">
              {model.storage_backend === 's3' ? <Cloud size={11} /> : <HardDrive size={11} />}{' '}
              {target?.name || model.storage_target}
              {remoteOnly ? ' · S3 only' : ''}
            </span>
          </div>
          <nav className="model-tabs" aria-label="Model sections">
            <div>
              {tabs.map((tab) => (
                <Link
                  key={tab.id}
                  to={tab.to}
                  className={section === tab.id || (tab.id === 'commits' && section === 'commit') ? 'active' : ''}
                  aria-current={section === tab.id ? 'page' : undefined}
                >
                  {tab.label}
                  {tab.count != null && <span>{formatNumber(tab.count)}</span>}
                </Link>
              ))}
            </div>
            <div className="model-tab-actions">
              {model.can_edit && section !== 'files' && (
                <button type="button" className="secondary-button compact" onClick={() => setParam({ upload: '1' })}>
                  <UploadCloud size={15} /> Upload changes
                </button>
              )}
              <button
                type="button"
                className="download-button compact"
                onClick={() => setParam(vllm ? { 'local-app': 'vllm', clone: null } : { clone: 'true', 'local-app': null })}
              >
                <Rocket size={15} /> Use this model
              </button>
            </div>
          </nav>
        </div>
      </header>

      <div className={section === 'card' ? 'model-page-body with-aside' : 'model-page-body'}>
        <main className="model-page-main">
          {section === 'card' && (
            model.model_card ? (
              <ModelCardDocument
                source={model.model_card}
                sourceUrl=""
                revision={model.revision || 'main'}
                localRepoId={model.id}
              />
            ) : (
              <div className="empty-state">
                <File size={28} />
                <h2>No model card yet</h2>
                <p>
                  Add a README.md to this repository to describe the model.
                  {model.can_edit ? ' Use Upload changes to add one.' : ''}
                </p>
              </div>
            )
          )}
          {section === 'files' && <FilesSection model={model} onUpload={() => setParam({ upload: '1' })} />}
          {section === 'commits' && <CommitsSection model={model} />}
          {section === 'commit' && <CommitSection model={model} commitId={rest.slice('commit/'.length)} />}
          {section === 'gguf' && (
            <GgufInspector
              repoId={model.id}
              revision={model.revision || 'main'}
              files={ggufFiles}
            />
          )}
        </main>

        {section === 'card' && (
          <aside className="model-page-aside">
            <section className="aside-card use-card">
              <h2>Use this model</h2>
              <p>Pull it from any machine on your network.</p>
              <div className="use-model-actions">
                {vllm && (
                  <button type="button" className="download-button" onClick={() => setParam({ 'local-app': 'vllm', clone: null })}>
                    <Rocket size={16} /> Deploy with vLLM
                  </button>
                )}
                <button
                  type="button"
                  className={vllm ? 'secondary-button' : 'download-button'}
                  onClick={() => setParam({ clone: 'true', 'local-app': null })}
                >
                  <GitBranch size={16} /> Clone repository
                </button>
              </div>
            </section>

            <section className="aside-card">
              <h2>Details</h2>
              <dl className="model-facts">
                <dt>Parameters</dt>
                <dd>{model.parameter_count ? formatNumber(model.parameter_count) : '—'}</dd>
                <dt>{remoteOnly ? 'Size in S3' : 'Size'}</dt>
                <dd>{formatBytes(model.size_bytes)}</dd>
                <dt>Files</dt>
                <dd>
                  <Link to={`${base}/tree`}>{formatNumber(model.file_count)}</Link>
                </dd>
                <dt>Storage</dt>
                <dd>{target?.name || model.storage_target}</dd>
                <dt>Location</dt>
                <dd><code>{remoteOnly ? model.remote_uri || model.local_path : model.local_path}</code></dd>
                {model.latest_commit && (
                  <>
                    <dt>Last commit</dt>
                    <dd>
                      <Link to={`${base}/commit/${model.latest_commit.id}`}>{model.latest_commit.message}</Link>
                      <small>{relativeTime(model.latest_commit.created_at)}</small>
                    </dd>
                  </>
                )}
                {model.revision && (
                  <>
                    <dt>Source revision</dt>
                    <dd><code>{model.revision}{model.sha ? ` · ${model.sha.slice(0, 10)}` : ''}</code></dd>
                  </>
                )}
              </dl>
              {model.description && <p className="model-description">{model.description}</p>}
            </section>

            <section className="aside-card aside-actions">
              <ModelActions
                repoId={model.id}
                storageBackend={model.storage_backend}
                cached={model.cached}
                files={model.files}
                canManageRuntimes={can('runtimes.use')}
                canManageCache={can('library.cache')}
                onCacheChanged={() => setReloadKey((value) => value + 1)}
                onToast={onToast}
              />
              {model.unsafe_file_count > 0 ? (
                <div className="security-note warning">
                  <AlertTriangle size={16} />
                  {model.unsafe_file_count} file{model.unsafe_file_count === 1 ? '' : 's'} may use
                  pickle serialization. Only load them from publishers you trust.
                </div>
              ) : (
                <div className="security-note">
                  <ShieldCheck size={16} />
                  No pickle-compatible file extensions in this repository.
                </div>
              )}
            </section>
          </aside>
        )}
      </div>

      {useMode && (
        <UseModelDialog
          repoId={model.id}
          pipelineTag={model.pipeline_tag}
          vllmSupported={vllm}
          mode={useMode}
          onModeChange={(mode) => setParam(mode === 'vllm' ? { 'local-app': 'vllm', clone: null } : { clone: 'true', 'local-app': null })}
          onClose={() => setParam({ 'local-app': null, clone: null })}
        />
      )}
      {uploading && model.can_edit && (
        <UploadChangeDialog
          model={model}
          directory={section === 'files' ? searchParams.get('path') || '' : ''}
          onClose={() => setParam({ upload: null })}
          onQueued={() => {
            setParam({ upload: null })
            onToast('Uploading your change. You can keep browsing; progress stays at the bottom of the screen.')
          }}
        />
      )}
    </div>
  )
}

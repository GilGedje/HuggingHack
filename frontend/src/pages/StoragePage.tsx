import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Cloud,
  Database,
  HardDrive,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  Search,
  Users,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { formatLabels } from '../components/RepositoryRows'
import type { StorageOverview, StorageTarget } from '../types'
import { formatBytes, formatNumber, relativeTime } from '../utils'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

function location(target: StorageTarget): string {
  if (target.kind === 'filesystem') return target.path || '/models'
  return `s3://${target.bucket}${target.prefix ? `/${target.prefix}` : ''}`
}

function Capacity({ used, total }: { used: number; total: number }) {
  const percent = total ? Math.min(100, (used / total) * 100) : 0
  return (
    <div className="capacity-track" aria-label={`${percent.toFixed(0)} percent used`}>
      <span style={{ width: `${percent}%` }} />
    </div>
  )
}

function TargetSection({ target, query }: { target: StorageTarget; query: string }) {
  const models = target.models.filter((model) =>
    model.repo_id.toLowerCase().includes(query.trim().toLowerCase()),
  )
  const Icon = target.kind === 's3' ? Cloud : HardDrive
  return (
    <section className="storage-target" aria-label={target.name}>
      <header className="storage-target-header">
        <div className="storage-icon">
          <Icon size={22} />
        </div>
        <div className="storage-target-title">
          <div>
            <h2>{target.name}</h2>
            {target.default && <span className="local-badge">Default for new models</span>}
          </div>
          <code>{location(target)}</code>
          {target.endpoint && <small>{target.endpoint}{target.region ? ` · ${target.region}` : ''}</small>}
        </div>
        <span className={target.connected ? 'status-pill ok' : 'status-pill danger'}>
          {target.connected ? <Check size={13} /> : <AlertCircle size={13} />}
          {target.connected ? 'Connected' : 'Offline'}
        </span>
      </header>
      {target.error && (
        <div className="inline-error storage-target-error">
          <AlertTriangle size={16} /> {target.error}
        </div>
      )}
      <div className="storage-target-stats">
        <span>
          <strong>{formatNumber(target.model_count)}</strong> model{target.model_count === 1 ? '' : 's'}
        </span>
        <span>
          <strong>{formatBytes(target.total_bytes)}</strong> of models
        </span>
        {target.kind === 's3' ? (
          <span>
            <strong>{formatNumber(target.cached_count)}</strong> cached locally
          </span>
        ) : (
          <span>
            <strong>{target.capacity ? formatBytes(target.capacity.free_bytes) : '—'}</strong> free
          </span>
        )}
      </div>
      {target.capacity && (
        <Capacity used={target.capacity.used_bytes} total={target.capacity.total_bytes} />
      )}
      <div className="storage-model-table" role="table" aria-label={`Models in ${target.name}`}>
        <div className="storage-model-row header" role="row">
          <span role="columnheader">Model</span>
          <span role="columnheader">Size</span>
          <span role="columnheader">Files</span>
          <span role="columnheader">Parameters</span>
          <span role="columnheader">Status</span>
          <span role="columnheader">Updated</span>
        </div>
        {models.map((model) => (
          <div className="storage-model-row" role="row" key={model.repo_id}>
            <span role="cell" className="storage-model-name">
              <Link to={`/models/${model.repo_id}`}>{model.repo_id}</Link>
              <small>
                {model.formats.map((format) => formatLabels[format] || format).join(' · ') || 'No weights'}
                {model.visibility !== 'public' && (
                  <>
                    {' · '}
                    {model.visibility === 'private' ? <LockKeyhole size={10} /> : <Users size={10} />}
                    {model.visibility}
                  </>
                )}
              </small>
            </span>
            <span role="cell">{formatBytes(model.size_bytes)}</span>
            <span role="cell">{formatNumber(model.file_count)}</span>
            <span role="cell">{model.parameter_count ? formatNumber(model.parameter_count) : '—'}</span>
            <span role="cell">
              {target.kind === 'filesystem' ? 'On disk' : model.cached ? 'Cached' : 'S3 only'}
            </span>
            <span role="cell">{relativeTime(model.modified_at)}</span>
          </div>
        ))}
        {models.length === 0 && (
          <div className="empty-compact">
            {target.models.length ? 'No models match your search.' : 'No models in this location yet.'}
          </div>
        )}
      </div>
    </section>
  )
}

export function StoragePage({ onToast }: { onToast: ToastHandler }) {
  const [overview, setOverview] = useState<StorageOverview | null>(null)
  const [error, setError] = useState('')
  const [scanning, setScanning] = useState(false)
  const [query, setQuery] = useState('')

  const load = useCallback(() => {
    setError('')
    api
      .storageTargets()
      .then(setOverview)
      .catch((reason) => setError(reason.message))
  }, [])

  useEffect(() => {
    load()
    window.addEventListener('hugginghack:repository-changed', load)
    return () => window.removeEventListener('hugginghack:repository-changed', load)
  }, [load])

  async function scan() {
    setScanning(true)
    try {
      const result = await api.scanLocalModels()
      onToast(`Storage scan complete: ${result.count} model${result.count === 1 ? '' : 's'} indexed.`)
      load()
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Storage scan failed', 'error')
    } finally {
      setScanning(false)
    }
  }

  const totals = useMemo(() => {
    const targets = overview?.targets || []
    return {
      models: targets.reduce((sum, target) => sum + target.model_count, 0),
      bytes: targets.reduce((sum, target) => sum + target.total_bytes, 0),
    }
  }, [overview])

  return (
    <div className="standard-page storage-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Administration</span>
          <h1>Storage</h1>
          <p>Every bucket and folder that holds models, with the models stored in each.</p>
        </div>
        <button className="secondary-button" onClick={scan} disabled={scanning}>
          {scanning ? <LoaderCircle size={16} className="spin" /> : <RefreshCw size={16} />}
          {scanning ? 'Scanning…' : 'Scan storage'}
        </button>
      </div>

      {error && <div className="inline-error">{error}</div>}
      {!overview && !error && (
        <div className="drawer-loading">
          <LoaderCircle size={24} className="spin" /> Checking storage…
        </div>
      )}

      {overview && (
        <>
          <section className="storage-strip">
            <div className="storage-icon">
              <Database size={23} />
            </div>
            <div className="storage-main">
              <div className="storage-title">
                <strong>
                  {overview.targets.length} location{overview.targets.length === 1 ? '' : 's'} ·{' '}
                  {formatNumber(totals.models)} model{totals.models === 1 ? '' : 's'} · {formatBytes(totals.bytes)}
                </strong>
                <code>Working cache {overview.cache.path}</code>
              </div>
              <Capacity used={overview.cache.used_bytes} total={overview.cache.total_bytes} />
              <div className="storage-meta">
                <span>{formatBytes(overview.cache.free_bytes)} free in the working cache</span>
                <span>
                  {formatNumber(overview.cache.model_count)} model{overview.cache.model_count === 1 ? '' : 's'} on disk ·{' '}
                  {formatBytes(overview.cache.model_bytes)}
                </span>
              </div>
            </div>
          </section>

          {overview.conflicts.length > 0 && (
            <div className="security-note warning storage-conflicts">
              <AlertTriangle size={16} />
              <div>
                <strong>
                  {overview.conflicts.length} model{overview.conflicts.length === 1 ? ' exists' : 's exist'} in
                  more than one location
                </strong>
                <ul>
                  {overview.conflicts.map((conflict) => (
                    <li key={`${conflict.repo_id}-${conflict.skipped_target}`}>
                      <code>{conflict.repo_id}</code> is served from <code>{conflict.kept_target}</code>; the
                      copy in <code>{conflict.skipped_target}</code> is ignored.
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          <div className="local-tools">
            <div className="catalog-search">
              <Search size={18} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter models in every location"
              />
            </div>
          </div>

          {overview.targets.map((target) => (
            <TargetSection key={target.id} target={target} query={query} />
          ))}
        </>
      )}
    </div>
  )
}

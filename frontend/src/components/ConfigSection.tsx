import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  FilePlus2,
  FileText,
  FileUp,
  LoaderCircle,
  Pencil,
  Plus,
  RotateCcw,
  SlidersHorizontal,
  Trash2,
  X,
} from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { compareRows, formatValue, headlineMetrics } from '../configCompare'
import { droppedEntries, readDrop, type DroppedFile } from '../dropFiles'
import type {
  ConfigListing,
  ConfigMetric,
  ConfigResults,
  ConfigRevision,
  ConfigRevisionDetail,
  LibraryModelDetails,
  MetricDirection,
} from '../types'
import { formatBytes, relativeTime } from '../utils'
import { DownloadLink } from './DownloadLink'
import { FileDiff } from './FileDiff'
import { LoadError } from './LoadError'
import { RowSkeletons } from './Skeletons'
import { CopyButton } from './UseModel'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void

const MAX_FILE_BYTES = 512 * 1024
const GROUPS: Array<[ConfigMetric['group'], string]> = [
  ['speed', 'Throughput'],
  ['latency', 'Latency'],
  ['capacity', 'Capacity'],
  ['speculative', 'Speculative decoding'],
]

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback
}

function hasResults(results: ConfigResults | undefined): boolean {
  return Boolean(results && (Object.keys(results.values || {}).length || results.custom?.length || results.notes))
}

/* Results are edited as text so fields can be empty while typing. */

interface ResultsDraft {
  values: Record<string, string>
  hardware: string
  vllm_version: string
  custom: Array<{ name: string; value: string; unit: string; better: '' | 'higher' | 'lower' }>
  notes: string
}

function toDraft(results?: ConfigResults | null): ResultsDraft {
  return {
    values: Object.fromEntries(Object.entries(results?.values || {}).map(([key, value]) => [key, String(value)])),
    hardware: results?.hardware || '',
    vllm_version: results?.vllm_version || '',
    custom: (results?.custom || []).map((item) => ({
      name: item.name,
      value: String(item.value),
      unit: item.unit,
      better: item.better || '',
    })),
    notes: results?.notes || '',
  }
}

function fromDraft(draft: ResultsDraft, metrics: ConfigMetric[]): { results?: ConfigResults; error?: string } {
  const values: Record<string, number> = {}
  for (const metric of metrics) {
    const raw = draft.values[metric.id]?.trim()
    if (!raw) continue
    const value = Number(raw)
    if (!Number.isFinite(value)) return { error: `${metric.label} must be a number.` }
    values[metric.id] = value
  }
  const custom = []
  for (const item of draft.custom) {
    if (!item.name.trim() && !item.value.trim()) continue
    const value = Number(item.value.trim())
    if (!item.name.trim()) return { error: 'Every custom metric needs a name.' }
    if (!item.value.trim() || !Number.isFinite(value)) return { error: `${item.name} must be a number.` }
    custom.push({ name: item.name.trim(), value, unit: item.unit.trim(), better: (item.better || null) as MetricDirection })
  }
  return {
    results: {
      values,
      hardware: draft.hardware || null,
      vllm_version: draft.vllm_version.trim() || null,
      custom,
      notes: draft.notes.trim(),
    },
  }
}

function MetricInput({
  metric,
  value,
  onChange,
}: {
  metric: ConfigMetric
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="metric-input">
      <span>{metric.label}</span>
      <span className="metric-field">
        <input inputMode="decimal" value={value} onChange={(event) => onChange(event.target.value)} placeholder="—" />
        {metric.unit && <em>{metric.unit}</em>}
      </span>
    </label>
  )
}

function ResultsEditor({
  listing,
  draft,
  onChange,
}: {
  listing: ConfigListing
  draft: ResultsDraft
  onChange: (draft: ResultsDraft) => void
}) {
  const setValue = (id: string, value: string) => onChange({ ...draft, values: { ...draft.values, [id]: value } })
  const setCustom = (index: number, changes: Partial<ResultsDraft['custom'][number]>) =>
    onChange({ ...draft, custom: draft.custom.map((item, position) => (position === index ? { ...item, ...changes } : item)) })
  return (
    <div className="results-editor">
      <fieldset>
        <legend>Test setup</legend>
        <div className="metric-grid">
          <label className="metric-input">
            <span>Hardware</span>
            <select value={draft.hardware} onChange={(event) => onChange({ ...draft, hardware: event.target.value })}>
              <option value="">Not set</option>
              {listing.hardware.map(([id, label]) => (
                <option key={id} value={id}>{label}</option>
              ))}
            </select>
          </label>
          <label className="metric-input">
            <span>vLLM version</span>
            <span className="metric-field">
              <input value={draft.vllm_version} onChange={(event) => onChange({ ...draft, vllm_version: event.target.value })} placeholder="0.11.0" maxLength={40} />
            </span>
          </label>
          {listing.metrics.filter((metric) => metric.group === 'context').map((metric) => (
            <MetricInput key={metric.id} metric={metric} value={draft.values[metric.id] || ''} onChange={(value) => setValue(metric.id, value)} />
          ))}
        </div>
      </fieldset>
      {GROUPS.map(([group, label]) => (
        <fieldset key={group}>
          <legend>{label}</legend>
          <div className="metric-grid">
            {listing.metrics.filter((metric) => metric.group === group).map((metric) => (
              <MetricInput key={metric.id} metric={metric} value={draft.values[metric.id] || ''} onChange={(value) => setValue(metric.id, value)} />
            ))}
          </div>
        </fieldset>
      ))}
      <fieldset>
        <legend>Your own metrics</legend>
        {draft.custom.map((item, index) => (
          <div className="custom-metric" key={index}>
            <input aria-label="Metric name" value={item.name} onChange={(event) => setCustom(index, { name: event.target.value })} placeholder="Name, e.g. Prefix cache hit rate" maxLength={60} />
            <input aria-label="Value" inputMode="decimal" value={item.value} onChange={(event) => setCustom(index, { value: event.target.value })} placeholder="Value" />
            <input aria-label="Unit" value={item.unit} onChange={(event) => setCustom(index, { unit: event.target.value })} placeholder="Unit" maxLength={16} />
            <select aria-label="Better when" value={item.better} onChange={(event) => setCustom(index, { better: event.target.value as ResultsDraft['custom'][number]['better'] })}>
              <option value="">No direction</option>
              <option value="higher">Higher is better</option>
              <option value="lower">Lower is better</option>
            </select>
            <button type="button" className="icon-button" aria-label="Remove metric" onClick={() => onChange({ ...draft, custom: draft.custom.filter((_, position) => position !== index) })}>
              <X size={14} />
            </button>
          </div>
        ))}
        {draft.custom.length < 20 && (
          <button type="button" className="secondary-button compact" onClick={() => onChange({ ...draft, custom: [...draft.custom, { name: '', value: '', unit: '', better: 'higher' }] })}>
            <Plus size={14} /> Add metric
          </button>
        )}
      </fieldset>
      <label className="metric-input notes">
        <span>Notes</span>
        <textarea value={draft.notes} onChange={(event) => onChange({ ...draft, notes: event.target.value })} maxLength={2000} placeholder="Benchmark command, dataset, anything that explains the numbers" />
      </label>
    </div>
  )
}

function ResultsView({ results, listing }: { results: ConfigResults; listing: ConfigListing }) {
  if (!hasResults(results)) return <p className="config-empty-note">No results recorded yet.</p>
  const hardware = listing.hardware.find(([id]) => id === results.hardware)?.[1]
  const groups: Array<[string, Array<[string, string]>]> = []
  const setup: Array<[string, string]> = []
  if (hardware) setup.push(['Hardware', hardware])
  if (results.vllm_version) setup.push(['vLLM', results.vllm_version])
  for (const metric of listing.metrics.filter((item) => item.group === 'context')) {
    const value = results.values[metric.id]
    if (value != null) setup.push([metric.label, formatValue(value, metric.unit)])
  }
  if (setup.length) groups.push(['Test setup', setup])
  for (const [group, label] of GROUPS) {
    const rows = listing.metrics
      .filter((metric) => metric.group === group && results.values[metric.id] != null)
      .map((metric): [string, string] => [metric.label, formatValue(results.values[metric.id], metric.unit)])
    if (rows.length) groups.push([label, rows])
  }
  if (results.custom.length) {
    groups.push(['Your own metrics', results.custom.map((item): [string, string] => [item.name, formatValue(item.value, item.unit)])])
  }
  return (
    <div className="results-view">
      {groups.map(([label, rows]) => (
        <section key={label}>
          <h4>{label}</h4>
          <dl>
            {rows.map(([name, value]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      {results.notes && <p className="results-notes">{results.notes}</p>}
    </div>
  )
}

function CompareTable({ base, listing }: { base: string; listing: ConfigListing }) {
  const measured = listing.items.filter((item) => hasResults(item.results))
  const rows = useMemo(() => compareRows(measured, listing.metrics), [measured, listing.metrics])
  if (!measured.length) return null
  const hardwareLabel = (id: string | null) => listing.hardware.find(([key]) => key === id)?.[1] || '—'
  return (
    <section className="config-compare" aria-label="Compare results">
      <div className="config-compare-heading">
        <h3>Compare results</h3>
        <p>The best value in each row is highlighted. Compare runs with the same hardware and test setup.</p>
      </div>
      <div className="config-compare-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Metric</th>
              {measured.map((revision) => (
                <th scope="col" key={revision.id}>
                  <Link to={`${base}/config/${revision.id}`}>
                    <span>#{revision.sequence}</span> {revision.message}
                  </Link>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr className="context">
              <th scope="row">Hardware</th>
              {measured.map((revision) => <td key={revision.id}>{hardwareLabel(revision.results.hardware)}</td>)}
            </tr>
            {measured.some((revision) => revision.results.vllm_version) && (
              <tr className="context">
                <th scope="row">vLLM</th>
                {measured.map((revision) => <td key={revision.id}>{revision.results.vllm_version || '—'}</td>)}
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.key} className={row.context ? 'context' : ''}>
                <th scope="row">
                  {row.label}
                  {row.better && <small>{row.better === 'higher' ? 'higher is better' : 'lower is better'}</small>}
                </th>
                {measured.map((revision) => {
                  const best = row.best.includes(revision.id)
                  return (
                    <td key={revision.id} className={best ? 'best' : ''}>
                      {formatValue(row.values[revision.id], row.unit)}
                      {best && <span className="sr-only"> (best)</span>}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function RevisionBadges({ revision }: { revision: ConfigRevision }) {
  return (
    <span className="commit-badges">
      {revision.summary.added > 0 && <span className="added">+{revision.summary.added}</span>}
      {revision.summary.modified > 0 && <span className="modified">~{revision.summary.modified}</span>}
      {revision.summary.deleted > 0 && <span className="deleted">−{revision.summary.deleted}</span>}
    </span>
  )
}

function Overview({ model, listing }: { model: LibraryModelDetails; listing: ConfigListing }) {
  const base = `/models/${model.id}`
  if (!listing.items.length) {
    return (
      <div className="empty-state config-empty">
        <SlidersHorizontal size={28} />
        <h2>No deployment configs yet</h2>
        <p>
          Keep the files you deploy this model with (launch script, compose file, vLLM arguments) and what each
          version achieved, so you can tell which one was better.
        </p>
        {listing.can_edit && (
          <Link className="download-button" to={`${base}/config/new`}>
            <Plus size={15} /> Add the first config
          </Link>
        )}
      </div>
    )
  }
  return (
    <div className="config-overview">
      <div className="config-toolbar">
        <div>
          <h2>Deployment configs</h2>
          <p>Every revision keeps its files and the results measured with them.</p>
        </div>
        {listing.can_edit && (
          <Link className="download-button compact" to={`${base}/config/new`}>
            <Plus size={15} /> New revision
          </Link>
        )}
      </div>
      <CompareTable base={base} listing={listing} />
      <ol className="config-list">
        {listing.items.map((revision) => {
          const chips = headlineMetrics(revision.results, listing.metrics)
          return (
            <li key={revision.id}>
              <Link to={`${base}/config/${revision.id}`}>
                <span className="config-seq">#{revision.sequence}</span>
                <span className="config-list-main">
                  <strong>{revision.message}</strong>
                  <small>
                    {revision.author_name} · {relativeTime(revision.created_at)} · {revision.file_count} file
                    {revision.file_count === 1 ? '' : 's'} <RevisionBadges revision={revision} />
                  </small>
                </span>
                <span className="config-chips">
                  {chips.length ? (
                    chips.map((chip) => (
                      <span key={chip.label} title={chip.label}>{chip.text}</span>
                    ))
                  ) : (
                    <span className="pending">{hasResults(revision.results) ? 'Results recorded' : 'No results yet'}</span>
                  )}
                </span>
              </Link>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

function RevisionView({
  model,
  listing,
  revisionId,
  onChanged,
  onToast,
}: {
  model: LibraryModelDetails
  listing: ConfigListing
  revisionId: string
  onChanged: () => void
  onToast: ToastHandler
}) {
  const base = `/models/${model.id}`
  const [revision, setRevision] = useState<ConfigRevisionDetail | null>(null)
  const [error, setError] = useState('')
  const [activeFile, setActiveFile] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<ResultsDraft>(toDraft())
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  // Only the latest request may answer, so a slow reply for the revision just
  // left never shows under the one now open.
  const latestRequest = useRef(0)
  const load = useCallback(() => {
    const request = ++latestRequest.current
    api
      .configRevision(model.id, revisionId)
      .then((detail) => {
        if (request !== latestRequest.current) return
        setRevision(detail)
        setActiveFile((current) => (detail.files.some((file) => file.path === current) ? current : detail.files[0]?.path || ''))
      })
      .catch((reason) => {
        if (request === latestRequest.current) setError(message(reason, 'Could not load the revision.'))
      })
  }, [model.id, revisionId])

  useEffect(() => {
    setRevision(null)
    setError('')
    setEditing(false)
    load()
  }, [load])

  async function save(event: FormEvent) {
    event.preventDefault()
    const parsed = fromDraft(draft, listing.metrics)
    if (!parsed.results) {
      setFormError(parsed.error || 'Check the values.')
      return
    }
    setSaving(true)
    setFormError('')
    try {
      await api.updateConfigResults(model.id, revisionId, parsed.results)
      setEditing(false)
      load()
      onChanged()
      onToast('Results saved.')
    } catch (reason) {
      setFormError(message(reason, 'Could not save the results.'))
    } finally {
      setSaving(false)
    }
  }

  if (error) {
    return (
      <LoadError
        what="this revision"
        message={error}
        onRetry={() => {
          setError('')
          load()
        }}
      />
    )
  }
  if (!revision) return <RowSkeletons rows={6} cells={1} label="Loading the revision" />
  const file = revision.files.find((item) => item.path === activeFile)
  const parent = listing.items.find((item) => item.id === revision.parent_id)
  const latest = listing.items[0]?.id === revision.id
  return (
    <section className="config-revision" aria-label={`Config revision ${revision.sequence}`}>
      <Link to={`${base}/config`} className="text-link">
        <ArrowLeft size={14} /> All configs
      </Link>
      <header className="commit-view-header">
        <h2>
          <span className="config-seq">#{revision.sequence}</span> {revision.message}
        </h2>
        {revision.description && <p className="commit-description">{revision.description}</p>}
        <div className="commit-view-meta">
          <span>
            <strong>{revision.author_name}</strong> saved {relativeTime(revision.created_at)}
          </span>
          {parent && (
            <span>
              after <Link to={`${base}/config/${parent.id}`}>#{parent.sequence}</Link>
            </span>
          )}
          <RevisionBadges revision={revision} />
          <span className="config-revision-actions">
            <DownloadLink className="secondary-button compact" href={api.configArchiveUrl(model.id, revision.id)}>
              Download .zip
            </DownloadLink>
            {latest && listing.can_edit && (
              <Link className="secondary-button compact" to={`${base}/config/new`}>
                <Pencil size={14} /> Change files
              </Link>
            )}
          </span>
        </div>
      </header>

      <section className="config-panel">
        <div className="config-panel-heading">
          <h3>Results</h3>
          {revision.results_updated_at && (
            <small>
              Updated {relativeTime(revision.results_updated_at)}
              {revision.results_updated_by ? ` by ${revision.results_updated_by}` : ''}
            </small>
          )}
          {listing.can_edit && !editing && (
            <button type="button" className="secondary-button compact" onClick={() => {
              setDraft(toDraft(revision.results))
              setFormError('')
              setEditing(true)
            }}>
              <Pencil size={14} /> {hasResults(revision.results) ? 'Edit results' : 'Add results'}
            </button>
          )}
        </div>
        {editing ? (
          <form onSubmit={save}>
            <ResultsEditor listing={listing} draft={draft} onChange={setDraft} />
            {formError && <div className="inline-error"><AlertTriangle size={16} /> {formError}</div>}
            <div className="settings-block-actions">
              <button type="button" className="secondary-button compact" onClick={() => setEditing(false)} disabled={saving}>Cancel</button>
              <button className="download-button compact" disabled={saving}>
                {saving && <LoaderCircle size={14} className="spin" />} Save results
              </button>
            </div>
          </form>
        ) : (
          <ResultsView results={revision.results} listing={listing} />
        )}
      </section>

      <section className="config-panel">
        <div className="config-panel-heading">
          <h3>Files</h3>
          <small>{revision.files.length} file{revision.files.length === 1 ? '' : 's'}</small>
        </div>
        <div className="config-files">
          <ul role="tablist" aria-label="Files">
            {revision.files.map((item) => (
              <li key={item.path}>
                <button
                  type="button"
                  role="tab"
                  aria-selected={item.path === activeFile}
                  className={item.path === activeFile ? 'active' : ''}
                  onClick={() => setActiveFile(item.path)}
                >
                  <FileText size={13} /> <span>{item.path}</span>
                </button>
              </li>
            ))}
          </ul>
          {file && (
            <div className="config-file" role="tabpanel">
              <header>
                <code>{file.path}</code>
                <small>{formatBytes(file.size)}</small>
                <CopyButton text={file.content} label={`Copy ${file.path}`} />
              </header>
              <pre>{file.content}</pre>
            </div>
          )}
        </div>
      </section>

      <section className="config-panel">
        <div className="config-panel-heading">
          <h3>{parent ? `Changes since #${parent.sequence}` : 'Initial files'}</h3>
        </div>
        {revision.changes.map((change) => (
          <FileDiff key={change.path} change={change} />
        ))}
      </section>
    </section>
  )
}

interface DraftFile {
  path: string
  content: string
  /** Content in the latest revision; undefined for a new file. */
  original?: string
  removed: boolean
  open: boolean
}

async function readText(file: File, path: string): Promise<{ path: string; content?: string; error?: string }> {
  if (file.size > MAX_FILE_BYTES) return { path, error: `${path} is larger than 512 KB.` }
  const content = await file.text()
  if (content.includes('\u0000')) return { path, error: `${path} is not a text file.` }
  return { path, content }
}

function NewRevision({
  model,
  listing,
  onCreated,
}: {
  model: LibraryModelDetails
  listing: ConfigListing
  onCreated: (revision: ConfigRevision) => void
}) {
  const base = `/models/${model.id}`
  const latest = listing.items[0] || null
  const picker = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<DraftFile[] | null>(latest ? null : [])
  const [messageText, setMessageText] = useState('')
  const [description, setDescription] = useState('')
  const [withResults, setWithResults] = useState(false)
  const [draft, setDraft] = useState<ResultsDraft>(() => toDraft(latest ? { ...latest.results, values: pickContext(latest.results, listing.metrics), custom: [], notes: '' } : null))
  const [newPath, setNewPath] = useState('')
  // A file written from scratch opens its editor; typing starts there.
  const focusEditor = useRef<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [problems, setProblems] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!latest) return
    let ignore = false
    setLoadError('')
    api
      .configRevision(model.id, latest.id)
      .then((detail) => {
        if (!ignore) setFiles(detail.files.map((file) => ({ path: file.path, content: file.content, original: file.content, removed: false, open: false })))
      })
      .catch((reason) => {
        if (!ignore) setLoadError(message(reason, 'Could not load the latest revision.'))
      })
    return () => {
      ignore = true
    }
  }, [latest, model.id, attempt])

  function merge(incoming: Array<{ path: string; content: string }>) {
    setFiles((current) => {
      const next = [...(current || [])]
      for (const item of incoming) {
        const existing = next.findIndex((file) => file.path === item.path)
        if (existing >= 0) next[existing] = { ...next[existing], content: item.content, removed: false }
        else next.push({ path: item.path, content: item.content, removed: false, open: false })
      }
      return next.sort((a, b) => a.path.localeCompare(b.path))
    })
  }

  async function addFiles(items: DroppedFile[]) {
    const read = await Promise.all(items.map((item) => readText(item.file, item.path)))
    setProblems(read.filter((item) => item.error).map((item) => item.error as string))
    merge(read.filter((item) => item.content != null) as Array<{ path: string; content: string }>)
  }

  async function onDrop(event: DragEvent) {
    event.preventDefault()
    setDragging(false)
    const entries = droppedEntries(event.dataTransfer)
    if (entries.length) addFiles(await readDrop(entries))
  }

  const update = (path: string, changes: Partial<DraftFile>) =>
    setFiles((current) => (current || []).map((file) => (file.path === path ? { ...file, ...changes } : file)))

  const changed = (files || []).filter((file) => !file.removed && file.content !== file.original)
  const deletions = (files || []).filter((file) => file.removed && file.original !== undefined).map((file) => file.path)
  const remaining = (files || []).filter((file) => !file.removed)
  const ready = messageText.trim() && (changed.length || deletions.length) && remaining.length > 0

  async function submit(event: FormEvent) {
    event.preventDefault()
    let results: ConfigResults | undefined
    if (withResults) {
      const parsed = fromDraft(draft, listing.metrics)
      if (!parsed.results) {
        setError(parsed.error || 'Check the results.')
        return
      }
      results = parsed.results
    }
    setSaving(true)
    setError('')
    try {
      const revision = await api.createConfigRevision(model.id, {
        parent_id: latest?.id || null,
        message: messageText.trim(),
        description: description.trim(),
        files: changed.map((file) => ({ path: file.path, content: file.content })),
        deletions,
        results,
      })
      onCreated(revision)
    } catch (reason) {
      setError(message(reason, 'Could not save the revision.'))
      setSaving(false)
    }
  }

  if (!files && loadError) {
    return (
      <div className="config-new">
        <Link to={`${base}/config`} className="text-link">
          <ArrowLeft size={14} /> All configs
        </Link>
        <LoadError what="the latest files" message={loadError} onRetry={() => setAttempt((value) => value + 1)} />
      </div>
    )
  }
  if (!files) return <RowSkeletons rows={4} cells={2} label="Loading the latest files" />
  return (
    <form className="config-new" onSubmit={submit}>
      <Link to={`${base}/config`} className="text-link">
        <ArrowLeft size={14} /> All configs
      </Link>
      <div className="config-toolbar">
        <div>
          <h2>{latest ? `New revision after #${latest.sequence}` : 'First deployment config'}</h2>
          <p>{latest ? 'Starts from the latest files. Change what you tried, then save it as a new revision.' : 'Add the files you deploy this model with.'}</p>
        </div>
      </div>

      <section className="config-panel">
        <div className="config-panel-heading">
          <h3>Files</h3>
          <small>{remaining.length} file{remaining.length === 1 ? '' : 's'}</small>
        </div>
        {files.length > 0 && (
          <ul className="draft-files">
            {files.map((file) => {
              const state = file.removed ? 'deleted' : file.original === undefined ? 'added' : file.content !== file.original ? 'modified' : ''
              return (
                <li key={file.path} className={state}>
                  <div className="draft-file-row">
                    <FileText size={14} />
                    <code>{file.path}</code>
                    {state && <span className={`commit-change-kind ${state}`}>{state}</span>}
                    <span className="draft-file-actions">
                      {!file.removed && (
                        <button type="button" onClick={() => update(file.path, { open: !file.open })}>
                          <Pencil size={13} /> {file.open ? 'Close' : 'Edit'}
                        </button>
                      )}
                      {file.removed ? (
                        <button type="button" onClick={() => update(file.path, { removed: false })}>
                          <RotateCcw size={13} /> Keep
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="danger-text"
                          onClick={() =>
                            file.original === undefined
                              ? setFiles(files.filter((item) => item.path !== file.path))
                              : update(file.path, { removed: true, open: false })
                          }
                        >
                          <Trash2 size={13} /> Remove
                        </button>
                      )}
                    </span>
                  </div>
                  {file.open && !file.removed && (
                    <textarea
                      ref={(node) => {
                        if (node && focusEditor.current === file.path) {
                          focusEditor.current = null
                          node.focus()
                        }
                      }}
                      className="draft-file-editor"
                      aria-label={`Contents of ${file.path}`}
                      value={file.content}
                      spellCheck={false}
                      onChange={(event) => update(file.path, { content: event.target.value })}
                    />
                  )}
                </li>
              )
            })}
          </ul>
        )}
        <label
          className={`folder-picker config-drop ${dragging ? 'dragging' : ''}`}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false)
          }}
          onDrop={onDrop}
        >
          <input
            ref={picker}
            type="file"
            multiple
            onChange={(event) => {
              addFiles(Array.from(event.target.files || []).map((file) => ({ file, path: file.name })))
              event.target.value = ''
            }}
          />
          <FileUp size={22} />
          <strong>Drop config files or a folder, or click to choose</strong>
          <span>Text files up to 512 KB. A file with the same name replaces the current one.</span>
        </label>
        <div className="draft-new-file">
          <input
            aria-label="New file name"
            value={newPath}
            onChange={(event) => setNewPath(event.target.value)}
            placeholder="New file, e.g. serve.sh"
            spellCheck={false}
          />
          <button
            type="button"
            className="secondary-button compact"
            disabled={!newPath.trim() || files.some((file) => file.path === newPath.trim())}
            onClick={() => {
              focusEditor.current = newPath.trim()
              setFiles([...files, { path: newPath.trim(), content: '', removed: false, open: true }].sort((a, b) => a.path.localeCompare(b.path)))
              setNewPath('')
            }}
          >
            <FilePlus2 size={14} /> Write a file
          </button>
        </div>
        {problems.map((problem) => (
          <div className="inline-error" key={problem}><AlertTriangle size={16} /> {problem}</div>
        ))}
        <p className="settings-warning compact">
          <AlertTriangle size={14} />
          <span>Everyone who can see this model can read its configs. Keep tokens and API keys out of them.</span>
        </p>
      </section>

      <section className="config-panel">
        <div className="config-panel-heading">
          <h3>What changed</h3>
        </div>
        <label className="wizard-label">
          <span>Summary</span>
          <input value={messageText} onChange={(event) => setMessageText(event.target.value)} maxLength={200} placeholder="Enable MTP with 3 draft tokens" required />
        </label>
        <label className="wizard-label">
          <span>Details <small>Optional</small></span>
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={5000} />
        </label>
      </section>

      <section className="config-panel">
        <label className="config-results-toggle">
          <input type="checkbox" checked={withResults} onChange={(event) => setWithResults(event.target.checked)} />
          <span>
            <strong>Add results now</strong>
            <small>Or deploy first and add them to the revision later.</small>
          </span>
        </label>
        {withResults && <ResultsEditor listing={listing} draft={draft} onChange={setDraft} />}
      </section>

      {error && <div className="inline-error"><AlertTriangle size={16} /> {error}</div>}
      <div className="settings-block-actions">
        <Link className="secondary-button compact" to={`${base}/config`}>Cancel</Link>
        <button className="download-button compact" disabled={!ready || saving}>
          {saving && <LoaderCircle size={14} className="spin" />} Save revision
        </button>
      </div>
    </form>
  )
}

/** The test setup carries over to the next revision; measured numbers do not. */
function pickContext(results: ConfigResults, metrics: ConfigMetric[]): Record<string, number> {
  const context = new Set(metrics.filter((metric) => metric.group === 'context').map((metric) => metric.id))
  return Object.fromEntries(Object.entries(results.values || {}).filter(([key]) => context.has(key)))
}

/** The Config tab: deployment config revisions and what each one achieved. */
export function ConfigSection({
  model,
  path,
  onChanged,
  onToast,
}: {
  model: LibraryModelDetails
  /** What follows `config/` in the address: nothing, `new`, or a revision id. */
  path: string
  onChanged: () => void
  onToast: ToastHandler
}) {
  const navigate = useNavigate()
  const [listing, setListing] = useState<ConfigListing | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setError('')
    api
      .configRevisions(model.id)
      .then(setListing)
      .catch((reason) => setError(message(reason, 'Could not load configs.')))
  }, [model.id])

  useEffect(() => {
    setListing(null)
    load()
  }, [load])

  if (error) return <LoadError what="the configs" message={error} onRetry={load} />
  if (!listing) return <RowSkeletons rows={4} cells={3} label="Loading configs" />
  if (path === 'new') {
    if (!listing.can_edit) return <div className="inline-error">You cannot add configs to this model.</div>
    return (
      <NewRevision
        model={model}
        listing={listing}
        onCreated={(revision) => {
          load()
          onChanged()
          onToast(`Saved revision #${revision.sequence}.`)
          navigate(`/models/${model.id}/config/${revision.id}`)
        }}
      />
    )
  }
  if (path) {
    return (
      <RevisionView
        model={model}
        listing={listing}
        revisionId={path}
        onChanged={load}
        onToast={onToast}
      />
    )
  }
  return <Overview model={model} listing={listing} />
}

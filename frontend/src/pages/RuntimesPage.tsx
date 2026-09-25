import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, Check, Cloud, RefreshCw, Server } from 'lucide-react'
import { api } from '../api'
import type { RuntimeJob, RuntimeTarget } from '../types'
import { formatBytes, relativeTime } from '../utils'
import { RowSkeletons } from '../components/Skeletons'

const runtimeActiveStatuses = ['queued', 'preparing', 'transferring', 'loading']
/** The pause between one answer and the next poll. */
const POLL_MS = 2000
/** The least time Refresh shows its spinner. */
const MIN_SPIN_MS = 500

export function RuntimesPage() {
  const [targets, setTargets] = useState<RuntimeTarget[]>([])
  const [jobs, setJobs] = useState<RuntimeJob[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  const latest = useRef(0)
  const timer = useRef(0)
  const stopped = useRef(false)

  // One chain of polls: each waits for the previous answer, and a newer request
  // (a Refresh press) makes any older answer still on its way count for nothing.
  const load = useCallback((manual = false) => {
    window.clearTimeout(timer.current)
    const request = ++latest.current
    const started = performance.now()
    if (manual) setRefreshing(true)
    Promise.all([api.runtimeTargets(), api.runtimeJobs()])
      .then(([targetPayload, jobPayload]) => {
        if (request !== latest.current) return
        setError('')
        setTargets(targetPayload.items)
        setJobs(jobPayload.items)
      })
      .catch((reason) => {
        if (request !== latest.current) return
        setError(reason instanceof Error ? reason.message : 'Unable to read runtime targets.')
      })
      .finally(() => {
        if (request !== latest.current || stopped.current) return
        setLoading(false)
        // A quick answer still spins long enough to be seen.
        if (manual) window.setTimeout(() => setRefreshing(false), Math.max(0, MIN_SPIN_MS - (performance.now() - started)))
        timer.current = window.setTimeout(() => load(), POLL_MS)
      })
  }, [])

  useEffect(() => {
    stopped.current = false
    load()
    return () => {
      stopped.current = true
      latest.current += 1
      window.clearTimeout(timer.current)
    }
  }, [load])

  return (
    <div className="standard-page runtimes-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Network inference destinations</span>
          <h1>Runtimes</h1>
          <p>Send cached models to Ollama over HTTP or switch a vLLM rig through the authenticated runtime agent.</p>
        </div>
        <button type="button" className="secondary-button" onClick={() => load(true)} disabled={loading || refreshing} aria-busy={refreshing || undefined}>
          <RefreshCw size={16} className={loading || refreshing ? 'spin' : undefined} />
          Refresh
        </button>
      </div>

      {error && <div className="inline-error">{error}</div>}

      <section className="runtime-target-grid">
        {targets.map((target) => (
          <article key={target.id} className="runtime-target-card">
            <div className={`runtime-kind-icon ${target.kind}`}>
              {target.kind === 'ollama' ? <Cloud size={20} /> : <Server size={20} />}
            </div>
            <div>
              <span>{target.kind === 'ollama' ? 'Ollama' : 'vLLM agent'}</span>
              <h2>{target.name}</h2>
              <code>{target.base_url}</code>
              <p>
                {target.transfer_mode === 'blob-upload'
                  ? `Model blobs transfer over the LAN · keep alive ${target.keep_alive || '5m'}`
                  : `Shared model root ${target.remote_model_root}`}
              </p>
            </div>
            <span className="status-pill ok"><Check size={13} /> Configured</span>
          </article>
        ))}
        {loading && targets.length === 0 && <RowSkeletons rows={2} cells={1} label="Loading runtimes" />}
        {!loading && targets.length === 0 && (
          <div className="empty-state spacious runtime-empty">
            <Server size={34} />
            <h2>No runtime destinations configured</h2>
            <p>Add Ollama or vLLM targets through <code>RUNTIME_TARGETS_JSON</code>, then restart HuggingHack.</p>
          </div>
        )}
      </section>

      <section className="runtime-history">
        <div className="section-heading-line">
          <div>
            <span className="eyebrow">Persistent history</span>
            <h2>Runtime jobs</h2>
          </div>
          <span>{jobs.filter((job) => runtimeActiveStatuses.includes(job.status)).length} active</span>
        </div>
        <div className="download-list">
          {jobs.map((job) => (
            <article key={job.id} className="runtime-job-row">
              <div className={`runtime-state-icon ${job.status}`}>
                {job.status === 'failed'
                  ? <AlertCircle size={18} />
                  : job.status === 'ready'
                    ? <Check size={18} />
                    : <Server size={18} />}
              </div>
              <div className="runtime-job-main">
                <div className="runtime-job-heading">
                  <div>
                    <h3>{job.runtime_model_name}</h3>
                    <span>{job.repo_id} → {job.target_name}</span>
                  </div>
                  <strong className={`job-status ${job.status}`}>{job.status}</strong>
                </div>
                <p>{job.error || job.message}</p>
                {runtimeActiveStatuses.includes(job.status) && (
                  <>
                    <div className="job-progress">
                      <span style={{ width: `${job.progress}%` }} />
                    </div>
                    <div className="download-stats">
                      <span>{job.progress.toFixed(0)}%</span>
                      {job.target_kind === 'ollama' && job.total_bytes > 0 && (
                        <span>{formatBytes(job.processed_bytes)} of {formatBytes(job.total_bytes)}</span>
                      )}
                      <span>Updated {relativeTime(job.updated_at)}</span>
                    </div>
                  </>
                )}
                {!runtimeActiveStatuses.includes(job.status) && (
                  <div className="download-stats">
                    <span>{job.target_kind}</span>
                    <span>Finished {relativeTime(job.completed_at || job.updated_at)}</span>
                    {job.source_file && <code>{job.source_file}</code>}
                  </div>
                )}
              </div>
            </article>
          ))}
          {loading && jobs.length === 0 && <RowSkeletons rows={3} cells={2} label="Loading runtime jobs" />}
          {!loading && jobs.length === 0 && (
            <div className="empty-compact">Send a model from its model page to create the first runtime job.</div>
          )}
        </div>
      </section>
    </div>
  )
}

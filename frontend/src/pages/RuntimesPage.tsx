import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, Check, Cloud, LoaderCircle, RefreshCw, Server } from 'lucide-react'
import { api } from '../api'
import type { RuntimeJob, RuntimeTarget } from '../types'
import { formatBytes, relativeTime } from '../utils'

const runtimeActiveStatuses = ['queued', 'preparing', 'transferring', 'loading']

export function RuntimesPage() {
  const [targets, setTargets] = useState<RuntimeTarget[]>([])
  const [jobs, setJobs] = useState<RuntimeJob[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    setError('')
    Promise.all([api.runtimeTargets(), api.runtimeJobs()])
      .then(([targetPayload, jobPayload]) => {
        setTargets(targetPayload.items)
        setJobs(jobPayload.items)
      })
      .catch((reason) => {
        const message = reason instanceof Error ? reason.message : 'Unable to read runtime targets.'
        setError(message)
      })
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
    const timer = window.setInterval(load, 2000)
    return () => window.clearInterval(timer)
  }, [load])

  return (
    <div className="standard-page runtimes-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Network inference destinations</span>
          <h1>Runtimes</h1>
          <p>Send cached models to Ollama over HTTP or switch a vLLM rig through the authenticated runtime agent.</p>
        </div>
        <button type="button" className="secondary-button" onClick={load} disabled={loading}>
          {loading ? <LoaderCircle size={16} className="spin" /> : <RefreshCw size={16} />}
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
          {!loading && jobs.length === 0 && (
            <div className="empty-compact">Load a model from its local-library drawer to create the first runtime job.</div>
          )}
        </div>
      </section>
    </div>
  )
}

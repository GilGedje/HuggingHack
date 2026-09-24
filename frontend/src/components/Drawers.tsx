import 'katex/dist/katex.min.css'
import {
  Children,
  isValidElement,
  memo,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import ReactMarkdown, { defaultUrlTransform, type Components } from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import rehypeRaw from 'rehype-raw'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import {
  CloudDownload,
  ExternalLink,
  LoaderCircle,
  Rocket,
  Server,
  Trash2,
} from 'lucide-react'
import { api } from '../api'
import {
  modelCardHeadingId,
  modelCardSanitizeSchema,
  prepareModelCardMarkdown,
  resolveLocalModelCardUrl,
  resolveModelCardUrl,
} from '../modelCard'
import type {
  HubFile,
  RuntimeJob,
  RuntimeTarget,
} from '../types'
import { formatBytes } from '../utils'

function childText(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => {
      if (typeof child === 'string' || typeof child === 'number') return String(child)
      if (isValidElement<{ children?: ReactNode }>(child)) return childText(child.props.children)
      return ''
    })
    .join('')
}

function scrollToCardHeading(event: React.MouseEvent<HTMLAnchorElement>, href: string) {
  event.preventDefault()
  let targetId = href.slice(1)
  try {
    targetId = decodeURIComponent(targetId)
  } catch {
    // Keep the literal fragment when a model card contains malformed escaping.
  }
  const documentRoot = event.currentTarget.closest('.model-card-document')
  const target = Array.from(documentRoot?.querySelectorAll<HTMLElement>('[id]') || []).find(
    (element) => element.id === targetId,
  )
  target?.scrollIntoView({ block: 'start' })
}

const modelCardComponents: Components = {
  h1: ({ node: _node, children, ...props }) => (
    <h1 {...props} id={modelCardHeadingId(childText(children)) || undefined}>
      {children}
    </h1>
  ),
  h2: ({ node: _node, children, ...props }) => (
    <h2 {...props} id={modelCardHeadingId(childText(children)) || undefined}>
      {children}
    </h2>
  ),
  h3: ({ node: _node, children, ...props }) => (
    <h3 {...props} id={modelCardHeadingId(childText(children)) || undefined}>
      {children}
    </h3>
  ),
  h4: ({ node: _node, children, ...props }) => (
    <h4 {...props} id={modelCardHeadingId(childText(children)) || undefined}>
      {children}
    </h4>
  ),
  h5: ({ node: _node, children, ...props }) => (
    <h5 {...props} id={modelCardHeadingId(childText(children)) || undefined}>
      {children}
    </h5>
  ),
  h6: ({ node: _node, children, ...props }) => (
    <h6 {...props} id={modelCardHeadingId(childText(children)) || undefined}>
      {children}
    </h6>
  ),
  a: ({ node: _node, href, children, ...props }) => {
    const isHeadingLink = href?.startsWith('#') || false
    const showExternalIcon =
      !isHeadingLink && /^https?:/i.test(href || '') && childText(children).trim().length > 0
    return (
      <a
        {...props}
        href={href}
        target={isHeadingLink ? undefined : '_blank'}
        rel={isHeadingLink ? undefined : 'noreferrer noopener'}
        onClick={isHeadingLink && href ? (event) => scrollToCardHeading(event, href) : undefined}
      >
        {children}
        {showExternalIcon && <ExternalLink size={11} aria-hidden="true" />}
      </a>
    )
  },
  img: ({ node: _node, className, src, alt, ...props }) => {
    const badge =
      typeof src === 'string' &&
      /(?:img\.shields\.io|badge(?:s)?[./_-]|colab-badge)/i.test(src)
    const classes = [className, badge ? 'model-card-badge' : ''].filter(Boolean).join(' ')
    return (
      <img
        {...props}
        className={classes || undefined}
        src={src}
        alt={alt || ''}
        loading="lazy"
        decoding="async"
      />
    )
  },
}

export const ModelCardDocument = memo(function ModelCardDocument({
  source,
  sourceUrl,
  revision,
  localRepoId,
}: {
  source: string
  sourceUrl: string
  revision: string
  localRepoId?: string
}) {
  const preparedSource = useMemo(() => prepareModelCardMarkdown(source), [source])
  return (
    <article className="model-card-document">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }]]}
        rehypePlugins={[
          rehypeRaw,
          [rehypeSanitize, modelCardSanitizeSchema],
          rehypeKatex,
        ]}
        urlTransform={(url, attribute) => {
          const resolved = localRepoId
            ? resolveLocalModelCardUrl(url, attribute, localRepoId)
            : resolveModelCardUrl(url, attribute, sourceUrl, revision)
          return resolved === null ? null : defaultUrlTransform(resolved)
        }}
        components={modelCardComponents}
      >
        {preparedSource}
      </ReactMarkdown>
    </article>
  )
})

const activeRuntimeStatuses = ['queued', 'preparing', 'transferring', 'loading']

interface ModelActionsProps {
  repoId: string
  storageBackend: 'filesystem' | 's3'
  cached: boolean
  files: HubFile[]
  canManageRuntimes: boolean
  canManageCache: boolean
  onCacheChanged: () => void
  onToast: (message: string, tone?: 'success' | 'error') => void
}

export function ModelActions({
  repoId,
  storageBackend,
  cached,
  files,
  canManageRuntimes,
  canManageCache,
  onCacheChanged,
  onToast,
}: ModelActionsProps) {
  const [error, setError] = useState('')
  const [changingCache, setChangingCache] = useState(false)
  const [runtimeTargets, setRuntimeTargets] = useState<RuntimeTarget[]>([])
  const [runtimeTargetId, setRuntimeTargetId] = useState('')
  const [runtimeModelName, setRuntimeModelName] = useState('')
  const [runtimeSourceFile, setRuntimeSourceFile] = useState('')
  const [runtimeJob, setRuntimeJob] = useState<RuntimeJob | null>(null)
  const [dispatching, setDispatching] = useState(false)

  useEffect(() => {
    setError('')
    setRuntimeJob(null)
    setRuntimeModelName(repoId.replace('/', '-').toLowerCase())
    setRuntimeSourceFile('')
  }, [repoId])

  useEffect(() => {
    if (!canManageRuntimes) {
      setRuntimeTargets([])
      setRuntimeTargetId('')
      return
    }
    api
      .runtimeTargets()
      .then((payload) => {
        setRuntimeTargets(payload.items)
        setRuntimeTargetId((current) =>
          payload.items.some((target) => target.id === current)
            ? current
            : payload.items[0]?.id || '',
        )
      })
      .catch(() => setRuntimeTargets([]))
  }, [canManageRuntimes, repoId])

  useEffect(() => {
    if (!runtimeJob || !activeRuntimeStatuses.includes(runtimeJob.status)) return
    const timer = window.setTimeout(() => {
      api
        .runtimeJob(runtimeJob.id)
        .then((next) => {
          setRuntimeJob(next)
          if (next.status === 'ready') {
            onToast(`${next.runtime_model_name} is ready on ${next.target_name}.`)
          } else if (next.status === 'failed') {
            onToast(next.error || 'Runtime load failed.', 'error')
          }
        })
        .catch(() => undefined)
    }, 1200)
    return () => window.clearTimeout(timer)
  }, [onToast, runtimeJob])

  const ggufFiles = files.filter((file) => file.path.toLowerCase().endsWith('.gguf'))
  const selectedTarget = runtimeTargets.find((target) => target.id === runtimeTargetId)
  const needsSourceFile = selectedTarget?.kind === 'ollama' && ggufFiles.length > 1
  const runtimeBusy = Boolean(runtimeJob && activeRuntimeStatuses.includes(runtimeJob.status))

  async function changeCache() {
    if (storageBackend !== 's3') return
    if (
      cached
      && !window.confirm(
        `Remove the local cache for ${repoId}? The complete S3 copy will remain.`,
      )
    ) return
    setChangingCache(true)
    setError('')
    try {
      if (cached) {
        await api.evictLocalModelCache(repoId)
        onToast(`${repoId} is now stored in S3 only.`)
      } else {
        await api.restoreLocalModel(repoId)
        onToast(`${repoId} was restored to the local cache.`)
      }
      onCacheChanged()
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Could not update the model cache.'
      setError(message)
      onToast(message, 'error')
    } finally {
      setChangingCache(false)
    }
  }

  async function dispatchRuntime() {
    if (!runtimeTargetId || !runtimeModelName.trim()) return
    setDispatching(true)
    setError('')
    try {
      const job = await api.loadRuntime(runtimeTargetId, {
        repo_id: repoId,
        runtime_model_name: runtimeModelName.trim(),
        ...(runtimeSourceFile ? { source_file: runtimeSourceFile } : {}),
      })
      setRuntimeJob(job)
      onToast(`${repoId} was queued for ${job.target_name}.`)
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Could not send the model to the runtime.'
      setError(message)
      onToast(message, 'error')
    } finally {
      setDispatching(false)
    }
  }

  return (
    <>
      {error && <div className="inline-error">{error}</div>}
      {storageBackend === 's3' && canManageCache && (
        <div className="local-storage-actions">
          <button
            type="button"
            className="secondary-button"
            disabled={changingCache}
            onClick={changeCache}
          >
            {changingCache
              ? <LoaderCircle size={16} className="spin" />
              : cached ? <Trash2 size={16} /> : <CloudDownload size={16} />}
            {changingCache
              ? 'Working…'
              : cached ? 'Remove local cache' : 'Restore to local cache'}
          </button>
          <p>
            {cached
              ? 'The durable S3 copy stays available.'
              : 'Restore before loading this model in vLLM, llama.cpp, or another local runtime.'}
          </p>
        </div>
      )}
      {canManageRuntimes && runtimeTargets.length > 0 && (
        <section className="runtime-dispatch">
          <div className="runtime-dispatch-title">
            <Server size={17} />
            <div>
              <strong>Send to runtime</strong>
              <p>Transfer to Ollama or start this shared path through a vLLM agent.</p>
            </div>
          </div>
          <div className="runtime-dispatch-form">
            <label>
              Destination
              <select
                value={runtimeTargetId}
                onChange={(event) => {
                  setRuntimeTargetId(event.target.value)
                  setRuntimeSourceFile('')
                }}
              >
                {runtimeTargets.map((target) => (
                  <option key={target.id} value={target.id}>
                    {target.name} · {target.kind}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Served model name
              <input
                value={runtimeModelName}
                onChange={(event) => setRuntimeModelName(event.target.value)}
                maxLength={128}
              />
            </label>
            {needsSourceFile && (
              <label className="runtime-source-field">
                GGUF quantization
                <select
                  value={runtimeSourceFile}
                  onChange={(event) => setRuntimeSourceFile(event.target.value)}
                >
                  <option value="">Choose a GGUF file</option>
                  {ggufFiles.map((file) => (
                    <option key={file.path} value={file.path}>
                      {file.path} · {formatBytes(file.size)}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button
              type="button"
              className="download-button"
              onClick={dispatchRuntime}
              disabled={
                dispatching
                || !cached
                || !runtimeTargetId
                || !runtimeModelName.trim()
                || (needsSourceFile && !runtimeSourceFile)
                || runtimeBusy
              }
            >
              {dispatching || runtimeBusy
                ? <LoaderCircle size={16} className="spin" />
                : <Rocket size={16} />}
              {runtimeBusy && runtimeJob
                ? runtimeJob.message
                : dispatching ? 'Queuing…' : 'Load model'}
            </button>
          </div>
          {runtimeJob && (
            <div className={`runtime-job-inline ${runtimeJob.status}`}>
              <div>
                <span>{runtimeJob.message}</span>
                <strong>{runtimeJob.progress.toFixed(0)}%</strong>
              </div>
              <div className="job-progress">
                <span style={{ width: `${runtimeJob.progress}%` }} />
              </div>
              {runtimeJob.error && <p>{runtimeJob.error}</p>}
            </div>
          )}
        </section>
      )}
    </>
  )
}

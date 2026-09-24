import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Copy, GitBranch, Rocket, Terminal, X } from 'lucide-react'
import { api } from '../api'
import {
  gitCloneSnippets,
  hfCliSnippets,
  isLoopbackUrl,
  resolveServerUrl,
  vllmDockerSnippets,
  vllmPipSnippets,
  type Snippet,
} from '../useModel'

export type UseModelMode = 'vllm' | 'clone'

interface UseModelDialogProps {
  repoId: string
  pipelineTag?: string | null
  vllmSupported: boolean
  mode: UseModelMode
  onModeChange: (mode: UseModelMode) => void
  onClose: () => void
}

async function copyText(text: string): Promise<void> {
  // navigator.clipboard only exists in secure contexts, and LAN installs are
  // usually served over plain http, so fall back to a hidden textarea.
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text)
    return
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    if (!document.execCommand('copy')) throw new Error('Copy was blocked by the browser.')
  } finally {
    document.body.removeChild(textarea)
  }
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 1600)
    return () => window.clearTimeout(timer)
  }, [copied])
  return (
    <button
      type="button"
      className="snippet-copy"
      onClick={() => {
        copyText(text).then(() => setCopied(true)).catch(() => setCopied(false))
      }}
      aria-label={copied ? 'Copied' : label}
      title={copied ? 'Copied' : label}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </button>
  )
}

function SnippetList({ snippets }: { snippets: Snippet[] }) {
  return (
    <ol className="snippet-list">
      {snippets.map((snippet) => (
        <li key={snippet.code}>
          <p># {snippet.comment}</p>
          <div className="snippet-code">
            <pre>
              <code>{snippet.code}</code>
            </pre>
            <CopyButton text={snippet.code} label="Copy command" />
          </div>
        </li>
      ))}
    </ol>
  )
}

export function UseModelDialog({
  repoId,
  pipelineTag,
  vllmSupported,
  mode,
  onModeChange,
  onClose,
}: UseModelDialogProps) {
  const [publicUrl, setPublicUrl] = useState<string | null>(null)
  const [hubEnabled, setHubEnabled] = useState(true)
  const [vllmVariant, setVllmVariant] = useState<'pip' | 'docker'>('pip')
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const activeMode: UseModelMode = mode === 'vllm' && !vllmSupported ? 'clone' : mode

  useEffect(() => {
    api
      .health()
      .then((health) => {
        setPublicUrl(health.public_url || null)
        setHubEnabled(health.hub_api_enabled !== false)
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    closeButtonRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown, true)
    return () => window.removeEventListener('keydown', handleKeyDown, true)
  }, [onClose])

  const server = resolveServerUrl(publicUrl, window.location.origin)

  return (
    <div className="use-model-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="use-model-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="use-model-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="use-model-header">
          <div>
            <span className="eyebrow">Use this model</span>
            <h2 id="use-model-title">{repoId}</h2>
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="icon-button"
            onClick={onClose}
            aria-label="Close"
          >
            <X size={20} />
          </button>
        </header>

        <div className="drawer-tabs" role="tablist" aria-label="How to use this model">
          {vllmSupported && (
            <button
              type="button"
              role="tab"
              aria-selected={activeMode === 'vllm'}
              className={activeMode === 'vllm' ? 'active' : ''}
              onClick={() => onModeChange('vllm')}
            >
              <Rocket size={14} /> vLLM
            </button>
          )}
          <button
            type="button"
            role="tab"
            aria-selected={activeMode === 'clone'}
            className={activeMode === 'clone' ? 'active' : ''}
            onClick={() => onModeChange('clone')}
          >
            <GitBranch size={14} /> Clone repository
          </button>
        </div>

        <div className="use-model-body">
          <div className="pull-endpoint">
            <div>
              <span>Pull endpoint</span>
              <code>HF_ENDPOINT={server}</code>
            </div>
            <CopyButton text={server} label="Copy pull endpoint" />
          </div>
          {!hubEnabled && (
            <div className="security-note warning">
              <AlertTriangle size={16} />
              Pulling is turned off on this server. Set HUB_API_ENABLED=true to allow it.
            </div>
          )}
          {isLoopbackUrl(server) && (
            <div className="security-note warning">
              <AlertTriangle size={16} />
              This address only works on this computer. Set PUBLIC_URL in .env to the
              server&apos;s LAN address so other machines can pull.
            </div>
          )}

          {activeMode === 'vllm' ? (
            <>
              <div className="snippet-variants" role="radiogroup" aria-label="vLLM install method">
                {(
                  [
                    ['pip', 'Install from pip'],
                    ['docker', 'Use Docker images'],
                  ] as const
                ).map(([id, label]) => (
                  <button
                    type="button"
                    key={id}
                    role="radio"
                    aria-checked={vllmVariant === id}
                    className={vllmVariant === id ? 'selected' : ''}
                    onClick={() => setVllmVariant(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <SnippetList
                snippets={
                  vllmVariant === 'pip'
                    ? vllmPipSnippets(repoId, server, pipelineTag)
                    : vllmDockerSnippets(repoId, server, pipelineTag)
                }
              />
            </>
          ) : (
            <>
              <h3 className="snippet-heading">
                <GitBranch size={15} /> Git
              </h3>
              <SnippetList snippets={gitCloneSnippets(repoId, server)} />
              <h3 className="snippet-heading">
                <Terminal size={15} /> Hugging Face CLI
              </h3>
              <SnippetList snippets={hfCliSnippets(repoId, server)} />
              {!vllmSupported && (
                <p className="use-model-note">
                  vLLM needs SafeTensors weights with a model config. Use the files from a clone
                  with llama.cpp, Ollama, or LM Studio instead.
                </p>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  )
}

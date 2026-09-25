import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, Check, Copy, GitBranch, Rocket, Terminal, X } from 'lucide-react'
import { api } from '../api'
import { useClosingTransition, useFadeOnChange, useTabIndicator } from '../motion'
import { DialogFrame } from './Dialog'
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
  // usually served over plain http, so fall back to a hidden textarea; the
  // fallback also covers a clipboard permission the browser refuses.
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {
      // Try the older way below.
    }
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

/** Selects the text a copy button sits beside, so it is ready for a manual copy. */
function selectNearby(button: HTMLElement | null) {
  const scope = button?.closest('.snippet-code, .config-file, .pull-endpoint') || button?.parentElement
  const text = scope?.querySelector('pre') || scope?.querySelector('code')
  if (text) window.getSelection()?.selectAllChildren(text)
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const button = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (state === 'idle') return
    const timer = window.setTimeout(() => setState('idle'), state === 'failed' ? 5000 : 1600)
    return () => window.clearTimeout(timer)
  }, [state])
  const name = state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy. Select the text and copy it yourself.' : label
  return (
    <button
      ref={button}
      type="button"
      className={state === 'failed' ? 'snippet-copy failed' : 'snippet-copy'}
      onClick={() => {
        copyText(text)
          .then(() => setState('copied'))
          .catch(() => {
            // Said out loud and on screen, with the text selected for a manual copy.
            setState('failed')
            selectNearby(button.current)
          })
      }}
      aria-label={name}
      title={name}
    >
      {state === 'copied' ? <Check size={14} /> : state === 'failed' ? <X size={14} /> : <Copy size={14} />}
      {state === 'failed' && (
        <span className="copy-failed" role="status">
          Couldn&apos;t copy. Select and copy it yourself.
        </span>
      )}
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

  const { closing, close } = useClosingTransition(onClose)
  const tabs = useTabIndicator<HTMLDivElement>(`${activeMode}:${vllmSupported}`)
  const body = useFadeOnChange<HTMLDivElement>(activeMode)

  useEffect(() => closeButtonRef.current?.focus(), [])

  const server = resolveServerUrl(publicUrl, window.location.origin)

  return (
    <DialogFrame labelledBy="use-model-title" closing={closing} onDismiss={close}>
      <header className="use-model-header">
        <div>
          <span className="eyebrow">Use this model</span>
          <h2 id="use-model-title">{repoId}</h2>
        </div>
        <button
          ref={closeButtonRef}
          type="button"
          className="icon-button"
          onClick={close}
          aria-label="Close"
        >
          <X size={20} />
        </button>
      </header>

      <div className="drawer-tabs" role="tablist" aria-label="How to use this model" ref={tabs}>
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

      <div className="use-model-body" ref={body}>
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
    </DialogFrame>
  )
}

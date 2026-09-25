import {
  Boxes,
  Check,
  Cloud,
  Cpu,
  FileBox,
  HardDrive,
  Heart,
  RefreshCw,
  Rocket,
} from 'lucide-react'
import { precisionLabel } from '../catalog'
import type { LibraryModel, ModelFormat } from '../types'
import { formatBytes, formatNumber, initials, relativeTime, taskLabel } from '../utils'

function visualClass(task?: string | null): string {
  if (!task) return 'model-visual-neutral'
  if (task.includes('image') || task.includes('video')) return 'model-visual-vision'
  if (task.includes('audio') || task.includes('speech')) return 'model-visual-audio'
  if (task.includes('generation')) return 'model-visual-generation'
  if (task.includes('embedding') || task.includes('similarity') || task.includes('extraction')) {
    return 'model-visual-embedding'
  }
  return 'model-visual-neutral'
}

function parameterLevel(value?: number | null): number {
  if (!value) return 0
  const billions = value / 1_000_000_000
  if (billions < 1) return 1
  if (billions < 7) return 2
  if (billions < 32) return 3
  if (billions < 128) return 4
  if (billions < 500) return 5
  return 6
}

export const formatLabels: Record<ModelFormat, string> = {
  safetensors: 'SafeTensors',
  gguf: 'GGUF',
  pytorch: 'PyTorch',
  onnx: 'ONNX',
  tensorflow: 'TensorFlow',
  flax: 'Flax',
}

function libraryTags(model: LibraryModel): string[] {
  // Every library model is SafeTensors, so the precision says more than the format.
  const precision = precisionLabel(model.precision)
  const labels = precision ? [precision] : model.formats.map((format) => formatLabels[format] || format)
  if (model.library_name && !model.formats.includes(model.library_name as ModelFormat)) {
    labels.push(model.library_name)
  }
  const ignored = new Set(
    [model.pipeline_tag || '', `license:${model.license || ''}`, ...labels].map((value) =>
      value.toLowerCase(),
    ),
  )
  const tags = model.tags.filter(
    (tag) => !ignored.has(tag.toLowerCase()) && !tag.startsWith('arxiv:') && tag.length < 28,
  )
  return [...labels, ...tags].slice(0, 3)
}

interface LibraryRowProps {
  model: LibraryModel
  onOpen: (repoId: string) => void
  onUse: (model: LibraryModel) => void
  onSave: (model: LibraryModel) => void
  saving?: boolean
  /** Hardware id → label, from the search facets. */
  hardwareLabels?: Record<string, string>
}

export function LibraryModelRow({ model, onOpen, onUse, onSave, saving, hardwareLabels = {} }: LibraryRowProps) {
  const [owner, ...nameParts] = model.id.split('/')
  const name = nameParts.join('/')
  const level = parameterLevel(model.parameter_count)
  const remoteOnly = model.storage_backend === 's3' && !model.cached
  return (
    <article
      className="model-card"
      onClick={() => onOpen(model.id)}
      tabIndex={0}
      role="button"
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onOpen(model.id)
        }
      }}
      aria-label={`Open ${model.id} model details`}
      aria-haspopup="dialog"
    >
      <div className={`model-visual ${visualClass(model.pipeline_tag)}`} aria-hidden="true">
        <div className="model-visual-topline">
          <span>{taskLabel(model.pipeline_tag)}</span>
          <span className="visual-local-badge">
            {remoteOnly ? <Cloud size={11} /> : <Check size={11} />}
            {remoteOnly ? ' S3 only' : ' On disk'}
          </span>
        </div>
        <div className="model-visual-core">
          <span className="model-monogram">{initials(model.id)}</span>
          <div className="parameter-viz">
            {Array.from({ length: 6 }).map((_, index) => (
              <span key={index} className={index < level ? 'filled' : ''} />
            ))}
          </div>
        </div>
        <div className="model-visual-caption">
          <Boxes size={13} />
          <span>{model.parameter_count ? `${formatNumber(model.parameter_count)} parameters` : 'Repository model'}</span>
        </div>
      </div>
      <div className="model-card-body">
        <div className="model-owner-line">
          <span>{owner}</span>
        </div>
        <h3 title={model.id}>{name || model.id}</h3>
        <div className="repo-tags">
          {libraryTags(model).map((tag) => (
            <span key={tag}>{tag}</span>
          ))}
        </div>
        <div className="model-card-meta">
          <span>Updated {relativeTime(model.last_modified)}</span>
          <span>
            <HardDrive size={13} /> {formatBytes(model.size_bytes)}
          </span>
          <span>
            <FileBox size={13} /> {formatNumber(model.file_count)}
          </span>
          {model.hardware.length > 0 && (
            <span title="Tested hardware">
              <Cpu size={13} /> {model.hardware.map((id) => hardwareLabels[id] || id).join(', ')}
            </span>
          )}
        </div>
        <div className="model-card-actions">
          <button
            type="button"
            className="download-button compact model-card-action"
            onClick={(event) => {
              event.stopPropagation()
              onUse(model)
            }}
          >
            <Rocket size={15} />
            Use model
          </button>
          <button
            type="button"
            className={model.saved ? 'save-model-button saved' : 'save-model-button'}
            disabled={saving}
            onClick={(event) => {
              event.stopPropagation()
              onSave(model)
            }}
            aria-label={model.saved ? `Remove ${model.id} from saved models` : `Save ${model.id}`}
            title={model.saved ? 'Remove from saved models' : 'Save for later'}
          >
            {saving ? (
              <RefreshCw size={16} className="spin" />
            ) : (
              <Heart size={16} fill={model.saved ? 'currentColor' : 'none'} />
            )}
          </button>
        </div>
      </div>
    </article>
  )
}

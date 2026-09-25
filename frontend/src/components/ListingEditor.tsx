import { useEffect, useState } from 'react'
import { RotateCcw } from 'lucide-react'
import { api } from '../api'
import { precisionLabel, TASKS } from '../catalog'
import { parseParameters, parseTags, tidyOverrides, writeParameters } from '../listingFields'
import { RELATIONS, relationName } from '../modelTree'
import type { ListingFields, ListingOverrides, ModelListing } from '../types'
import { formatNumber, taskLabel } from '../utils'

type Field = keyof ListingFields

const FIELDS: Array<{ id: Field; label: string; hint: string }> = [
  { id: 'pipeline_tag', label: 'Task', hint: 'From the model card, or the config when the card has none' },
  { id: 'precision', label: 'Precision', hint: 'From the quantization config, the dtype, or the weight headers' },
  { id: 'parameter_count', label: 'Parameters', hint: 'Counted from the weight headers' },
  { id: 'library_name', label: 'Library', hint: 'From the model card' },
  { id: 'license', label: 'License', hint: 'From the model card' },
  { id: 'tags', label: 'Tags', hint: 'From the model card' },
  { id: 'base_model', label: 'Base model', hint: 'The model this one was made from, from the card’s base_model' },
  { id: 'base_model_relation', label: 'Made as', hint: 'From the card, or worked out from the weights: quantized weights make a quantization' },
]
const REPO_ID = /^[A-Za-z0-9][\w.-]{0,95}\/[\w.-]{1,96}$/
const OTHER_TASK = '__other'

/** The task the files name, leaving out config.model_type, which is not a task. */
function detectedValue(listing: ModelListing, field: Field): unknown {
  const value = listing.detected[field]
  if (field === 'pipeline_tag' && value && value === listing.detected.model_type) return null
  return value ?? null
}

/** A field for reading. `exact` skips the name-based size, for counts it does not describe. */
function show(field: Field, value: unknown, listing: ModelListing, exact: boolean): string {
  if (value == null || (Array.isArray(value) && value.length === 0)) return '—'
  if (field === 'pipeline_tag') return taskLabel(String(value))
  if (field === 'precision') return precisionLabel(String(value)) || String(value)
  if (field === 'parameter_count') {
    // The library lists a model by the size its name gives when that agrees with the count.
    const nominal = !exact ? listing.nominal_parameters : null
    const count = Number(value)
    return nominal && nominal !== count
      ? `${writeParameters(nominal)} (counted ${formatNumber(count)})`
      : `${writeParameters(count)} (${count.toLocaleString('en')})`
  }
  if (field === 'tags') return (value as string[]).join(', ')
  if (field === 'base_model_relation') return relationName(String(value))
  return String(value)
}

function draftFor(field: Field, value: unknown): string {
  if (value == null) return ''
  if (field === 'parameter_count') return writeParameters(Number(value))
  if (field === 'tags') return (value as string[]).join(', ')
  return String(value)
}

/**
 * How a model will be listed, one field per row: what its files say, and a way
 * to correct each field. Corrections equal to the detected value are dropped.
 */
export function ListingEditor({
  listing,
  overrides,
  onChange,
  disabled = false,
}: {
  listing: ModelListing
  overrides: ListingOverrides
  onChange: (overrides: ListingOverrides) => void
  disabled?: boolean
}) {
  const [editing, setEditing] = useState<Field | null>(null)
  const [draft, setDraft] = useState('')
  const [otherTask, setOtherTask] = useState(false)
  const [problem, setProblem] = useState('')
  const detected = Object.fromEntries(FIELDS.map(({ id }) => [id, detectedValue(listing, id)])) as ListingOverrides
  // Library models to pick a base model from; any Hugging Face id also works.
  const [libraryIds, setLibraryIds] = useState<string[]>([])
  useEffect(() => {
    api
      .libraryModels(new URLSearchParams({ sort: 'name' }))
      .then((payload) => setLibraryIds(payload.items.map((item) => item.id)))
      .catch(() => undefined)
  }, [])
  const hasBase = Boolean(overrides.base_model ?? detected.base_model)

  function start(field: Field) {
    const value = overrides[field] ?? detected[field]
    const text = draftFor(field, value)
    setEditing(field)
    setDraft(text)
    setOtherTask(field === 'pipeline_tag' && Boolean(text) && !TASKS.includes(text))
    setProblem('')
  }

  function apply(field: Field) {
    let value: unknown = draft.trim() || null
    if (field === 'parameter_count' && value != null) {
      value = parseParameters(draft)
      if (value == null) {
        setProblem('Write a size like 8B, 595M, or a whole number.')
        return
      }
    }
    if (field === 'pipeline_tag' && value != null) {
      value = String(value).toLowerCase().replace(/\s+/g, '-')
      if (!/^[a-z0-9][a-z0-9-]{0,59}$/.test(String(value))) {
        setProblem('Use a Hugging Face task id, like text-generation.')
        return
      }
    }
    if (field === 'tags') value = parseTags(draft)
    if (field === 'base_model' && value != null && !REPO_ID.test(String(value))) {
      setProblem('Use a repository id, like Qwen/Qwen3-0.6B.')
      return
    }
    if (field === 'base_model' && value != null && !(overrides.base_model_relation ?? detected.base_model_relation)) {
      // A base named by hand needs a relation too; weights decide the likely one.
      const quantized = ['fp8', 'int8', 'nvfp4', 'mxfp4', 'int4'].includes(String(overrides.precision ?? detected.precision))
      onChange(tidyOverrides({ ...overrides, base_model: String(value), base_model_relation: quantized ? 'quantized' : 'finetune' }, detected))
      setEditing(null)
      return
    }
    onChange(tidyOverrides({ ...overrides, [field]: value }, detected))
    setEditing(null)
  }

  function reset(field: Field) {
    const next = { ...overrides }
    delete next[field]
    onChange(next)
    if (editing === field) setEditing(null)
  }

  return (
    <dl className="listing-editor">
      {FIELDS.filter(({ id }) => id !== 'base_model_relation' || hasBase).map(({ id, label, hint }) => {
        const corrected = overrides[id] !== undefined
        const value = corrected ? overrides[id] : detected[id]
        return (
          <div key={id} className={corrected ? 'listing-row corrected' : 'listing-row'}>
            <dt>
              {label}
              <small>{hint}</small>
            </dt>
            <dd>
              {editing === id ? (
                // Not a <form>: the upload wizard around it already is one.
                <div
                  className="listing-edit"
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      apply(id)
                    } else if (event.key === 'Escape') {
                      setEditing(null)
                    }
                  }}
                >
                  {id === 'pipeline_tag' && !otherTask ? (
                    <select
                      value={draft}
                      aria-label={label}
                      autoFocus
                      onChange={(event) => {
                        if (event.target.value === OTHER_TASK) {
                          setOtherTask(true)
                          setDraft('')
                        } else setDraft(event.target.value)
                      }}
                    >
                      <option value="" disabled>Choose a task</option>
                      {TASKS.map((task) => <option key={task} value={task}>{taskLabel(task)}</option>)}
                      <option value={OTHER_TASK}>Other task…</option>
                    </select>
                  ) : id === 'base_model_relation' ? (
                    <select value={draft} aria-label={label} autoFocus onChange={(event) => setDraft(event.target.value)}>
                      <option value="" disabled>Choose how it was made</option>
                      {RELATIONS.map((item) => <option key={item} value={item}>{relationName(item)}</option>)}
                    </select>
                  ) : id === 'base_model' ? (
                    <>
                      <input
                        value={draft}
                        aria-label={label}
                        autoFocus
                        list="listing-library-models"
                        placeholder="Qwen/Qwen3-0.6B"
                        onChange={(event) => setDraft(event.target.value)}
                        maxLength={200}
                      />
                      <datalist id="listing-library-models">
                        {libraryIds.map((item) => <option key={item} value={item} />)}
                      </datalist>
                    </>
                  ) : id === 'precision' ? (
                    <select value={draft} aria-label={label} autoFocus onChange={(event) => setDraft(event.target.value)}>
                      <option value="" disabled>Choose a precision</option>
                      {listing.precisions.map((item) => <option key={item} value={item}>{precisionLabel(item)}</option>)}
                    </select>
                  ) : (
                    <input
                      value={draft}
                      aria-label={label}
                      autoFocus
                      onChange={(event) => setDraft(event.target.value)}
                      placeholder={
                        id === 'parameter_count' ? '8B'
                          : id === 'pipeline_tag' ? 'text-to-speech'
                            : id === 'tags' ? 'llama, fp8'
                              : id === 'license' ? 'apache-2.0'
                                : 'transformers'
                      }
                      maxLength={id === 'tags' ? 2000 : 100}
                    />
                  )}
                  <button type="button" className="secondary-button compact" onClick={() => apply(id)}>Done</button>
                  <button type="button" className="text-link" onClick={() => setEditing(null)}>Cancel</button>
                  {problem && <p className="field-hint problem">{problem}</p>}
                </div>
              ) : (
                <>
                  <span className={value == null || (Array.isArray(value) && !value.length) ? 'listing-value empty' : 'listing-value'}>
                    {show(id, value, listing, corrected)}
                  </span>
                  {corrected && (
                    <span className="listing-detected">
                      {/* The server's nominal size already includes corrections, so the files' own count is shown as is. */}
                      Files say {show(id, detected[id], listing, true)}
                      {!disabled && (
                        <button type="button" onClick={() => reset(id)} title="Use what the files say">
                          <RotateCcw size={11} /> Reset
                        </button>
                      )}
                    </span>
                  )}
                  {!disabled && (
                    <button type="button" className="listing-edit-button" onClick={() => start(id)}>
                      {corrected ? 'Change' : 'Edit'}
                    </button>
                  )}
                </>
              )}
            </dd>
          </div>
        )
      })}
    </dl>
  )
}

import { Check } from 'lucide-react'
import {
  PARAMETER_STOPS,
  PRECISIONS,
  TASKS,
  isFullRange,
  parameterQuery,
  parameterRangeLabel,
  stopLabel,
} from '../catalog'
import type { LibraryFacets } from '../types'
import { taskLabel } from '../utils'

const LAST_STOP = PARAMETER_STOPS.length - 1
// Stops that get a label under the slider; the others only get a tick.
const LABELLED_STOPS = [0, 3, 5, 7, LAST_STOP]

export interface ModelFilterState {
  tasks: string[]
  precision: string[]
  hardware: string[]
  /** Slider stop indexes into PARAMETER_STOPS. */
  size: [number, number]
}

export const EMPTY_FILTERS: ModelFilterState = { tasks: [], precision: [], hardware: [], size: [0, LAST_STOP] }

export function activeFilterCount(filters: ModelFilterState): number {
  return (
    filters.tasks.length +
    filters.precision.length +
    filters.hardware.length +
    (isFullRange(...filters.size) ? 0 : 1)
  )
}

/** Adds the filters to a library search; values within one group match any of them. */
export function applyFilters(filters: ModelFilterState, params: URLSearchParams): URLSearchParams {
  if (filters.tasks.length) params.set('task', filters.tasks.join(','))
  if (filters.precision.length) params.set('precision', filters.precision.join(','))
  if (filters.hardware.length) params.set('hardware', filters.hardware.join(','))
  const parameters = parameterQuery(...filters.size)
  if (parameters) params.set('parameters', parameters)
  return params
}

function toggle(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value]
}

function Option({
  label,
  count,
  selected,
  onToggle,
}: {
  label: string
  count: number
  selected: boolean
  onToggle: () => void
}) {
  return (
    <button
      type="button"
      className={[selected ? 'selected' : '', count ? '' : 'empty'].filter(Boolean).join(' ')}
      aria-pressed={selected}
      onClick={onToggle}
    >
      <span className="filter-check">{selected && <Check size={12} />}</span>
      <span className="filter-label">{label}</span>
      <span className="filter-count">{count}</span>
    </button>
  )
}

function ParameterRange({ value, onChange }: { value: [number, number]; onChange: (value: [number, number]) => void }) {
  const [low, high] = value
  const percent = (index: number) => `${(index / LAST_STOP) * 100}%`
  return (
    <section className="filter-group parameter-range">
      <h3>
        Parameters <span>{parameterRangeLabel(low, high)}</span>
      </h3>
      <div
        className="range-slider"
        style={{ '--range-start': percent(low), '--range-end': percent(high) } as React.CSSProperties}
      >
        <div className="range-track" aria-hidden="true" />
        <input
          type="range"
          min={0}
          max={LAST_STOP}
          step={1}
          value={low}
          // At the right end the low thumb must stay on top, or it cannot be dragged back.
          className={low === LAST_STOP ? 'range-low on-top' : 'range-low'}
          aria-label="Smallest model size"
          aria-valuetext={stopLabel(low)}
          onChange={(event) => onChange([Math.min(Number(event.target.value), high), high])}
        />
        <input
          type="range"
          min={0}
          max={LAST_STOP}
          step={1}
          value={high}
          className="range-high"
          aria-label="Largest model size"
          aria-valuetext={stopLabel(high)}
          onChange={(event) => onChange([low, Math.max(Number(event.target.value), low)])}
        />
      </div>
      <div className="range-ticks" aria-hidden="true">
        {PARAMETER_STOPS.map((_, index) => (
          <span key={index} style={{ left: percent(index) }} className={LABELLED_STOPS.includes(index) ? 'labelled' : ''}>
            {LABELLED_STOPS.includes(index) && <em>{index === 0 ? '<1B' : stopLabel(index)}</em>}
          </span>
        ))}
      </div>
    </section>
  )
}

export function ModelFilters({
  facets,
  value,
  onChange,
}: {
  facets: LibraryFacets
  value: ModelFilterState
  onChange: (value: ModelFilterState) => void
}) {
  // Tasks beyond the usual ones still need a way in, so list any the library has.
  const otherTasks = Object.keys(facets.tasks)
    .filter((task) => !TASKS.includes(task))
    .sort((a, b) => taskLabel(a).localeCompare(taskLabel(b)))

  return (
    <>
      <ParameterRange value={value.size} onChange={(size) => onChange({ ...value, size })} />
      <section className="filter-group">
        <h3>Tasks</h3>
        {[...TASKS, ...otherTasks].map((task) => (
          <Option
            key={task}
            label={taskLabel(task)}
            count={facets.tasks[task] || 0}
            selected={value.tasks.includes(task)}
            onToggle={() => onChange({ ...value, tasks: toggle(value.tasks, task) })}
          />
        ))}
      </section>
      <section className="filter-group">
        <h3>Precision</h3>
        {PRECISIONS.map(([id, label]) => (
          <Option
            key={id}
            label={label}
            count={facets.precision[id] || 0}
            selected={value.precision.includes(id)}
            onToggle={() => onChange({ ...value, precision: toggle(value.precision, id) })}
          />
        ))}
      </section>
      <section className="filter-group">
        <h3>Hardware</h3>
        {facets.hardware.map(([id, label, count]) => (
          <Option
            key={id}
            label={label}
            count={count}
            selected={value.hardware.includes(id)}
            onToggle={() => onChange({ ...value, hardware: toggle(value.hardware, id) })}
          />
        ))}
      </section>
    </>
  )
}

import { useEffect, useRef, useState, type FormEvent } from 'react'
import { ArrowRightLeft, Cloud, HardDrive, LoaderCircle, ShieldCheck, X } from 'lucide-react'
import { api } from '../api'
import { useClosingTransition } from '../motion'
import type { StorageModel, StorageMove, StorageTarget } from '../types'
import { formatBytes } from '../utils'
import { ChoiceCard } from './ChoiceCard'
import { DialogFrame } from './Dialog'

function where(target: StorageTarget): string {
  if (target.kind === 'filesystem') {
    return target.capacity ? `${formatBytes(target.capacity.free_bytes)} free on this server` : 'This server’s disk'
  }
  return [target.bucket, target.prefix].filter(Boolean).join('/') || 'S3 bucket'
}

/**
 * Pick where a model goes and confirm by typing its name. The move itself runs
 * in the background; the model's row shows how it is going.
 */
export function MoveModelDialog({
  model,
  source,
  targets,
  onClose,
  onStarted,
}: {
  model: StorageModel
  source: StorageTarget
  targets: StorageTarget[]
  onClose: () => void
  onStarted: (move: StorageMove) => void
}) {
  const { closing, close } = useClosingTransition(onClose)
  const choices = targets.filter((target) => target.id !== source.id)
  const [destination, setDestination] = useState(choices.find((target) => target.connected)?.id || '')
  const [keepLocal, setKeepLocal] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const confirmField = useRef<HTMLInputElement>(null)
  const chosen = choices.find((target) => target.id === destination)
  const tooBig = Boolean(chosen?.capacity && chosen.capacity.free_bytes < model.size_bytes * 1.05)
  const leavingDisk = source.kind === 'filesystem' && chosen?.kind === 's3'

  useEffect(() => confirmField.current?.focus({ preventScroll: true }), [])

  async function start(event: FormEvent) {
    event.preventDefault()
    setStarting(true)
    setError('')
    try {
      const move = await api.startStorageMove({ repo_id: model.repo_id, destination, confirmation, keep_local: leavingDisk && keepLocal })
      onStarted(move)
      close()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not start the move.')
      setStarting(false)
    }
  }

  return (
    <DialogFrame labelledBy="move-model-title" className="move-model-dialog" closing={closing} onDismiss={close} onSubmit={start}>
      <header className="use-model-header">
        <div>
          <span className="eyebrow">Move to another location</span>
          <h2 id="move-model-title">{model.repo_id}</h2>
        </div>
        <button type="button" className="icon-button" onClick={close} aria-label="Close">
          <X size={20} />
        </button>
      </header>
      <div className="use-model-body move-model-body">
        <p className="move-model-from">
          {source.kind === 's3' ? <Cloud size={14} /> : <HardDrive size={14} />}
          Now in <strong>{source.name}</strong> · {formatBytes(model.size_bytes)} in {model.file_count} file{model.file_count === 1 ? '' : 's'}
        </p>
        <fieldset className="choice-group move-model-targets" disabled={starting}>
          <legend>Move it to</legend>
          {choices.map((target) => (
            <ChoiceCard
              key={target.id}
              name="move-destination"
              checked={destination === target.id}
              disabled={!target.connected}
              onChange={() => setDestination(target.id)}
              icon={target.kind === 's3' ? <Cloud size={17} /> : <HardDrive size={17} />}
              title={target.name}
              badge={target.connected ? undefined : 'Offline'}
            >
              {where(target)}
            </ChoiceCard>
          ))}
          {choices.length === 0 && <p className="account-note">There is no other storage location to move to.</p>}
        </fieldset>
        {tooBig && <p className="field-hint problem">{chosen?.name} does not have room for this model.</p>}
        {leavingDisk && (
          <label className="move-model-keep">
            <input type="checkbox" checked={keepLocal} onChange={(event) => setKeepLocal(event.target.checked)} />
            <span>
              Keep a copy on this server’s disk
              <small>
                Runtimes that load from the shared model path need one. Without it, restore the cache from the model page
                before loading it there.
              </small>
            </span>
          </label>
        )}
        <ol className="move-model-how">
          <li><span>1</span>Every file is copied and read back to check its SHA-256.</li>
          <li><span>2</span>The model switches to {chosen?.name || 'the new location'} in one step.</li>
          <li><span>3</span>The old copy is removed once downloads that started from it finish.</li>
        </ol>
        <p className="move-model-safe">
          <ShieldCheck size={14} /> Pulls keep working throughout, including ones pinned to the current revision.
        </p>
        <label className="wizard-label">
          <span>Type <code>{model.repo_id}</code> to confirm</span>
          <input
            ref={confirmField}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {error && <div className="inline-error">{error}</div>}
        <div className="add-user-footer">
          <span />
          <button type="button" className="secondary-button" onClick={close}>Cancel</button>
          <button
            type="submit"
            className="download-button"
            disabled={starting || !chosen || !chosen.connected || tooBig || confirmation !== model.repo_id}
          >
            {starting ? <LoaderCircle size={16} className="spin" /> : <ArrowRightLeft size={16} />} Move model
          </button>
        </div>
      </div>
    </DialogFrame>
  )
}

import { useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from 'react'
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Building2,
  Check,
  Cloud,
  FileUp,
  Globe2,
  HardDrive,
  LoaderCircle,
  LockKeyhole,
  UploadCloud,
  Users,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { precisionLabel } from '../catalog'
import { useFadeOnChange, useStepDirection } from '../motion'
import type { ListingOverrides, ModelListing, OwnedRepository, StorageOption, UploadNamespace, User, Visibility } from '../types'
import { droppedEntries, readDrop } from '../dropFiles'
import { listingPreviewRequest } from '../listingPreview'
import { detectPrecision, planUpload } from '../uploadPlan'
import { relativeUploadPath, useUploads, type UploadItem } from '../uploads'
import { formatBytes } from '../utils'
import { VISIBILITIES, visibilityAllowed, visibilityAudience, visibilityLabel } from '../visibility'
import { ChoiceCard } from './ChoiceCard'
import { ListingEditor } from './ListingEditor'
import { NamespacePicker } from './NamespacePicker'

type ToastHandler = (message: string, tone?: 'success' | 'error') => void
type Picked = UploadItem & { size: number }

const STEPS = ['Name', 'Access', 'Files', 'Review'] as const
const SLUG_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/
const VISIBILITY_ICONS = { private: LockKeyhole, organization: Users, public: Globe2 }

function picked(file: File, path: string): Picked {
  return { file, path, size: file.size }
}

async function readJson(item: Picked | undefined): Promise<unknown> {
  if (!item || item.size > 2_000_000) return undefined
  try {
    return JSON.parse(await item.file.text())
  } catch {
    return undefined
  }
}

export function UploadWizard({
  user,
  namespaces,
  repositories,
  resume,
  initialNamespace,
  onCreated,
  onExitResume,
  onToast,
}: {
  user: User
  namespaces: UploadNamespace[]
  repositories: OwnedRepository[]
  /** An unfinished repository to add files to; skips naming and access. */
  resume: OwnedRepository | null
  initialNamespace: string
  onCreated: () => void
  onExitResume: () => void
  onToast: ToastHandler
}) {
  const folderInput = useRef<HTMLInputElement>(null)
  const { jobs, enqueue } = useUploads()
  const [step, setStep] = useState(resume ? 2 : 0)
  const [namespace, setNamespace] = useState(initialNamespace)
  const [slug, setSlug] = useState('')
  const [description, setDescription] = useState('')
  const [visibility, setVisibility] = useState<Visibility>('private')
  const [storage, setStorage] = useState('')
  const [storageOptions, setStorageOptions] = useState<StorageOption[] | null>(null)
  const [storageError, setStorageError] = useState('')
  const [files, setFiles] = useState<Picked[]>([])
  const [dragging, setDragging] = useState(false)
  const [reading, setReading] = useState(false)
  const [precision, setPrecision] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [started, setStarted] = useState<string | null>(null)
  const [listing, setListing] = useState<ModelListing | null>(null)
  const [listingError, setListingError] = useState('')
  const [overrides, setOverrides] = useState<ListingOverrides>(resume?.listing_overrides || {})
  const shift = useStepDirection(step)
  const body = useFadeOnChange<HTMLDivElement>(started ? 'started' : String(step), { shift: started ? 0 : shift })

  const choices = namespaces.length
    ? namespaces
    : [{ name: user.username, kind: 'user' as const, display_name: user.display_name }]
  const owner = choices.find((item) => item.name.toLowerCase() === namespace.toLowerCase()) || choices[0]
  const organization = resume
    ? resume.organization_name || null
    : owner.kind === 'organization' ? owner.name : null
  const repoId = resume ? resume.repo_id : `${owner.name}/${slug.trim()}`
  const plan = useMemo(() => planUpload(files), [files])
  const slugProblem = !slug.trim()
    ? ''
    : !SLUG_PATTERN.test(slug.trim())
      ? 'Use letters, numbers, dots, underscores, or hyphens, starting with a letter or number.'
      : repositories.some((item) => item.repo_id.toLowerCase() === repoId.toLowerCase())
        ? `You already have ${repoId}.`
        : ''
  const chosenStorage = storageOptions?.find((item) => item.id === storage)
  const job = started ? [...jobs].reverse().find((item) => item.repoId === started) : undefined
  const resumeBusy = Boolean(
    resume && jobs.some((item) => item.repoId === resume.repo_id && ['queued', 'uploading', 'committing'].includes(item.status)),
  )

  useEffect(() => {
    folderInput.current?.setAttribute('webkitdirectory', '')
    folderInput.current?.setAttribute('directory', '')
  })

  // Where the repository can live depends on who owns it.
  useEffect(() => {
    if (resume) return
    let ignore = false
    setStorageError('')
    api
      .storageOptions(owner.name)
      .then((payload) => {
        if (ignore) return
        setStorageOptions(payload.items)
        const preferred = user.preferences?.default_storage_target
        const dedicated = payload.items.find((item) => item.dedicated)
        // A new owner starts from its own best choice: its dedicated location first.
        setStorage(
          dedicated?.id
            || (preferred && payload.items.some((item) => item.id === preferred) ? preferred : payload.default || ''),
        )
      })
      .catch((reason) => {
        if (ignore) return
        setStorageOptions([])
        setStorageError(reason instanceof Error ? reason.message : 'Could not load storage locations.')
      })
    return () => {
      ignore = true
    }
  }, [owner.name, resume, user.preferences?.default_storage_target])

  useEffect(() => {
    if (!visibilityAllowed(visibility, organization)) setVisibility('private')
  }, [organization, visibility])

  // A failed create is about the name it used; editing the name clears it.
  useEffect(() => setError(''), [slug, owner.name])

  useEffect(() => {
    let ignore = false
    const top = (name: string) => plan.files.find((item) => item.path === name)
    Promise.all([readJson(top('config.json')), readJson(top('hf_quant_config.json'))]).then(([config, quant]) => {
      if (!ignore) setPrecision(config || quant ? detectPrecision(config, quant) : null)
    })
    return () => {
      ignore = true
    }
  }, [plan.files])

  // Resuming picks up the corrections saved when the repository was created.
  // Keyed on the repository, so a refreshed list does not undo edits in progress.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setOverrides(resume?.listing_overrides || {}), [resume?.repo_id])

  // One answer for precision: the server's, once the preview is in.
  const shownPrecision = listing ? listing.detected.precision ?? null : precision

  // How the files will be listed, read from their text files and weight headers.
  useEffect(() => {
    setListing(null)
    setListingError('')
    if (!plan.files.length) return
    let ignore = false
    const timer = window.setTimeout(() => {
      listingPreviewRequest(repoId, plan.files)
        .then((request) => api.previewListing(request))
        .then((result) => {
          if (!ignore) setListing(result)
        })
        .catch((reason) => {
          if (!ignore) setListingError(reason instanceof Error ? reason.message : 'Could not read how the model will be listed.')
        })
    }, 250)
    return () => {
      ignore = true
      window.clearTimeout(timer)
    }
    // The name only changes how the size reads, not what the files say.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan.files])

  function reset() {
    setStep(0)
    setOverrides({})
    setSlug('')
    setDescription('')
    setVisibility('private')
    setFiles([])
    setMessage('')
    setError('')
    setStarted(null)
    if (resume) onExitResume()
  }

  async function onDrop(event: DragEvent) {
    event.preventDefault()
    setDragging(false)
    const entries = droppedEntries(event.dataTransfer)
    if (!entries.length) return
    setReading(true)
    try {
      setFiles((await readDrop(entries)).map((item) => picked(item.file, item.path)))
    } catch {
      onToast('Could not read the dropped folder. Try choosing it instead.', 'error')
    } finally {
      setReading(false)
    }
  }

  const canContinue = [
    Boolean(slug.trim()) && !slugProblem,
    Boolean(chosenStorage),
    plan.files.length > 0 && !reading,
    true,
  ]

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (step < 3) {
      if (canContinue[step]) setStep(step + 1)
      return
    }
    setSubmitting(true)
    setError('')
    try {
      let target = repoId
      if (!resume) {
        const created = await api.createUploadRepository({
          slug: slug.trim(),
          description,
          visibility,
          storage_target: storage || undefined,
          namespace: owner.name,
        })
        target = created.repo_id
      }
      if (Object.keys(overrides).length) {
        // Saved before any file is sent, so the model is listed right from the start.
        await api.updateListing(target, overrides).catch((reason) =>
          onToast(
            `${reason instanceof Error ? reason.message : 'The listing corrections were not saved.'} Correct them later in the model's Settings.`,
            'error',
          ),
        )
      }
      enqueue({
        kind: 'new',
        repoId: target,
        items: plan.files.map(({ file, path }) => ({ file, path })),
        message:
          message.trim()
          || `Upload ${plan.files.length} file${plan.files.length === 1 ? '' : 's'}`,
      })
      setStarted(target)
      onCreated()
    } catch (reason) {
      const text = reason instanceof Error ? reason.message : 'Could not create the repository.'
      setError(text)
      if (/already exists/i.test(text)) setStep(0)
    } finally {
      setSubmitting(false)
    }
  }

  if (started) {
    const done = job?.status === 'done'
    const failed = job && ['error', 'cancelled', 'interrupted'].includes(job.status)
    const uploaded = job ? Object.values(job.uploaded).reduce((sum, value) => sum + value, 0) : 0
    const percent = job?.total ? Math.min(100, (uploaded / job.total) * 100) : done ? 100 : 0
    return (
      <section className="upload-wizard" aria-live="polite">
        <div ref={body} className="wizard-started">
          <span className={`wizard-started-icon ${done ? 'done' : failed ? 'failed' : ''}`}>
            {done ? <Check size={22} /> : failed ? <AlertTriangle size={22} /> : <UploadCloud size={22} />}
          </span>
          <h2>
            {done
              ? `${started} is ready`
              : job?.status === 'cancelled'
                ? 'Upload cancelled'
                : failed ? 'Upload paused' : `Uploading ${started}`}
          </h2>
          <p>
            {done
              ? 'Every file was committed and the model is in the library.'
              : job?.status === 'cancelled'
                ? 'Files already sent are kept. Resume or delete the repository under Unfinished uploads.'
                : job?.status === 'interrupted'
                  ? 'Choose the same folder in the upload panel at the bottom of the screen to resume; finished files are kept.'
                  : failed
                    ? 'Retry from the upload panel at the bottom of the screen; finished files are kept.'
                    : 'You can keep browsing. Progress also stays at the bottom of the screen.'}
          </p>
          <div
            className={done || failed ? 'capacity-track wizard-progress' : 'capacity-track wizard-progress live'}
            aria-label={`${percent.toFixed(0)} percent uploaded`}
          >
            <span style={{ width: `${percent}%` }} />
          </div>
          <small>
            {job?.status === 'committing'
              ? 'Committing…'
              : `${formatBytes(uploaded)} of ${formatBytes(job?.total || plan.totalBytes)}`}
          </small>
          <div className="wizard-actions">
            {done && (
              <Link className="download-button" to={`/models/${started}`}>
                Open model <ArrowRight size={15} />
              </Link>
            )}
            <button type="button" className="secondary-button" onClick={reset}>
              Upload another model
            </button>
          </div>
        </div>
      </section>
    )
  }

  return (
    <form className="upload-wizard" onSubmit={submit} noValidate>
      <ol className="wizard-steps" style={{ '--wizard-progress': step / (STEPS.length - 1) } as React.CSSProperties}>
        {STEPS.map((label, index) => {
          const locked = Boolean(resume) && index < 2
          const reachable = !locked && index < step
          return (
            <li key={label} className={index === step ? 'current' : index < step ? 'complete' : ''}>
              <button
                type="button"
                disabled={!reachable}
                aria-current={index === step ? 'step' : undefined}
                onClick={() => setStep(index)}
              >
                <span className="wizard-step-dot">{index < step ? <Check size={12} /> : index + 1}</span>
                <span>{label}</span>
              </button>
            </li>
          )
        })}
      </ol>

      <div ref={body} className="wizard-body">
        {resume && step < 3 && (
          <div className="wizard-resume">
            <span>
              Adding files to <strong>{resume.repo_id}</strong> · {visibilityLabel(resume.visibility)}
            </span>
            <button type="button" onClick={reset}>Start a new model instead</button>
          </div>
        )}

        {step === 0 && (
          <div className="wizard-fields">
            <div className="wizard-heading">
              <h2>Name your model</h2>
              <p>It becomes the repository path everyone pulls from, like on Hugging Face.</p>
            </div>
            <div className="repo-name-grid">
              <label htmlFor="new-repo-owner">Owner</label>
              <span aria-hidden="true" />
              <label htmlFor="new-repo-name">Model name</label>
              <NamespacePicker id="new-repo-owner" namespaces={choices} value={owner.name} onChange={setNamespace} />
              <span className="repo-name-slash" aria-hidden="true">/</span>
              <input
                id="new-repo-name"
                value={slug}
                onChange={(event) => setSlug(event.target.value)}
                placeholder="GLM-5.3-NVFP4"
                autoComplete="off"
                spellCheck={false}
                autoFocus
                aria-invalid={Boolean(slugProblem) || undefined}
                aria-describedby="new-repo-hint"
              />
            </div>
            <p id="new-repo-hint" className={slugProblem || error ? 'field-hint problem' : 'field-hint'}>
              {slugProblem || error || (slug.trim() ? <>Will be <code>{repoId}</code></> : 'Tip: use the name the model has on Hugging Face.')}
            </p>
            <label className="wizard-label">
              <span>Description <small>Optional</small></span>
              <textarea value={description} onChange={(event) => setDescription(event.target.value)} maxLength={500} />
            </label>
          </div>
        )}

        {step === 1 && (
          <div className="wizard-fields">
            <div className="wizard-heading">
              <h2>Who can see it, and where it lives</h2>
              <p>Visibility can change later. The storage location cannot.</p>
            </div>
            <fieldset className="choice-group">
              <legend>Visibility</legend>
              {VISIBILITIES.map((option) => {
                const Icon = VISIBILITY_ICONS[option]
                return (
                  <ChoiceCard
                    key={option}
                    name="visibility"
                    checked={visibility === option}
                    disabled={!visibilityAllowed(option, organization)}
                    onChange={() => setVisibility(option)}
                    icon={<Icon size={17} />}
                    title={visibilityLabel(option)}
                  >
                    {visibilityAudience(option, organization)}
                  </ChoiceCard>
                )
              })}
            </fieldset>
            <fieldset className="choice-group">
              <legend>Storage</legend>
              {storageOptions === null && (
                <div className="choice-loading"><LoaderCircle size={16} className="spin" /> Finding storage…</div>
              )}
              {storageOptions?.map((option) => (
                <ChoiceCard
                  key={option.id}
                  name="storage"
                  checked={storage === option.id}
                  onChange={() => setStorage(option.id)}
                  icon={option.kind === 's3' ? <Cloud size={17} /> : <HardDrive size={17} />}
                  title={option.name}
                  badge={option.dedicated ? `Dedicated to ${owner.name}` : option.restricted ? 'Restricted' : undefined}
                >
                  {option.kind === 's3' ? 'S3 bucket' : 'Server disk'}
                  {option.free_bytes != null && ` · ${formatBytes(option.free_bytes)} free`}
                </ChoiceCard>
              ))}
              {storageOptions?.length === 0 && (
                <div className="inline-error">
                  <AlertTriangle size={16} />
                  {storageError || `No storage location accepts uploads for ${owner.name}. Ask an administrator for access.`}
                </div>
              )}
            </fieldset>
          </div>
        )}

        {step === 2 && (
          <div className="wizard-fields">
            <div className="wizard-heading">
              <h2>Add the model folder</h2>
              <p>
                {resume
                  ? 'Choose the same folder again. Files that already arrived are skipped.'
                  : 'Drop the folder you downloaded or cloned. Its contents become the repository.'}
              </p>
            </div>
            <label
              className={`folder-picker wizard-drop ${dragging ? 'dragging' : ''}`}
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
                ref={folderInput}
                type="file"
                multiple
                onChange={(event) =>
                  setFiles(Array.from(event.target.files || []).map((file) => picked(file, relativeUploadPath(file))))
                }
              />
              {reading ? <LoaderCircle size={25} className="spin" /> : <FileUp size={25} />}
              <strong>
                {reading
                  ? 'Reading folder…'
                  : plan.files.length
                    ? `${plan.files.length} file${plan.files.length === 1 ? '' : 's'} · ${formatBytes(plan.totalBytes)}`
                    : 'Drop a model folder or click to choose'}
              </strong>
              <span>{plan.files.length ? 'Click or drop to choose a different folder' : 'Config, tokenizer, weights, and model card'}</span>
            </label>
            {files.length > 0 && (
              <ul className="upload-checks">
                <li className={plan.checks.config ? 'ok' : 'missing'}>
                  {plan.checks.config ? <Check size={13} /> : <AlertTriangle size={13} />} config.json
                </li>
                <li className={plan.checks.tokenizer ? 'ok' : 'missing'}>
                  {plan.checks.tokenizer ? <Check size={13} /> : <AlertTriangle size={13} />} Tokenizer
                </li>
                <li className={plan.checks.weights ? 'ok' : 'missing'}>
                  {plan.checks.weights ? <Check size={13} /> : <AlertTriangle size={13} />}
                  {plan.checks.weights
                    ? `${plan.weightFiles} safetensors file${plan.weightFiles === 1 ? '' : 's'} · ${formatBytes(plan.weightBytes)}`
                    : 'No safetensors weights'}
                </li>
                <li className={plan.checks.card ? 'ok' : 'missing'}>
                  {plan.checks.card ? <Check size={13} /> : <AlertTriangle size={13} />} Model card (README.md)
                </li>
                {shownPrecision && <li className="ok"><Check size={13} /> {precisionLabel(shownPrecision) || shownPrecision} weights</li>}
                {plan.skipped.length > 0 && (
                  <li className="skipped">
                    Skipping {plan.skipped.length} file{plan.skipped.length === 1 ? '' : 's'} from .git, caches, and system clutter
                  </li>
                )}
              </ul>
            )}
          </div>
        )}

        {step === 3 && (
          <div className="wizard-fields">
            <div className="wizard-heading">
              <h2>Review and upload</h2>
              <p>The upload runs in the background and resumes if the connection drops.</p>
            </div>
            <dl className="wizard-review">
              <dt>Repository</dt>
              <dd>
                <code>{repoId}</code>
                {!resume && <button type="button" onClick={() => setStep(0)}>Edit</button>}
              </dd>
              {!resume && description.trim() && (
                <>
                  <dt>Description</dt>
                  <dd>{description.trim()}</dd>
                </>
              )}
              <dt>Visibility</dt>
              <dd>
                {resume ? visibilityLabel(resume.visibility) : `${visibilityLabel(visibility)} · ${visibilityAudience(visibility, organization)}`}
                {!resume && <button type="button" onClick={() => setStep(1)}>Edit</button>}
              </dd>
              {!resume && (
                <>
                  <dt>Storage</dt>
                  <dd>
                    {chosenStorage?.name}
                    <button type="button" onClick={() => setStep(1)}>Edit</button>
                  </dd>
                </>
              )}
              <dt>Files</dt>
              <dd>
                {plan.files.length} · {formatBytes(plan.totalBytes)}
                {plan.skipped.length > 0 ? ` · ${plan.skipped.length} skipped` : ''}
                <button type="button" onClick={() => setStep(2)}>Edit</button>
              </dd>
            </dl>
            <label className="wizard-label">
              <span>Commit message <small>Optional</small></span>
              <input
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                maxLength={200}
                placeholder={`Upload ${plan.files.length} file${plan.files.length === 1 ? '' : 's'}`}
              />
            </label>
            <section className="wizard-listing" aria-labelledby="wizard-listing-title">
              <div className="wizard-heading">
                <h3 id="wizard-listing-title">How it will be listed</h3>
                <p>Read from config.json, the model card, and the weight headers. Correct anything that does not fit this repository.</p>
              </div>
              {listing ? (
                <ListingEditor listing={listing} overrides={overrides} onChange={setOverrides} />
              ) : listingError ? (
                <p className="field-hint problem">{listingError} The model is still listed from its files after upload.</p>
              ) : (
                <p className="choice-loading"><LoaderCircle size={14} className="spin" /> Reading the files…</p>
              )}
            </section>
            {error && <div className="inline-error"><AlertTriangle size={16} /> {error}</div>}
          </div>
        )}
      </div>

      <div className="wizard-actions">
        {step > (resume ? 2 : 0) && (
          <button type="button" className="secondary-button" onClick={() => setStep(step - 1)}>
            <ArrowLeft size={15} /> Back
          </button>
        )}
        <button className="download-button" disabled={!canContinue[step] || submitting || (step === 3 && resumeBusy)}>
          {step < 3 ? (
            <>Continue <ArrowRight size={15} /></>
          ) : submitting ? (
            <><LoaderCircle size={15} className="spin" /> Starting…</>
          ) : resumeBusy ? (
            'Already uploading…'
          ) : (
            <><UploadCloud size={15} /> {resume ? 'Upload files' : 'Create and upload'}</>
          )}
        </button>
        {organization && step === 0 && !resume && (
          <span className="wizard-owner-note"><Building2 size={13} /> Owned by {owner.display_name}</span>
        )}
      </div>
    </form>
  )
}

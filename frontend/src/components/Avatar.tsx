import { useEffect, useRef, useState } from 'react'
import { ImageUp, LoaderCircle, Trash2 } from 'lucide-react'
import { prepareAvatar } from '../avatarImage'
import { initials } from '../utils'
import { useConfirm } from './ConfirmDialog'

/**
 * What goes inside an avatar circle: the picture when there is one, the initials
 * otherwise, and the initials again if the picture cannot load. The circle itself
 * is the caller's element, so each place keeps its own size and shape.
 */
export function Avatar({ name, src }: { name: string; src?: string | null }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [src])
  if (!src || failed) return <>{initials(name)}</>
  return <img className="avatar-image" src={src} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} />
}

/** Change or remove a profile picture: the preview, a picker, and a remove button. */
export function AvatarEditor({
  name,
  label,
  src,
  onUpload,
  onRemove,
  onToast,
}: {
  name: string
  /** Who the picture is for, in words: "your profile", "NVIDIA". */
  label: string
  src: string | null
  onUpload: (picture: Blob) => Promise<void>
  onRemove: () => Promise<void>
  onToast: (message: string, tone?: 'success' | 'error') => void
}) {
  const confirm = useConfirm()
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)

  async function choose(file: File | undefined) {
    if (!file) return
    setBusy(true)
    try {
      await onUpload(await prepareAvatar(file))
      onToast('Picture updated.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not update the picture.', 'error')
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ''
    }
  }

  async function remove() {
    const sure = await confirm({
      title: 'Remove the picture?',
      message: `Initials show for ${label} again, everywhere.`,
      confirmLabel: 'Remove picture',
      danger: true,
    })
    if (!sure) return
    setBusy(true)
    try {
      await onRemove()
      onToast('Picture removed.')
    } catch (reason) {
      onToast(reason instanceof Error ? reason.message : 'Could not remove the picture.', 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="avatar-editor">
      <span className={busy ? 'avatar-editor-preview busy' : 'avatar-editor-preview'} aria-hidden="true">
        <Avatar name={name} src={src} />
        {busy && <span className="avatar-editor-spinner"><LoaderCircle size={18} className="spin" /></span>}
      </span>
      <div className="avatar-editor-body">
        <strong>Picture</strong>
        <small>Shown on model cards and pages. Square pictures work best; others are cropped to the middle.</small>
        <div className="avatar-editor-actions">
          <label className={busy ? 'secondary-button compact disabled' : 'secondary-button compact'}>
            <ImageUp size={14} /> {src ? 'Change picture' : 'Upload picture'}
            <input
              ref={input}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              disabled={busy}
              onChange={(event) => choose(event.target.files?.[0])}
            />
          </label>
          {src && (
            <button type="button" className="text-link" disabled={busy} onClick={remove}>
              <Trash2 size={13} /> Remove
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

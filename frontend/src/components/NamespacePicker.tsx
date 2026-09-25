import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Building2, Check, ChevronDown, UserRound } from 'lucide-react'
import type { UploadNamespace } from '../types'
import { avatarUrl } from '../utils'
import { Avatar } from './Avatar'

interface NamespacePickerProps {
  namespaces: UploadNamespace[]
  value: string
  onChange: (name: string) => void
  id?: string
}

/** Chooses who owns a new repository: yourself or an organization you can write to. */
export function NamespacePicker({ namespaces, value, onChange, id }: NamespacePickerProps) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(0)
  const root = useRef<HTMLDivElement>(null)
  const selected =
    namespaces.find((item) => item.name.toLowerCase() === value.toLowerCase()) || namespaces[0]

  useEffect(() => {
    if (!open) return
    const close = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  if (!selected) return null
  const single = namespaces.length < 2

  function choose(item: UploadNamespace) {
    onChange(item.name)
    setOpen(false)
  }

  function onKeyDown(event: KeyboardEvent) {
    if (single) return
    if (!open && ['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
      event.preventDefault()
      setActive(Math.max(0, namespaces.indexOf(selected)))
      setOpen(true)
      return
    }
    if (!open) return
    if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
    } else if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive((index) => (index + 1) % namespaces.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((index) => (index - 1 + namespaces.length) % namespaces.length)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      choose(namespaces[active])
    }
  }

  const Kind = ({ item }: { item: UploadNamespace }) =>
    item.kind === 'organization' ? <Building2 size={12} /> : <UserRound size={12} />

  return (
    <div
      className="namespace-picker"
      ref={root}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false)
      }}
    >
      <button
        id={id}
        type="button"
        className="namespace-trigger"
        onClick={() => !single && setOpen(!open)}
        onKeyDown={onKeyDown}
        aria-haspopup={single ? undefined : 'listbox'}
        aria-expanded={single ? undefined : open}
        disabled={single}
      >
        <span className={`namespace-avatar ${selected.kind}`}><Avatar name={selected.name} src={avatarUrl(selected.name, selected.avatar_updated_at)} /></span>
        <span className="namespace-name">{selected.name}</span>
        {!single && <ChevronDown size={15} className="namespace-chevron" />}
      </button>
      {open && (
        <ul className="namespace-menu" role="listbox" aria-activedescendant={`namespace-${active}`}>
          {namespaces.map((item, index) => (
            <li
              key={item.name}
              id={`namespace-${index}`}
              role="option"
              aria-selected={item.name === selected.name}
              className={index === active ? 'active' : undefined}
              onMouseEnter={() => setActive(index)}
              onMouseDown={(event) => {
                event.preventDefault()
                choose(item)
              }}
            >
              <span className={`namespace-avatar ${item.kind}`}><Avatar name={item.name} src={avatarUrl(item.name, item.avatar_updated_at)} /></span>
              <span className="namespace-option-text">
                <strong>{item.name}</strong>
                <small><Kind item={item} /> {item.kind === 'organization' ? item.display_name : 'Personal'}</small>
              </span>
              {item.name === selected.name && <Check size={15} className="namespace-check" />}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

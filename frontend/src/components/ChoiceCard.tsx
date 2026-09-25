import type { ReactNode } from 'react'
import { Check } from 'lucide-react'

/** A radio option drawn as a card: icon, title, optional badge, and a line of detail. */
export function ChoiceCard({
  name,
  checked,
  disabled,
  onChange,
  icon,
  title,
  badge,
  children,
}: {
  name: string
  checked: boolean
  disabled?: boolean
  onChange: () => void
  icon: ReactNode
  title: string
  badge?: string
  children: ReactNode
}) {
  return (
    <label className={['choice-card', checked ? 'checked' : '', disabled ? 'disabled' : ''].filter(Boolean).join(' ')}>
      <input type="radio" name={name} checked={checked} disabled={disabled} onChange={onChange} />
      <span className="choice-icon">{icon}</span>
      <span className="choice-text">
        <strong>
          {title}
          {badge && <em>{badge}</em>}
        </strong>
        <small>{children}</small>
      </span>
      <span className="choice-mark" aria-hidden="true">{checked && <Check size={12} />}</span>
    </label>
  )
}

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Bookmark,
  Box,
  LogOut,
  Menu,
  Moon,
  Search,
  Settings,
  ShieldCheck,
  Sun,
  UploadCloud,
  UserCircle,
  X,
} from 'lucide-react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { ADMIN_CAPABILITIES, useAccess } from '../access'
import { api } from '../api'
import { useTabIndicator } from '../motion'
import { announceThemePreference, type Theme } from '../theme'
import type { User } from '../types'
import { avatarUrl } from '../utils'
import { Avatar } from './Avatar'

interface ShellProps {
  children: ReactNode
  user: User
  /** The theme on screen, from useAppTheme. */
  theme: Theme
  onLogout: () => void
}

/** How long the phone menu takes to leave; matches `.mobile-panel.closing` in styles.css. */
const MENU_EXIT_MS = 180

const links = [
  { to: '/models', label: 'Models', icon: Box, capability: 'models.browse' },
  { to: '/saved', label: 'Saved', icon: Bookmark, capability: 'models.save' },
  { to: '/uploads', label: 'Uploads', icon: UploadCloud, capability: 'repos.create' },
]

export default function Shell({ children, user, theme, onLogout }: ShellProps) {
  const navigate = useNavigate()
  const searchInput = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  // `closing` keeps the phone menu on screen while it leaves; opening again
  // before it has gone turns it around from wherever it is.
  const [menu, setMenu] = useState<'closed' | 'open' | 'closing'>('closed')
  const menuTimer = useRef(0)
  const mobileOpen = menu === 'open'
  const { can, refresh } = useAccess()
  const visibleLinks = links.filter((link) => can(link.capability))
  const isAdmin = ADMIN_CAPABILITIES.some((capability) => can(capability))
  // Without accounts everyone is the built-in "local" user, and there is
  // nothing to sign out of.
  const signOut = user.id !== 'local'
  const { pathname } = useLocation()
  const nav = useTabIndicator<HTMLElement>(`${pathname.split('/')[1]}:${visibleLinks.length}:${isAdmin}`)

  useEffect(() => () => window.clearTimeout(menuTimer.current), [])

  function openMenu() {
    window.clearTimeout(menuTimer.current)
    setMenu('open')
  }

  function closeMenu() {
    window.clearTimeout(menuTimer.current)
    // With reduced motion it fades instead of lifting away (styles.css), so it still waits.
    setMenu((current) => (current === 'closed' ? current : 'closing'))
    menuTimer.current = window.setTimeout(() => setMenu('closed'), MENU_EXIT_MS)
  }

  useEffect(() => {
    if (!mobileOpen) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeMenu()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [mobileOpen])

  useEffect(() => {
    function focusSearch(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      const isTyping = target?.matches('input, textarea, select, [contenteditable="true"]')
      // An open dialog owns the keyboard; the search box is behind it.
      const dialogOpen = Boolean(document.querySelector('[aria-modal="true"]'))
      if (event.key === '/' && !isTyping && !dialogOpen) {
        event.preventDefault()
        searchInput.current?.focus()
      }
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])

  function search(event: FormEvent) {
    event.preventDefault()
    navigate(`/models?search=${encodeURIComponent(query.trim())}`)
    closeMenu()
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/models" className="brand" aria-label="HuggingHack home">
            <img src="/hugginghack-mark.svg" alt="" className="brand-mark" />
            <span className="brand-name">HuggingHack</span>
            <span className="brand-local">local</span>
          </NavLink>

          <form className="global-search" onSubmit={search} role="search">
            <Search size={17} aria-hidden="true" />
            <input
              ref={searchInput}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search your model library"
              aria-label="Search the local model library"
            />
            <kbd>/</kbd>
          </form>

          <nav className="primary-nav" aria-label="Primary navigation" ref={nav}>
            {visibleLinks.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} className={({ isActive }) => (isActive ? 'active' : '')}>
                <Icon size={16} aria-hidden="true" />
                <span>{label}</span>
              </NavLink>
            ))}
            {isAdmin && (
              <NavLink to="/admin" aria-label="Administration" title="Administration">
                <ShieldCheck size={16} aria-hidden="true" />
                <span>Admin</span>
              </NavLink>
            )}
            <NavLink to="/account" aria-label="Account settings" title="Account settings">
              <Settings size={17} aria-hidden="true" />
              <span className="desktop-hidden-label">Account</span>
            </NavLink>
          </nav>

          <button
            type="button"
            className="icon-button theme-toggle"
            onClick={() => {
              const next = theme === 'light' ? 'dark' : 'light'
              // Applies at once, and the Preferences tab follows the new choice.
              announceThemePreference(next)
              api.updatePreferences({ theme: next }).then(refresh).catch(() => undefined)
            }}
            aria-label={theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme'}
          >
            {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
          </button>
          <div className="account-chip" title={`${user.display_name} · ${user.role}`}>
            <Link to="/account" className="account-chip-link" aria-label="Your account">
              {user.avatar_updated_at ? (
                <span className="account-chip-avatar" aria-hidden="true">
                  <Avatar name={user.display_name || user.username} src={avatarUrl(user.username, user.avatar_updated_at)} />
                </span>
              ) : (
                <UserCircle size={18} />
              )}
              <span>{user.display_name}</span>
            </Link>
            {signOut && (
              <button type="button" onClick={onLogout} aria-label="Sign out" title="Sign out">
                <LogOut size={15} />
              </button>
            )}
          </div>
          <button
            type="button"
            className="icon-button mobile-menu-button"
            onClick={() => (mobileOpen ? closeMenu() : openMenu())}
            aria-label="Toggle navigation"
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
        {menu !== 'closed' && (
          <div className={menu === 'closing' ? 'mobile-panel closing' : 'mobile-panel'}>
            <form className="mobile-search" onSubmit={search}>
              <Search size={17} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search your library"
              />
            </form>
            {visibleLinks.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} onClick={closeMenu}>
                <Icon size={18} />
                {label}
              </NavLink>
            ))}
            {isAdmin && (
              <NavLink to="/admin" onClick={closeMenu}>
                <ShieldCheck size={18} />
                Administration
              </NavLink>
            )}
            <NavLink to="/account" onClick={closeMenu}>
              <Settings size={18} />
              Account
            </NavLink>
            {signOut && (
              <button type="button" className="mobile-account" onClick={onLogout}>
                <LogOut size={18} />
                Sign out {user.display_name}
              </button>
            )}
          </div>
        )}
      </header>
      <main>{children}</main>
    </div>
  )
}

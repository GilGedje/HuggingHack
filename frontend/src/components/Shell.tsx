import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  Bookmark,
  Box,
  Download,
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
import { Link, NavLink, useNavigate } from 'react-router-dom'
import { ADMIN_CAPABILITIES, useAccess } from '../access'
import { api } from '../api'
import type { User } from '../types'

interface ShellProps {
  children: ReactNode
  activeDownloads: number
  user: User
  onLogout: () => void
}

const links = [
  { to: '/models', label: 'Models', icon: Box, capability: 'models.browse' },
  { to: '/saved', label: 'Saved', icon: Bookmark, capability: 'models.save' },
  { to: '/uploads', label: 'Uploads', icon: UploadCloud, capability: 'repos.create' },
  { to: '/downloads', label: 'Downloads', icon: Download, capability: 'hub.download' },
]

type Theme = 'light' | 'dark'

function systemTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function storedTheme(): Theme | null {
  try {
    const value = localStorage.getItem('hugginghack-theme')
    return value === 'dark' || value === 'light' ? value : null
  } catch {
    return null
  }
}

export default function Shell({ children, activeDownloads, user, onLogout }: ShellProps) {
  const navigate = useNavigate()
  const searchInput = useRef<HTMLInputElement>(null)
  const [query, setQuery] = useState('')
  const [mobileOpen, setMobileOpen] = useState(false)
  const { can } = useAccess()
  const visibleLinks = links.filter((link) => can(link.capability))
  const isAdmin = ADMIN_CAPABILITIES.some((capability) => can(capability))
  const preferred = user.preferences?.theme
  const [theme, setTheme] = useState<Theme>(() =>
    preferred === 'light' || preferred === 'dark'
      ? preferred
      : preferred === 'system'
        ? systemTheme()
        : storedTheme() || 'light',
  )

  // The saved account preference wins; localStorage covers the moment before it loads.
  useEffect(() => {
    if (preferred === 'light' || preferred === 'dark') setTheme(preferred)
    if (preferred === 'system') setTheme(systemTheme())
  }, [preferred])

  useEffect(() => {
    const apply = (event: Event) => {
      const value = (event as CustomEvent<string>).detail
      setTheme(value === 'light' || value === 'dark' ? value : systemTheme())
    }
    window.addEventListener('hugginghack:theme', apply)
    return () => window.removeEventListener('hugginghack:theme', apply)
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('hugginghack-theme', theme)
    } catch {
      // The theme still applies for this page view.
    }
  }, [theme])

  useEffect(() => {
    function focusSearch(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null
      const isTyping = target?.matches('input, textarea, select, [contenteditable="true"]')
      if (event.key === '/' && !isTyping) {
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
    setMobileOpen(false)
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

          <nav className="primary-nav" aria-label="Primary navigation">
            {visibleLinks.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} className={({ isActive }) => (isActive ? 'active' : '')}>
                <Icon size={16} aria-hidden="true" />
                <span>{label}</span>
                {to === '/downloads' && activeDownloads > 0 && (
                  <span className="nav-count">{activeDownloads}</span>
                )}
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
              setTheme(next)
              api.updatePreferences({ theme: next }).catch(() => undefined)
            }}
            aria-label={theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme'}
          >
            {theme === 'light' ? <Moon size={18} /> : <Sun size={18} />}
          </button>
          <div className="account-chip" title={`${user.display_name} · ${user.role}`}>
            <Link to="/account" className="account-chip-link" aria-label="Your account">
              <UserCircle size={18} />
              <span>{user.display_name}</span>
            </Link>
            <button type="button" onClick={onLogout} aria-label="Sign out" title="Sign out">
              <LogOut size={15} />
            </button>
          </div>
          <button
            type="button"
            className="icon-button mobile-menu-button"
            onClick={() => setMobileOpen(!mobileOpen)}
            aria-label="Toggle navigation"
            aria-expanded={mobileOpen}
          >
            {mobileOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
        {mobileOpen && (
          <div className="mobile-panel">
            <form className="mobile-search" onSubmit={search}>
              <Search size={17} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search your library"
              />
            </form>
            {visibleLinks.map(({ to, label, icon: Icon }) => (
              <NavLink key={to} to={to} onClick={() => setMobileOpen(false)}>
                <Icon size={18} />
                {label}
                {to === '/downloads' && activeDownloads > 0 && (
                  <span className="nav-count">{activeDownloads}</span>
                )}
              </NavLink>
            ))}
            {isAdmin && (
              <NavLink to="/admin" onClick={() => setMobileOpen(false)}>
                <ShieldCheck size={18} />
                Administration
              </NavLink>
            )}
            <NavLink to="/account" onClick={() => setMobileOpen(false)}>
              <Settings size={18} />
              Account
            </NavLink>
            <button type="button" className="mobile-account" onClick={onLogout}>
              <LogOut size={18} />
              Sign out {user.display_name}
            </button>
          </div>
        )}
      </header>
      <main>{children}</main>
    </div>
  )
}

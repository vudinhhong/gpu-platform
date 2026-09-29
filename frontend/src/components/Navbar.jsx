import React, { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../App.jsx'
import { logoutRequest } from '../api/index.js'
import AccountMenu from './AccountMenu.jsx'

// ---------------------------------------------------------------------------
// Icons (inline SVG helpers)
// ---------------------------------------------------------------------------

function ChipIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <rect x="9" y="9" width="6" height="6" rx="1" strokeLinecap="round" strokeLinejoin="round" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 2v2m6-2v2M9 20v2m6-2v2M2 9h2m-2 6h2M20 9h2m-2 6h2" />
      <rect x="3" y="3" width="18" height="18" rx="3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function LogoutIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
    </svg>
  )
}

function ShieldIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
    </svg>
  )
}

function HomeIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Navbar
// ---------------------------------------------------------------------------

export default function Navbar() {
  const { user, logout } = useAuth()
  const navigate          = useNavigate()
  const location          = useLocation()
  const [loggingOut, setLoggingOut] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)

  const handleLogout = async () => {
    setLoggingOut(true)
    try { await logoutRequest() } catch { /* best-effort */ }
    logout()
    navigate('/login')
  }

  const isActive = (path) => location.pathname === path

  const navLinks = [
    { to: '/',      label: 'Dashboard', icon: HomeIcon,   show: true },
    { to: '/admin', label: 'Admin Panel', icon: ShieldIcon, show: user?.is_admin },
  ].filter((l) => l.show)

  return (
    <nav className="bg-indigo-700 shadow-lg shadow-indigo-900/30 sticky top-0 z-40">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="flex items-center justify-between h-16">

          {/* Brand */}
          <div className="flex items-center space-x-6">
            <Link to="/" className="flex items-center space-x-2.5 group">
              <div className="w-9 h-9 bg-indigo-500 group-hover:bg-indigo-400 rounded-xl flex items-center justify-center transition-colors shadow-inner">
                <ChipIcon className="w-5 h-5 text-white" />
              </div>
              <span className="text-white text-lg font-bold tracking-tight hidden sm:block">
                GPU Platform
              </span>
            </Link>

            {/* Desktop nav links */}
            <div className="hidden md:flex items-center space-x-1">
              {navLinks.map(({ to, label, icon: Icon }) => (
                <Link
                  key={to}
                  to={to}
                  className={`flex items-center space-x-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                    isActive(to)
                      ? 'bg-indigo-900/60 text-white'
                      : 'text-indigo-100 hover:text-white hover:bg-indigo-600'
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  <span>{label}</span>
                </Link>
              ))}
            </div>
          </div>

          {/* Right side */}
          <div className="flex items-center space-x-3">
            {/* The name is the account menu: editing details and changing the
                password open over the page, so nothing in flight is lost. */}
            <AccountMenu onSignOut={handleLogout} signingOut={loggingOut} />

            {/* Logout */}
            <button
              onClick={handleLogout}
              disabled={loggingOut}
              className="hidden sm:flex items-center space-x-1.5 bg-indigo-800 hover:bg-indigo-900 disabled:opacity-60 text-white px-3 py-2 rounded-lg text-sm font-medium transition-colors"
              title="Logout"
            >
              <LogoutIcon className="w-4 h-4" />
              <span>Logout</span>
            </button>

            {/* Mobile hamburger */}
            <button
              onClick={() => setMobileOpen(!mobileOpen)}
              className="md:hidden p-2 text-indigo-100 hover:text-white rounded-lg"
            >
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                {mobileOpen
                  ? <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  : <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
                }
              </svg>
            </button>
          </div>
        </div>
      </div>

      {/* Mobile dropdown */}
      {mobileOpen && (
        <div className="md:hidden border-t border-indigo-600 bg-indigo-800 px-4 py-3 space-y-1">
          {navLinks.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              onClick={() => setMobileOpen(false)}
              className={`flex items-center space-x-2 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors ${
                isActive(to)
                  ? 'bg-indigo-900 text-white'
                  : 'text-indigo-100 hover:text-white hover:bg-indigo-700'
              }`}
            >
              <Icon className="w-4 h-4" />
              <span>{label}</span>
            </Link>
          ))}
        </div>
      )}
    </nav>
  )
}

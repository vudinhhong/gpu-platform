import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useAuth } from '../App.jsx'
import { changePasswordRequest, updateProfile } from '../api/index.js'

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

function ChevronIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
    </svg>
  )
}

function PencilIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
    </svg>
  )
}

function KeyIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round"
        d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
    </svg>
  )
}

function CloseIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Modal shell
//
// Rendered over the dashboard rather than routed to: the page behind keeps
// its state: polling sessions, an open job output, a half-filled form.  So
// editing a profile never costs the user what they were in the middle of.
// ---------------------------------------------------------------------------

function Modal({ title, subtitle, onClose, children, busy }) {
  // Escape closes, and while the modal is up the page behind does not scroll.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !busy) onClose() }
    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose, busy])

  // Into the body, not into the navbar: the navbar is a sticky z-40 element
  // and so a stacking context of its own, which would leave this dialog
  // painting *under* the page's own z-50 overlays and toasts.
  return createPortal((
    <div
      className="fixed inset-0 bg-black/40 flex items-start sm:items-center justify-center z-50 p-4 overflow-y-auto"
      onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose() }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="bg-white rounded-xl shadow-xl w-full max-w-md my-8 sm:my-0"
      >
        <div className="flex items-start justify-between px-6 pt-5 pb-3 border-b border-gray-100">
          <div>
            <h3 className="text-base font-semibold text-gray-900">{title}</h3>
            {subtitle && <p className="text-xs text-gray-500 mt-1">{subtitle}</p>}
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="text-gray-400 hover:text-gray-600 disabled:opacity-40 -mr-1 p-1"
            aria-label="Close"
          >
            <CloseIcon className="w-5 h-5" />
          </button>
        </div>
        {children}
      </div>
    </div>
  ), document.body)
}

function Field({ label, hint, children }) {
  return (
    <label className="block">
      <span className="block text-xs font-semibold text-gray-600 mb-1.5">{label}</span>
      {children}
      {hint && <span className="block text-[11px] text-gray-400 mt-1">{hint}</span>}
    </label>
  )
}

const INPUT =
  'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none ' +
  'focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 disabled:bg-gray-50'

function Alert({ kind, children }) {
  const style = kind === 'error'
    ? 'bg-red-50 border-red-200 text-red-700'
    : 'bg-emerald-50 border-emerald-200 text-emerald-700'
  return (
    <div className={`border rounded-lg px-3 py-2 text-xs ${style}`}>{children}</div>
  )
}

/** Pull the server's own wording out of an axios error. */
function errorText(err, fallback) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg
  return fallback
}

// ---------------------------------------------------------------------------
// Edit profile
// ---------------------------------------------------------------------------

function ProfileModal({ onClose }) {
  const { user, updateUser } = useAuth()
  const [fullName, setFullName] = useState(user?.full_name ?? '')
  const [email, setEmail]       = useState(user?.email ?? '')
  const [busy, setBusy]         = useState(false)
  const [error, setError]       = useState(null)
  const [done, setDone]         = useState(null)

  const dirty =
    fullName.trim() !== (user?.full_name ?? '') || email.trim() !== (user?.email ?? '')

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const res = await updateProfile({
        full_name: fullName.trim(),
        email: email.trim(),
      })
      updateUser(res.data)
      setDone('Saved.')
      // Long enough to read the confirmation, short enough not to be a wait.
      setTimeout(onClose, 900)
    } catch (err) {
      setError(errorText(err, 'Could not save your details.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Edit your details"
      subtitle="Your username and resource limits are set by an administrator."
      onClose={onClose}
      busy={busy}
    >
      <form onSubmit={submit} className="px-6 py-5 space-y-4">
        <Field label="Username">
          <input value={user?.username ?? ''} disabled className={`${INPUT} font-mono text-gray-500`} />
        </Field>
        <Field label="Full name">
          <input
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            maxLength={255}
            autoFocus
            placeholder="How your name should appear"
            className={INPUT}
          />
        </Field>
        <Field label="Email">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            maxLength={255}
            required
            className={INPUT}
          />
        </Field>

        {error && <Alert kind="error">{error}</Alert>}
        {done && <Alert kind="ok">{done}</Alert>}

        <div className="flex justify-end space-x-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !dirty}
            className="px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg disabled:opacity-50"
          >
            {busy ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// Change password
// ---------------------------------------------------------------------------

function PasswordModal({ onClose }) {
  const { user, updateUser, updateToken } = useAuth()
  const [current, setCurrent]   = useState('')
  const [next, setNext]         = useState('')
  const [confirm, setConfirm]   = useState('')
  // Only ever shown to the few users who set a separate Jupyter password.
  const [resetJupyter, setResetJupyter] = useState(true)
  const [busy, setBusy]         = useState(false)
  const [error, setError]       = useState(null)
  const [done, setDone]         = useState(null)

  const mismatch = confirm.length > 0 && next !== confirm
  const ready = current && next.length >= 8 && next === confirm && !busy

  const submit = async (e) => {
    e.preventDefault()
    if (!ready) return
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const res = await changePasswordRequest(current, next, resetJupyter)
      // Before anything else: the token in localStorage was just revoked, and
      // the dashboard behind this modal is still polling with it.
      updateToken(res.data.access_token)
      if (res.data.user) updateUser(res.data.user)
      setDone(res.data.message || 'Password updated.')
      setCurrent('')
      setNext('')
      setConfirm('')
      setTimeout(onClose, 2600)
    } catch (err) {
      setError(errorText(err, 'Could not change your password.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title="Change password"
      subtitle="One password covers the dashboard, SSH and Jupyter."
      onClose={onClose}
      busy={busy}
    >
      <form onSubmit={submit} className="px-6 py-5 space-y-4">
        <Field label="Current password">
          <input
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
            autoFocus
            required
            className={INPUT}
          />
        </Field>
        <Field label="New password" hint="At least 8 characters, with a letter and a digit.">
          <input
            type="password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            autoComplete="new-password"
            required
            className={INPUT}
          />
        </Field>
        <Field label="Confirm new password">
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
            className={`${INPUT} ${mismatch ? 'border-red-300 focus:ring-red-500 focus:border-red-500' : ''}`}
          />
          {mismatch && (
            <span className="block text-[11px] text-red-600 mt-1">
              The two passwords do not match.
            </span>
          )}
        </Field>

        {user?.jupyter_password_set && (
          <label className="flex items-start space-x-2.5 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5">
            <input
              type="checkbox"
              checked={resetJupyter}
              onChange={(e) => setResetJupyter(e.target.checked)}
              className="mt-0.5 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
            />
            <span className="text-[11px] text-gray-600">
              Use this password for Jupyter too. You set a separate Jupyter password;
              unchecking this leaves it as your second factor.
            </span>
          </label>
        )}

        <p className="text-[11px] text-gray-400">
          Your running workspace is not interrupted, and you stay signed in here.
          Any other browser you are signed in on is signed out.
        </p>

        {error && <Alert kind="error">{error}</Alert>}
        {done && <Alert kind="ok">{done}</Alert>}

        <div className="flex justify-end space-x-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg disabled:opacity-60"
          >
            Close
          </button>
          <button
            type="submit"
            disabled={!ready}
            className="px-4 py-2 text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg disabled:opacity-50"
          >
            {busy ? 'Changing…' : 'Change password'}
          </button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// The name in the navbar, as a menu
// ---------------------------------------------------------------------------

export default function AccountMenu({ onSignOut, signingOut }) {
  const { user } = useAuth()
  const [open, setOpen]   = useState(false)
  const [modal, setModal] = useState(null)   // 'profile' | 'password' | null
  const wrapper = useRef(null)

  // A click anywhere else, or Escape, closes the menu.
  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => {
      if (wrapper.current && !wrapper.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const items = [
    { key: 'profile',  label: 'Edit your details', icon: PencilIcon },
    { key: 'password', label: 'Change password',   icon: KeyIcon },
  ]

  const choose = (key) => {
    setOpen(false)
    setModal(key)
  }

  return (
    <>
      <div className="relative" ref={wrapper}>
        <button
          onClick={() => setOpen(!open)}
          aria-haspopup="menu"
          aria-expanded={open}
          className="flex items-center space-x-2.5 bg-indigo-800/60 hover:bg-indigo-800 rounded-xl px-3 py-1.5 transition-colors"
        >
          <div className="w-7 h-7 bg-indigo-500 rounded-full flex items-center justify-center flex-shrink-0">
            <span className="text-white text-xs font-bold">
              {user?.username?.[0]?.toUpperCase() ?? '?'}
            </span>
          </div>
          <div className="leading-tight text-left hidden sm:block">
            <p className="text-white text-sm font-semibold leading-none">
              {user?.full_name || user?.username}
            </p>
            {user?.is_admin && (
              <p className="text-indigo-300 text-xs mt-0.5">Administrator</p>
            )}
          </div>
          <ChevronIcon
            className={`w-3.5 h-3.5 text-indigo-200 transition-transform ${open ? 'rotate-180' : ''}`}
          />
        </button>

        {open && (
          <div
            role="menu"
            className="absolute right-0 mt-2 w-60 bg-white rounded-xl shadow-xl border border-gray-100 py-1.5 z-50"
          >
            <div className="px-4 py-2 border-b border-gray-100">
              <p className="text-sm font-semibold text-gray-900 truncate">
                {user?.full_name || user?.username}
              </p>
              <p className="text-[11px] text-gray-500 truncate">{user?.email}</p>
            </div>
            {items.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                role="menuitem"
                onClick={() => choose(key)}
                className="w-full flex items-center space-x-2.5 px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50"
              >
                <Icon className="w-4 h-4 text-gray-400" />
                <span>{label}</span>
              </button>
            ))}
            {/* Signing out lives here too on a phone, where the navbar's own
                button is the only other place it appears. */}
            {onSignOut && (
              <button
                role="menuitem"
                onClick={() => { setOpen(false); onSignOut() }}
                disabled={signingOut}
                className="sm:hidden w-full flex items-center space-x-2.5 px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-50 border-t border-gray-100 disabled:opacity-60"
              >
                <span className="w-4" />
                <span>Sign out</span>
              </button>
            )}
          </div>
        )}
      </div>

      {modal === 'profile'  && <ProfileModal  onClose={() => setModal(null)} />}
      {modal === 'password' && <PasswordModal onClose={() => setModal(null)} />}
    </>
  )
}

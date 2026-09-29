import React, { useState, useEffect, useCallback } from 'react'
import { getJupyterStatus, startJupyter, stopJupyter, getSshInfo, setSshKey, setJupyterPassword, getMe, getMyImages, setPreferredImage } from '../api/index.js'
import { useConfirm } from './ConfirmDialog.jsx'
import { useAuth } from '../App.jsx'

// ---------------------------------------------------------------------------
// Status Badge
// ---------------------------------------------------------------------------

const STATUS_STYLE = {
  running:  { ring: 'bg-green-100 text-green-700 border-green-200',  dot: 'bg-green-500',  label: 'Running'     },
  stopped:  { ring: 'bg-gray-100  text-gray-600  border-gray-200',   dot: 'bg-gray-400',   label: 'Stopped'     },
  starting: { ring: 'bg-yellow-100 text-yellow-700 border-yellow-200', dot: 'bg-yellow-500 animate-ping', label: 'Starting…'   },
  error:    { ring: 'bg-red-100   text-red-700   border-red-200',    dot: 'bg-red-500',    label: 'Error'       },
}

function StatusBadge({ status }) {
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.stopped
  return (
    <span className={`inline-flex items-center space-x-1.5 px-3 py-1 rounded-full text-xs font-semibold border ${s.ring}`}>
      <span className="relative flex h-2 w-2">
        <span className={`absolute inline-flex h-full w-full rounded-full opacity-75 ${s.dot}`} />
        <span className={`relative inline-flex rounded-full h-2 w-2 ${s.dot.split(' ')[0]}`} />
      </span>
      <span>{s.label}</span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// Spinner helper
// ---------------------------------------------------------------------------

function Spinner({ className = 'w-4 h-4' }) {
  return (
    <svg className={`animate-spin ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Environment (image) picker.  Shown only when there is more than one image.
// ---------------------------------------------------------------------------

function ImagePicker({ disabled, onPick }) {
  const [images, setImages]   = useState([])
  const [selected, setSelect] = useState(null)
  const [saving, setSaving]   = useState(false)

  useEffect(() => {
    let cancelled = false
    getMyImages().then(
      (res) => {
        if (cancelled) return
        setImages(res.data?.images ?? [])
        setSelect(res.data?.selected ?? null)
      },
      () => {},
    )
    return () => { cancelled = true }
  }, [])

  // Nothing to choose from → don't clutter the card.
  if (images.length <= 1) return null

  const change = async (value) => {
    setSelect(value)
    setSaving(true)
    try {
      await setPreferredImage(value)
      onPick?.(value)
    } catch {
      /* the next session start falls back to the platform default */
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-1.5">
      <label className="text-xs font-semibold text-gray-500 uppercase tracking-widest">Environment</label>
      <select
        className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 bg-white disabled:bg-gray-50"
        value={selected ?? ''}
        disabled={disabled || saving}
        onChange={(e) => change(e.target.value)}
      >
        {images.map((img) => (
          <option key={img.image} value={img.image} disabled={!img.available}>
            {img.label}{img.available ? '' : ' (not built)'}
          </option>
        ))}
      </select>
      <p className="text-[11px] text-gray-400">
        {disabled ? 'Stop the session to switch environment.' : 'Applies on your next session start.'}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// SSH section: connection command, password reveal, key editor.
// ---------------------------------------------------------------------------

function SshSection({ sshPort }) {
  const [info, setInfo]         = useState(null)
  const [showPw, setShowPw]     = useState(false)
  const [editingKey, setEditingKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')
  const [keyMsg, setKeyMsg]     = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await getSshInfo()
      setInfo(res.data)
    } catch {
      setInfo(null)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (!info) return null

  const host = window.location.hostname
  const cmd = `ssh -p ${info.port} ${info.username}@${host}`

  const saveKey = async () => {
    setKeyMsg(null)
    try {
      // The server reports whether the key reached the running workspace; if it
      // did, it works straight away with no restart.
      const res = await setSshKey(keyDraft.trim())
      setKeyMsg(res.data?.message ?? (keyDraft.trim() ? 'Key saved.' : 'Key cleared.'))
      setEditingKey(false)
      load()
    } catch (err) {
      setKeyMsg(err.response?.data?.detail ?? 'Could not save key.')
    }
  }

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg p-3.5 space-y-2.5">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest">SSH access</p>
        <button
          onClick={() => setEditingKey(!editingKey)}
          className="text-xs font-medium text-indigo-600 hover:text-indigo-800"
        >
          {info.public_key_set ? 'Edit key' : 'Add SSH key'}
        </button>
      </div>

      {/* Copyable command */}
      <div className="flex items-center bg-gray-900 rounded-lg px-3 py-2">
        <code className="text-xs text-green-400 font-mono flex-1 overflow-x-auto whitespace-nowrap">{cmd}</code>
        <button
          onClick={() => navigator.clipboard?.writeText(cmd)}
          className="ml-2 text-gray-400 hover:text-white transition-colors"
          title="Copy"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h8a2 2 0 002-2v-2m-6-12h8a2 2 0 012 2v8m-2-2h4a2 2 0 002-2V7a2 2 0 00-2-2h-4a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
        </button>
      </div>

      {/* Credential hint.  With unified passwords there is no second secret. */}
      {info.uses_account_password && (
        <p className="text-[11px] text-gray-500">
          Sign in with <span className="font-medium text-gray-700">your account password</span>
          {info.public_key_set ? ', or the SSH key you registered.' : '.'}
        </p>
      )}
      {!info.password_auth_enabled && (
        <p className="text-[11px] text-amber-600">
          Password login is disabled on this platform. Use your SSH key.
        </p>
      )}

      {/* Legacy per-session password (accounts with no derived credential) */}
      {info.password && (
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-500">Password:</span>
          <div className="flex items-center space-x-2">
            <code className="font-mono text-gray-800 bg-white border border-gray-200 rounded px-2 py-0.5">
              {showPw ? info.password : '••••••••••••'}
            </code>
            <button onClick={() => setShowPw(!showPw)} className="text-indigo-600 hover:text-indigo-800 font-medium">
              {showPw ? 'Hide' : 'Show'}
            </button>
          </div>
        </div>
      )}

      {info.public_key_set && (
        <p className="text-xs text-emerald-700">✓ Public key registered, key auth is active</p>
      )}

      {/* Key editor */}
      {editingKey && (
        <div className="space-y-2 pt-1">
          <textarea
            className="form-input font-mono text-xs"
            rows={3}
            placeholder="ssh-ed25519 AAAA... user@laptop"
            defaultValue={keyDraft}
            onChange={(e) => setKeyDraft(e.target.value)}
          />
          <div className="flex justify-end space-x-2">
            <button onClick={() => setEditingKey(false)} className="text-xs px-2.5 py-1.5 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-700 font-medium">Cancel</button>
            <button onClick={saveKey} className="text-xs px-2.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-semibold">Save key</button>
          </div>
        </div>
      )}
      {keyMsg && <p className="text-xs text-gray-600">{keyMsg}</p>}

      <p className="text-[11px] text-gray-400 leading-snug">
        The port changes each time you restart your workspace. Port forwarding is disabled.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Jupyter password.  Replaces the ?token= link with a real login form.
// ---------------------------------------------------------------------------

function JupyterPasswordSection({ jupyterPasswordSet }) {
  const [editing, setEditing]       = useState(false)
  const [pw, setPw]                 = useState('')
  const [pw2, setPw2]               = useState('')
  const [msg, setMsg]               = useState(null)
  const [busy, setBusy]             = useState(false)

  const save = async () => {
    setMsg(null)
    if (pw && pw !== pw2) {
      setMsg('Passwords do not match.')
      return
    }
    setBusy(true)
    try {
      const res = await setJupyterPassword(pw)
      setMsg(res.data?.message ?? 'Saved.')
      setEditing(false)
      setPw('')
      setPw2('')
      window.dispatchEvent(new CustomEvent('jupyter-password-changed'))
    } catch (err) {
      setMsg(err.response?.data?.detail ?? 'Could not save password.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg p-3.5 space-y-2.5">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold text-gray-500 uppercase tracking-widest">Jupyter password</p>
        <button
          onClick={() => { setEditing(!editing); setMsg(null) }}
          className="text-xs font-medium text-indigo-600 hover:text-indigo-800"
        >
          {jupyterPasswordSet ? 'Change password' : 'Set password'}
        </button>
      </div>
      <p className="text-[11px] text-gray-500 leading-snug">
        {jupyterPasswordSet
          ? '✓ Set. Jupyter now asks for this password as a second factor, even when you arrive from the dashboard. It applies from the next session start.'
          : 'Optional. Jupyter already accepts your account password and the dashboard signs you in automatically. Set one here only if you want a separate second factor.'}
      </p>
      {editing && (
        <div className="space-y-2 pt-1">
          <input
            type="password"
            className="form-input text-xs"
            placeholder={jupyterPasswordSet ? 'New password (min 6 chars)' : 'Password (min 6 chars)'}
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            autoComplete="new-password"
          />
          <input
            type="password"
            className="form-input text-xs"
            placeholder="Repeat password"
            value={pw2}
            onChange={(e) => setPw2(e.target.value)}
            autoComplete="new-password"
          />
          <div className="flex justify-end space-x-2">
            <button onClick={() => setEditing(false)} className="text-xs px-2.5 py-1.5 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-700 font-medium">Cancel</button>
            <button onClick={save} disabled={busy} className="text-xs px-2.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 text-white font-semibold">
              {busy ? 'Saving…' : 'Save password'}
            </button>
          </div>
          {jupyterPasswordSet && (
            <p className="text-[11px] text-gray-500">Leave both empty and save to clear the password (back to token links).</p>
          )
          }
        </div>
      )}
      {msg && <p className="text-xs text-gray-600">{msg}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// JupyterCard
// ---------------------------------------------------------------------------

export default function JupyterCard() {
  const { user }                    = useAuth()
  const [data, setData]             = useState(null)
  const [loading, setLoading]       = useState(true)
  const [actionLoading, setAction]  = useState(false)
  const [error, setError]           = useState(null)
  const [notice, setNotice]         = useState(null)
  const [pwSet, setPwSet]           = useState(false)
  // Image chosen in <ImagePicker>; null means "use the stored preference".
  const [chosenImage, setChosen]    = useState(null)
  const [confirmDialog, confirm]    = useConfirm()

  const fetchStatus = useCallback(async () => {
    try {
      const res = await getJupyterStatus()
      setData(res.data)
      setError(null)
    } catch (err) {
      if (err.response?.status === 404) {
        setData(null)   // no session yet, which is not an error as far as the UI goes
        setError(null)
      } else if (err.response?.status !== 401) {
        setError('Could not fetch Jupyter status')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  // Password flag lives on the profile (works even without a session)
  useEffect(() => {
    let cancelled = false
    getMe().then(
      (res) => { if (!cancelled) setPwSet(!!res.data?.jupyter_password_set) },
      () => {},
    )
    const onChanged = () => {
      getMe().then(
        (res) => setPwSet(!!res.data?.jupyter_password_set),
        () => {},
      )
    }
    window.addEventListener('jupyter-password-changed', onChanged)
    return () => {
      cancelled = true
      window.removeEventListener('jupyter-password-changed', onChanged)
    }
  }, [])

  useEffect(() => {
    fetchStatus()
    const id = setInterval(fetchStatus, 5000)
    return () => clearInterval(id)
  }, [fetchStatus])

  // Chosen by <ImagePicker>; undefined means "use the stored preference".
  const handleStart = async () => {
    setAction(true)
    setError(null)
    setNotice(null)
    try {
      const res = await startJupyter(chosenImage)
      // A workspace that started over its space budget started without a GPU;
      // say so at the moment it happens, not only on the resource card.
      if (res?.data?.notice) setNotice(res.data.notice)
      await fetchStatus()
    } catch (err) {
      setError(err.response?.data?.detail ?? 'Failed to start Jupyter')
    } finally {
      setAction(false)
    }
  }

  const handleStop = async () => {
    const ok = await confirm({
      title: 'Stop your workspace?',
      body: 'Notebooks and terminals in it stop, and any kernel state goes with them.',
      detail: [
        'Your files and the packages you installed stay exactly as they are.',
        'Starting it again takes a few seconds.',
        'Jobs you submitted keep running; they do not live in the workspace.',
      ],
      confirmLabel: 'Stop it',
      cancelLabel: 'Leave it running',
    })
    if (!ok) return
    setAction(true)
    setError(null)
    try {
      await stopJupyter()
      await fetchStatus()
    } catch (err) {
      setError(err.response?.data?.detail ?? 'Failed to stop Jupyter')
    } finally {
      setAction(false)
    }
  }

  // ---- Skeleton ----
  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 animate-pulse space-y-4">
        <div className="flex items-center space-x-3">
          <div className="w-10 h-10 bg-gray-200 rounded-xl" />
          <div className="space-y-1.5 flex-1">
            <div className="h-4 bg-gray-200 rounded w-1/3" />
            <div className="h-3 bg-gray-100 rounded w-1/2" />
          </div>
        </div>
        <div className="h-7 bg-gray-100 rounded-full w-28" />
        <div className="h-11 bg-gray-200 rounded-lg" />
      </div>
    )
  }

  const status    = data?.status  ?? 'stopped'
  const isRunning = status === 'running'
  const isStarting= status === 'starting'
  // Always a clean URL.  The platform session already authenticates the
  // request (the proxy presents Jupyter's own credential upstream), so there
  // is no token to put in the address bar and nothing for the user to paste.
  const jupyterUrl = `/jupyter/${user?.username}/`

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 flex flex-col gap-5">

      {/* Header */}
      <div className="flex items-center space-x-3">
        <div className="w-10 h-10 bg-orange-100 rounded-xl flex items-center justify-center flex-shrink-0">
          <svg className="w-5 h-5 text-orange-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        </div>
        <div>
          <h2 className="text-base font-semibold text-gray-900">Jupyter Lab</h2>
          <p className="text-xs text-gray-500">Interactive notebook environment</p>
        </div>
      </div>

      {/* Status row */}
      <div className="flex items-center justify-between">
        <StatusBadge status={status} />
        {isRunning && data?.port && (
          <span className="text-xs text-gray-500 font-medium">
            Port <span className="font-mono text-gray-700">{data.port}</span>
          </span>
        )}
      </div>

      {notice && (
        <div className="flex items-start space-x-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2.5 text-sm text-amber-800">
          <svg className="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
          </svg>
          <span>{notice}</span>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="flex items-start space-x-2 bg-red-50 border border-red-200 rounded-lg px-3 py-2.5 text-sm text-red-700">
          <svg className="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>{error}</span>
        </div>
      )}

      {/* Jupyter password panel.  Always visible; it works before starting too. */}
      {/* A restart is the only way to pick up a rebuilt environment, so say
          so rather than letting someone run an old one indefinitely. */}
      {isRunning && data?.runtime?.environment_current === false && (
        <div className="bg-sky-50 border border-sky-200 rounded-lg px-3.5 py-2.5 text-xs text-sky-800">
          A newer environment is available. Stop and start your workspace to
          get it. Your files and installed packages are not affected.
        </div>
      )}

      <ImagePicker disabled={isRunning || isStarting} onPick={setChosen} />

      <JupyterPasswordSection jupyterPasswordSet={pwSet} />

      {/* SSH section */}
      {isRunning && data?.ssh_port && (
        <SshSection sshPort={data.ssh_port} />
      )}

      {/* Actions */}
      <div className="flex flex-col gap-2.5">
        {/* Open, but only when it is running */}
        {isRunning && (
          <a
            href={jupyterUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center justify-center space-x-2 bg-orange-500 hover:bg-orange-600 active:bg-orange-700 text-white py-2.5 px-4 rounded-lg font-medium text-sm transition-colors shadow-sm shadow-orange-200"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
            </svg>
            <span>Open Jupyter Lab</span>
          </a>
        )}

        {/* Start button */}
        {!isRunning && !isStarting && (
          <button
            onClick={handleStart}
            disabled={actionLoading}
            className="flex items-center justify-center space-x-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-emerald-400 text-white py-2.5 px-4 rounded-lg font-medium text-sm transition-colors shadow-sm"
          >
            {actionLoading ? (
              <><Spinner /><span>Starting…</span></>
            ) : (
              <>
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span>Start Jupyter</span>
              </>
            )}
          </button>
        )}

        {/* Starting indicator */}
        {isStarting && !actionLoading && (
          <div className="flex items-center justify-center space-x-2 py-2.5 text-yellow-700 text-sm font-medium">
            <Spinner className="w-4 h-4 text-yellow-600" />
            <span>Jupyter is starting, please wait…</span>
          </div>
        )}

        {/* Stop button */}
        {(isRunning || isStarting) && (
          <button
            onClick={handleStop}
            disabled={actionLoading || isStarting}
            className="flex items-center justify-center space-x-2 bg-red-600 hover:bg-red-700 disabled:bg-red-400 disabled:cursor-not-allowed text-white py-2 px-4 rounded-lg font-medium text-sm transition-colors"
          >
            {actionLoading ? (
              <><Spinner /><span>Stopping…</span></>
            ) : (
              <>
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
                  <rect x="6" y="6" width="12" height="12" rx="2" />
                </svg>
                <span>Stop Jupyter</span>
              </>
            )}
          </button>
        )}
      </div>
      {confirmDialog}
    </div>
  )
}

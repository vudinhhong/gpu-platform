import React, { useState, useEffect, useCallback } from 'react'
import {
  getAdminUsers,
  createUser,
  updateUser,
  deleteUser,
  resetPassword,
  getGPUAssignments,
  createGPUAssignment,
  updateGPUAssignment,
  deleteGPUAssignment,
  getGPUStatus,
  getJupyterSessions,
  stopUserJupyter,
} from '../api/index.js'
import Navbar from '../components/Navbar.jsx'
import { ResourcesTab, UsageTab, AuditTab, JobsTab, TrashTab } from '../components/AdminPanels.jsx'
import { useConfirm } from '../components/ConfirmDialog.jsx'

// ---------------------------------------------------------------------------
// Small shared UI helpers
// ---------------------------------------------------------------------------

function Spinner({ className = 'w-4 h-4' }) {
  return (
    <svg className={`animate-spin ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

function Toast({ message, type = 'success', onClose }) {
  useEffect(() => {
    const id = setTimeout(onClose, 3500)
    return () => clearTimeout(id)
  }, [onClose])

  const style = type === 'error'
    ? 'bg-red-600'
    : 'bg-emerald-600'

  return (
    <div className={`fixed bottom-6 right-6 z-50 ${style} text-white px-4 py-3 rounded-xl shadow-lg text-sm font-medium animate-fade-in`}>
      {message}
    </div>
  )
}

// A confirmation needs one column of text; a form with eight fields in one
// column becomes a column of scrolling.  `size="lg"` is for the latter, and
// the forms inside lay their fields out two or three across to use it.
const MODAL_WIDTH = { md: 'max-w-md', lg: 'max-w-3xl' }

function Modal({ title, children, onClose, size = 'md' }) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className={`bg-white rounded-2xl shadow-2xl w-full ${MODAL_WIDTH[size]} max-h-[90vh] overflow-y-auto p-6`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-5">
          <h3 className="text-lg font-semibold text-gray-900">{title}</h3>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors p-1 rounded-lg hover:bg-gray-100"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

function Field({ label, children }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1.5">{label}</label>
      {children}
    </div>
  )
}

function ApiError(err, fallback) {
  const detail = err.response?.data?.detail
  return typeof detail === 'string' ? detail : fallback
}

// ---------------------------------------------------------------------------
// Users tab
// ---------------------------------------------------------------------------

function UsersTab({ showToast }) {
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState(null)      // 'create' | {type:'edit', user} | {type:'password', user} | {type:'delete', user}
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState(null)

  const fetchUsers = useCallback(async () => {
    try {
      const res = await getAdminUsers()
      setUsers(res.data)
    } catch (err) {
      showToast(ApiError(err, 'Could not load users.'), 'error')
    } finally {
      setLoading(false)
    }
  }, [showToast])

  useEffect(() => {
    fetchUsers()
  }, [fetchUsers])

  const handleCreate = async (payload) => {
    setBusy(true); setFormError(null)
    try {
      await createUser(payload)
      showToast(`User "${payload.username}" created.`)
      setModal(null)
      fetchUsers()
    } catch (err) {
      setFormError(ApiError(err, 'Could not create user.'))
    } finally {
      setBusy(false)
    }
  }

  const handleUpdate = async (id, payload) => {
    setBusy(true); setFormError(null)
    try {
      await updateUser(id, payload)
      showToast('User updated.')
      setModal(null)
      fetchUsers()
    } catch (err) {
      setFormError(ApiError(err, 'Could not update user.'))
    } finally {
      setBusy(false)
    }
  }

  const handleResetPassword = async (id, newPassword) => {
    setBusy(true); setFormError(null)
    try {
      await resetPassword(id, newPassword)
      showToast('Password reset.')
      setModal(null)
    } catch (err) {
      setFormError(ApiError(err, 'Could not reset password.'))
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (id) => {
    setBusy(true); setFormError(null)
    try {
      const res = await deleteUser(id)
      showToast(res.data?.message || 'User moved to the trash.')
      setModal(null)
      fetchUsers()
    } catch (err) {
      setFormError(ApiError(err, 'Could not delete user.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-gray-900">Users ({users.length})</h2>
        <button
          onClick={() => setModal('create')}
          className="flex items-center space-x-1.5 bg-indigo-600 hover:bg-indigo-700 text-white px-3.5 py-2 rounded-lg text-sm font-medium transition-colors shadow-sm"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          <span>Create User</span>
        </button>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-400"><Spinner className="w-6 h-6" /></div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200 text-left text-xs uppercase tracking-wider text-gray-500">
                <th className="px-5 py-3 font-semibold">User</th>
                <th className="px-5 py-3 font-semibold">Role</th>
                <th className="px-5 py-3 font-semibold">GPUs</th>
                <th className="px-5 py-3 font-semibold">Status</th>
                <th className="px-5 py-3 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {users.map((u) => (
                <tr key={u.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-5 py-3.5">
                    <div className="flex items-center space-x-3">
                      <div className="w-8 h-8 bg-indigo-100 rounded-full flex items-center justify-center flex-shrink-0">
                        <span className="text-indigo-700 text-xs font-bold">{u.username[0].toUpperCase()}</span>
                      </div>
                      <div>
                        <p className="font-semibold text-gray-900">{u.username}</p>
                        <p className="text-xs text-gray-400">{u.full_name || u.email}</p>
                      </div>
                    </div>
                  </td>
                  <td className="px-5 py-3.5">
                    {u.is_admin
                      ? <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-purple-100 text-purple-700">Admin</span>
                      : <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">User</span>}
                  </td>
                  <td className="px-5 py-3.5">
                    {u.gpu_assignment
                      ? <span className="font-mono text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded">GPU {u.gpu_assignment.gpu_indices.join(', ')}</span>
                      : <span className="text-xs text-gray-400">—</span>}
                  </td>
                  <td className="px-5 py-3.5">
                    {u.is_active
                      ? <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-green-100 text-green-700">Active</span>
                      : <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-red-100 text-red-700">Disabled</span>}
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex items-center justify-end space-x-1">
                      <button onClick={() => setModal({ type: 'edit', user: u })} className="text-xs font-medium text-indigo-600 hover:text-indigo-800 px-2 py-1 rounded hover:bg-indigo-50">Edit</button>
                      <button onClick={() => setModal({ type: 'password', user: u })} className="text-xs font-medium text-amber-600 hover:text-amber-800 px-2 py-1 rounded hover:bg-amber-50">Password</button>
                      {u.username !== 'admin' && (
                        <button onClick={() => setModal({ type: 'delete', user: u })} className="text-xs font-medium text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50">Delete</button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr><td colSpan={5} className="px-5 py-8 text-center text-gray-400">No users yet.</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {/* Modals */}
      {modal === 'create' && (
        <Modal title="Create User" size="lg" onClose={() => setModal(null)}>
          <UserForm
            busy={busy}
            error={formError}
            onCancel={() => setModal(null)}
            onSubmit={handleCreate}
          />
        </Modal>
      )}
      {modal?.type === 'edit' && (
        <Modal title={`Edit ${modal.user.username}`} size="lg" onClose={() => setModal(null)}>
          <UserForm
            user={modal.user}
            busy={busy}
            error={formError}
            onCancel={() => setModal(null)}
            onSubmit={(payload) => handleUpdate(modal.user.id, payload)}
          />
        </Modal>
      )}
      {modal?.type === 'password' && (
        <Modal title={`Reset password: ${modal.user.username}`} onClose={() => setModal(null)}>
          <PasswordForm
            busy={busy}
            error={formError}
            onCancel={() => setModal(null)}
            onSubmit={(pw) => handleResetPassword(modal.user.id, pw)}
          />
        </Modal>
      )}
      {modal?.type === 'delete' && (
        <Modal title="Delete user" onClose={() => setModal(null)}>
          <p className="text-sm text-gray-600 mb-2">
            Delete <strong>{modal.user.username}</strong>? Their workspace and any running jobs
            stop immediately and they lose access at once.
          </p>
          <ul className="text-xs text-gray-500 space-y-1 mb-3 list-disc pl-5">
            <li>The account moves to the <strong>Trash</strong>, where it can be restored.</li>
            <li>
              {modal.user.home_path
                ? <>Their home directory <span className="font-mono">{modal.user.home_path}</span> is left exactly as it is.</>
                : <>Their files are kept and renamed aside. Nothing is erased.</>}
            </li>
            <li>The username stays reserved, so no new account can take it and inherit their files.</li>
          </ul>
          {formError && <p className="text-sm text-red-600 mb-3">{formError}</p>}
          <div className="flex justify-end space-x-2 mt-4">
            <button onClick={() => setModal(null)} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">Cancel</button>
            <button
              onClick={() => handleDelete(modal.user.id)}
              disabled={busy}
              className="px-4 py-2 text-sm font-semibold text-white bg-red-600 hover:bg-red-700 disabled:bg-red-400 rounded-lg transition-colors"
            >
              {busy ? 'Deleting…' : 'Move to trash'}
            </button>
          </div>
        </Modal>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// User form (create / edit)
// ---------------------------------------------------------------------------

function UserForm({ user, busy, error, onCancel, onSubmit }) {
  const isEdit = Boolean(user)
  const [username, setUsername]   = useState(user?.username ?? '')
  const [email, setEmail]         = useState(user?.email ?? '')
  const [fullName, setFullName]   = useState(user?.full_name ?? '')
  const [password, setPassword]   = useState('')
  const [isAdmin, setIsAdmin]     = useState(user?.is_admin ?? false)
  const [isActive, setIsActive]   = useState(user?.is_active ?? true)
  // Empty string = "platform default"; 0 also means unlimited server-side.
  const [diskQuota, setDiskQuota] = useState(user?.disk_quota_mb ?? '')
  const [gpuHours, setGpuHours]   = useState(user?.gpu_hours_quota ?? '')
  const [cpuHours, setCpuHours]   = useState(user?.cpu_hours_quota ?? '')
  const [homePath, setHomePath]   = useState(user?.home_path ?? '')

  const numOrNull = (v) => (v === '' || v === null ? null : Number(v))

  const submit = (e) => {
    e.preventDefault()
    const quotas = {
      disk_quota_mb: numOrNull(diskQuota),
      gpu_hours_quota: numOrNull(gpuHours),
      cpu_hours_quota: numOrNull(cpuHours),
      home_path: homePath.trim(),
    }
    if (isEdit) {
      onSubmit({ email, full_name: fullName, is_admin: isAdmin, is_active: isActive, ...quotas })
    } else {
      onSubmit({ username: username.trim(), email: email.trim(), full_name: fullName, is_admin: isAdmin, password, ...quotas })
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-3 py-2.5">{error}</div>
      )}

      {!isEdit && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Field label="Username">
            <input className="form-input" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} maxLength={64} autoFocus />
          </Field>
          <Field label="Password">
            <input className="form-input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={10} placeholder="min. 10 chars, with a letter and a digit" />
          </Field>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <Field label="Email">
          <input className="form-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>

        <Field label="Full name (optional)">
          <input className="form-input" value={fullName} onChange={(e) => setFullName(e.target.value)} />
        </Field>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <Field label="Disk quota (MB)">
          <input className="form-input" type="number" min={0} value={diskQuota}
                 onChange={(e) => setDiskQuota(e.target.value)} placeholder="platform default" />
        </Field>
        <Field label="Job GPU hours / week">
          <input className="form-input" type="number" min={0} step="0.5" value={gpuHours}
                 onChange={(e) => setGpuHours(e.target.value)} placeholder="platform default" />
        </Field>
        <Field label="Job CPU hours / week">
          <input className="form-input" type="number" min={0} step="1" value={cpuHours}
                 onChange={(e) => setCpuHours(e.target.value)} placeholder="platform default" />
        </Field>
      </div>
      <p className="text-[11px] text-gray-400 -mt-2">
        Leave blank for the platform default; 0 = unlimited. The two hour
        budgets cover the job queue only, so this person's own workspace costs
        them nothing here. A job is charged for what it holds: an hour on two
        GPUs is two GPU hours, an hour of a four-core job is four CPU hours.
        They refill every Monday, and a job still running when they run out
        goes back into the queue until they do.
      </p>

      <Field label="Home directory on this machine (optional)">
        <input className="form-input" value={homePath}
               onChange={(e) => setHomePath(e.target.value)}
               placeholder="leave blank to give them a new workspace" />
        <p className="text-[11px] text-gray-400 mt-1">
          Only if this person already has a directory on this server, for example{' '}
          <code className="font-mono">/home/alice</code>. It is mounted as their
          workspace and keeps its existing owner. Nothing is copied and nothing
          is re-owned. Set it only when you know the platform account and the
          host account are the same person: whoever holds this account gets full
          access to that directory.
        </p>
      </Field>

      <div className="flex items-center space-x-6 pt-1">
        <label className="flex items-center space-x-2 text-sm text-gray-700">
          <input type="checkbox" checked={isAdmin} onChange={(e) => setIsAdmin(e.target.checked)} className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500" />
          <span>Administrator</span>
        </label>
        {isEdit && (
          <label className="flex items-center space-x-2 text-sm text-gray-700">
            <input type="checkbox" checked={isActive} onChange={(e) => setIsActive(e.target.checked)} className="rounded border-gray-300 text-indigo-600 focus:ring-indigo-500" />
            <span>Active</span>
          </label>
        )}
      </div>

      <div className="flex justify-end space-x-2 pt-2">
        <button type="button" onClick={onCancel} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">Cancel</button>
        <button type="submit" disabled={busy} className="px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 rounded-lg transition-colors">
          {busy ? 'Saving…' : isEdit ? 'Save changes' : 'Create user'}
        </button>
      </div>
    </form>
  )
}

function PasswordForm({ busy, error, onCancel, onSubmit }) {
  const [pw, setPw] = useState('')
  const [confirm, setConfirm] = useState('')

  const submit = (e) => {
    e.preventDefault()
    if (pw.length < 6) return
    if (pw !== confirm) return
    onSubmit(pw)
  }

  const mismatch = confirm.length > 0 && pw !== confirm

  return (
    <form onSubmit={submit} className="space-y-4">
      {error && <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-3 py-2.5">{error}</div>}
      <Field label="New password">
        <input className="form-input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} required minLength={10} autoFocus />
      </Field>
      <Field label="Confirm password">
        <input className="form-input" type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
      </Field>
      {mismatch && <p className="text-xs text-red-600">Passwords do not match.</p>}
      <div className="flex justify-end space-x-2 pt-2">
        <button type="button" onClick={onCancel} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">Cancel</button>
        <button type="submit" disabled={busy || pw.length < 6 || mismatch} className="px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 rounded-lg transition-colors">
          {busy ? 'Saving…' : 'Reset password'}
        </button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// GPU Assignments tab
// ---------------------------------------------------------------------------

function AssignmentsTab({ showToast }) {
  const [users, setUsers] = useState([])
  const [assignments, setAssignments] = useState([])
  const [gpuCount, setGpuCount] = useState(2)
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState(null)    // 'create' | {type:'edit', assignment}
  const [busy, setBusy] = useState(false)
  const [formError, setFormError] = useState(null)
  const [confirmDialog, confirm] = useConfirm()

  const userById = Object.fromEntries(users.map((u) => [u.id, u]))

  const load = useCallback(async () => {
    try {
      const [uRes, aRes, gRes] = await Promise.all([getAdminUsers(), getGPUAssignments(), getGPUStatus()])
      setUsers(uRes.data)
      setAssignments(aRes.data)
      setGpuCount(Math.max(gRes.data.length, 1))
    } catch (err) {
      showToast(ApiError(err, 'Could not load assignments.'), 'error')
    } finally {
      setLoading(false)
    }
  }, [showToast])

  useEffect(() => { load() }, [load])

  const handleSubmit = async (payload) => {
    setBusy(true); setFormError(null)
    try {
      if (modal === 'create') {
        await createGPUAssignment(payload)
        showToast('Assignment created.')
      } else {
        await updateGPUAssignment(modal.assignment.id, payload)
        showToast('Assignment updated.')
      }
      setModal(null)
      load()
    } catch (err) {
      setFormError(ApiError(err, 'Could not save assignment.'))
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async (assignment) => {
    const username = userById[assignment.user_id]?.username ?? `user #${assignment.user_id}`
    const cards = assignment.gpu_indices.length
      ? `GPU ${assignment.gpu_indices.join(', ')}`
      : 'no GPU'
    const ok = await confirm({
      title: `Remove ${username}'s assignment?`,
      body: `They hold ${cards} under it, with the RAM, core and process limits it sets.`,
      detail: [
        'They fall back to the platform defaults, and to no GPU at all.',
        'A workspace they have running keeps what it was given until they restart it.',
        'Their files, jobs and history are untouched.',
      ],
      confirmLabel: 'Remove it',
    })
    if (!ok) return
    try {
      await deleteGPUAssignment(assignment.id)
      showToast('Assignment removed.')
      load()
    } catch (err) {
      showToast(ApiError(err, 'Could not remove assignment.'), 'error')
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-gray-900">
          Assignments <span className="text-gray-400 font-normal text-sm">GPU, CPU and RAM ({gpuCount} GPUs available)</span>
        </h2>
        <button
          onClick={() => setModal('create')}
          className="flex items-center space-x-1.5 bg-indigo-600 hover:bg-indigo-700 text-white px-3.5 py-2 rounded-lg text-sm font-medium transition-colors shadow-sm"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          <span>New Assignment</span>
        </button>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-400"><Spinner className="w-6 h-6" /></div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200 text-left text-xs uppercase tracking-wider text-gray-500">
                <th className="px-5 py-3 font-semibold">User</th>
                <th className="px-5 py-3 font-semibold">GPUs</th>
                <th className="px-5 py-3 font-semibold">Limits</th>
                <th className="px-5 py-3 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {assignments.map((a) => (
                <tr key={a.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-5 py-3.5 font-semibold text-gray-900">
                    {userById[a.user_id]?.username ?? `user #${a.user_id}`}
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex space-x-1.5">
                      {a.gpu_indices.length === 0
                        ? <span className="text-xs text-gray-400">No GPU, limits only</span>
                        : a.gpu_indices.map((idx) => (
                            <span key={idx} className="font-mono text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded">GPU {idx}</span>
                          ))}
                    </div>
                  </td>
                  <td className="px-5 py-3.5 text-gray-600 text-xs">
                    {a.memory_limit_mb ? `${a.memory_limit_mb} MB` : '—'}
                    {a.cpu_cores ? ` · ${a.cpu_cores} cores` : ''}
                    {a.max_processes ? ` · ${a.max_processes} processes` : ''}
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex items-center justify-end space-x-1">
                      <button onClick={() => setModal({ type: 'edit', assignment: a })} className="text-xs font-medium text-indigo-600 hover:text-indigo-800 px-2 py-1 rounded hover:bg-indigo-50">Edit</button>
                      <button onClick={() => handleDelete(a)} className="text-xs font-medium text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50">Remove</button>
                    </div>
                  </td>
                </tr>
              ))}
              {assignments.length === 0 && (
                <tr><td colSpan={4} className="px-5 py-8 text-center text-gray-400">No assignments yet.</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {modal && (
        <Modal
          title={modal === 'create' ? 'New assignment' : 'Edit assignment'}
          size="lg"
          onClose={() => setModal(null)}
        >
          <AssignmentForm
            users={users}
            assignment={modal === 'create' ? null : modal.assignment}
            gpuCount={gpuCount}
            busy={busy}
            error={formError}
            onCancel={() => setModal(null)}
            onSubmit={handleSubmit}
          />
        </Modal>
      )}
      {confirmDialog}
    </div>
  )
}

function AssignmentForm({ users, assignment, gpuCount, busy, error, onCancel, onSubmit }) {
  const isEdit = Boolean(assignment)
  const assignableUsers = isEdit
    ? users
    : users.filter((u) => !u.gpu_assignment && !u.is_admin)

  const [userId, setUserId]   = useState(assignment?.user_id ?? (assignableUsers[0]?.id ?? ''))
  const [checked, setChecked] = useState(new Set(assignment?.gpu_indices ?? []))
  const [memLimit, setMemLimit]     = useState(assignment?.memory_limit_mb ?? '')
  const [cpuCores, setCpuCores]     = useState(assignment?.cpu_cores ?? '')
  const [maxProcs, setMaxProcs]     = useState(assignment?.max_processes ?? '')

  const toggle = (idx) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })
  }

  // Each limit is optional by itself, so a user can be capped on CPU and RAM
  // without being given a GPU.  An assignment that sets nothing means nothing,
  // though, so at least one field has to be filled in.
  const nothingSet =
    checked.size === 0 && memLimit === '' && cpuCores === '' && maxProcs === ''

  const submit = (e) => {
    e.preventDefault()
    if (nothingSet) return
    const payload = {
      gpu_indices: [...checked].sort((a, b) => a - b),
      memory_limit_mb: memLimit === '' ? null : Number(memLimit),
      cpu_cores: cpuCores === '' ? null : Number(cpuCores),
      max_processes: maxProcs === '' ? null : Number(maxProcs),
    }
    if (!isEdit) payload.user_id = Number(userId)
    onSubmit(payload)
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      {error && <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-3 py-2.5">{error}</div>}

      {!isEdit && (
        <Field label="User">
          <select className="form-input" value={userId} onChange={(e) => setUserId(e.target.value)} required>
            {assignableUsers.length === 0 && <option value="">No users without an assignment</option>}
            {assignableUsers.map((u) => (
              <option key={u.id} value={u.id}>{u.username}</option>
            ))}
          </select>
        </Field>
      )}

      <Field label="GPUs (optional)">
        {/* Wrapping buttons of a fixed width rather than a grid: a grid
            stretches two cards across the whole row on a two-GPU host, and
            squeezes eight into slivers on a full one. */}
        <div className="flex flex-wrap gap-2">
          {Array.from({ length: gpuCount }, (_, i) => (
            <button
              key={i}
              type="button"
              onClick={() => toggle(i)}
              className={`w-14 py-2 rounded-lg text-sm font-semibold border transition-colors ${
                checked.has(i)
                  ? 'bg-indigo-600 border-indigo-600 text-white shadow-sm'
                  : 'bg-white border-gray-300 text-gray-600 hover:border-indigo-400'
              }`}
            >
              {i}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-gray-400 mt-1">
          {checked.size === 0
            ? 'Nothing selected, so the user gets no GPU and only the limits below apply.'
            : `Visible to the user as GPU ${[...checked].sort((a, b) => a - b).map((_, n) => n).join(', ')}.`}
        </p>
      </Field>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <Field label="RAM limit (MB)">
          <input className="form-input" type="number" min={0} value={memLimit ?? ''} onChange={(e) => setMemLimit(e.target.value)} placeholder="8192 = 8 GB" />
          <p className="text-[11px] text-gray-400 mt-1">
            Hard cap, with swap off. Blank = platform default.
          </p>
        </Field>
        <Field label="CPU cores">
          <input className="form-input" type="number" min={0} step="0.25" value={cpuCores}
                 onChange={(e) => setCpuCores(e.target.value)} placeholder="platform default" />
          <p className="text-[11px] text-gray-400 mt-1">
            How many cores the container may use (cgroup cap). Blank = platform default.
          </p>
        </Field>
        <Field label="Processes">
          <input className="form-input" type="number" min={16} value={maxProcs}
                 onChange={(e) => setMaxProcs(e.target.value)} placeholder="platform default (512)" />
          <p className="text-[11px] text-gray-400 mt-1">
            Most processes and threads at once. Raise it for parallel builds or many
            DataLoader workers; the minimum is 16.
          </p>
        </Field>
      </div>
      <p className="text-xs text-gray-400">
        Every field here is optional, but fill in at least one. A blank field falls back to
        the platform default, and clearing a field on an existing assignment removes that
        limit. Changes apply the next time the user starts their workspace.
      </p>
      <p className="text-[11px] text-gray-400">
        A person's compute budget is not here: GPU hours and CPU hours per week
        belong to the user, not to one assignment, and are set when you edit the
        user. This form is the ceiling on what a single workspace may hold at
        any moment.
      </p>
      {nothingSet && (
        <p className="text-xs text-amber-600">
          Nothing is set yet: pick a GPU, or enter a RAM, core or process limit.
        </p>
      )}

      <div className="flex justify-end space-x-2 pt-2">
        <button type="button" onClick={onCancel} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors">Cancel</button>
        <button type="submit" disabled={busy || nothingSet || (!isEdit && !userId)} className="px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 rounded-lg transition-colors">
          {busy ? 'Saving…' : isEdit ? 'Save changes' : 'Create assignment'}
        </button>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// Sessions tab
// ---------------------------------------------------------------------------

function SessionsTab({ showToast }) {
  const [sessions, setSessions] = useState([])
  const [loading, setLoading] = useState(true)
  const [stopping, setStopping] = useState(null)
  const [confirmDialog, confirm] = useConfirm()

  const load = useCallback(async () => {
    try {
      const res = await getJupyterSessions()
      setSessions(res.data)
    } catch (err) {
      showToast(ApiError(err, 'Could not load sessions.'), 'error')
    } finally {
      setLoading(false)
    }
  }, [showToast])

  useEffect(() => {
    load()
    const id = setInterval(load, 8000)
    return () => clearInterval(id)
  }, [load])

  const handleStop = async (userId, username) => {
    const ok = await confirm({
      title: `Stop ${username}'s workspace?`,
      body: 'It stops now, whether or not they are working in it.',
      detail: [
        'Anything running in a notebook stops, and unsaved kernel state is lost.',
        'Their files and installed packages are untouched.',
        'They can start it again themselves from their dashboard.',
      ],
      confirmLabel: 'Stop the workspace',
    })
    if (!ok) return
    setStopping(userId)
    try {
      await stopUserJupyter(userId)
      showToast(`Stopped Jupyter for ${username}.`)
      load()
    } catch (err) {
      showToast(ApiError(err, 'Could not stop session.'), 'error')
    } finally {
      setStopping(null)
    }
  }

  const STATUS_BADGE = {
    running:  'bg-green-100 text-green-700',
    starting: 'bg-yellow-100 text-yellow-700',
    error:    'bg-red-100 text-red-700',
    stopped:  'bg-gray-100 text-gray-600',
  }

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold text-gray-900">Jupyter Sessions ({sessions.length})</h2>

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-gray-400"><Spinner className="w-6 h-6" /></div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200 text-left text-xs uppercase tracking-wider text-gray-500">
                <th className="px-5 py-3 font-semibold">User</th>
                <th className="px-5 py-3 font-semibold">Status</th>
                <th className="px-5 py-3 font-semibold">Port</th>
                <th className="px-5 py-3 font-semibold">PID</th>
                <th className="px-5 py-3 font-semibold">Last activity</th>
                <th className="px-5 py-3 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {sessions.map((s) => (
                <tr key={s.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-5 py-3.5 font-semibold text-gray-900">{s.user?.username ?? `#${s.user_id}`}</td>
                  <td className="px-5 py-3.5">
                    <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${STATUS_BADGE[s.status] ?? STATUS_BADGE.stopped}`}>
                      {s.status}
                    </span>
                  </td>
                  <td className="px-5 py-3.5 font-mono text-gray-600">{s.port}</td>
                  <td className="px-5 py-3.5 font-mono text-gray-600">{s.pid ?? '—'}</td>
                  <td className="px-5 py-3.5 text-gray-500 text-xs">
                    {s.last_activity ? new Date(s.last_activity).toLocaleString() : '—'}
                  </td>
                  <td className="px-5 py-3.5 text-right">
                    {s.status === 'running' && (
                      <button
                        onClick={() => handleStop(s.user_id, s.user?.username)}
                        disabled={stopping === s.user_id}
                        className="text-xs font-semibold text-white bg-red-600 hover:bg-red-700 disabled:bg-red-400 px-3 py-1.5 rounded-lg transition-colors"
                      >
                        {stopping === s.user_id ? 'Stopping…' : 'Force stop'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {sessions.length === 0 && (
                <tr><td colSpan={6} className="px-5 py-8 text-center text-gray-400">No Jupyter sessions yet.</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>
      {confirmDialog}
    </div>
  )
}

// ---------------------------------------------------------------------------
// AdminDashboard root
// ---------------------------------------------------------------------------

const TABS = [
  { key: 'resources',   label: 'Resources' },
  { key: 'users',       label: 'Users' },
  { key: 'assignments', label: 'Assignments' },
  { key: 'sessions',    label: 'Jupyter Sessions' },
  { key: 'jobs',        label: 'Job Queue' },
  { key: 'usage',       label: 'Usage' },
  { key: 'audit',       label: 'Audit' },
  { key: 'trash',       label: 'Trash' },
]

export default function AdminDashboard() {
  const [tab, setTab] = useState('resources')
  const [toast, setToast] = useState(null)

  const showToast = useCallback((message, type = 'success') => {
    setToast({ message, type, id: Date.now() })
  }, [])

  return (
    <div className="min-h-screen bg-gray-50">
      <Navbar />

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <h1 className="text-2xl font-bold text-gray-900 mb-6">Admin Panel</h1>

        {/* Tabs */}
        <div className="flex space-x-1 bg-gray-100 rounded-xl p-1 mb-6 w-fit">
          {TABS.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`px-4 py-2 rounded-lg text-sm font-semibold transition-colors ${
                tab === key
                  ? 'bg-white text-indigo-700 shadow-sm'
                  : 'text-gray-500 hover:text-gray-800'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === 'resources'   && <ResourcesTab />}
        {tab === 'users'       && <UsersTab showToast={showToast} />}
        {tab === 'assignments' && <AssignmentsTab showToast={showToast} />}
        {tab === 'sessions'    && <SessionsTab showToast={showToast} />}
        {tab === 'jobs'        && <JobsTab />}
        {tab === 'usage'       && <UsageTab />}
        {tab === 'audit'       && <AuditTab />}
        {tab === 'trash'       && <TrashTab showToast={showToast} />}
      </main>

      {toast && <Toast key={toast.id} message={toast.message} type={toast.type} onClose={() => setToast(null)} />}
    </div>
  )
}

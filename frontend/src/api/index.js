import axios from 'axios'

// ---------------------------------------------------------------------------
// Axios instance
// ---------------------------------------------------------------------------

const api = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
  timeout: 30000,
})

// Attach JWT on every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Set while a password change is in flight.  That request revokes every token
// issued before it, including the one the dashboard's pollers are holding, so
// a 401 during this window means "your token was replaced", not "you are out".
let credentialRefresh = null

function signOut() {
  localStorage.removeItem('token')
  localStorage.removeItem('user')
  // Avoid redirect loop on the login page itself
  if (!window.location.pathname.startsWith('/login')) {
    window.location.href = '/login'
  }
}

// On 401, clear auth state and redirect to login, unless the token simply
// changed underneath a request that was already on its way.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const config = error.config
    const retriable = error.response?.status === 401 && config && !config._retried
    // The password change itself must never wait on its own completion.
    if (retriable && !config._credentialChange) {
      if (credentialRefresh) {
        config._retried = true
        await credentialRefresh
        return api(config)
      }
      // No change in flight, but the token on disk is not the one this
      // request carried, because another tab changed it.  Same answer.
      const current = localStorage.getItem('token')
      if (current && config.headers?.Authorization !== `Bearer ${current}`) {
        config._retried = true
        return api(config)
      }
    }
    if (error.response?.status === 401) signOut()
    return Promise.reject(error)
  },
)

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

/** Login via OAuth2 form data.  Returns {access_token, token_type, username, is_admin, full_name}. */
export const loginRequest = (username, password) => {
  const fd = new FormData()
  fd.append('username', username)
  fd.append('password', password)
  return api.post('/auth/login', fd, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

export const logoutRequest = () => api.post('/auth/logout')

/**
 * Change the signed-in user's password.
 *
 * The same password covers the platform, SSH and Jupyter, so all three change
 * together.  Every other session is revoked, and the response carries a fresh
 * token for this one, so the caller has to store it or the very next request 401s.
 */
export const changePasswordRequest = (oldPassword, newPassword, resetJupyter = true) => {
  // Opened before the request goes out and resolved once the replacement token
  // is stored: anything that 401s in between waits here and is retried, so a
  // four-second poll cannot land on the revoked token and sign the user out.
  let settled
  credentialRefresh = new Promise((resolve) => { settled = resolve })

  const call = api.put(
    '/auth/password',
    {
      old_password: oldPassword,
      new_password: newPassword,
      reset_jupyter_password: resetJupyter,
    },
    { _credentialChange: true },
  )

  call.then(
    (res) => {
      // Stored here rather than left to the caller, so it is in place before
      // any retry reads it.
      if (res.data?.access_token) localStorage.setItem('token', res.data.access_token)
    },
    () => {},
  ).then(() => {
    credentialRefresh = null
    settled()
  })

  return call
}

// ---------------------------------------------------------------------------
// Current user
// ---------------------------------------------------------------------------

export const getMe             = ()     => api.get('/user/me')
// Self-service profile edit: name and email only.  Everything else about an
// account stays an administrator's to set.
export const updateProfile     = (data) => api.put('/user/me/profile', data)
export const getMyGPU          = ()     => api.get('/user/me/gpu')
export const getJupyterStatus  = ()     => api.get('/user/me/jupyter/status')
// Starting waits for the container's Jupyter to answer HTTP (up to ~60s).
// `image` is optional and validated against the platform allow-list server-side.
export const startJupyter      = (image) => api.post('/user/me/jupyter/start', image ? { image } : {}, { timeout: 95000 })
export const stopJupyter       = ()     => api.post('/user/me/jupyter/stop')
export const getSshInfo        = ()     => api.get('/user/me/ssh')
export const setSshKey         = (key)  => api.put('/user/me/ssh-key', { public_key: key })
export const setJupyterPassword = (pw)  => api.put('/user/me/jupyter-password', { password: pw })

// Live CPU/RAM/disk for the caller's own session + their quota standing
export const getMyResources     = ()     => api.get('/user/me/resources')
// GPU hours, CPU hours and job counts for the signed-in user
export const getMyUsage         = (days = 30) => api.get('/user/me/usage', { params: { days } })
// Jupyter images this user may launch
export const getMyImages        = ()     => api.get('/user/me/images')
export const setPreferredImage  = (img)  => api.put('/user/me/image', { image: img })

// ---------------------------------------------------------------------------
// Jobs.  Submitted from the workspace with `submit`, watched here.
// ---------------------------------------------------------------------------

// What is running now: short, changes constantly, and never paged, because a job must not
// disappear onto page two while its owner is watching it.
export const getRunningJobs = () => api.get('/jobs', { params: { active_only: true } })

// The history: long and static, so ten at a time and only the page in view.
// The history is filtered and sorted on the server: the client only ever has
// one page of it, so sorting what it holds would sort ten rows out of two
// hundred.
export const getFinishedJobs = (page = 1, perPage = 10, opts = {}) =>
  api.get('/jobs', {
    params: {
      finished_only: true,
      page,
      per_page: perPage,
      ...(opts.status ? { status: opts.status } : {}),
      ...(opts.q ? { q: opts.q } : {}),
      ...(opts.sort ? { sort: opts.sort.field, order: opts.sort.order } : {}),
    },
  })
export const getJob       = (id)         => api.get(`/jobs/${id}`)
export const cancelJobs   = (ids)        => api.post('/jobs/cancel', { ids })

// ---------------------------------------------------------------------------
// GPU
// ---------------------------------------------------------------------------

export const getGPUStatus = () => api.get('/gpu/status')
// Combined frame: GPUs + host CPU/RAM + per-container usage, scoped by role
export const getResourceSnapshot = () => api.get('/gpu/resources')

// ---------------------------------------------------------------------------
// Admin: users
// ---------------------------------------------------------------------------

export const getAdminUsers   = ()              => api.get('/admin/users')
export const createUser      = (data)          => api.post('/admin/users', data)
export const updateUser      = (id, data)      => api.put(`/admin/users/${id}`, data)
export const deleteUser      = (id)            => api.delete(`/admin/users/${id}`)
export const resetPassword   = (id, newPass)   =>
  api.post(`/admin/users/${id}/reset-password`, { new_password: newPass })

// ---------------------------------------------------------------------------
// Admin: trash
//
// Deleting a user moves them here; nothing on disk is removed until an
// administrator empties it deliberately.
// ---------------------------------------------------------------------------

export const getTrash          = ()     => api.get('/admin/trash')
export const restoreUser       = (id)   => api.post(`/admin/trash/${id}/restore`)
export const purgeUser         = (id)   => api.delete(`/admin/trash/${id}`)
export const purgeDirectory    = (name) =>
  api.delete(`/admin/trash/directories/${encodeURIComponent(name)}`)

// ---------------------------------------------------------------------------
// Admin: GPU assignments
// ---------------------------------------------------------------------------

export const getGPUAssignments    = ()         => api.get('/admin/gpu/assignments')
export const createGPUAssignment  = (data)     => api.post('/admin/gpu/assignments', data)
export const updateGPUAssignment  = (id, data) => api.put(`/admin/gpu/assignments/${id}`, data)
export const deleteGPUAssignment  = (id)       => api.delete(`/admin/gpu/assignments/${id}`)

// ---------------------------------------------------------------------------
// Admin: Jupyter sessions
// ---------------------------------------------------------------------------

export const getJupyterSessions = ()       => api.get('/admin/jupyter/sessions')
export const stopUserJupyter    = (userId) => api.post(`/admin/jupyter/sessions/${userId}/stop`)

// ---------------------------------------------------------------------------
// Admin: resources, usage accounting, audit trail
// ---------------------------------------------------------------------------

export const getPlatformResources = ()        => api.get('/admin/resources')
export const getUsageReport       = (days=30) => api.get('/admin/usage', { params: { days } })
export const getAuditLog          = (limit=100) => api.get('/admin/audit', { params: { limit } })
// The admin queue is two paged tables over every user's work. `capacity` is
// off for the second of them, so the GPU, CPU and fair-share figures are
// computed once per refresh rather than twice.
export const getAllJobs = (opts = {}) =>
  api.get('/admin/jobs', {
    params: {
      page: opts.page ?? 1,
      per_page: opts.perPage ?? 20,
      capacity: opts.capacity !== false,
      ...(opts.activeOnly ? { active_only: true } : {}),
      ...(opts.finishedOnly ? { finished_only: true } : {}),
      ...(opts.status ? { status: opts.status } : {}),
      ...(opts.q ? { q: opts.q } : {}),
      ...(opts.sort ? { sort: opts.sort.field, order: opts.sort.order } : {}),
    },
  })
export const adminCancelJobs      = (ids)       => api.post('/admin/jobs/cancel', { ids })
export const stopGpuProcess       = (pid, force = false) =>
  api.post(`/admin/gpu/processes/${pid}/stop`, null, { params: force ? { force: true } : {} })

// ---------------------------------------------------------------------------

export default api

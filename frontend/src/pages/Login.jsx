import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { loginRequest } from '../api/index.js'
import { useAuth } from '../App.jsx'

// ---------------------------------------------------------------------------
// Icons
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

function AlertIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Login page
// ---------------------------------------------------------------------------

export default function Login() {
  const { login } = useAuth()
  const navigate = useNavigate()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState(null)

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('Please enter both username and password.')
      return
    }

    setLoading(true)
    setError(null)
    try {
      const res = await loginRequest(username.trim(), password)
      const { access_token, user } = res.data
      login(user, access_token)
      navigate(user?.is_admin ? '/admin' : '/', { replace: true })
    } catch (err) {
      const detail = err.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Login failed. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-indigo-900 via-indigo-800 to-purple-900 px-4">
      <div className="w-full max-w-md">

        {/* Brand header */}
        <div className="flex flex-col items-center mb-8">
          <div className="w-16 h-16 bg-indigo-500/30 backdrop-blur rounded-2xl flex items-center justify-center mb-4 ring-1 ring-indigo-400/50">
            <ChipIcon className="w-9 h-9 text-indigo-200" />
          </div>
          <h1 className="text-3xl font-bold text-white tracking-tight">GPU Platform</h1>
          <p className="text-indigo-300 text-sm mt-1.5">
            Multi-user GPU resource management
          </p>
        </div>

        {/* Card */}
        <div className="bg-white rounded-2xl shadow-2xl shadow-indigo-950/50 p-8">
          <h2 className="text-lg font-semibold text-gray-900 mb-1">Sign in</h2>
          <p className="text-sm text-gray-500 mb-6">
            Use the account your administrator gave you.
          </p>

          {error && (
            <div className="flex items-start space-x-2 bg-red-50 border border-red-200 rounded-lg px-3.5 py-3 mb-5 text-sm text-red-700">
              <AlertIcon className="w-4 h-4 mt-0.5 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4" noValidate>
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-gray-700 mb-1.5">
                Username
              </label>
              <input
                id="username"
                name="username"
                type="text"
                autoComplete="username"
                autoFocus
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="e.g. admin"
                className="form-input focus-ring"
                disabled={loading}
              />
            </div>

            <div>
              <label htmlFor="password" className="block text-sm font-medium text-gray-700 mb-1.5">
                Password
              </label>
              <div className="relative">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  className="form-input focus-ring pr-11"
                  disabled={loading}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 transition-colors"
                  tabIndex={-1}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? (
                    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M13.875 18.825A10.05 10.05 0 0112 19c-4.478 0-8.268-2.943-9.543-7a9.97 9.97 0 011.563-3.029m5.858.908a3 3 0 114.243 4.243M9.878 9.878l4.242 4.242M9.88 9.88l-3.29-3.29m7.532 7.532l3.29 3.29M3 3l3.59 3.59m0 0A9.953 9.953 0 0112 5c4.478 0 8.268 2.943 9.543 7a10.025 10.025 0 01-4.132 5.411m0 0L21 21" />
                    </svg>
                  ) : (
                    <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  )}
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full flex items-center justify-center space-x-2 bg-indigo-600 hover:bg-indigo-700 active:bg-indigo-800 disabled:bg-indigo-400 text-white py-2.5 px-4 rounded-lg font-semibold text-sm transition-colors shadow-md shadow-indigo-200"
            >
              {loading && (
                <svg className="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              )}
              <span>{loading ? 'Signing in…' : 'Sign in'}</span>
            </button>
          </form>
        </div>

        <p className="text-center text-indigo-300/70 text-xs mt-6">
          Experiencing issues? Contact your system administrator.
        </p>
      </div>
    </div>
  )
}

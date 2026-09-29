import React, { createContext, useContext, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import AdminDashboard from './pages/AdminDashboard.jsx'

// ---------------------------------------------------------------------------
// Auth Context
// ---------------------------------------------------------------------------

export const AuthContext = createContext(null)

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}

function loadPersistedUser() {
  try {
    const token = localStorage.getItem('token')
    const raw   = localStorage.getItem('user')
    if (token && raw) return JSON.parse(raw)
  } catch {
    // storage is corrupted; throw it away
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }
  return null
}

// ---------------------------------------------------------------------------
// Route guards
// ---------------------------------------------------------------------------

function PrivateRoute({ children }) {
  const { user } = useAuth()
  return user ? children : <Navigate to="/login" replace />
}

function AdminRoute({ children }) {
  const { user } = useAuth()
  if (!user)          return <Navigate to="/login" replace />
  if (!user.is_admin) return <Navigate to="/"      replace />
  return children
}

// ---------------------------------------------------------------------------
// App root
// ---------------------------------------------------------------------------

export default function App() {
  const [user, setUser] = useState(loadPersistedUser)

  const login = (userData, token) => {
    localStorage.setItem('token', token)
    localStorage.setItem('user', JSON.stringify(userData))
    setUser(userData)
  }

  const logout = () => {
    localStorage.removeItem('token')
    localStorage.removeItem('user')
    setUser(null)
  }

  // Merge a fresh server copy of the account over the cached one, so a name
  // or email edited in a modal shows up in the navbar without a reload.
  const updateUser = (patch) => {
    setUser((current) => {
      const merged = { ...(current ?? {}), ...(patch ?? {}) }
      localStorage.setItem('user', JSON.stringify(merged))
      return merged
    })
  }

  // Changing the password revokes every token issued before it, including the
  // one this tab is holding.  The server hands back a replacement; swapping it
  // in here is what keeps the current session alive instead of bouncing the
  // user to the login page mid-run.
  const updateToken = (token) => {
    if (token) localStorage.setItem('token', token)
  }

  return (
    <AuthContext.Provider value={{ user, login, logout, updateUser, updateToken }}>
      <BrowserRouter>
        <Routes>
          {/* Public */}
          <Route
            path="/login"
            element={user ? <Navigate to="/" replace /> : <Login />}
          />

          {/* User dashboard */}
          <Route
            path="/"
            element={
              <PrivateRoute>
                <Dashboard />
              </PrivateRoute>
            }
          />

          {/* Admin dashboard */}
          <Route
            path="/admin"
            element={
              <AdminRoute>
                <AdminDashboard />
              </AdminRoute>
            }
          />

          {/* Fallback */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthContext.Provider>
  )
}

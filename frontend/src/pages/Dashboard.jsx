import React, { useState, useEffect, useCallback } from 'react'
import { useAuth } from '../App.jsx'
import { getMyGPU } from '../api/index.js'
import Navbar from '../components/Navbar.jsx'
import JupyterCard from '../components/JupyterCard.jsx'
import ResourcePanel from '../components/ResourcePanel.jsx'
import { RunningJobsPanel, FinishedJobsPanel } from '../components/JobsPanel.jsx'
import GPUCard from '../components/GPUCard.jsx'

function RefreshIcon({ className }) {
  return (
    <svg className={className} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0A8.003 8.003 0 015.419 15" />
    </svg>
  )
}

export default function Dashboard() {
  const { user } = useAuth()
  const [myGpus, setMyGpus] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const fetchMyGpus = useCallback(async () => {
    try {
      const res = await getMyGPU()
      setMyGpus(res.data)
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) {
        setError('Could not load your GPU assignment.')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchMyGpus()
    const id = setInterval(fetchMyGpus, 10000)
    return () => clearInterval(id)
  }, [fetchMyGpus])

  return (
    <div className="min-h-screen bg-gray-50">
      <Navbar />

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">
        {/* Greeting */}
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">
              Welcome back, {user?.full_name || user?.username} 👋
            </h1>
            <p className="text-gray-500 text-sm mt-1">
              Launch your personal JupyterLab and monitor your assigned GPUs.
            </p>
          </div>
          <button
            onClick={fetchMyGpus}
            className="self-start sm:self-auto flex items-center space-x-2 bg-white hover:bg-gray-50 border border-gray-200 text-gray-700 px-3.5 py-2 rounded-lg text-sm font-medium transition-colors shadow-sm"
          >
            <RefreshIcon className="w-4 h-4" />
            <span>Refresh</span>
          </button>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Left: the things this user acts on, then the cards they only
              read.  My GPUs used to sit top-centre, which is the most
              valuable space on the page, and nothing on it is clickable. */}
          <div className="lg:col-span-1 space-y-6">
            <JupyterCard />
            <ResourcePanel />

            <div>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-semibold text-gray-900">My GPUs</h2>
                {myGpus.length > 0 && (
                  <span className="text-[11px] text-gray-400">
                    {myGpus.length} assigned
                  </span>
                )}
              </div>

              {loading ? (
                <div className="bg-white rounded-xl border border-gray-200 p-5 animate-pulse space-y-4">
                  <div className="h-10 bg-gray-200 rounded-lg" />
                  <div className="h-2.5 bg-gray-200 rounded-full" />
                  <div className="h-2.5 bg-gray-200 rounded-full" />
                </div>
              ) : error ? (
                <div className="bg-red-50 border border-red-200 rounded-xl px-4 py-5 text-sm text-red-700">
                  {error}
                </div>
              ) : myGpus.length === 0 ? (
                <div className="bg-white rounded-xl border border-dashed border-gray-300 px-4 py-8 text-center">
                  <svg className="w-9 h-9 text-gray-300 mx-auto mb-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <rect x="9" y="9" width="6" height="6" rx="1" strokeLinecap="round" strokeLinejoin="round" />
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 2v2m6-2v2M9 20v2m6-2v2M2 9h2m-2 6h2M20 9h2m-2 6h2" />
                    <rect x="3" y="3" width="18" height="18" rx="3" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <p className="text-gray-600 font-medium text-sm">No GPU assigned</p>
                  <p className="text-gray-400 text-xs mt-1">
                    Ask your administrator to grant you GPU access.
                  </p>
                </div>
              ) : (
                <div className="space-y-4">
                  {myGpus.map((gpu) => (
                    <GPUCard key={gpu.index} gpu={gpu} />
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Right: the work itself.  What is in flight, then what is done. */}
          <div className="lg:col-span-2 space-y-6">
            <RunningJobsPanel />
            <FinishedJobsPanel />
          </div>
        </div>
      </main>
    </div>
  )
}

import React, { useState, useEffect, useCallback } from 'react'
import { getGPUStatus } from '../api/index.js'
import GPUCard from './GPUCard.jsx'

function Skeleton() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 animate-pulse space-y-4">
      <div className="flex items-center space-x-3">
        <div className="w-9 h-9 bg-gray-200 rounded-lg" />
        <div className="space-y-1.5 flex-1">
          <div className="h-3.5 bg-gray-200 rounded w-2/3" />
          <div className="h-2.5 bg-gray-100 rounded w-1/4" />
        </div>
      </div>
      <div className="space-y-3">
        <div className="h-2.5 bg-gray-200 rounded-full" />
        <div className="h-2.5 bg-gray-200 rounded-full" />
      </div>
      <div className="h-8 bg-gray-100 rounded-lg" />
    </div>
  )
}

export default function GPUMonitor({ refreshInterval = 5000 }) {
  const [gpus, setGpus]           = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [lastUpdated, setUpdated] = useState(null)

  const fetchGPUs = useCallback(async () => {
    try {
      const res = await getGPUStatus()
      setGpus(res.data)
      setUpdated(new Date())
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) {
        setError('Could not reach GPU status endpoint.')
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchGPUs()
    const id = setInterval(fetchGPUs, refreshInterval)
    return () => clearInterval(id)
  }, [fetchGPUs, refreshInterval])

  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Skeleton /><Skeleton />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <svg className="w-12 h-12 text-red-300 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round"
            d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
        </svg>
        <p className="text-red-600 font-semibold">{error}</p>
        <button
          onClick={fetchGPUs}
          className="mt-3 text-sm text-indigo-600 hover:text-indigo-800 font-medium underline underline-offset-2"
        >
          Retry
        </button>
      </div>
    )
  }

  if (gpus.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <svg className="w-12 h-12 text-gray-300 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <rect x="9" y="9" width="6" height="6" rx="1" strokeLinecap="round" strokeLinejoin="round" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 2v2m6-2v2M9 20v2m6-2v2M2 9h2m-2 6h2M20 9h2m-2 6h2" />
          <rect x="3" y="3" width="18" height="18" rx="3" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        <p className="text-gray-500 font-medium">No GPUs detected</p>
        <p className="text-gray-400 text-sm mt-1">Check that the backend has GPU access</p>
      </div>
    )
  }

  return (
    <div>
      {lastUpdated && (
        <p className="text-xs text-gray-400 text-right mb-3">
          Updated {lastUpdated.toLocaleTimeString()}
        </p>
      )}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {gpus.map((gpu) => (
          <GPUCard key={gpu.index} gpu={gpu} />
        ))}
      </div>
    </div>
  )
}

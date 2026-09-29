import React from 'react'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function memColor(pct) {
  if (pct < 70) return 'bg-green-500'
  if (pct < 85) return 'bg-yellow-500'
  return 'bg-red-500'
}

function utilColor(pct) {
  if (pct < 70) return 'bg-blue-500'
  if (pct < 90) return 'bg-yellow-500'
  return 'bg-red-500'
}

function tempColor(temp) {
  if (temp < 60) return 'text-green-700 bg-green-100 border-green-200'
  if (temp < 80) return 'text-yellow-700 bg-yellow-100 border-yellow-200'
  return 'text-red-700 bg-red-100 border-red-200'
}

function mbToGiB(mb) {
  return (mb / 1024).toFixed(1)
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ProgressBar({ pct, colorClass, height = 'h-2.5' }) {
  const clampedPct = Math.min(Math.max(pct, 0), 100)
  return (
    <div className={`w-full bg-gray-200 rounded-full overflow-hidden ${height}`}>
      <div
        className={`${height} rounded-full transition-all duration-700 ease-out ${colorClass}`}
        style={{ width: `${clampedPct}%` }}
      />
    </div>
  )
}

function Stat({ label, bar, value, colorClass }) {
  return (
    <div>
      <div className="flex justify-between items-center mb-1">
        <span className="text-xs font-medium text-gray-500">{label}</span>
        <span className="text-xs font-semibold text-gray-700">{value}</span>
      </div>
      <ProgressBar pct={bar} colorClass={colorClass} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// GPUCard
// ---------------------------------------------------------------------------

export default function GPUCard({ gpu }) {
  const memUsedMB  = gpu.used_memory_mb   ?? 0
  const memTotalMB = gpu.total_memory_mb  ?? 1
  const memPct     = (memUsedMB / memTotalMB) * 100
  const utilPct    = gpu.gpu_utilization    ?? 0
  const memBwPct   = gpu.memory_utilization ?? null
  const temp       = gpu.temperature        ?? null
  const processes  = gpu.processes          ?? []

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm hover:shadow-md transition-shadow p-5 flex flex-col gap-4">

      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center space-x-3 min-w-0">
          <div className="w-9 h-9 bg-indigo-100 rounded-lg flex items-center justify-center flex-shrink-0">
            <svg className="w-5 h-5 text-indigo-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
              <rect x="9" y="9" width="6" height="6" rx="1" strokeLinecap="round" strokeLinejoin="round" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 2v2m6-2v2M9 20v2m6-2v2M2 9h2m-2 6h2M20 9h2m-2 6h2" />
              <rect x="3" y="3" width="18" height="18" rx="3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-gray-900 truncate">{gpu.name ?? `GPU ${gpu.index}`}</p>
            <p className="text-xs text-gray-400">Index {gpu.index}</p>
          </div>
        </div>

        {temp !== null && (
          <span className={`text-xs font-bold px-2.5 py-1 rounded-full border flex-shrink-0 ${tempColor(temp)}`}>
            {temp}°C
          </span>
        )}
      </div>

      {/* Stats */}
      <div className="space-y-3">
        <Stat
          label="VRAM"
          bar={memPct}
          value={`${mbToGiB(memUsedMB)} / ${mbToGiB(memTotalMB)} GiB (${memPct.toFixed(0)}%)`}
          colorClass={memColor(memPct)}
        />
        <Stat
          label="GPU Utilization"
          bar={utilPct}
          value={`${utilPct}%`}
          colorClass={utilColor(utilPct)}
        />
        {memBwPct !== null && (
          <Stat
            label="Mem Bandwidth"
            bar={memBwPct}
            value={`${memBwPct}%`}
            colorClass="bg-purple-500"
          />
        )}
      </div>

      {/* Free memory callout */}
      <div className="flex items-center space-x-4 bg-gray-50 rounded-lg px-3 py-2 text-xs">
        <span className="text-gray-500">Free: <span className="font-semibold text-gray-700">{mbToGiB(gpu.free_memory_mb ?? 0)} GiB</span></span>
        <span className="text-gray-300">|</span>
        <span className="text-gray-500">Total: <span className="font-semibold text-gray-700">{mbToGiB(memTotalMB)} GiB</span></span>
      </div>

      {/* Processes */}
      <div>
        <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-2">
          Processes ({processes.length})
        </p>
        {processes.length === 0 ? (
          <p className="text-xs text-gray-400 italic text-center py-2">No running processes</p>
        ) : (
          <div className="space-y-1 max-h-36 overflow-y-auto pr-1">
            {processes.map((proc, i) => (
              <div
                key={i}
                className="flex items-center justify-between bg-gray-50 hover:bg-gray-100 rounded-md px-2.5 py-1.5 transition-colors"
              >
                <div className="flex items-center space-x-2 min-w-0">
                  <span className="text-xs font-mono text-gray-400 flex-shrink-0">
                    {proc.pid}
                  </span>
                  {/* Who is on this card is the question the monitor exists
                      to answer, and on a shared GPU the answer has to name
                      other people.  What they are RUNNING does not: a command
                      line carries their paths and their project names, and
                      nobody needs that to understand a card they share.  The
                      API does not send it here either. */}
                  <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 flex-shrink-0">
                    {proc.user ?? 'outside the platform'}
                  </span>
                </div>
                <span className="text-xs text-gray-500 font-semibold flex-shrink-0 ml-2 tabular-nums">
                  {mbToGiB(proc.memory_used_mb ?? 0)} GiB
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

import React, { useState, useEffect, useCallback } from 'react'
import { getMyResources } from '../api/index.js'

// ---------------------------------------------------------------------------
// Shared meter
// ---------------------------------------------------------------------------

function toneFor(percent) {
  if (percent == null) return 'bg-gray-300'
  if (percent >= 90) return 'bg-red-500'
  if (percent >= 75) return 'bg-amber-500'
  return 'bg-indigo-500'
}

export function Meter({ label, value, percent, hint }) {
  const width = percent == null ? 0 : Math.max(0, Math.min(100, percent))
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-medium text-gray-600">{label}</span>
        <span className="text-xs font-mono text-gray-800">{value}</span>
      </div>
      <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-[width] duration-500 ${toneFor(percent)}`}
          style={{ width: `${width}%` }}
        />
      </div>
      {hint && <p className="text-[11px] text-gray-400">{hint}</p>}
    </div>
  )
}

const fmtMb = (mb) =>
  mb == null ? '—' : mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`

// ---------------------------------------------------------------------------
// User-facing resource + quota card
// ---------------------------------------------------------------------------

export default function ResourcePanel({ refreshInterval = 8000 }) {
  const [data, setData]   = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await getMyResources()
      setData(res.data)
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) setError('Could not load resource usage.')
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, refreshInterval)
    return () => clearInterval(id)
  }, [load, refreshInterval])

  if (error) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-5 text-sm text-red-600">{error}</div>
    )
  }
  if (!data) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-5 animate-pulse space-y-3">
        <div className="h-3 bg-gray-200 rounded w-1/3" />
        <div className="h-2 bg-gray-100 rounded-full" />
        <div className="h-2 bg-gray-100 rounded-full" />
      </div>
    )
  }

  const live   = data.container
  const quota  = data.quota ?? {}
  const disk   = quota.disk ?? {}
  const gpuH   = quota.gpu_hours ?? {}
  const cpuH   = quota.cpu_hours ?? {}
  const period = quota.period ?? {}
  // "this week" rather than "2026-W39": the label is what the API counts
  // against, the sentence is what a person reads.
  const periodWord = period.kind === 'month' ? 'this month' : 'this week'
  const refills = period.resets_text ? ` It refills on ${period.resets_text}.` : ''
  // Said the same way under both meters: what running out actually costs.
  const outOfHours = (what) =>
    `You are out of ${what} for jobs ${periodWord}. Nothing new starts from the `
    + 'queue, and a job still running goes back into it to start again when the '
    + `budget refills.${refills} Your workspace is not affected.`
  const memory = live?.memory ?? {}
  const cpuCores = data.limits?.cpu_cores

  // The collector reports CPU per-core-summed: 200% means two fully busy
  // cores.  Shown as cores against the allocation, because "200%" beside a
  // 4-core limit reads like something is over budget when it is at half.
  const cpuCoresUsed = live?.cpu_percent == null ? null : live.cpu_percent / 100
  const cpuPercent = cpuCoresUsed == null || !cpuCores
    ? null
    : Math.min(100, (cpuCoresUsed / cpuCores) * 100)

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">Resource usage</h3>
        <span className="text-[11px] text-gray-400">
          {live ? 'workspace running' : 'workspace stopped'}
        </span>
      </div>

      {live ? (
        <div className="space-y-3">
          <Meter
            label="CPU"
            value={
              cpuCoresUsed == null
                ? '—'
                : cpuCores
                  ? `${cpuCoresUsed.toFixed(2)} / ${cpuCores} cores`
                  : `${cpuCoresUsed.toFixed(2)} cores`
            }
            percent={cpuPercent}
          />
          <Meter
            label="Memory"
            value={`${fmtMb(memory.used_mb)} of ${fmtMb(memory.limit_mb ?? data.limits?.memory_limit_mb)}`}
            percent={memory.percent}
          />
          <div className="flex items-center justify-between text-[11px] text-gray-500 pt-1 border-t border-gray-100">
            <span>
              Processes: {live.pids ?? '—'}
              {(data.enforced?.pids_limit ?? data.limits?.max_processes)
                ? ` of ${data.enforced?.pids_limit ?? data.limits?.max_processes}`
                : ''}
            </span>
            <span>Disk read {live.disk_read_mb ?? 0} MB · written {live.disk_write_mb ?? 0} MB</span>
          </div>
        </div>
      ) : (
        <p className="text-xs text-gray-400">
          Start your workspace to see live CPU and memory usage.
        </p>
      )}

      {/* Quotas apply whether or not a session is running */}
      <div className="space-y-3 pt-3 border-t border-gray-100">
        <Meter
          label="Disk space"
          value={
            disk.quota_mb
              ? `${fmtMb(disk.used_mb)} / ${fmtMb(disk.quota_mb)}`
              : `${fmtMb(disk.used_mb)} (no quota)`
          }
          percent={disk.percent}
          hint={disk.over
            ? 'You are over your space budget. The workspace still starts so you can delete files, but until you are back under it runs without a GPU and cannot submit jobs.'
            : null}
        />
        <Meter
          label={`Job GPU hours (${periodWord})`}
          value={gpuH.quota ? `${gpuH.used ?? 0}h / ${gpuH.quota}h` : `${gpuH.used ?? 0}h (no quota)`}
          percent={gpuH.percent}
          hint={gpuH.over ? outOfHours('GPU hours') : null}
        />
        <Meter
          label={`Job CPU hours (${periodWord})`}
          value={cpuH.quota ? `${cpuH.used ?? 0}h / ${cpuH.quota}h` : `${cpuH.used ?? 0}h (no quota)`}
          percent={cpuH.percent}
          hint={cpuH.over ? outOfHours('CPU hours') : null}
        />
        <p className="text-[11px] text-gray-400">
          These two cover jobs you send to the queue. This workspace is not
          counted, however long you leave it open. A job is charged for what it
          holds: an hour on two GPUs is two GPU hours, and an hour of a
          {' '}{cpuCores ? `${cpuCores}-core` : 'four-core'} job is
          {' '}{cpuCores || 'four'} CPU hours, busy or idle.
        </p>
      </div>
    </div>
  )
}

import React, { useState, useEffect, useCallback } from 'react'
import {
  getPlatformResources, getUsageReport, getAuditLog, getAllJobs, adminCancelJobs,
  getTrash, restoreUser, purgeUser, purgeDirectory, stopGpuProcess,
} from '../api/index.js'
import { useConfirm } from './ConfirmDialog.jsx'
import Pager from './Pager.jsx'
import { TimeCell, duration } from './format.jsx'
import { Meter } from './ResourcePanel.jsx'

const fmtMb = (mb) =>
  mb == null ? '—' : mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`

function Card({ title, children, right }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        {right}
      </div>
      <div className="p-5">{children}</div>
    </div>
  )
}

function Stat({ label, value, sub }) {
  return (
    <div className="bg-gray-50 rounded-lg px-4 py-3">
      <p className="text-[11px] uppercase tracking-wider text-gray-400 font-semibold">{label}</p>
      <p className="text-lg font-bold text-gray-900 mt-0.5">{value}</p>
      {sub && <p className="text-[11px] text-gray-500">{sub}</p>}
    </div>
  )
}

function usePolled(fetcher, interval, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await fetcher()
      setData(res.data)
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) {
        setError(err.response?.data?.detail ?? 'Request failed.')
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    load()
    if (!interval) return undefined
    const id = setInterval(load, interval)
    return () => clearInterval(id)
  }, [load, interval])

  return { data, error, reload: load }
}

// ---------------------------------------------------------------------------
// Resources tab: the whole machine on one screen.
// ---------------------------------------------------------------------------

export function ResourcesTab() {
  const { data, error, reload } = usePolled(getPlatformResources, 8000)
  const [busyPid, setBusyPid] = useState(null)
  const [notice, setNotice] = useState(null)
  const [confirmDialog, confirm] = useConfirm()

  const stopProcess = async (proc) => {
    const ok = await confirm({
      title: `Stop ${proc.user}'s process ${proc.pid}?`,
      body: proc.name,
      detail: [
        'It is sent SIGTERM, so anything it was writing is lost unless it saves on exit.',
        'Their workspace keeps running; only this process is stopped.',
      ],
      confirmLabel: 'Stop the process',
    })
    if (!ok) return
    setBusyPid(proc.pid)
    setNotice(null)
    try {
      const res = await stopGpuProcess(proc.pid)
      setNotice({ tone: 'ok', text: res.data.message })
      await reload()
    } catch (err) {
      setNotice({ tone: 'error', text: err.response?.data?.detail ?? 'Could not stop it.' })
    } finally {
      setBusyPid(null)
    }
  }

  if (error) return <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">{error}</div>
  if (!data) return <div className="text-sm text-gray-400">Loading resource snapshot…</div>

  const host = data.host ?? {}
  const mem = host.memory ?? {}
  const disk = host.data_disk ?? {}
  const locked = Object.entries(data.locked_accounts ?? {})
  const periodWord = data.settings?.quota_period === 'month' ? 'month' : 'week'

  return (
    <div className="space-y-6">
      {data.settings?.allow_mock_gpu && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 text-sm rounded-lg px-4 py-2.5">
          ALLOW_MOCK_GPU is enabled, so the GPU figures below may be simulated.
        </div>
      )}

      {/* Settings Docker accepted that the kernel is not actually holding.
          Read from the cgroup, because `docker inspect` reports what Docker
          recorded and the two can disagree without it noticing. Normally this
          renders nothing at all. */}
      {(data.limit_divergences ?? []).length > 0 && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3">
          <p className="text-sm font-semibold text-red-800">
            {data.limit_divergences.length === 1
              ? 'One limit is not being held by the kernel'
              : `${data.limit_divergences.length} limits are not being held by the kernel`}
          </p>
          <table className="mt-2 w-full text-xs">
            <thead>
              <tr className="text-left text-red-700/70">
                <th className="pr-4 pb-1 font-medium">User</th>
                <th className="pr-4 pb-1 font-medium">Setting</th>
                <th className="pr-4 pb-1 font-medium">Asked for</th>
                <th className="pr-4 pb-1 font-medium">Kernel holds</th>
                <th className="pb-1 font-medium">What it means</th>
              </tr>
            </thead>
            <tbody className="text-red-900">
              {data.limit_divergences.map((d, i) => (
                <tr key={`${d.username}-${d.setting}-${i}`}>
                  <td className="pr-4 py-0.5 font-medium">{d.username}</td>
                  <td className="pr-4 py-0.5">{d.setting}</td>
                  <td className="pr-4 py-0.5 font-mono">{d.configured}</td>
                  <td className="pr-4 py-0.5 font-mono">{d.kernel}</td>
                  <td className="py-0.5 text-red-700">{d.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-red-700">
            Stopping the workspace and starting it again removes the container
            and builds a new one, which is what puts the setting back.
          </p>
        </div>
      )}

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Host CPU" value={host.cpu_percent == null ? '—' : `${host.cpu_percent}%`}
              sub={`across ${host.cpu_count ?? '?'} cores`} />
        <Stat label="Host RAM" value={`${mem.percent ?? '—'}%`} sub={`${fmtMb(mem.used_mb)} / ${fmtMb(mem.total_mb)}`} />
        <Stat label="Data disk" value={`${disk.percent ?? '—'}%`}
              sub={`${disk.used_gib ?? '—'} / ${disk.total_gib ?? '—'} GiB · ${disk.free_gib ?? '—'} GiB free`} />
        <Stat label="Idle reap" value={data.settings?.idle_timeout_minutes ? `${data.settings.idle_timeout_minutes}m` : 'off'}
              sub={`defaults ${data.settings?.default_cpu_cores ?? '—'} cores · ${fmtMb(data.settings?.default_memory_limit_mb)}`} />
      </div>

      <Card title="GPUs">
        {notice && (
          <div className={`mb-4 text-sm rounded-lg px-4 py-2.5 border ${
            notice.tone === 'ok'
              ? 'bg-green-50 border-green-200 text-green-800'
              : 'bg-red-50 border-red-200 text-red-700'}`}>
            {notice.text}
          </div>
        )}
        {/* "No GPUs" and "the backend cannot read the GPUs" are different
            problems with the same appearance, and the second one is the
            common one: say which it is. */}
        {data.gpu_telemetry && !data.gpu_telemetry.ok && (
          <div className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
            <p className="font-semibold">
              The backend cannot read the GPUs
              {data.gpu_telemetry.failing_for_seconds != null
                ? ` (for ${Math.round(data.gpu_telemetry.failing_for_seconds / 60)} min)`
                : ''}.
            </p>
            {data.gpu_telemetry.error && (
              <p className="mt-1 font-mono text-[11px]">{data.gpu_telemetry.error}</p>
            )}
            <p className="mt-1 text-[12px]">
              {data.gpu_telemetry.ever_seen
                ? 'The figures below are the last good reading and are no longer live. No new GPU job will be placed until this clears.'
                : 'No reading has ever succeeded on this deployment.'}
              {' '}Check <code className="font-mono">nvidia-smi</code> on the host: if the host
              is fine, the container has lost its device access and recreating
              it with <code className="font-mono">./deploy.sh</code> restores it.
            </p>
          </div>
        )}
        {(data.gpus ?? []).length === 0 ? (
          <p className="text-sm text-gray-400">No GPUs visible to the platform.</p>
        ) : (
          <div className="space-y-4">
            {data.gpus.map((gpu) => {
              const procs = gpu.processes ?? []
              const named = procs.reduce((n, p) => n + (p.user ? p.memory_used_mb : 0), 0)
              const outside = procs.reduce((n, p) => n + (p.user ? 0 : p.memory_used_mb), 0)
              return (
                <div key={gpu.index} className="border border-gray-100 rounded-lg p-4 space-y-2.5">
                  <div className="flex items-baseline justify-between">
                    <span className="text-sm font-semibold text-gray-900">GPU {gpu.index}</span>
                    <span className="text-[11px] text-gray-400 truncate ml-2">{gpu.name}</span>
                  </div>
                  <Meter label="Memory"
                         value={`${fmtMb(gpu.used_memory_mb)} / ${fmtMb(gpu.total_memory_mb)}`}
                         percent={gpu.total_memory_mb ? (gpu.used_memory_mb / gpu.total_memory_mb) * 100 : 0} />
                  <Meter label="Utilisation" value={`${gpu.gpu_utilization}%`} percent={gpu.gpu_utilization} />
                  <div className="flex items-center justify-between text-[11px] text-gray-500">
                    <span>{gpu.temperature}°C{gpu.power_watts ? ` · ${gpu.power_watts} W` : ''}</span>
                    <span>
                      {named ? `${fmtMb(named)} by platform users` : 'nothing the platform owns'}
                      {outside ? ` · ${fmtMb(outside)} outside it` : ''}
                    </span>
                  </div>

                  {procs.length === 0 ? (
                    <p className="text-[11px] text-gray-400 pt-1">
                      No compute processes on this card.
                    </p>
                  ) : (
                    <div className="overflow-x-auto -mx-4 px-4 pt-1">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="text-left text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                            <th className="py-1.5 pr-4 font-semibold">User</th>
                            <th className="py-1.5 pr-4 font-semibold">Memory</th>
                            <th className="py-1.5 pr-4 font-semibold">PID</th>
                            <th className="py-1.5 pr-4 font-semibold">Command</th>
                            <th className="py-1.5 pr-4" />
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-50">
                          {procs.map((p) => (
                            <tr key={p.pid} className={p.user ? '' : 'text-gray-400'}>
                              <td className="py-1.5 pr-4 font-medium whitespace-nowrap">
                                {p.user ?? <span className="italic">outside the platform</span>}
                              </td>
                              <td className="py-1.5 pr-4 font-mono text-xs whitespace-nowrap">
                                {fmtMb(p.memory_used_mb)}
                              </td>
                              <td className="py-1.5 pr-4 font-mono text-xs">{p.pid}</td>
                              <td className="py-1.5 pr-4 font-mono text-[11px] break-all max-w-[32rem]">
                                {p.name}
                              </td>
                              <td className="py-1.5 pr-4 text-right whitespace-nowrap">
                                {p.stoppable ? (
                                  <button onClick={() => stopProcess(p)} disabled={busyPid === p.pid}
                                          className="text-xs font-medium text-red-600 hover:text-red-800 disabled:text-red-300">
                                    {busyPid === p.pid ? 'Stopping…' : 'Stop'}
                                  </button>
                                ) : (
                                  <span className="text-[11px] text-gray-300">—</span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </Card>

      <Card title="Per-user consumption">
        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                <th className="py-2 pr-4 font-semibold">User</th>
                <th className="py-2 pr-4 font-semibold">Session</th>
                <th className="py-2 pr-4 font-semibold">GPUs</th>
                <th className="py-2 pr-4 font-semibold">CPU</th>
                <th className="py-2 pr-4 font-semibold">RAM</th>
                <th className="py-2 pr-4 font-semibold">Disk</th>
                <th className="py-2 pr-4 font-semibold">Job GPU h ({periodWord})</th>
                <th className="py-2 pr-4 font-semibold">Job CPU h ({periodWord})</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {(data.users ?? []).map((u) => {
                const q = u.quota ?? {}
                return (
                  <tr key={u.username} className={u.is_active ? '' : 'opacity-50'}>
                    <td className="py-2 pr-4 font-medium text-gray-900">{u.username}</td>
                    <td className="py-2 pr-4">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        u.session_status === 'running' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                      }`}>{u.session_status}</span>
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">{u.gpu_indices?.length ? u.gpu_indices.join(',') : '—'}</td>
                    <td className="py-2 pr-4 font-mono text-xs">
                      {/* Cores, not percent.  Docker sums across cores, so
                          200% means two busy cores, which reads very oddly
                          next to a cap expressed in cores. */}
                      {u.cpu_percent == null ? '—' : `${(u.cpu_percent / 100).toFixed(2)} cores`}
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">
                      {u.memory?.used_mb == null ? '—' : `${fmtMb(u.memory.used_mb)}${u.memory.limit_mb ? ` / ${fmtMb(u.memory.limit_mb)}` : ''}`}
                    </td>
                    {/* The figure and the red both come from the quota
                        snapshot.  Reading the number from one source and the
                        verdict from another is how "0 MB / 195.3 GB" ended up
                        in red. */}
                    <td className={`py-2 pr-4 font-mono text-xs ${q.disk?.over ? 'text-red-600 font-semibold' : ''}`}>
                      {fmtMb(q.disk?.used_mb ?? u.disk_used_mb)}{q.disk?.quota_mb ? ` / ${fmtMb(q.disk.quota_mb)}` : ''}
                    </td>
                    <td className={`py-2 pr-4 font-mono text-xs ${q.gpu_hours?.over ? 'text-red-600 font-semibold' : ''}`}>
                      {q.gpu_hours?.used ?? 0}{q.gpu_hours?.quota ? ` / ${q.gpu_hours.quota}` : ''}
                    </td>
                    <td className={`py-2 pr-4 font-mono text-xs ${q.cpu_hours?.over ? 'text-red-600 font-semibold' : ''}`}>
                      {q.cpu_hours?.used ?? 0}{q.cpu_hours?.quota ? ` / ${q.cpu_hours.quota}` : ''}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Card>

      {locked.length > 0 && (
        <Card title="Locked out (failed logins)">
          <ul className="text-sm text-gray-700 space-y-1">
            {locked.map(([key, seconds]) => (
              <li key={key} className="font-mono text-xs">{key}: {seconds}s remaining</li>
            ))}
          </ul>
        </Card>
      )}
      {confirmDialog}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Usage tab: GPU-hour accounting.
// ---------------------------------------------------------------------------

export function UsageTab() {
  const [days, setDays] = useState(30)
  const { data, error } = usePolled(() => getUsageReport(days), 0, [days])

  if (error) return <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">{error}</div>
  if (!data) return <div className="text-sm text-gray-400">Loading usage…</div>

  return (
    <div className="space-y-6">
      <div className="flex items-center space-x-2">
        <span className="text-sm text-gray-500">Period:</span>
        {[7, 30, 90].map((d) => (
          <button key={d} onClick={() => setDays(d)}
            className={`px-3 py-1 rounded-lg text-xs font-semibold ${
              days === d ? 'bg-indigo-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            }`}>
            {d} days
          </button>
        ))}
      </div>

      <Card title={`Consumption per user, last ${data.days} days`}>
        {data.totals.length === 0 ? (
          <p className="text-sm text-gray-400">No sessions recorded in this period.</p>
        ) : (
          <div className="space-y-2.5">
            {data.totals.map((row) => {
              const max = Math.max(...data.totals.map((t) => t.gpu_hours), 1)
              return (
                <div key={row.username} className="flex items-center space-x-3">
                  <span className="w-32 truncate text-sm font-medium text-gray-800">{row.username}</span>
                  <div className="flex-1 h-5 bg-gray-100 rounded-md overflow-hidden">
                    <div className="h-full bg-indigo-500 rounded-md transition-[width] duration-500"
                         style={{ width: `${(row.gpu_hours / max) * 100}%` }} />
                  </div>
                  <span className="w-52 text-right text-xs font-mono text-gray-600">
                    {row.gpu_hours}h GPU · {row.cpu_hours}h CPU · {row.jobs_submitted ?? 0} jobs
                  </span>
                </div>
              )
            })}
          </div>
        )}
      </Card>

      <Card title="Recent sessions">
        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                <th className="py-2 pr-4 font-semibold">User</th>
                <th className="py-2 pr-4 font-semibold">Started</th>
                <th className="py-2 pr-4 font-semibold">Ended</th>
                <th className="py-2 pr-4 font-semibold">GPUs</th>
                <th className="py-2 pr-4 font-semibold">GPU h</th>
                <th className="py-2 pr-4 font-semibold">CPU h</th>
                <th className="py-2 pr-4 font-semibold">Peak RAM</th>
                <th className="py-2 pr-4 font-semibold">Reason</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {data.sessions.map((s, i) => (
                <tr key={i}>
                  <td className="py-2 pr-4 font-medium text-gray-900">{s.username}</td>
                  <td className="py-2 pr-4 text-xs text-gray-500">{new Date(s.started_at + 'Z').toLocaleString()}</td>
                  <td className="py-2 pr-4 text-xs text-gray-500">
                    {s.ended_at ? new Date(s.ended_at + 'Z').toLocaleString() : <span className="text-green-600 font-semibold">running</span>}
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs">{s.gpu_indices || '—'}</td>
                  <td className="py-2 pr-4 font-mono text-xs">{s.gpu_hours}</td>
                  <td className="py-2 pr-4 font-mono text-xs">{s.cpu_hours ?? '—'}</td>
                  <td className="py-2 pr-4 font-mono text-xs">{fmtMb(s.peak_memory_mb)}</td>
                  <td className="py-2 pr-4 text-xs text-gray-500">{s.end_reason ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Jobs tab: the whole queue, every user.
// ---------------------------------------------------------------------------

const JOB_TONE = {
  queued: 'bg-amber-100 text-amber-700', starting: 'bg-sky-100 text-sky-700',
  running: 'bg-green-100 text-green-700', paused: 'bg-violet-100 text-violet-700',
  succeeded: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700', cancelled: 'bg-gray-100 text-gray-500',
  timeout: 'bg-red-100 text-red-700',
}

// The same statuses again, as text rather than a pill: a failure reason is
// read, not glanced at, so it wants a readable colour and not a badge.
const JOB_MESSAGE_TONE = {
  failed: 'text-red-600', timeout: 'text-red-600', paused: 'text-violet-600',
}

const JOB_STATUS_FILTERS = {
  active: [
    ['', 'All'],
    ['running,starting', 'Running'],
    ['queued', 'Waiting'],
    ['paused', 'Paused'],
  ],
  finished: [
    ['', 'All'],
    ['succeeded', 'Finished'],
    ['failed', 'Failed'],
    ['timeout', 'Timed out'],
    ['cancelled', 'Cancelled'],
  ],
}

function SortTh({ children, field, sort, onSort, className = '' }) {
  const active = sort.field === field
  return (
    <th className={`py-2 pr-4 font-semibold ${className}`}>
      <button
        type="button"
        onClick={() => onSort(field)}
        className={`inline-flex items-center gap-1 uppercase tracking-wider transition-colors ${
          active ? 'text-gray-700' : 'hover:text-gray-600'
        }`}
      >
        {children}
        <span className={active ? 'opacity-100' : 'opacity-0'}>
          {active && sort.order === 'asc' ? '▲' : '▼'}
        </span>
      </button>
    </th>
  )
}

/**
 * What a job is holding on the card, or held at its highest once it is over.
 *
 * While it runs the live figure is the useful one: it is what the reservation
 * is being judged against right now.  Afterwards it is close to meaningless --
 * the last reading usually catches a job that has already released everything,
 * so it reads zero -- and the peak is what answers whether the reservation was
 * anywhere near the truth.
 */
function VramCell({ job, live }) {
  if (!job.gpu_count || !job.gpu_memory_mb) return <span className="text-gray-400">—</span>
  const mb = live ? job.gpu_memory_used_mb : job.gpu_memory_peak_mb
  if (mb == null) return <span className="text-gray-400">—</span>
  const share = mb / job.gpu_memory_mb
  const tone = share >= 1 ? 'text-red-600 font-medium'
    : share >= 0.9 ? 'text-amber-600' : 'text-gray-700'
  return (
    <span className={tone} title={`${mb} MB of the ${job.gpu_memory_mb} MB reserved`}>
      {(mb / 1024).toFixed(1)}G
      <span className="text-gray-400"> · {Math.round(share * 100)}%</span>
    </span>
  )
}

/**
 * One of the administrator's two job tables. The same list with a different
 * half of the statuses in it, so it is one component asked twice: `mode` is
 * 'active' or 'finished', and only the active one can cancel.
 */
function AdminJobTable({ mode, title, hint, perPage, refreshInterval, onCancel,
                        busy, onData }) {
  const active = mode === 'active'
  const [data, setData] = React.useState(null)
  const [error, setError] = React.useState(null)
  const [page, setPage] = React.useState(1)
  const [status, setStatus] = React.useState('')
  const [nameInput, setNameInput] = React.useState('')
  const [search, setSearch] = React.useState('')
  const [sort, setSort] = React.useState(
    active ? { field: 'id', order: 'desc' } : { field: 'finished', order: 'desc' })
  const [turning, setTurning] = React.useState(false)

  // One request per keystroke would be one per letter of a username.
  React.useEffect(() => {
    const id = setTimeout(() => setSearch(nameInput.trim()), 300)
    return () => clearTimeout(id)
  }, [nameInput])

  React.useEffect(() => { setPage(1) }, [status, search, sort.field, sort.order])

  const load = React.useCallback(async (wanted) => {
    try {
      const res = await getAllJobs({
        page: wanted,
        perPage,
        capacity: active,          // the second table does not need it twice
        activeOnly: active,
        finishedOnly: !active,
        status,
        q: search,
        sort,
      })
      setData(res.data)
      onData?.(res.data)
      const landed = res.data?.pagination?.page
      if (landed && landed !== wanted) setPage(landed)
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) setError('Could not load the queue.')
    }
  }, [active, perPage, status, search, sort, onData])

  React.useEffect(() => {
    let cancelled = false
    setTurning(true)
    load(page).finally(() => { if (!cancelled) setTurning(false) })
    const id = setInterval(() => load(page), refreshInterval)
    return () => { cancelled = true; clearInterval(id) }
  }, [load, page, refreshInterval])

  const onSort = (field) => setSort((prev) => (
    prev.field === field
      ? { field, order: prev.order === 'asc' ? 'desc' : 'asc' }
      : { field, order: ['user', 'name', 'status'].includes(field) ? 'asc' : 'desc' }
  ))

  if (error) return <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">{error}</div>
  if (!data) return <div className="text-sm text-gray-400">Loading…</div>

  const jobs = data.jobs ?? []
  const meta = data.pagination ?? { page: 1, pages: 1, total: jobs.length, per_page: perPage }
  const filtered = Boolean(status || search)

  return (
    <Card
      title={title}
      hint={hint}
      right={
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-[11px] text-gray-500">
            {meta.total}{filtered ? ' matching' : ''}
          </span>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="text-xs border border-gray-300 rounded-lg pl-2 pr-6 py-1 text-gray-700 bg-white"
          >
            {JOB_STATUS_FILTERS[active ? 'active' : 'finished'].map(([value, label]) => (
              <option key={value || 'all'} value={value}>{label}</option>
            ))}
          </select>
          <input
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
            placeholder="Search user, name or script"
            className="text-xs border border-gray-300 rounded-lg px-2.5 py-1 text-gray-700 w-44 sm:w-56"
          />
          {filtered && (
            <button
              type="button"
              onClick={() => { setStatus(''); setNameInput(''); setSearch('') }}
              className="text-xs font-medium text-indigo-600 hover:text-indigo-800"
            >
              Clear
            </button>
          )}
        </div>
      }
    >
      <div className="p-5">
        {jobs.length === 0 ? (
          <p className="text-sm text-gray-400">
            {filtered
              ? 'No job matches that.'
              : active ? 'Nothing is running or waiting.' : 'Nothing has finished yet.'}
          </p>
        ) : (
          <div className={`overflow-x-auto -mx-5 px-5 transition-opacity ${turning ? 'opacity-50' : ''}`}>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                  <SortTh field="id" sort={sort} onSort={onSort}>ID</SortTh>
                  <SortTh field="user" sort={sort} onSort={onSort}>User</SortTh>
                  <SortTh field="status" sort={sort} onSort={onSort}>Status</SortTh>
                  <SortTh field="name" sort={sort} onSort={onSort}>Name</SortTh>
                  <th className="py-2 pr-4 font-semibold">Asked for</th>
                  <th className="py-2 pr-4 font-semibold">VRAM used</th>
                  <th className="py-2 pr-4 font-semibold">Where</th>
                  <SortTh field="runtime" sort={sort} onSort={onSort}>Runtime</SortTh>
                  <SortTh field="created" sort={sort} onSort={onSort}>Submitted</SortTh>
                  {!active && <SortTh field="finished" sort={sort} onSort={onSort}>Finished</SortTh>}
                  <th className="py-2 pr-4" />
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {jobs.map((j) => (
                  <React.Fragment key={j.id}>
                  <tr className="hover:bg-gray-50">
                    <td className="py-2 pr-4 font-mono text-xs text-gray-500">{j.id}</td>
                    <td className="py-2 pr-4 font-medium text-gray-900">{j.user}</td>
                    <td className="py-2 pr-4">
                      <span className={`text-[11px] px-2 py-0.5 rounded-full ${JOB_TONE[j.status] ?? ''}`}>
                        {j.status}{j.queue_position ? ` #${j.queue_position}` : ''}
                      </span>
                    </td>
                    <td className="py-2 pr-4 text-xs text-gray-700 truncate max-w-[10rem]">
                      {j.name || <span className="text-gray-400">—</span>}
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs">
                      {j.gpu_count ? `${j.gpu_count} × ${j.gpu_memory_mb} MB` : 'CPU only'}
                    </td>
                    <td className="py-2 pr-4 font-mono text-xs"><VramCell job={j} live={active} /></td>
                    <td className="py-2 pr-4 font-mono text-xs">{j.gpus?.length ? j.gpus.join(',') : '—'}</td>
                    <td className="py-2 pr-4 font-mono text-xs text-gray-600">
                      {duration(j.runtime_seconds)}
                    </td>
                    <td className="py-2 pr-4"><TimeCell iso={j.created_at} /></td>
                    {!active && <td className="py-2 pr-4"><TimeCell iso={j.finished_at} /></td>}
                    <td className="py-2 pr-4 text-right">
                      {active && (
                        <button onClick={() => onCancel(j)} disabled={busy}
                                className="text-xs font-medium text-red-600 hover:text-red-800 disabled:text-red-300">
                          Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                  {/* Why it ended, on its own line because it is a sentence and
                      not a field. Without it a failed job is a red word with no
                      cause, and the administrator has to open each one to find
                      out whether it ran out of memory, overran its reservation
                      or simply exited non-zero. */}
                  {j.message && (
                    <tr className="border-0">
                      <td />
                      <td colSpan={active ? 9 : 10}
                          className="pb-2 pr-4 text-xs text-gray-500 align-top">
                        <span className={JOB_MESSAGE_TONE[j.status] ?? ''}>{j.message}</span>
                      </td>
                    </tr>
                  )}
                  </React.Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <Pager meta={meta} onPage={setPage} busy={turning} shown={jobs.length} />
    </Card>
  )
}

export function JobsTab() {
  const [capacity, setCapacity] = React.useState(null)
  const [busy, setBusy] = React.useState(false)
  const [confirmDialog, confirm] = useConfirm()
  // Bumped after a cancel so both tables refetch: the job leaves one and
  // appears in the other.
  const [generation, setGeneration] = React.useState(0)

  const cancel = async (job) => {
    const running = job.status !== 'queued'
    const ok = await confirm({
      title: `Cancel job ${job.id}?`,
      body: `It belongs to ${job.user}${job.name ? ` and is called ${job.name}` : ''}.`,
      detail: [
        running
          ? 'It stops now, wherever it has got to, and its output ends there.'
          : 'It leaves the queue and will not start.',
        'They see that an administrator cancelled it, and it is written to the audit log.',
      ],
      confirmLabel: 'Cancel the job',
      cancelLabel: running ? 'Leave it running' : 'Leave it queued',
    })
    if (!ok) return
    setBusy(true)
    try {
      await adminCancelJobs([job.id])
      setGeneration((n) => n + 1)
    } finally {
      setBusy(false)
    }
  }

  const counts = capacity?.counts ?? {}
  const running = (counts.running ?? 0) + (counts.starting ?? 0)
  const fairShare = capacity?.fair_share ?? {}
  const maxScore = Math.max(...Object.values(fairShare).map((v) => v.score), 1)

  return (
    <div className="space-y-6">
      {/* The two summaries sit side by side: each is a handful of lines, and
          stacked they pushed the queue itself off the screen. */}
      <div className="grid gap-4 lg:grid-cols-2 items-start">
        <Card title="What the machine is doing">
          <div className="p-5 grid grid-cols-2 gap-3">
            <Stat label="Running" value={running} />
            <Stat label="Waiting" value={counts.queued ?? 0} />
            {(counts.paused ?? 0) > 0 && (
              <Stat label="Paused (out of budget)" value={counts.paused} />
            )}
            {capacity?.cpu && (
              <Stat label="CPU free" value={`${capacity.cpu.free_cores} cores`}
                    sub={`of ${capacity.cpu.total_cores} available to jobs`} />
            )}
            {(capacity?.gpus ?? []).map((g) => (
              <Stat key={g.index} label={`GPU ${g.index} free`}
                    value={`${(g.free_mb / 1024).toFixed(1)} GB`}
                    sub={`${g.running_jobs} job(s) · ${(g.reserved_mb / 1024).toFixed(1)} GB reserved`} />
            ))}
          </div>
        </Card>

        <Card title="Queue order, lowest score served first">
          <div className="p-5">
            <p className="text-[11px] text-gray-500 mb-3">
              Order is by how much each user has been given, not by who submitted
              first. A score is recent usage (which fades over hours) plus what
              they are running right now.
            </p>
            {Object.keys(fairShare).length === 0 ? (
              <p className="text-sm text-gray-400">Nobody has used the queue recently.</p>
            ) : (
              <div className="space-y-1.5">
                {Object.entries(fairShare)
                  .sort((a, b) => a[1].score - b[1].score)
                  .map(([name, s]) => (
                    <div key={name} className="flex items-center space-x-3">
                      <span className="w-24 truncate text-sm text-gray-800">{name}</span>
                      <div className="flex-1 h-4 bg-gray-100 rounded overflow-hidden">
                        <div className="h-full bg-indigo-400 rounded"
                             style={{ width: `${(s.score / maxScore) * 100}%` }} />
                      </div>
                      <span className="w-40 text-right text-[11px] font-mono text-gray-500">
                        {s.score} ({s.recent_usage} + {s.holding_now})
                      </span>
                    </div>
                  ))}
              </div>
            )}
          </div>
        </Card>
      </div>

      <AdminJobTable
        key={`active-${generation}`}
        mode="active"
        title="Running and waiting"
        hint="Everything on the machine right now, whoever owns it"
        perPage={15}
        refreshInterval={5000}
        onCancel={cancel}
        busy={busy}
        onData={setCapacity}
      />

      <AdminJobTable
        key={`finished-${generation}`}
        mode="finished"
        title="Finished jobs"
        hint="Every user's history, newest first"
        perPage={15}
        refreshInterval={20000}
      />

      {confirmDialog}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Audit tab
// ---------------------------------------------------------------------------

const ACTION_TONE = {
  'auth.login_failed': 'bg-red-100 text-red-700',
  'auth.login_denied': 'bg-red-100 text-red-700',
  'user.delete':       'bg-red-100 text-red-700',
  'session.force_stop': 'bg-amber-100 text-amber-700',
  'session.reaped':    'bg-amber-100 text-amber-700',
}

export function AuditTab() {
  const { data, error, reload } = usePolled(() => getAuditLog(200), 15000)

  if (error) return <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-4 py-3">{error}</div>
  if (!data) return <div className="text-sm text-gray-400">Loading audit trail…</div>

  return (
    <Card
      title="Audit trail"
      right={
        <button onClick={reload} className="text-xs font-medium text-indigo-600 hover:text-indigo-800">
          Refresh
        </button>
      }
    >
      {data.length === 0 ? (
        <p className="text-sm text-gray-400">Nothing recorded yet.</p>
      ) : (
        <div className="overflow-x-auto -mx-5 px-5">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
                <th className="py-2 pr-4 font-semibold">When</th>
                <th className="py-2 pr-4 font-semibold">Actor</th>
                <th className="py-2 pr-4 font-semibold">Action</th>
                <th className="py-2 pr-4 font-semibold">Target</th>
                <th className="py-2 pr-4 font-semibold">Detail</th>
                <th className="py-2 pr-4 font-semibold">IP</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {data.map((row, i) => (
                <tr key={i}>
                  <td className="py-2 pr-4 text-xs text-gray-500 whitespace-nowrap">
                    {new Date(row.created_at + 'Z').toLocaleString()}
                  </td>
                  <td className="py-2 pr-4 font-medium text-gray-800">{row.actor}</td>
                  <td className="py-2 pr-4">
                    <span className={`text-[11px] font-mono px-2 py-0.5 rounded ${ACTION_TONE[row.action] ?? 'bg-gray-100 text-gray-600'}`}>
                      {row.action}
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-gray-700">{row.target ?? '—'}</td>
                  <td className="py-2 pr-4 text-xs text-gray-500 max-w-md truncate">{row.detail ?? '—'}</td>
                  <td className="py-2 pr-4 font-mono text-[11px] text-gray-400">{row.ip_address ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Trash tab
//
// Deleting a user does not remove anything: the account is kept so its name
// stays reserved and its history stays attached to it, and the workspace is
// renamed aside.  This is where both are dealt with: restored, or finally
// removed.  The second table is for directories left behind by the older
// delete, which removed the account row and left the files behind under the
// original name, invisible and owned by nobody.
// ---------------------------------------------------------------------------

const fmtWhen = (value) =>
  value ? new Date(value.endsWith?.('Z') ? value : `${value}Z`).toLocaleString() : '—'

export function TrashTab({ showToast }) {
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(null)
  const [confirm, setConfirm] = useState(null)

  const load = useCallback(async () => {
    try {
      const res = await getTrash()
      setData(res.data)
    } catch (err) {
      showToast?.(err.response?.data?.detail || 'Could not load the trash.', 'error')
    }
  }, [showToast])

  useEffect(() => { load() }, [load])

  const run = async (key, action, fallback) => {
    setBusy(key)
    try {
      const res = await action()
      showToast?.(res.data?.message || 'Done.')
      setConfirm(null)
      await load()
    } catch (err) {
      showToast?.(err.response?.data?.detail || fallback, 'error')
    } finally {
      setBusy(null)
    }
  }

  if (!data) {
    return <div className="p-8 text-center text-gray-400 text-sm">Loading…</div>
  }

  const { users, orphans } = data
  const reclaimable =
    users.reduce((sum, u) => sum + (u.size_mb || 0), 0) +
    orphans.reduce((sum, o) => sum + (o.size_mb || 0), 0)

  return (
    <div className="space-y-4">
      <Card
        title={`Deleted users (${users.length})`}
        right={
          reclaimable > 0 && (
            <span className="text-xs text-gray-500">{fmtMb(reclaimable)} held in the trash</span>
          )
        }
      >
        <p className="text-xs text-gray-500 mb-4">
          These accounts are stopped and cannot sign in, but their files and history are kept
          and their usernames stay reserved. Restoring one brings its files back; deleting one
          permanently cannot be undone.
        </p>
        {users.length === 0 ? (
          <p className="text-sm text-gray-400">The trash is empty.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-gray-500 border-b border-gray-200">
                <th className="py-2 font-semibold">User</th>
                <th className="py-2 font-semibold">Deleted</th>
                <th className="py-2 font-semibold">Files</th>
                <th className="py-2 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {users.map((u) => (
                <tr key={u.id}>
                  <td className="py-3">
                    <p className="font-semibold text-gray-900">{u.username}</p>
                    <p className="text-xs text-gray-400">{u.full_name || u.email}</p>
                  </td>
                  <td className="py-3 text-gray-600 text-xs">{fmtWhen(u.deleted_at)}</td>
                  <td className="py-3 text-xs">
                    {u.home_path ? (
                      <span className="text-gray-600">
                        Home directory <span className="font-mono">{u.home_path}</span>, kept as it is
                      </span>
                    ) : u.archive_exists ? (
                      <span className="font-mono text-gray-600">
                        {u.archived_workspace} <span className="text-gray-400">({fmtMb(u.size_mb)})</span>
                      </span>
                    ) : (
                      <span className="text-gray-400">no files kept</span>
                    )}
                  </td>
                  <td className="py-3 text-right space-x-1">
                    <button
                      onClick={() => run(`restore-${u.id}`, () => restoreUser(u.id), 'Could not restore.')}
                      disabled={busy !== null}
                      className="text-xs font-medium text-emerald-700 hover:text-emerald-900 px-2 py-1 rounded hover:bg-emerald-50 disabled:opacity-50"
                    >
                      {busy === `restore-${u.id}` ? 'Restoring…' : 'Restore'}
                    </button>
                    <button
                      onClick={() => setConfirm({ kind: 'user', user: u })}
                      disabled={busy !== null}
                      className="text-xs font-medium text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-50"
                    >
                      Delete permanently
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {orphans.length > 0 && (
        <Card title={`Workspace directories with no account (${orphans.length})`}>
          <p className="text-xs text-gray-500 mb-4">
            Files belonging to accounts that no longer exist. Nothing on the platform can reach
            them, and they keep taking up space until they are removed here.
          </p>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-gray-500 border-b border-gray-200">
                <th className="py-2 font-semibold">Directory</th>
                <th className="py-2 font-semibold">Was</th>
                <th className="py-2 font-semibold">Size</th>
                <th className="py-2 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {orphans.map((o) => (
                <tr key={o.name}>
                  <td className="py-3 font-mono text-xs text-gray-700">{o.name}</td>
                  <td className="py-3 text-xs text-gray-500">
                    {o.username}
                    {o.deleted_at && <> · deleted {fmtWhen(o.deleted_at)}</>}
                  </td>
                  <td className="py-3 text-xs text-gray-600">{fmtMb(o.size_mb)}</td>
                  <td className="py-3 text-right">
                    <button
                      onClick={() => setConfirm({ kind: 'directory', directory: o })}
                      disabled={busy !== null}
                      className="text-xs font-medium text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50 disabled:opacity-50"
                    >
                      Delete permanently
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {confirm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-xl max-w-md w-full p-6">
            <h3 className="text-base font-semibold text-gray-900 mb-2">Delete permanently</h3>
            {confirm.kind === 'user' ? (
              <p className="text-sm text-gray-600">
                Delete <strong>{confirm.user.username}</strong> for good?
                {confirm.user.archive_exists && (
                  <> Their files ({fmtMb(confirm.user.size_mb)}) will be deleted with them.</>
                )}
                {confirm.user.home_path && (
                  <> Their home directory <span className="font-mono">{confirm.user.home_path}</span> will
                  be left exactly as it is.</>
                )}
                {' '}Their usage history goes too, and the username becomes available again.
                This cannot be undone.
              </p>
            ) : (
              <p className="text-sm text-gray-600">
                Delete <span className="font-mono">{confirm.directory.name}</span> and everything in it
                ({fmtMb(confirm.directory.size_mb)})? This cannot be undone.
              </p>
            )}
            <div className="flex justify-end space-x-2 mt-5">
              <button
                onClick={() => setConfirm(null)}
                className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg"
              >
                Cancel
              </button>
              <button
                onClick={() =>
                  confirm.kind === 'user'
                    ? run(`purge-${confirm.user.id}`, () => purgeUser(confirm.user.id), 'Could not delete.')
                    : run(`purge-${confirm.directory.name}`, () => purgeDirectory(confirm.directory.name), 'Could not delete.')
                }
                disabled={busy !== null}
                className="px-4 py-2 text-sm font-semibold text-white bg-red-600 hover:bg-red-700 disabled:bg-red-400 rounded-lg"
              >
                {busy ? 'Deleting…' : 'Delete permanently'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

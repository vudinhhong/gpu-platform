import React, { useState, useEffect, useCallback } from 'react'
import { getRunningJobs, getFinishedJobs, getJob, cancelJobs, getMyUsage } from '../api/index.js'
import { useConfirm } from './ConfirmDialog.jsx'
import Pager from './Pager.jsx'
import { TimeCell, duration } from './format.jsx'

const STATUS_STYLE = {
  queued:    { chip: 'bg-amber-100 text-amber-700',   label: 'Waiting' },
  starting:  { chip: 'bg-sky-100 text-sky-700',       label: 'Starting' },
  running:   { chip: 'bg-green-100 text-green-700',   label: 'Running' },
  paused:    { chip: 'bg-violet-100 text-violet-700', label: 'Paused' },
  succeeded: { chip: 'bg-emerald-100 text-emerald-700', label: 'Finished' },
  failed:    { chip: 'bg-red-100 text-red-700',       label: 'Failed' },
  cancelled: { chip: 'bg-gray-100 text-gray-500',     label: 'Cancelled' },
  timeout:   { chip: 'bg-red-100 text-red-700',       label: 'Timed out' },
}

const ACTIVE = new Set(['queued', 'starting', 'running', 'paused'])
// A paused job is on the machine but nothing is happening in it, so it gets
// no pulsing dot beside its chip.
const LIVE = new Set(['queued', 'starting', 'running'])

// What the history can be narrowed to, in the order the dropdown lists them.
const FINISHED_STATUSES = [
  ['', 'All statuses'],
  ['succeeded', 'Finished'],
  ['failed', 'Failed'],
  ['timeout', 'Timed out'],
  ['cancelled', 'Cancelled'],
]

// Warn well before the job is actually stopped.  A warning that arrives with
// the stop is no use to anyone; its owner needs time to react.
const WARN_AT = 0.8
const CRITICAL_AT = 0.95

function humanBytes(n) {
  if (n == null) return null
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`
  if (n >= 1024) return `${Math.round(n / 1024)} KB`
  return `${n} B`
}

function gb(mb) {
  if (mb == null) return null
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`
}

function StatusChip({ status }) {
  const s = STATUS_STYLE[status] ?? STATUS_STYLE.queued
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-semibold ${s.chip}`}>
      {LIVE.has(status) && (
        <span className="w-1.5 h-1.5 rounded-full bg-current mr-1.5 animate-pulse" />
      )}
      {s.label}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Risk
//
// A running job can hit two limits, the VRAM it reserved and the runtime it
// asked for, and both end the same way: the platform stops it.  Neither used
// to show up until afterwards, and the explanation sat inside the output
// panel, which nobody opens until something has already gone wrong.
// ---------------------------------------------------------------------------

function jobRisks(job) {
  const out = []

  // Measured against what the job ASKED FOR, not against the allowance.  The
  // allowance carries a small grace for the CUDA context, which is the
  // platform's business.  The figure the user chose is the one they think in
  // and the one they would change, so that is the one on the bar.
  const used = job.gpu_memory_used_mb
  const asked = job.gpu_memory_mb
  const allowance = job.gpu_memory_allowance_mb
  if (used != null && asked) {
    const ratio = used / asked
    if (ratio >= WARN_AT) {
      const over = ratio >= 1
      out.push({
        kind: 'vram',
        ratio,
        level: over ? 'critical' : 'warn',
        short: `VRAM ${Math.round(ratio * 100)}%`,
        detail: over
          ? `is holding ${gb(used)} against the ${gb(asked)} it asked for`
            + (job.gpu_memory_enforced
              ? `, and is stopped past ${gb(allowance)}. Cancel it and resubmit `
                + 'with a higher --gpu-memory.'
              : '. The scheduler fitted other work beside it on that figure, so '
                + 'resubmit with a higher --gpu-memory.')
          : `is holding ${gb(used)} of the ${gb(asked)} GPU memory it asked for.`,
      })
    }
  }

  const limitSeconds = (job.max_runtime_minutes || 0) * 60
  if (limitSeconds && job.runtime_seconds != null) {
    const ratio = job.runtime_seconds / limitSeconds
    if (ratio >= WARN_AT) {
      const left = Math.max(0, limitSeconds - job.runtime_seconds)
      out.push({
        kind: 'time',
        ratio,
        level: ratio >= CRITICAL_AT ? 'critical' : 'warn',
        short: `Time ${Math.round(ratio * 100)}%`,
        detail: `has ${duration(left)} left of its ${job.max_runtime_minutes} minute limit, `
          + 'after which it is stopped.',
      })
    }
  }

  return out
}

function RiskChip({ risk }) {
  const style = risk.level === 'critical'
    ? 'bg-red-100 text-red-700'
    : 'bg-amber-100 text-amber-700'
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold ${style}`}>
      {risk.short}
    </span>
  )
}

/** A thin bar for "how much of the budget is gone".
 *
 * `criticalAt` is where red starts.  For memory that is 1.0: past what you
 * asked for is past what you asked for, whatever grace the platform adds on
 * top. For a runtime limit it is just short of 1.0, because reaching that one
 * really does end the job.
 */
function Meter({ value, limit, label, criticalAt = 1 }) {
  if (value == null || !limit) return <span className="text-gray-400">—</span>
  const ratio = value / limit
  const colour = ratio >= criticalAt ? 'bg-red-500'
    : ratio >= WARN_AT ? 'bg-amber-500'
      : 'bg-emerald-500'
  return (
    <div className="min-w-[5.5rem]">
      <div className="text-[11px] font-mono text-gray-600 leading-none mb-1">{label}</div>
      <div className="h-1 rounded-full bg-gray-200 overflow-hidden">
        <div className={`h-full rounded-full ${colour}`}
             style={{ width: `${Math.min(1, ratio) * 100}%` }} />
      </div>
    </div>
  )
}

/** The banner above the table: what is at risk, and what to do about it. */
function RiskBanner({ jobs }) {
  const flagged = jobs
    .map((job) => ({ job, risks: jobRisks(job) }))
    .filter((entry) => entry.risks.length > 0)

  if (flagged.length === 0) return null

  const critical = flagged.some((e) => e.risks.some((r) => r.level === 'critical'))
  const box = critical
    ? 'bg-red-50 border-red-200 text-red-800'
    : 'bg-amber-50 border-amber-200 text-amber-800'

  return (
    <div className={`px-5 py-3 border-b ${box}`}>
      <div className="flex items-start space-x-2">
        <svg className="w-4 h-4 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24"
             stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round"
            d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
        </svg>
        <div className="space-y-1">
          {flagged.map(({ job, risks }) => (
            <p key={job.id} className="text-xs leading-snug">
              <span className="font-semibold">
                Job {job.id}{job.name ? ` (${job.name})` : ''}
              </span>{' '}
              {risks.map((r) => r.detail).join(' It also ')}
            </p>
          ))}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// One job, expandable to its recent output
// ---------------------------------------------------------------------------

function JobRow({ job, onCancel, busy, live, columns }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState(null)

  // Only poll the output of the row the user actually opened, and only while
  // the job is still producing any.
  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    const load = () => getJob(job.id).then(
      (res) => { if (!cancelled) setDetail(res.data) },
      () => {},
    )
    load()
    if (!ACTIVE.has(job.status)) return () => { cancelled = true }
    const id = setInterval(load, 4000)
    return () => { cancelled = true; clearInterval(id) }
  }, [open, job.id, job.status])

  const risks = live ? jobRisks(job) : []
  const limitSeconds = (job.max_runtime_minutes || 0) * 60

  return (
    <>
      <tr className="hover:bg-gray-50">
        <td className="py-2 pr-3 font-mono text-xs text-gray-500 align-top">{job.id}</td>
        <td className="py-2 pr-3 align-top">
          <div className="flex flex-col items-start space-y-1">
            <StatusChip status={job.status} />
            {risks.map((r) => <RiskChip key={r.kind} risk={r} />)}
          </div>
        </td>
        <td className="py-2 pr-3 align-top">
          <div className="font-medium text-gray-900 truncate max-w-[12rem]">
            {job.name || job.script}
          </div>
          {/* The platform's own note about this job, in the open rather than
              folded away behind the output panel, since it is usually the reason
              somebody is looking at this row at all. */}
          {job.message && (
            <p className="text-[11px] text-amber-700 mt-0.5 max-w-[16rem] leading-snug">
              {job.message}
            </p>
          )}
        </td>
        <td className="py-2 pr-3 font-mono text-xs align-top">
          {job.gpus?.length
            ? `GPU ${job.gpus.join(', ')}`
            : job.queue_position
              ? <span className="text-amber-600">#{job.queue_position} in queue</span>
              : <span className="text-gray-400">—</span>}
        </td>
        <td className="py-2 pr-3 align-top">
          {!job.gpu_memory_mb ? (
            <span className="text-xs text-gray-400">no GPU</span>
          ) : live ? (
            <Meter
              value={job.gpu_memory_used_mb}
              limit={job.gpu_memory_mb}
              label={job.gpu_memory_used_mb == null
                ? `— / ${gb(job.gpu_memory_mb)}`
                : `${gb(job.gpu_memory_used_mb)} / ${gb(job.gpu_memory_mb)}`}
            />
          ) : (
            /* Finished: the live figure is whatever the last reading caught,
               often zero because the job released its memory on the way out.
               The peak is the one that still answers "what did this need?". */
            <span className="font-mono text-xs text-gray-700">
              {job.gpu_memory_peak_mb == null
                ? <span className="text-gray-400">— / {gb(job.gpu_memory_mb)}</span>
                : <>{gb(job.gpu_memory_peak_mb)} / {gb(job.gpu_memory_mb)}</>}
              {job.gpu_memory_peak_mb != null && (
                <span className="block text-[10px] text-gray-400">peak</span>
              )}
            </span>
          )}
        </td>
        <td className="py-2 pr-3 align-top">
          {live && limitSeconds ? (
            <Meter
              value={job.runtime_seconds}
              limit={limitSeconds}
              criticalAt={CRITICAL_AT}
              label={`${duration(job.runtime_seconds)} / ${job.max_runtime_minutes}m`}
            />
          ) : (
            <span className="font-mono text-xs text-gray-600">
              {duration(job.runtime_seconds)}
            </span>
          )}
        </td>
        <td className="py-2 pr-3 align-top"><TimeCell iso={job.created_at} /></td>
        {!live && (
          <td className="py-2 pr-3 align-top"><TimeCell iso={job.finished_at} /></td>
        )}
        <td className="py-2 pr-3 text-right align-top whitespace-nowrap">
          <button onClick={() => setOpen(!open)}
                  className="text-xs font-medium text-indigo-600 hover:text-indigo-800">
            {open ? 'Hide' : 'Output'}
          </button>
          {ACTIVE.has(job.status) && (
            <button onClick={() => onCancel(job.id)} disabled={busy}
                    className="ml-3 text-xs font-medium text-red-600 hover:text-red-800 disabled:text-red-300">
              Cancel
            </button>
          )}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={columns} className="pb-3">
            <div className="bg-gray-900 rounded-lg p-3 space-y-2">
              <div className="flex items-center justify-between text-[11px] text-gray-400 font-mono">
                <span>{job.output || job.script}</span>
                {job.exit_code != null && <span>exit {job.exit_code}</span>}
              </div>
              {/* Output can be gigabytes; only the end is ever fetched. Say so,
                  so nobody mistakes this for the whole thing. */}
              {detail?.output_truncated && (
                <p className="text-[11px] text-gray-400">
                  Showing the last {detail.output_tail_lines ?? 500} lines
                  {detail.output_size_bytes
                    ? ` of ${humanBytes(detail.output_size_bytes)}`
                    : ''}
                  . For all of it, run{' '}
                  <code className="text-gray-300">tail -f {job.output}</code>{' '}
                  in a terminal.
                </p>
              )}
              <pre className="text-[11px] text-gray-200 font-mono whitespace-pre-wrap max-h-64 overflow-y-auto">
                {detail?.output_tail?.trim() || 'No output yet.'}
              </pre>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// Shared chrome
// ---------------------------------------------------------------------------

function Card({ title, hint, right, children }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      {/* Centred on the title unless there is a hint under it, which makes the
          left side two lines tall and the top the only sensible alignment. */}
      <div className={`px-5 py-3 border-b border-gray-100 flex flex-wrap gap-x-4 gap-y-2 justify-between ${
        hint ? 'items-start' : 'items-center'
      }`}>
        <div>
          <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
          {hint && <p className="text-[11px] text-gray-400 mt-0.5">{hint}</p>}
        </div>
        {right && <div className="flex-shrink-0">{right}</div>}
      </div>
      {children}
    </div>
  )
}

function Skeleton() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 animate-pulse space-y-3">
      <div className="h-3 bg-gray-200 rounded w-1/4" />
      <div className="h-2 bg-gray-100 rounded" />
      <div className="h-2 bg-gray-100 rounded w-2/3" />
    </div>
  )
}

function ErrorCard({ children }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 text-sm text-red-600">
      {children}
    </div>
  )
}

// A sortable column header.  `sort` is undefined on the running list, which
// is short and ordered by the scheduler rather than by the reader.
function Th({ children, field, sort, onSort, className = '' }) {
  const active = sort && sort.field === field
  if (!sort || !field) {
    return <th className={`py-2 pr-3 font-semibold ${className}`}>{children}</th>
  }
  return (
    <th className={`py-2 pr-3 font-semibold ${className}`}>
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

function TableHead({ live, sort, onSort }) {
  const col = { sort, onSort }
  return (
    <thead>
      <tr className="text-left text-[11px] uppercase tracking-wider text-gray-400 border-b border-gray-100">
        <Th field="id" {...col}>ID</Th>
        <Th field="status" {...col}>Status</Th>
        <Th field="name" {...col}>Name</Th>
        <th className="py-2 pr-3 font-semibold">Placed on</th>
        <th className="py-2 pr-3 font-semibold">GPU memory</th>
        <Th field="runtime" {...col}>Runtime</Th>
        <Th field="created" {...col}>Submitted</Th>
        {!live && <Th field="finished" {...col}>Finished</Th>}
        <th className="py-2 pr-3" />
      </tr>
    </thead>
  )
}

// ---------------------------------------------------------------------------
// Running jobs: everything in flight, never paged.
// ---------------------------------------------------------------------------

export function RunningJobsPanel({ refreshInterval = 4000 }) {
  const [data, setData]   = useState(null)
  const [usage, setUsage] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy]   = useState(false)
  const [confirmDialog, confirm] = useConfirm()

  const load = useCallback(async () => {
    try {
      const res = await getRunningJobs()
      setData(res.data)
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) setError('Could not load your jobs.')
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, refreshInterval)
    return () => clearInterval(id)
  }, [load, refreshInterval])

  // Consumption changes slowly; no reason to poll it at the job cadence.
  useEffect(() => {
    const fetchUsage = () => getMyUsage(30).then((res) => setUsage(res.data), () => {})
    fetchUsage()
    const id = setInterval(fetchUsage, 60000)
    return () => clearInterval(id)
  }, [])

  const cancel = async (id) => {
    const job = (data?.jobs ?? []).find((j) => j.id === id)
    const running = job && job.status !== 'queued'
    const ok = await confirm({
      title: `Cancel job ${id}?`,
      body: running
        ? 'It stops now, wherever it has got to, and its output ends there.'
        : 'It leaves the queue and will not start.',
      detail: [
        'A cancelled job cannot be resumed; you would submit it again from the beginning.',
        ...(running && job?.output ? [`Its output so far stays in ${job.output}.`] : []),
      ],
      confirmLabel: 'Cancel the job',
      cancelLabel: running ? 'Leave it running' : 'Leave it queued',
    })
    if (!ok) return

    setBusy(true)
    try {
      await cancelJobs([id])
      await load()
    } catch {
      setError('Could not cancel that job.')
    } finally {
      setBusy(false)
    }
  }

  if (error) return <ErrorCard>{error}</ErrorCard>
  if (!data) return <Skeleton />

  const jobs = data.jobs ?? []

  return (
    <Card
      title="Running jobs"
      hint={<>Submit from a terminal: <code className="font-mono">submit script.sh --gpu-memory 20000</code></>}
      right={
        <div className="text-right">
          <span className="text-[11px] text-gray-500">
            {jobs.length === 0 ? 'nothing running' : `${jobs.length} active`}
          </span>
          {usage && (
            <p className="text-[11px] text-gray-400 mt-0.5">
              Last 30 days: <span className="font-medium text-gray-600">{usage.gpu_hours}h GPU</span>
              {' · '}<span className="font-medium text-gray-600">{usage.cpu_hours}h CPU</span>
              {' · '}{Object.values(usage.jobs || {}).reduce((a, b) => a + b, 0)} jobs
            </p>
          )}
        </div>
      }
    >
      <RiskBanner jobs={jobs} />

      {/* What the queue is waiting on.  Capacity belongs beside the work in
          flight, not beside a list of things that already finished. */}
      {((data.gpus ?? []).length > 0 || data.cpu) && (
        <div className="px-5 py-3 bg-gray-50 border-b border-gray-100 flex flex-wrap gap-4">
          {(data.gpus ?? []).map((g) => (
            <div key={g.index} className="text-[11px]">
              <span className="font-semibold text-gray-700">GPU {g.index}</span>
              <span className="text-gray-500 ml-2">
                {(g.free_mb / 1024).toFixed(1)} GB free
                {g.running_jobs > 0 && ` · ${g.running_jobs} running`}
              </span>
            </div>
          ))}
          {data.cpu && (
            <div className="text-[11px]">
              <span className="font-semibold text-gray-700">CPU</span>
              <span className="text-gray-500 ml-2">
                {data.cpu.free_cores} of {data.cpu.total_cores} cores free
              </span>
            </div>
          )}
        </div>
      )}

      <div className="p-5">
        {jobs.length === 0 ? (
          <p className="text-sm text-gray-400">
            Nothing running. From a terminal in your workspace, run{' '}
            <code className="font-mono text-gray-600">submit your-script.sh</code>.
          </p>
        ) : (
          <div className="overflow-x-auto -mx-5 px-5">
            <table className="w-full text-sm">
              <TableHead live />
              <tbody className="divide-y divide-gray-50">
                {jobs.map((job) => (
                  <JobRow key={job.id} job={job} onCancel={cancel} busy={busy}
                          live columns={8} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {confirmDialog}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Finished jobs: the history, ten to a page.
// ---------------------------------------------------------------------------

export function FinishedJobsPanel({ refreshInterval = 15000, perPage = 10 }) {
  const [data, setData]   = useState(null)
  const [page, setPage]   = useState(1)
  const [status, setStatus] = useState('')
  // What is typed, and what has been asked for: a request per keystroke would
  // be one per letter of a job name.
  const [nameInput, setNameInput] = useState('')
  const [name, setName] = useState('')
  const [sort, setSort] = useState({ field: 'id', order: 'desc' })
  // Turning a page fetches it; the table stays on screen and dims rather than
  // collapsing to a skeleton, so the row heights do not jump under the cursor.
  const [turning, setTurning] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    const id = setTimeout(() => setName(nameInput.trim()), 300)
    return () => clearTimeout(id)
  }, [nameInput])

  // Any change to what is being asked for starts again at page one: page four
  // of the old filter means nothing under the new one.
  useEffect(() => { setPage(1) }, [status, name, sort.field, sort.order])

  const load = useCallback(async (wanted) => {
    try {
      const res = await getFinishedJobs(wanted, perPage, { status, q: name, sort })
      setData(res.data)
      // The server clamps a page past the end, so follow what it returned.
      const landed = res.data?.pagination?.page
      if (landed && landed !== wanted) setPage(landed)
      setError(null)
    } catch (err) {
      if (err.response?.status !== 401) setError('Could not load your job history.')
    }
  }, [perPage, status, name, sort])

  useEffect(() => {
    let cancelled = false
    setTurning(true)
    load(page).finally(() => { if (!cancelled) setTurning(false) })
    // Slower than the running list: this only changes when a job ends.
    const id = setInterval(() => load(page), refreshInterval)
    return () => { cancelled = true; clearInterval(id) }
  }, [load, page, refreshInterval])

  // Clicking the column you are already sorted by turns it around.
  const onSort = (field) => setSort((prev) => (
    prev.field === field
      ? { field, order: prev.order === 'asc' ? 'desc' : 'asc' }
      : { field, order: field === 'name' || field === 'status' ? 'asc' : 'desc' }
  ))

  if (error) return <ErrorCard>{error}</ErrorCard>
  if (!data) return <Skeleton />

  const jobs  = data.jobs ?? []
  const meta  = data.pagination ?? { page: 1, pages: 1, total: jobs.length, per_page: perPage }

  const filtered = Boolean(status || name)

  // The filters live in the card header rather than in a row of their own:
  // they are one line of chrome, and a row inside the box pushed the table
  // down by more than the filters were worth.
  return (
    <Card
      title="Finished jobs"
      right={
        <div className="flex flex-wrap items-center justify-end gap-2">
          {meta.total > 0 && (
            <span className="text-[11px] text-gray-500">
              {meta.total}{filtered ? ' matching' : ' in total'}
            </span>
          )}
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="text-xs border border-gray-300 rounded-lg pl-2 pr-6 py-1 text-gray-700 bg-white"
          >
            {FINISHED_STATUSES.map(([value, label]) => (
              <option key={value || 'all'} value={value}>{label}</option>
            ))}
          </select>
          <input
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
            placeholder="Search name or script"
            className="text-xs border border-gray-300 rounded-lg px-2.5 py-1 text-gray-700 w-40 sm:w-52"
          />
          {filtered && (
            <button
              type="button"
              onClick={() => { setStatus(''); setNameInput(''); setName('') }}
              className="text-xs font-medium text-indigo-600 hover:text-indigo-800"
            >
              Clear
            </button>
          )}
        </div>
      }
    >
      <div className="px-5">
        {jobs.length === 0 ? (
          <p className="text-sm text-gray-400">
            {filtered ? 'No job matches that.' : 'Nothing has finished yet.'}
          </p>
        ) : (
          <div className={`overflow-x-auto -mx-5 px-5 transition-opacity ${turning ? 'opacity-50' : ''}`}>
            <table className="w-full text-sm">
              <TableHead live={false} sort={sort} onSort={onSort} />
              <tbody className="divide-y divide-gray-50">
                {jobs.map((job) => (
                  <JobRow key={job.id} job={job} onCancel={() => {}} busy={false}
                          live={false} columns={9} />
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

import React, { useState, useEffect, useCallback } from 'react'
import { getNodes, createJoinToken, drainNode, resumeNode, removeNode } from '../api/index.js'
import { useConfirm } from './ConfirmDialog.jsx'

// Machines that have joined this one.  A node is added by running deploy.sh
// on it with a token from here; it then calls in by itself, so there is
// nothing to configure on this side (docs/multi-node.md).

const fmtGb = (mb) => (mb == null ? '—' : `${Math.round(mb / 1024)} GB`)

function stateBadge(node) {
  if (node.state === 'draining') {
    return <span className="text-xs font-medium text-amber-700 bg-amber-50 px-2 py-0.5 rounded">draining</span>
  }
  if (!node.ready) {
    return <span className="text-xs font-medium text-red-700 bg-red-50 px-2 py-0.5 rounded">not answering</span>
  }
  return <span className="text-xs font-medium text-green-700 bg-green-50 px-2 py-0.5 rounded">ready</span>
}

function AddNode({ onClose, showToast }) {
  const [site, setSite] = useState('default')
  const [nfs, setNfs] = useState('')
  const [token, setToken] = useState(null)
  const [busy, setBusy] = useState(false)

  const issue = async () => {
    setBusy(true)
    try {
      const res = await createJoinToken(site.trim() || 'default')
      setToken(res.data)
    } catch (err) {
      showToast(err.response?.data?.detail || 'Could not issue a token.', 'error')
    } finally {
      setBusy(false)
    }
  }

  const command = token
    ? `./deploy.sh --join ${window.location.origin} ${token.token}` + (nfs.trim() ? ` --nfs ${nfs.trim()}` : '')
    : ''

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl w-full max-w-xl p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-base font-semibold text-gray-900">Add a node</h3>
        {!token ? (
          <>
            <p className="text-sm text-gray-600">
              Install the platform on the other machine (same repository, Docker and the NVIDIA
              runtime), then run the command this produces there. The token works once, for an hour.
            </p>
            <label className="block text-sm">
              <span className="text-gray-700 font-medium">Site</span>
              <input className="form-input mt-1" value={site} onChange={(e) => setSite(e.target.value)} />
              <span className="text-[11px] text-gray-400">
                Machines on one LAN share a site and the users' homes. A machine elsewhere gets a site of
                its own; users then run only on the machines of their site.
              </span>
            </label>
            <label className="block text-sm">
              <span className="text-gray-700 font-medium">Homes over NFS (optional)</span>
              <input className="form-input mt-1" value={nfs} placeholder="master-host:/srv/gpu/jupyter_data"
                     onChange={(e) => setNfs(e.target.value)} />
              <span className="text-[11px] text-gray-400">
                The export of this machine's jupyter_data, for a node on the same LAN.
              </span>
            </label>
            <div className="flex justify-end space-x-2">
              <button onClick={onClose} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg">Cancel</button>
              <button onClick={issue} disabled={busy} className="px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-400 rounded-lg">
                {busy ? 'Issuing…' : 'Issue a token'}
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="text-sm text-gray-600">
              Run this in the platform directory on the new machine
              {nfs.trim() ? ' (as root, so it can mount the homes)' : ''}:
            </p>
            <pre className="bg-gray-900 text-gray-100 text-xs rounded-lg p-3 overflow-x-auto whitespace-pre-wrap break-all">{command}</pre>
            <p className="text-[11px] text-gray-400">
              Site {token.site}. Expires {new Date(token.expires_at + 'Z').toLocaleTimeString()}. The machine
              shows up below once it has joined.
            </p>
            <div className="flex justify-end space-x-2">
              <button onClick={() => navigator.clipboard?.writeText(command)} className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg">Copy</button>
              <button onClick={onClose} className="px-4 py-2 text-sm font-semibold text-white bg-indigo-600 hover:bg-indigo-700 rounded-lg">Done</button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export default function NodesTab({ showToast }) {
  const [nodes, setNodes] = useState(null)
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(null)
  const [confirmDialog, confirm] = useConfirm()

  const load = useCallback(async () => {
    try {
      const res = await getNodes()
      setNodes(res.data)
    } catch (err) {
      showToast(err.response?.data?.detail || 'Could not load nodes.', 'error')
    }
  }, [showToast])

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  const act = async (node, action, done) => {
    setBusy(node.id)
    try {
      await action(node.id)
      showToast(done)
      await load()
    } catch (err) {
      showToast(err.response?.data?.detail || 'That did not work.', 'error')
    } finally {
      setBusy(null)
    }
  }

  const remove = async (node) => {
    const ok = await confirm({
      title: `Remove ${node.name}?`,
      body: 'Its credential stops working and nothing new is placed on it.',
      detail: [
        'Drain it first and wait for its jobs to finish; a node with jobs running is not removed.',
        'To bring the machine back later, issue a new token and run deploy.sh --join on it again.',
      ],
      confirmLabel: 'Remove it',
    })
    if (ok) act(node, removeNode, `${node.name} removed.`)
  }

  if (!nodes) return <div className="p-8 text-center text-gray-400 text-sm">Loading…</div>

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-gray-900">
          Nodes <span className="text-gray-400 font-normal text-sm">machines whose GPUs this platform schedules</span>
        </h2>
        <button onClick={() => setAdding(true)} className="bg-indigo-600 hover:bg-indigo-700 text-white px-3.5 py-2 rounded-lg text-sm font-medium shadow-sm">
          Add a node
        </button>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 border-b border-gray-200 text-left text-xs uppercase tracking-wider text-gray-500">
              <th className="px-5 py-3 font-semibold">Machine</th>
              <th className="px-5 py-3 font-semibold">Site</th>
              <th className="px-5 py-3 font-semibold">State</th>
              <th className="px-5 py-3 font-semibold">GPUs</th>
              <th className="px-5 py-3 font-semibold">CPU / RAM</th>
              <th className="px-5 py-3 font-semibold text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {nodes.map((n) => {
              const gpus = n.gpus || n.inventory?.gpus || []
              return (
                <tr key={n.id}>
                  <td className="px-5 py-3.5">
                    <div className="font-semibold text-gray-900">{n.name}</div>
                    <div className="text-[11px] text-gray-400">
                      {n.is_local ? 'master' : n.data_url || 'no data port'}
                      {n.heartbeat_age_s != null && !n.is_local ? ` · seen ${n.heartbeat_age_s}s ago` : ''}
                    </div>
                  </td>
                  <td className="px-5 py-3.5 text-gray-600">{n.site}</td>
                  <td className="px-5 py-3.5">{stateBadge(n)}</td>
                  <td className="px-5 py-3.5 text-xs text-gray-600">
                    {n.is_local ? 'see Resources' : gpus.length === 0 ? 'none' : gpus.map((g) => (
                      <div key={g.index}>
                        <span className="font-mono">GPU {g.index}</span> {g.name}
                        {g.used_memory_mb != null ? ` · ${fmtGb(g.used_memory_mb)} of ${fmtGb(g.total_memory_mb)} used` : ''}
                      </div>
                    ))}
                  </td>
                  <td className="px-5 py-3.5 text-xs text-gray-600">
                    {n.inventory?.cpu_count ? `${n.inventory.cpu_count} cores` : '—'}
                    {n.inventory?.mem_total_mb ? ` · ${fmtGb(n.inventory.mem_total_mb)}` : ''}
                  </td>
                  <td className="px-5 py-3.5">
                    {!n.is_local && (
                      <div className="flex items-center justify-end space-x-1">
                        {n.state === 'draining' ? (
                          <button disabled={busy === n.id} onClick={() => act(n, resumeNode, `${n.name} takes work again.`)}
                                  className="text-xs font-medium text-indigo-600 hover:text-indigo-800 px-2 py-1 rounded hover:bg-indigo-50">Resume</button>
                        ) : (
                          <button disabled={busy === n.id} onClick={() => act(n, drainNode, `Nothing new goes to ${n.name}.`)}
                                  className="text-xs font-medium text-amber-700 hover:text-amber-900 px-2 py-1 rounded hover:bg-amber-50">Drain</button>
                        )}
                        <button disabled={busy === n.id} onClick={() => remove(n)}
                                className="text-xs font-medium text-red-600 hover:text-red-800 px-2 py-1 rounded hover:bg-red-50">Remove</button>
                      </div>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-gray-400">
        Drain stops new jobs and workspaces from going to a machine; what already runs there keeps
        running. A machine that misses heartbeats for a minute is skipped until it calls in again.
      </p>

      {adding && <AddNode onClose={() => { setAdding(false); load() }} showToast={showToast} />}
      {confirmDialog}
    </div>
  )
}

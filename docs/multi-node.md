# Several nodes

One installation, the **master**, holds the database, the queue, fair share,
accounts and the web interface.  Every other machine runs the same software as
a **node**: it owns its GPUs, CPUs and containers and does what the master
tells it.  The master is also a node for its own GPUs.

```
  browser ──HTTPS──► master ─── scheduler, fair share, DB (local disk) ───┐
                      │  ▲                                                │
             commands │  │ heartbeats, results                            │ NFS (same LAN)
            (answers  │  │ (node → master, outbound only)                 │ /srv/.../jupyter_data
             to polls)▼  │                                                │
                      node agent ── Docker ── gpu-<node>-job-17, gpu-<node>-jupyter-alice
```

## Joining

1. An administrator creates a join token on the master (**Admin → Nodes →
   Add a node**).  It is single-use and expires after an hour.  The dialog
   prints the command for step 2.
2. On the new machine, in a checkout of the same version:

   ```
   sudo ./deploy.sh --join https://gpu.example.org gpj_... --nfs master:/srv/gpu/jupyter_data
   ```

   This writes `ROLE=node`, `MASTER_URL` and the token into `.env`, mounts the
   homes (and adds the line to `/etc/fstab`), builds the workspace image and
   starts only the backend, which runs as the agent.  Without root, leave out
   `--nfs`; the script prints the mount commands to run first.
3. The agent sends the token with what the machine has (GPU UUIDs, names and
   memory, cores, RAM) and receives two secrets, kept in `data/node.json`
   (0600): its credential, and a key the master signs proxied requests with.
   `deploy.sh` then drops the spent token from `.env`.

Joining again with a new token (a reinstalled machine, a lost `node.json`)
keeps the node's name and replaces both secrets.

No SSH and no inbound port is needed for control: the agent opens every
connection.  It long-polls the master for commands and posts heartbeats and
results, so a node behind NAT or a firewall that allows only outbound HTTPS
can join.

## Control channel

* **Heartbeat** every `NODE_HEARTBEAT_SECONDS` (2 s): per-GPU memory and
  utilisation, VRAM held by each platform container (the same host-wide scan
  the single-node platform uses), CPU load and memory.  The master's scheduler
  and VRAM guard read a node's heartbeat where they would read the local scan.
* **Commands**: start, stop, inspect and remove a job or workspace container,
  read container state.  A command is a name and JSON arguments; the agent
  runs the same functions the master runs for its own GPUs.  The master waits
  for the result with a timeout.
* A node silent for `NODE_STALE_SECONDS` (60 s) is **unreachable**: nothing
  new is placed on it, and its running jobs are left alone, not failed, until
  it comes back or an administrator removes it.  On return its heartbeat lists
  the containers it really has, and the master reconciles against that.

## Data

* **Homes**: one copy, on the master (or a NAS), exported over NFS to the
  nodes of the same LAN and mounted at `./jupyter_data` on each.  A job or
  workspace on any node sees the same `~`.  The database never lives on NFS.
  On the master:

  ```
  # /etc/exports, one line per node; no_root_squash because the node's
  # backend sets the owner of a home it creates
  /srv/gpu/jupyter_data  10.0.0.42(rw,sync,no_subtree_check,no_root_squash)
  ```

  `no_root_squash` gives root on that node root on the export, so list nodes
  by address and nothing wider.
* **Jupyter traffic**: browser → master proxy → the node's data port
  (`NODE_DATA_PORT`, 8100) → container.  The master reaches the port over the
  LAN or a VPN; each request carries `X-Node-Proxy`, an HMAC over the time and
  the user name with the node's proxy key.  The port answers nothing else, and
  a captured header is good for one user's workspace for a minute.
* **SSH to a workspace**: straight to the node's address and the workspace's
  port; the user's dashboard shows that address.

## Workspaces on a node

A workspace runs where its GPUs are.  An assignment names a machine (**Admin
→ Assignments → Machine**) and GPU indices as that machine numbers them; the
user's Start then goes to that node, and status, stop and the proxy follow the
session row.  A user with no assignment, or one without a machine, runs on the
master as before.

If the node stops calling in, its workspaces are left as they are: the proxy
answers 502 and the status stays "running" until the node is back or an
administrator stops the session.  Starting a workspace on a node that is away
is refused with the node's name.

## Sites

Nodes in different LANs are grouped into **sites**.  Each site has its own NFS
server, a user's home belongs to one site, and the scheduler places a user's
jobs and workspace only on nodes of that site.  Control traffic crosses the
internet; storage does not.  Moving a user to another site is an
administrator's action (copy the home, change the site).

## Scheduling across nodes

Fair share is computed once, over the whole installation, exactly as before.
For the job at the head of the order the scheduler considers every ready node
of the user's site and takes the densest fit: the node and cards with the
least free memory that still fit, which keeps whole cards and whole nodes free
for larger jobs.  The VRAM allowance, the per-user running cap and the core
pool are the same rules, applied per node.

## What a node does not do

It has no database, no users and no scheduler.  If the master is down, running
jobs keep running and nothing new starts; workspaces on NFS homes wait for the
mount (`hard`) rather than fail.

## Trying it on one machine

`docker-compose.emu.yml` runs two nodes beside a development master, each
offered one of the host's GPUs (`NODE_GPUS`) and with its own container names
(`INSTANCE_NAME`).  `scripts/experiments/emu_nodes.sh up` asks the master for
tokens and starts them; `down` stops them.  They share the master's
`jupyter_data` directly, which is what the NFS mount gives real nodes.


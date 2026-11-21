# Changelog

## Unreleased

### Changed

- The messages about a job's GPU memory say that other jobs were placed
  beside it on the basis of its request, instead of "on that figure".

## 1.3.1

### Changed

- HAMi-core is pinned at a newer commit (3ab81ce). A process that only loads
  the CUDA driver, such as a compiler's child or a tool, no longer waits on
  the library's host-wide lock: 16 such processes start in 0.3 s instead of
  9 s. A process that uses the GPU still waits about half a second, one at a
  time, so 16 of them take about 10 s instead of 2 s.
- `deploy.sh` rebuilds HAMi-core when the pinned commit changes, not only when
  the driver does.
- The experiment scripts no longer contain the password of the experiment
  accounts: it is read from `EXP_PASSWORD` or derived from the admin password.

### Fixed

- The HAMi-core build failed on the new commit, whose tests need CUDA 13
  headers; only the library is built now.
- The check that a build exports the symbols the platform needs could report
  one missing although it was there.

### Added

- `scripts/hami/startup_check.py` times CUDA start-up with and without a
  HAMi-core build; `scripts/experiments/e4_diag.py` looks for the cause of
  slow requests to a workspace on the master while a node is slow (it did not
  recur).

## 1.3.0

### Added

- **Several machines.** Further machines join an installation with a one-time
  token (`deploy.sh --join`). A node opens every connection itself, so it needs
  no SSH access and no inbound control port. Jobs and workspaces are placed on
  any node with room; homes are shared over NFS. Admin → Nodes adds, drains,
  resumes and removes nodes. See [docs/multi-node.md](docs/multi-node.md).
- **Hard GPU memory limit** (`GPU_HARD_VRAM_LIMIT`, off by default). An
  allocation past a job's allowance fails with a CUDA out-of-memory error.
  `deploy.sh` builds HAMi-core for the installed driver and checks the build
  on a GPU before using it. The measurement-based check stays on beside it.
- **Sign-in with Google** or another OpenID Connect provider (`OIDC_*`), for
  existing accounts, matched by verified email.
- `scripts/experiments/`: the runner that starts experiments only while no
  user's job needs the GPUs, the scripts of experiments E1–E6 and their
  summarised results.

### Fixed

- With many users starting workspaces at once, starts failed because database
  connections ran out.
- The job list ran one `docker exec` per job to read GPU use; it is cached now.
- A command handed to a node's agent just before the agent restarted was
  lost; it is handed out again.
- A node that did not answer held up the whole scheduling pass; asking a node
  for a job's state now times out after 15 s and is retried on the next pass.
- Placement did not count GPU memory held outside the queue (a workspace,
  another program) against the card.
- A job that ended while its node was out of contact was charged until the
  node came back, not until the job's container exited.
- A workspace started when no SSH port was free ran under a default account
  instead of its owner's.
- The test suite no longer runs the app's periodic scheduling pass beside the
  tests, which made one test fail now and then.

### Changed

- Version numbers of the API and the frontend follow the release.

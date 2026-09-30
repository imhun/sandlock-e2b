# Worker seccomp profile

`sandlock-worker.json` is the syscall filter the **worker** container runs
under. It replaces `seccomp=unconfined`, which every production manifest used
until this change: unconfined drops the entire default filter from the
container, and the worker is the process that runs untrusted workloads.

## How it reaches the runtime

- **compose**: `seccomp=../seccomp/sandlock-worker.json` in the service spec (or
  `E2B_SECCOMP_PROFILE=<absolute path>` when only the compose file is shipped).
- **k8s**: `seccompProfile: {type: Localhost, localhostProfile:
  sandlock-worker.json}`. The kubelet resolves that name on the *node*, so the
  file has to be installed there: `deploy/k8s/seccomp-installer.yaml` (ConfigMap
  + DaemonSet) writes this exact file into `/var/lib/kubelet/seccomp/` on every
  node. Apply the installer and wait for it to be ready *before* rolling the
  worker StatefulSet.

The profile is **the Docker default profile plus `pidfd_getfd`, plus a masked
`unshare`** — nothing else is relaxed, and the `unshare` entry is not a blanket
allow (see "The `unshare` mask" below). Everything the sandbox itself needs
(`seccomp` with `NEW_LISTENER`, `setgroups`, `pidfd_open`, `landlock_*`,
`fork`/`clone`, `ioctl`, …) is already in the default allowlist.

## The worker checks that it is actually running under this profile

`envd_service/config.py::check_seccomp_filter` runs as the first step of
`create_app` (next to the net-isolation pairing guard) and refuses to serve when
the profile is not in effect — both failures below are otherwise silent:

* `Seccomp: 0` in `/proc/self/status` (no filter at all: profile dropped,
  `seccomp=unconfined`, or a runtime that ignored an unknown profile) →
  `SECCOMP_FILTER_MISSING`.
* a filter is loaded but `unshare(CLONE_NEWUSER)` is still gated (an active
  probe in a child process) → `SECCOMP_PROFILE_NOT_APPLIED`. That is the runtime
  default profile, and it is how a k8s node with a *missing* Localhost profile
  presents itself: the kubelet silently skips the missing file and the pod comes
  up anyway (kubernetes#124944, 1.28/1.29), so every sandbox create would fail
  later instead of at startup. A host-level restriction
  (`apparmor_restrict_unprivileged_userns=1`, `max_user_namespaces=0`) is
  reported as the cause instead of blaming the profile.

`E2B_REQUIRE_SECCOMP_FILTER=0` downgrades both to warnings for the shapes that
are unfiltered on purpose (the test runner compose, the autoscaler's local
backend).

## What it changes vs the Docker default

| syscall | upstream default | here | why |
|---|---|---|---|
| `pidfd_getfd` | gated on `CAP_SYS_PTRACE` | unconditional | sandlock picks up the child's seccomp-notification fd with it (`crates/sandlock-core/src/sandbox.rs::dup_child_fd`). Neither worker shape carries `CAP_SYS_PTRACE`, so the gate refused it and **the sandbox could not be created at all** (measured: create fails, exit `-1`, no child output). |
| `unshare` | gated on `CAP_SYS_ADMIN` | allowed only for the namespace types this deployment builds | The worker builds a user namespace for the per-sandbox host uid (E3.2, and the route-B slot's F18 self-map), plus net/pid/mount namespaces for `E2B_ENABLE_NET_ISOLATION` / `pid_ns` / the real-root shapes. See "The `unshare` mask" below. |
| `ptrace`, `process_vm_readv`, `process_vm_writev` | gated on `CAP_SYS_PTRACE` in older profile revisions | unconditional | Current daemons already allow these unconditionally (measured on the local engine with `CapEff` lacking `CAP_SYS_PTRACE`); kept aligned so this file matches the shape the deployment is verified against. |

**Not** relaxed: `mount`, `keyctl`, `bpf`, `clone3`, `setns`, `open_tree`,
`perf_event_open`, … — the capability-gated groups of the default profile are
kept verbatim, so a deployment that does carry a capability (e.g. the
quota-agent's `SYS_ADMIN`, or the test runner's) keeps exactly the access the
kernel would have granted it anyway.

### Which rows are deltas, and which are just the default

Only **`pidfd_getfd` and `unshare` are changes**: the default profile gates them
(behind `CAP_SYS_PTRACE` / `CAP_SYS_ADMIN`) and neither worker shape carries
those capabilities. The other three are here because this file was generated
from an older revision of the default profile that still gated them; current
daemons allow all three unconditionally, and the supervisor does use them, so
leaving them gated would make this profile *stricter than the default* and break
the sandbox-create path:

| syscall | supervisor use |
|---|---|
| `ptrace` | fork tracking for process accounting / per-child handling (`PTRACE_SEIZE` + `PTRACE_O_TRACEFORK/VFORK/CLONE`, `resource.rs`), checkpoint capture (`checkpoint/capture.rs`) |
| `process_vm_readv` | seccomp-notif argument reads (`read_child_mem`), netlink struct reads (`netlink/handlers.rs`), checkpoint memory capture |
| `process_vm_writev` | checkpoint restore (`checkpoint/restore_blob.rs`) — the only one not on a hot path |

Most of this does not reach sandbox code: sandlock's own
`DEFAULT_BLOCKLIST_SYSCALLS` (`crates/sandlock-core/src/sys/structs.rs`) lists
`ptrace`, `process_vm_readv`, `process_vm_writev`, `unshare`, `setns`, `mount`
and `bpf`, and a probe run **inside** a sandbox under this profile gets `EPERM`
for every one of them (measured 2026-09-15, `tmp/seccomp-probe/sandbox_syscalls.py`).
The container-level allowances are for the worker/supervisor.

**One measured exception (2026-09-30): `pidfd_getfd` is *not* in that
blocklist**, so sandbox code can call it — re-measured inside a real sandbox on
the k0s cluster (arm64) after the `unshare` narrowing: `unshare` (every type),
`setns`, `ptrace`, `process_vm_*` and `mount` all returned `EPERM`, while
`pidfd_getfd` returned `EBADF` for a deliberately-bad argument, i.e. seccomp let
the call through and the kernel refused the arguments. With a real pidfd it
succeeds: a sandbox process can `pidfd_open` + `pidfd_getfd` **its own**
sandbox's processes (measured: pids 1/2/3/7 of its own command tree, and a write
through a duplicated fd landed). Two things bound it: the kernel's
`ptrace_may_access` gate is what decides the target set (same-uid siblings
inside the sandbox; the supervisor is a different uid/userns and sets
`PR_SET_DUMPABLE=0`), and the sandbox cannot even *name* what it stole —
`/proc/self/fd/<n>` is denied by the sandbox's own fs mediation. So the reach was
intra-sandbox, not an escape — but it is the same class `ptrace` is blocked for,
so `pidfd_getfd` was added to `DEFAULT_BLOCKLIST_SYSCALLS` (fork `a21a507`; wheel
and images rebuilt). Re-measured after that deploy, same probe: `pidfd_getfd` is
now **`EPERM`** as well, and the supervisor's own use is unaffected (it runs
outside this filter — the worker's log shows no `pidfd_getfd` failure, and
route-B slots still hand off their descriptor).

`process_vm_writev` is the single candidate for going *below* the default
(checkpoint restore is not part of what E2B exposes); that would be a deliberate
deviation to argue for on its own merits, not something this file does silently.
`ptrace` and `process_vm_readv` cannot be re-gated without breaking fork
tracking and the notification path.

### The `unshare` mask (2026-09-30)

`unshare` used to be an unconditional allow. It is now one entry whose argument
mask excludes the namespace types this deployment never builds:

```json
{"names": ["unshare"], "action": "SCMP_ACT_ALLOW",
 "args": [{"index": 0, "value": 234881152, "op": "SCMP_CMP_MASKED_EQ"}]}
```

`234881152` is `0x0E000080` = `CLONE_NEWTIME|NEWCGROUP|CLONE_NEWUTS|CLONE_NEWIPC`,
and `valueTwo` defaults to `0`, so the condition reads "none of those four bits
are set". The four the deployment *does* build stay reachable: **user** (the
per-sandbox host uid — `envd_service/slot_identity.py`,
`crates/sandlock-core/src/context.rs`, `procfs.rs`, `supervise/src/serve.rs`),
**net** (`context.rs`, when `E2B_ENABLE_NET_ISOLATION` is on), **pid**
(`procfs.rs`, when `pid_ns` is on) and **mount** (`realroot.rs`, and the
real-root probe in `envd_service/executors/sandlock.py`).

**Why it was worth narrowing.** The container-level filter is the outer bound
for everything in the worker pod, sandboxes included. Under the old
unconditional entry, a process that is root *inside its own user namespace*
could create any of the four dropped types. Measured in two arms, both
`--cap-drop ALL`, uid 65534, `unshare(CLONE_NEWUSER)` + a self-map first so the
probe really is root in a namespace:

| namespace | shipped (before) | masked (now) |
|---|---|---|
| `NEWNS` / `NEWNET` / `NEWPID` | ALLOW | ALLOW |
| `NEWUTS` / `NEWIPC` / `NEWCGROUP` / `NEWTIME` | **ALLOW** | **DENY `EPERM`** |

**Why the entry cannot simply be dropped.** Two measurements say the relax is
load-bearing and that no other arrangement removes it:

* The *first* namespace has to be created by the unprivileged side. Writing
  `/proc/<pid>/uid_map` presupposes an existing namespace — `as_uid` refuses a
  target whose map is still the initial full range ("this pid has not unshared a
  user namespace"), so the agent's grant necessarily comes *after* the worker's
  child has unshared.
* "Let the agent create it and hand it to the worker" does **not** help. The
  joiner would need `setns`, which this profile (and the default) gates on
  `CAP_SYS_ADMIN`. Measured: with the shipped profile the join is refused
  (`VERDICT=SETNS-REFUSED`, `EPERM`) *even though the caller shares uid 65534
  with the namespace's owner* — seccomp is static BPF and cannot read
  `cred->cap_effective`, so a `caps:` condition is resolved when the runtime
  builds the filter, from the container's **declared** capabilities, which the
  worker deliberately has none of. Removing the `includes` from that one rule
  flips the same probe to `SETNS-ALLOWED`, i.e. seccomp was the only thing in
  the way. So the alternative buys a `setns` relax instead of an `unshare` one.

The only arrangement that removes the relax is the one the C3 plan rejected:
the agent forks the slot itself, which moves the slot out of the worker's
process tree and cgroup.

## Evidence (measured 2026-09-15, Docker on x86_64)

`errno` of the same probe under the default profile vs `seccomp=unconfined` in
an otherwise identical container (`CapEff=0xa80425fb`, no `SYS_ADMIN`, no
`SYS_PTRACE`):

| syscall | default | unconfined | verdict |
|---|---|---|---|
| `unshare(CLONE_NEWUSER)` | `EPERM` | `ok` | blocked by the profile |
| `pidfd_getfd(-1)` | `EPERM` | `EBADF` | blocked by the profile |
| `unshare(NEWNET/NEWPID/NEWNS)` | `EPERM` | `EPERM` | capability, **not** seccomp |
| `mount` / `keyctl` / `bpf` / `clone3` | `EPERM`/`EPERM`/`EPERM`/`ENOSYS` | `ENOENT`/`EINVAL`/`EINVAL`/`EINVAL` | blocked, and not needed |
| `pidfd_open`, `process_vm_readv/writev`, `seccomp(SET_MODE_FILTER)`, `setgroups`, `ptrace` | allowed | allowed | no change needed |

End-to-end (real sandbox create + command execution, same image):

| profile | result |
|---|---|
| Docker default | ❌ create fails |
| default + **only** `pidfd_getfd` | ✅ runs |
| default + **only** `unshare` | ❌ create fails |
| default + both / `unconfined` | ✅ runs |

So `pidfd_getfd` is what the *base* path needs; `unshare` is what the
namespace-bearing paths need. Both are in this file because the deployed
worker runs with `E2B_PER_SANDBOX_UID` (default on) and route-B slots.

Security boundary — the sandbox does **not** inherit the relaxation: a
sandboxed process calling `unshare(CLONE_NEWUSER)` gets `EPERM` both under this
profile and under `unconfined` (sandlock's own filter denies it). The
relaxation therefore reaches the worker/supervisor only.

## Deploying

* **compose** (`deploy/compose/docker-compose.prod.yml`,
  `deploy/stack/docker-compose.prod.yml`): Compose reads the profile and sends
  it with the request, resolving the relative path against the compose file's
  directory — hence the default `seccomp=../seccomp/sandlock-worker.json`.
  **A host that keeps only the compose file** (the deployed
  `/opt/sandlock/docker-compose.prod.yml` layout) has no `../seccomp/`, so it
  must set `E2B_SECCOMP_PROFILE` to an absolute path, e.g.
  `E2B_SECCOMP_PROFILE=/opt/sandlock/seccomp/sandlock-worker.json`. Compose
  interpolation happens before the request, so either form works and the
  rendered value can be checked with `docker compose config`.
* **kubernetes** (`deploy/k8s/worker.yaml`): `Localhost` profiles are
  node-local state. Install the file as
  `/var/lib/kubelet/seccomp/sandlock-worker.json` on every node before rolling
  the StatefulSet. `Localhost` is also an allowed value under the Pod Security
  `baseline` seccomp rule, which `Unconfined` is not.
  Not every pod on the node carries this profile, and that is deliberate: the
  per-node `e2b-c3-agent` DaemonSet (`deploy/k8s/c3-agent.yaml`) runs the runtime
  **default** profile. Its root pieces are face B (`maint`) and the two owner
  inits (`storage-init`, `workspace-root-init`); face B's only job is to
  `fork`/`exec` *itself* (`/var/lib/e2b-priv/e2b-maint`) for `chown`/`rm`/`walk`
  requests, so the worker's relaxations (`pidfd_getfd`, `unshare`) have no user
  in it — and installing `sandlock-worker.json` there would only widen a
  process that never runs sandbox code. (C1's per-node `e2b-priv-broker`
  DaemonSet was the same shape and was retired in C3 Task 7.) The installer's
  DaemonSet and the agent's are separate objects; only `worker.yaml` (and the
  profiles the lanes use) need this file.
* **lanes**: `deploy/scripts/test-prod-shaped.sh` and
  `deploy/scripts/smoke-prod-worker.sh` use the same file (override with
  `SECCOMP_PROFILE=...`) so the gate reproduces the deployed shape.

## Regenerating

Take the default profile shipped by the daemon you deploy on and apply the two
edits above — i.e. move `pidfd_getfd` and `unshare` out of their
capability-gated entries into the unconditional allowlist. Do not append a
second entry for a syscall that is already listed: whether a duplicate
allow-rule wins depends on how that engine compiles same-syscall entries.

This file was generated from moby `profiles/seccomp/default.json` (v20.10.24)
with those two moves, plus the `ptrace` / `process_vm_*` alignment noted in the
table above.

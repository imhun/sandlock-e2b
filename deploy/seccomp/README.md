# Worker seccomp profile

`sandlock-worker.json` is the syscall filter the **worker** container runs
under. It replaces `seccomp=unconfined`, which every production manifest used
until this change: unconfined drops the entire default filter from the
container, and the worker is the process that runs untrusted workloads.

The profile is **the Docker default profile plus exactly two syscalls** —
nothing else is relaxed. Everything the sandbox itself needs (`seccomp` with
`NEW_LISTENER`, `setgroups`, `pidfd_open`, `landlock_*`, `fork`/`clone`,
`ioctl`, …) is already in the default allowlist.

## What it changes vs the Docker default

| syscall | upstream default | here | why |
|---|---|---|---|
| `pidfd_getfd` | gated on `CAP_SYS_PTRACE` | unconditional | sandlock picks up the child's seccomp-notification fd with it (`crates/sandlock-core/src/sandbox.rs::dup_child_fd`). Neither worker shape carries `CAP_SYS_PTRACE`, so the gate refused it and **the sandbox could not be created at all** (measured: create fails, exit `-1`, no child output). |
| `unshare` | gated on `CAP_SYS_ADMIN` | unconditional | The worker builds a user namespace for the per-sandbox host uid (E3.2, and the route-B slot's F18 self-map), plus a net/pid namespace when `E2B_ENABLE_NET_ISOLATION` / `pid_ns` are on. |
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

None of this reaches sandbox code: sandlock's own `DEFAULT_BLOCKLIST_SYSCALLS`
lists `ptrace`, `process_vm_readv`, `process_vm_writev`, `unshare`, `setns`,
`mount` and `bpf`, and a probe run **inside** a sandbox under this profile gets
`EPERM` for every one of them (measured 2026-09-15,
`tmp/seccomp-probe/sandbox_syscalls.py`). The container-level allowances are for
the worker/supervisor only.

`process_vm_writev` is the single candidate for going *below* the default
(checkpoint restore is not part of what E2B exposes); that would be a deliberate
deviation to argue for on its own merits, not something this file does silently.
`ptrace` and `process_vm_readv` cannot be re-gated without breaking fork
tracking and the notification path.

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
  the Deployment. `Localhost` is also an allowed value under the Pod Security
  `baseline` seccomp rule, which `Unconfined` is not.
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

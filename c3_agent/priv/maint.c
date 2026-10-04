/* SPDX-License-Identifier: Apache-2.0
 *
 * e2b-maint -- the privileged file-operation binary for sandbox-owned trees.
 *
 * Installed with `setcap cap_chown,cap_dac_override+ep` in the agent image
 * (`deploy/docker/Dockerfile.agent`) and executed one verb per call by the
 * node agent's face B (`c3_agent/fileops.py`), on the control plane's
 * instructions. Those two capabilities are what a non-root worker cannot do
 * for itself:
 *
 *   chown  --uid X [--recursive] --path P   workspace / volume-slice ownership
 *   chown  --worker    [--recursive] --path P   hand a reclaimed orphan back
 *   rm     --path P                          sandbox teardown
 *   walk   --path P                          size/owner scans (metrics, reconcile)
 *
 * `walk` prints one line per entry -- "<kind> <uid> <gid> <mode-octal> <size>
 * <path>", kind `d` for a directory, `f` for a regular file, `l` for a
 * symlink -- and never the same entry twice, because the byte accounting that
 * consumes it sums the directories too.  For a directory the size is its
 * **allocated** bytes (`st_blocks x 512`), not `st_size`: measured on the
 * cluster's NAS (2026-09-21) `st_size` was 4096 for an empty directory and
 * 16384 at 2000 entries while `st_blocks` and `du -s` stayed at 512 the whole
 * way, and the consumer's contract is to be the number `du` reports.
 *
 * Every path is `realpath`-resolved and must land under `<workspace_base>/`,
 * `<state_base>/` (E2B_STATE_BASE, N27), `<shared_volume_root>/` or
 * `<image_cache_dir>/` (E2B_IMAGE_CACHE_DIR, named -- C1: the sandbox secret
 * trees live there); symlinks are never followed while recursing
 * (`FTS_PHYSICAL` + `lchown`/`unlinkat` semantics), so a tenant cannot plant a
 * link that makes this binary touch something outside the roots.
 *
 * Who runs it, and with what: the caller is the agent's face B (root, plus the
 * two capabilities above), the request it turns into argv comes from the
 * control plane and names `{sandbox_id, op}` -- never a path, never a uid (C3
 * §14.4 hard rules 1/3) -- and that face decides which argv this binary sees.
 * The binary re-derives every path against the roots above anyway; the two
 * gates are independent on purpose.
 *
 * It used to be C1's per-node socket broker as well (`e2b-maint serve` /
 * `ping`: SO_PEERCRED authorization, one root child per request, a second
 * "health" socket for the DaemonSet probes). That shape was retired with C3
 * Task 7 (`docs/open-issues.md` N47) and the dead code removed on 2026-09-30:
 * no shipped image ever started it, no client could reach it, and a socket
 * server compiled into a binary that holds `cap_chown,cap_dac_override` is an
 * unreachable entry point nobody audits.
 *
 * usage:
 *   e2b-maint chown  (--uid U --gid G | --worker) [--recursive] --path P
 *   e2b-maint rm     --path P
 *   e2b-maint walk   --path P
 */
#define _GNU_SOURCE

#include "priv_common.h"

#include <errno.h>
#include <fts.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* --path P (the only path spelling: it keeps the argv shape unambiguous). */
static const char *path_flag_value(int argc, char **argv, int *index) {
    const char *arg = argv[*index];
    if (strcmp(arg, "--path") == 0) {
        if (*index + 1 >= argc) {
            priv_usage("--path needs a value");
        }
        *index += 1;
        return argv[*index];
    }
    if (strncmp(arg, "--path=", 7) == 0 && arg[7] != '\0') {
        return arg + 7;
    }
    return NULL;
}

static char *resolve_or_refuse(const char *path, int strict) {
    static char resolved[PATH_MAX];
    char err[PRIV_ERR_LEN];
    if (priv_resolve_allowed_path(path, strict, resolved, sizeof(resolved), err,
                                  sizeof(err)) != 0) {
        priv_fail("%s", err);
    }
    return resolved;
}

static int chown_tree(const char *root, uid_t uid, gid_t gid) {
    char *argv[2] = {(char *)root, NULL};
    FTS *tree = fts_open(argv, FTS_PHYSICAL | FTS_NOCHDIR, NULL);
    FTSENT *entry;
    int failures = 0;
    if (tree == NULL) {
        return -1;
    }
    while ((entry = fts_read(tree)) != NULL) {
        /* Symlinks themselves, never their targets (hence lchown). */
        if (entry->fts_info == FTS_DC || entry->fts_info == FTS_DNR ||
            entry->fts_info == FTS_ERR || entry->fts_info == FTS_NS) {
            failures += 1;
            continue;
        }
        if (lchown(entry->fts_accpath, uid, gid) != 0) {
            failures += 1;
        }
    }
    if (errno != 0 && errno != ENOENT) {
        failures += 1;
    }
    fts_close(tree);
    return failures == 0 ? 0 : -1;
}

static int remove_tree(const char *root) {
    char *argv[2] = {(char *)root, NULL};
    FTS *tree = fts_open(argv, FTS_PHYSICAL | FTS_NOCHDIR, NULL);
    FTSENT *entry;
    int failures = 0;
    if (tree == NULL) {
        return -1;
    }
    while ((entry = fts_read(tree)) != NULL) {
        switch (entry->fts_info) {
        case FTS_DP:
            if (rmdir(entry->fts_accpath) != 0) {
                failures += 1;
            }
            break;
        case FTS_F:
        case FTS_SL:
        case FTS_SLNONE:
        case FTS_DEFAULT:
            if (unlink(entry->fts_accpath) != 0) {
                failures += 1;
            }
            break;
        case FTS_NS:
        case FTS_ERR:
        case FTS_DNR:
            failures += 1;
            break;
        default:
            break;
        }
    }
    fts_close(tree);
    return failures == 0 ? 0 : -1;
}

static char walk_kind(const FTSENT *entry) {
    switch (entry->fts_info) {
    case FTS_D:
    case FTS_DC:
        return 'd';
    case FTS_F:
        return 'f';
    case FTS_SL:
    case FTS_SLNONE:
        return 'l';
    default:
        return 'o';
    }
}

static int walk_tree(const char *root) {
    char *argv[2] = {(char *)root, NULL};
    FTS *tree = fts_open(argv, FTS_PHYSICAL | FTS_NOCHDIR, NULL);
    FTSENT *entry;
    if (tree == NULL) {
        return -1;
    }
    while ((entry = fts_read(tree)) != NULL) {
        /* Each entry exactly once.  FTS visits a directory twice -- FTS_D
         * before its contents and FTS_DP after -- and the caller sums the
         * directories' own st_size as well as the files' (N31 fix 2: on NFS a
         * directory costs 16 KiB of real space that the byte ledger used to
         * ignore entirely), so emitting the post-order visit too would count
         * every directory's blocks twice.  The pre-order visit is the one
         * that matches os.walk's `dirpath`, which is the number this must
         * agree with byte for byte. */
        if (entry->fts_info == FTS_DP) {
            continue;
        }
        char kind = walk_kind(entry);
        const struct stat *st = entry->fts_statp;
        if (st == NULL) {
            continue;
        }
        /* A directory is charged its allocation, not its st_size (see the
         * header): the block count is in 512-byte units by definition. */
        long long size = (kind == 'd')
                             ? (long long)st->st_blocks * 512
                             : (long long)st->st_size;
        /* "<kind> <uid> <gid> <mode-octal> <size> <path>" */
        if (printf("%c %lu %lu %o %lld %s\n", kind, (unsigned long)st->st_uid,
                   (unsigned long)st->st_gid, (unsigned int)(st->st_mode & 07777),
                   size, entry->fts_accpath) < 0) {
            fts_close(tree);
            return -1;
        }
    }
    fts_close(tree);
    return fflush(stdout) == 0 ? 0 : -1;
}

int main(int argc, char **argv) {
    const char *verb;
    const char *path = NULL;
    long uid = -1, gid = -1;
    int recursive = 0;
    int to_worker = 0;
    int index;
    char err[PRIV_ERR_LEN];

    priv_set_progname("e2b-maint");
    /* Ignore SIGPIPE for the whole program, before anything can be forked or
     * executed from here: a caller that hangs up mid-answer makes a write fail
     * with EPIPE, and the default disposition would kill this process instead
     * of letting it report the failure the caller has to see. */
    signal(SIGPIPE, SIG_IGN);
    if (argc < 2) {
        priv_usage("expected chown|rm|walk");
    }
    verb = argv[1];
    /* The verb is checked **before** the flags: with the C1 socket verbs gone,
     * an unknown one (a stale caller, an operator probing the binary) must be
     * named as unknown -- not reported as "--path is required", which is what
     * the flag loop would say first. */
    if (strcmp(verb, "chown") != 0 && strcmp(verb, "rm") != 0 &&
        strcmp(verb, "walk") != 0) {
        priv_usage("unknown verb '%s' (expected chown|rm|walk)", verb);
    }
    for (index = 2; index < argc; index++) {
        const char *arg = argv[index];
        const char *value;
        if (strcmp(arg, "--recursive") == 0) {
            recursive = 1;
            continue;
        }
        if (strcmp(arg, "--worker") == 0) {
            to_worker = 1;
            continue;
        }
        if (strcmp(arg, "--uid") == 0 || strncmp(arg, "--uid=", 6) == 0) {
            value = (arg[5] == '=') ? arg + 6 : NULL;
            if (value == NULL) {
                if (index + 1 >= argc) {
                    priv_usage("--uid needs a value");
                }
                value = argv[++index];
            }
            if (priv_parse_uid(value, &uid, err, sizeof(err)) != 0) {
                priv_usage("--uid: %s", err);
            }
            continue;
        }
        if (strcmp(arg, "--gid") == 0 || strncmp(arg, "--gid=", 6) == 0) {
            value = (arg[5] == '=') ? arg + 6 : NULL;
            if (value == NULL) {
                if (index + 1 >= argc) {
                    priv_usage("--gid needs a value");
                }
                value = argv[++index];
            }
            if (priv_parse_uid(value, &gid, err, sizeof(err)) != 0) {
                priv_usage("--gid: %s", err);
            }
            continue;
        }
        value = path_flag_value(argc, argv, &index);
        if (value != NULL) {
            path = value;
            continue;
        }
        priv_usage("unexpected argument '%s'", arg);
    }
    if (path == NULL) {
        priv_usage("--path is required");
    }

    if (strcmp(verb, "chown") == 0) {
        const char *resolved = resolve_or_refuse(path, 1);
        if (to_worker) {
            /* --worker: owner stays the worker's own identity. An optional
             * --gid scopes the *group* to a pooled uid (the slot documents
             * are "worker-writable, readable by the slot only"), so the gid
             * gets the same pool gate as a --uid would. */
            if (uid >= 0) {
                priv_usage("--worker cannot be combined with --uid");
            }
            /* SEC-2026-10-04 (audit STATIC-5): this arm used to hand
             * ``priv_worker_uid()`` straight to ``lchown`` with no gate at
             * all, while the ``--uid`` arm below runs ``priv_validate_uid``.
             * ``priv_worker_uid()`` reads ``E2B_BROKER_WORKER_UID``, which the
             * agent fills from the request body, so the two arms disagreed
             * about what may be written into a privileged tree. Reproduced
             * locally against this binary: ``chown --worker --recursive`` with
             * the worker uid set to 1 (outside the pool) reached ``lchown``.
             *
             * The invariant that actually matters is narrower than "in the
             * pool": a caller may only give a privileged tree to an identity it
             * cannot act as. The one set of uids a caller *can* act as is the
             * sandbox uid pool, so refusing exactly that set closes the
             * cross-tenant hole (a tree owned by a pooled uid is a tree the
             * matching sandbox can read and write) without breaking the
             * legitimate deployments: the k8s worker is 65534 and the
             * compose/test root worker is 0, both outside the pool by
             * construction. */
            uid = priv_worker_uid();
            if (priv_uid_in_pool(uid, err, sizeof(err)) != 0) {
                priv_fail(
                    "--worker would hand a privileged tree to uid %ld, which is "
                    "inside the sandbox uid pool: refusing (%s)",
                    uid, err);
            }
            /* The same audit finding, second half: ``--worker --recursive``
             * applied that identity to an entire tree, turning one legitimate
             * document-scope operation into a node-wide ownership change. The
             * only caller that uses ``--worker`` is the slot-document scope
             * (``control_plane/file_ops.py``), and it is never recursive. */
            if (recursive) {
                priv_usage(
                    "--worker cannot be combined with --recursive: the worker "
                    "identity is only ever scoped to a single document");
            }
            if (gid < 0) {
                gid = priv_worker_gid();
            } else if (priv_gid_allowed(gid, err, sizeof(err)) != 0) {
                priv_fail("%s", err);
            }
        } else {
            if (uid < 0) {
                priv_usage("chown needs --uid (or --worker)");
            }
            if (priv_validate_uid(uid, err, sizeof(err)) != 0) {
                priv_fail("%s", err);
            }
            if (gid < 0) {
                gid = uid;
            }
            /*
             * Fix round 1 (裁定 c1): the group of a sandbox tree is the
             * *worker's* gid ("0770 owner=<sandbox uid> group=<worker gid>"),
             * so --gid may be the worker's own gid as well as a pooled uid.
             * Naming the worker's own gid is not a widening -- a process may
             * always chgrp a file it owns to its own gid -- and the uid above
             * still has to be a pooled one (never 0).
             */
            if (priv_gid_allowed(gid, err, sizeof(err)) != 0) {
                priv_fail("%s", err);
            }
        }
        if (recursive) {
            if (chown_tree(resolved, (uid_t)uid, (gid_t)gid) != 0) {
                priv_fail("recursive chown of %s to %ld:%ld failed: %s", path,
                          uid, gid, strerror(errno));
            }
        } else if (lchown(resolved, (uid_t)uid, (gid_t)gid) != 0) {
            priv_fail("chown %s to %ld:%ld failed: %s", path, uid, gid,
                      strerror(errno));
        }
        return PRIV_EXIT_OK;
    }

    if (strcmp(verb, "rm") == 0) {
        const char *resolved = resolve_or_refuse(path, 1);
        if (remove_tree(resolved) != 0) {
            priv_fail("removing %s failed: %s", path, strerror(errno));
        }
        return PRIV_EXIT_OK;
    }

    if (strcmp(verb, "walk") == 0) {
        const char *resolved = resolve_or_refuse(path, 0);
        if (walk_tree(resolved) != 0) {
            priv_fail("walking %s failed: %s", path, strerror(errno));
        }
        return PRIV_EXIT_OK;
    }

    priv_usage("unknown verb '%s'", verb);
}

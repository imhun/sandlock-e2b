/* SPDX-License-Identifier: Apache-2.0
 *
 * e2b-maint -- the worker's management-plane broker for sandbox-owned trees.
 *
 * Installed with `setcap cap_chown,cap_dac_override+ep` in the FINAL image
 * stage. A worker that is not root needs both capabilities for the paths that
 * today only a root worker can walk:
 *
 *   chown  --uid X [--recursive] --path P   workspace / volume-slice ownership
 *   chown  --worker    [--recursive] --path P   hand a reclaimed orphan back
 *   rm     --path P                          sandbox teardown
 *   walk   --path P                          size/owner scans (metrics, reconcile)
 *
 * Every path is `realpath`-resolved and must land under `<workspace_base>/` or
 * `<shared_volume_root>/`; symlinks are never followed while recursing
 * (`FTS_PHYSICAL` + `lchown`/`unlinkat` semantics), so a tenant cannot plant a
 * link that makes the broker touch something outside the roots.
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
    case FTS_DP:
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
        char kind = walk_kind(entry);
        const struct stat *st = entry->fts_statp;
        if (st == NULL) {
            continue;
        }
        /* "<kind> <uid> <gid> <mode-octal> <size> <path>" */
        if (printf("%c %lu %lu %o %lld %s\n", kind, (unsigned long)st->st_uid,
                   (unsigned long)st->st_gid, (unsigned int)(st->st_mode & 07777),
                   (long long)st->st_size, entry->fts_accpath) < 0) {
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
    if (argc < 2) {
        priv_usage("expected chown|rm|walk");
    }
    verb = argv[1];
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
            uid = (long)getuid();
            if (gid < 0) {
                gid = (long)getgid();
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
             * so --gid may be the broker's own gid as well as a pooled uid.
             * Naming the broker's own gid is not a widening -- a process may
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

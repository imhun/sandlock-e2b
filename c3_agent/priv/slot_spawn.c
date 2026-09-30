/* SPDX-License-Identifier: Apache-2.0
 *
 * e2b-slot-spawn -- start one route-B slot at a pooled host uid.
 *
 * Installed with `setcap cap_setuid,cap_setgid+ep` in the FINAL image stage
 * (a `COPY --from` would drop the security.capability xattr). A worker that is
 * not root execs it; the kernel grants exactly those two capabilities from the
 * file, this program performs
 *
 *     setgroups([]) -> setgid(X) -> setuid(X) -> execve(sandlock-supervise)
 *
 * and the exec'd slot has **no** capabilities: the uid change clears the
 * permitted/effective sets and the supervise binary carries no file
 * capabilities of its own.
 *
 * It is deliberately not a general launcher: the program must be the pinned
 * absolute `sandlock-supervise` path and the uid must come from the configured
 * pool (never 0). It never execs a capability-less helper either -- that was
 * measured to lose the capabilities at exec time.
 *
 * usage: e2b-slot-spawn spawn --uid X --gid X -- <sandlock-supervise> [args...]
 */
#define _GNU_SOURCE

#include "priv_common.h"

#include <errno.h>
#include <grp.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static const char *need_value(int argc, char **argv, int *index, const char *name,
                              const char *inline_value) {
    if (inline_value != NULL) {
        return inline_value;
    }
    if (*index + 1 >= argc) {
        priv_usage("%s needs a value", name);
    }
    *index += 1;
    return argv[*index];
}

/* Support both "--uid 10001" and "--uid=10001". */
static const char *flag_value(const char *arg, const char *name,
                              const char **inline_value) {
    size_t len = strlen(name);
    if (strncmp(arg, name, len) != 0) {
        return NULL;
    }
    if (arg[len] == '\0') {
        *inline_value = NULL;
        return arg;
    }
    if (arg[len] == '=') {
        *inline_value = arg + len + 1;
        return arg;
    }
    return NULL;
}

int main(int argc, char **argv) {
    long uid = -1, gid = -1;
    char err[PRIV_ERR_LEN];
    int index = 1;
    const char *pinned;

    priv_set_progname("e2b-slot-spawn");

    if (argc < 2 || strcmp(argv[1], "spawn") != 0) {
        priv_usage("expected: spawn --uid X --gid X -- <sandlock-supervise> [args...]");
    }
    index = 2;
    while (index < argc) {
        const char *inline_value = NULL;
        const char *value;
        if (strcmp(argv[index], "--") == 0) {
            index += 1;
            break;
        }
        if (flag_value(argv[index], "--uid", &inline_value) != NULL) {
            value = need_value(argc, argv, &index, "--uid", inline_value);
            if (uid >= 0) {
                priv_usage("--uid given twice");
            }
            if (priv_parse_uid(value, &uid, err, sizeof(err)) != 0) {
                priv_usage("--uid: %s", err);
            }
        } else if (flag_value(argv[index], "--gid", &inline_value) != NULL) {
            value = need_value(argc, argv, &index, "--gid", inline_value);
            if (gid >= 0) {
                priv_usage("--gid given twice");
            }
            if (priv_parse_uid(value, &gid, err, sizeof(err)) != 0) {
                priv_usage("--gid: %s", err);
            }
        } else {
            priv_usage("unexpected argument '%s'", argv[index]);
        }
        index += 1;
    }
    if (uid < 0 || gid < 0) {
        priv_usage("both --uid and --gid are required");
    }
    if (uid != gid) {
        priv_fail("e2b-slot-spawn starts one host identity: uid %ld and gid %ld must match",
                  uid, gid);
    }
    if (priv_validate_uid(uid, err, sizeof(err)) != 0) {
        priv_fail("%s", err);
    }
    if (index >= argc) {
        priv_usage("no program given after '--'");
    }
    pinned = priv_supervise_bin();
    if (strcmp(argv[index], pinned) != 0 || argv[index][0] != '/') {
        priv_fail(
            "the spawned program must be the absolute path %s (got '%s'): "
            "e2b-slot-spawn is not a general run-as-uid-X launcher",
            pinned, argv[index]);
    }

    /*
     * Drop every supplementary group first: a slot must not inherit a group
     * that could make a tenant-owned 1777+sticky tree writable across uids.
     */
    if (setgroups(0, NULL) != 0) {
        priv_fail("setgroups([]) failed: %s", strerror(errno));
    }
    if (setgid((gid_t)gid) != 0) {
        priv_fail("setgid(%ld) failed: %s (is cap_setgid on this binary?)", gid,
                  strerror(errno));
    }
    if (setuid((uid_t)uid) != 0) {
        priv_fail("setuid(%ld) failed: %s (is cap_setuid on this binary?)", uid,
                  strerror(errno));
    }
    if (getuid() != (uid_t)uid || geteuid() != (uid_t)uid) {
        priv_fail("identity check failed after setuid(%ld)", uid);
    }

    execv(argv[index], &argv[index]);
    priv_fail("exec %s failed: %s", argv[index], strerror(errno));
}

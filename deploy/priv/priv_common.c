/* SPDX-License-Identifier: Apache-2.0
 * Shared validators for the file-capability brokers (see priv_common.h).
 */
#define _GNU_SOURCE

#include "priv_common.h"

#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static const char *g_progname = "e2b-priv";

const char *priv_progname(void) { return g_progname; }

void priv_set_progname(const char *name) {
    if (name != NULL) {
        g_progname = name;
    }
}

static void priv_vreport(const char *prefix, const char *fmt, va_list ap) {
    fprintf(stderr, "%s: %s: ", priv_progname(), prefix);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
}

void priv_fail(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    priv_vreport("refused", fmt, ap);
    va_end(ap);
    exit(PRIV_EXIT_REFUSED);
}

void priv_usage(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    priv_vreport("usage", fmt, ap);
    va_end(ap);
    exit(PRIV_EXIT_USAGE);
}

static int priv_env_positive_long(const char *name, long fallback, long *out,
                                  char *err, size_t errlen) {
    const char *text = getenv(name);
    long value;
    if (text == NULL || *text == '\0') {
        *out = fallback;
        return 0;
    }
    if (priv_parse_uid(text, &value, err, errlen) != 0) {
        return -1;
    }
    *out = value;
    return 0;
}

int priv_parse_uid(const char *text, long *out, char *err, size_t errlen) {
    char *end = NULL;
    long value;
    if (text == NULL || *text == '\0') {
        snprintf(err, errlen, "empty numeric argument");
        return -1;
    }
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        snprintf(err, errlen, "not a decimal integer: '%s'", text);
        return -1;
    }
    if (value <= 0) {
        snprintf(err, errlen, "uid/gid must be positive (got %ld)", value);
        return -1;
    }
    *out = value;
    return 0;
}

void priv_uid_pool(long *start, long *size) {
    char err[PRIV_ERR_LEN];
    if (priv_env_positive_long("E2B_UID_POOL_START", PRIV_DEFAULT_UID_POOL_START,
                               start, err, sizeof(err)) != 0 ||
        priv_env_positive_long("E2B_UID_POOL_SIZE", PRIV_DEFAULT_UID_POOL_SIZE,
                               size, err, sizeof(err)) != 0) {
        priv_fail("invalid uid pool configuration: %s", err);
    }
}

int priv_validate_uid(long uid, char *err, size_t errlen) {
    long start, size;
    priv_uid_pool(&start, &size);
    if (uid < start || uid > start + size - 1) {
        snprintf(err, errlen,
                 "uid %ld is outside the privileged helper uid pool %ld..%ld",
                 uid, start, start + size - 1);
        return -1;
    }
    return 0;
}

int priv_gid_allowed(long gid, char *err, size_t errlen) {
    long start, size;
    if (gid == (long)getgid()) {
        return 0;
    }
    priv_uid_pool(&start, &size);
    if (gid >= start && gid <= start + size - 1) {
        return 0;
    }
    snprintf(err, errlen,
             "gid %ld is neither the worker's own gid (%ld) nor a member of "
             "the privileged helper uid pool %ld..%ld",
             gid, (long)getgid(), start, start + size - 1);
    return -1;
}

static const char *priv_workspace_base(void) {
    const char *value = getenv("E2B_WORKSPACE_BASE");
    return (value != NULL && *value != '\0') ? value : PRIV_DEFAULT_WORKSPACE_BASE;
}

/* N27: the platform's own records (``_runtime/<id>/sandbox.json``, the
 * checkpoint/snapshot stores, the uid pool) live under ``E2B_STATE_BASE``,
 * which is a *sibling* of the sandbox trees once the deployment names one.
 * Without the variable it *is* the workspace base -- one shape, one root. */
const char *priv_state_base(void) {
    const char *value = getenv("E2B_STATE_BASE");
    return (value != NULL && *value != '\0') ? value : priv_workspace_base();
}

/* A second root only when the deployment names one. Both the whitelist and the
 * diagnostic that reports it ask this, so "which roots do you accept?" has a
 * single answer. */
static int priv_has_second_state_root(void) {
    return strcmp(priv_state_base(), priv_workspace_base()) != 0;
}

static const char *priv_shared_volume_root(void) {
    const char *value = getenv("E2B_SHARED_VOLUME_ROOT");
    return (value != NULL && *value != '\0') ? value : NULL;
}

static void priv_append_root(char *out, size_t outlen, const char *root) {
    size_t used = strlen(out);
    if (used >= outlen) {
        return;
    }
    snprintf(out + used, outlen - used, ", %s", root);
}

void priv_roots_text(char *out, size_t outlen) {
    const char *workspace = priv_workspace_base();
    const char *shared = priv_shared_volume_root();

    snprintf(out, outlen, "%s", workspace);
    if (priv_has_second_state_root()) {
        priv_append_root(out, outlen, priv_state_base());
    }
    if (shared != NULL) {
        priv_append_root(out, outlen, shared);
    }
}

const char *priv_supervise_bin(void) {
    const char *value = getenv("E2B_SUPERVISE_BIN");
    return (value != NULL && *value != '\0') ? value : PRIV_DEFAULT_SUPERVISE_BIN;
}

/* len(prefix) == strlen(prefix); "equal or a proper subpath" test. */
static int priv_is_within(const char *path, const char *root, int strict) {
    size_t root_len = strlen(root);
    if (strcmp(path, root) == 0) {
        return strict ? 0 : 1;
    }
    if (strncmp(path, root, root_len) != 0) {
        return 0;
    }
    /* "/root/" exactly -- not "/root-suffix". */
    return root_len > 0 && root[root_len - 1] == '/'
               ? 1
               : path[root_len] == '/';
}

static int priv_check_root(const char *raw_root, const char *resolved,
                           int strict, int *matched, char *err,
                           size_t errlen) {
    char *root_real = realpath(raw_root, NULL);
    if (root_real == NULL) {
        /* An absent root cannot be escaped *into*; it simply matches nothing. */
        return 0;
    }
    *matched = priv_is_within(resolved, root_real, strict);
    free(root_real);
    (void)err;
    (void)errlen;
    return 0;
}

int priv_resolve_allowed_path(const char *path, int strict, char *resolved,
                              size_t resolved_len, char *err, size_t errlen) {
    char *real = realpath(path, NULL);
    int matched = 0;
    char roots[PRIV_ERR_LEN];
    if (real == NULL) {
        snprintf(err, errlen, "cannot resolve path %s: %s", path, strerror(errno));
        return -1;
    }
    if (strlen(real) + 1 > resolved_len) {
        snprintf(err, errlen, "resolved path is too long: %s", real);
        free(real);
        return -1;
    }
    priv_check_root(priv_workspace_base(), real, strict, &matched, err, errlen);
    if (!matched) {
        if (priv_has_second_state_root()) {
            priv_check_root(priv_state_base(), real, strict, &matched, err,
                            errlen);
        }
    }
    if (!matched) {
        const char *shared = priv_shared_volume_root();
        if (shared != NULL) {
            priv_check_root(shared, real, strict, &matched, err, errlen);
        }
    }
    if (!matched) {
        priv_roots_text(roots, sizeof(roots));
        snprintf(err, errlen,
                 "path %s is outside the privileged helper roots (%s)", path,
                 roots);
        free(real);
        return -1;
    }
    snprintf(resolved, resolved_len, "%s", real);
    free(real);
    return 0;
}

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

/* A positive-integer env override, or ``fallback`` when unset/empty. */
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

/* See priv_worker_uid()/priv_worker_gid() in priv_common.h. The override is a
 * decimal integer; 0 is legitimate (a root worker exists in the compose and
 * test shapes), anything else is a deployment defect, and falling back to
 * getuid()/getgid() on one would silently mean *root* -- which is exactly the
 * identity a `chown --worker` hands a tree to -- so this fails closed. */
static long priv_env_worker_identity(const char *name, long fallback) {
    const char *text = getenv(name);
    char *end = NULL;
    long value;
    if (text == NULL || *text == '\0') {
        return fallback;
    }
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < 0) {
        priv_fail("%s must be a non-negative decimal integer (got '%s')", name,
                  text);
    }
    return value;
}

long priv_worker_uid(void) {
    return priv_env_worker_identity("E2B_BROKER_WORKER_UID", (long)getuid());
}

long priv_worker_gid(void) {
    return priv_env_worker_identity("E2B_BROKER_WORKER_GID", (long)getgid());
}

int priv_gid_allowed(long gid, char *err, size_t errlen) {
    long start, size;
    long own = priv_worker_gid();
    if (gid == own) {
        return 0;
    }
    priv_uid_pool(&start, &size);
    if (gid >= start && gid <= start + size - 1) {
        return 0;
    }
    snprintf(err, errlen,
             "gid %ld is neither the worker's own gid (%ld) nor a member of "
             "the privileged helper uid pool %ld..%ld",
             gid, own, start, start + size - 1);
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

/* N57: the *node-local* platform state -- the create marker, the disk-stat
 * seed, ``.route-b`` and the uid pool's local files. A root of its own because
 * it is neither the shared state base (which the other node's worker reads) nor
 * the tree root (which the sandbox itself reaches): with the trees on
 * node-local disk the two are different media, and a path under this root would
 * otherwise be refused as "outside every root the helper may act on".
 *
 * Unset means the deployment names none -- no extra root, exactly like
 * ``E2B_SHARED_VOLUME_ROOT``. */
static const char *priv_node_state_base(void) {
    const char *value = getenv("E2B_NODE_STATE_BASE");
    return (value != NULL && *value != '\0') ? value : NULL;
}

static const char *priv_shared_volume_root(void) {
    const char *value = getenv("E2B_SHARED_VOLUME_ROOT");
    return (value != NULL && *value != '\0') ? value : NULL;
}

/* C1: the extracted-rootfs cache. A sandbox's secret file is written to
 * `<image_cache_dir>/secrets/<sandbox_id>/` and then handed to that sandbox's
 * own uid; the worker is not the owner of it, so the hand-over needs this root.
 * **Only** when the deployment names one (every real one does): the Python
 * side's unset value is `tmp/sandboxes/_images`, relative to *its* cwd, and
 * whitelisting a resolved-into-the-daemon's-cwd directory would both widen the
 * whitelist and make the two sides' root lists disagree. */
static const char *priv_image_cache(void) {
    const char *value = getenv("E2B_IMAGE_CACHE_DIR");
    return (value != NULL && *value != '\0') ? value : NULL;
}

size_t priv_root_paths(const char **out, size_t max) {
    /* The roots, in the order -- and with the same conditional entries -- as
     * the fleet's deployments name them: a consumer that reads the diagnostic
     * and one that walks the whitelist must not disagree. */
    const char *workspace = priv_workspace_base();
    const char *node_state = priv_node_state_base();
    const char *state = priv_state_base();
    const char *shared = priv_shared_volume_root();
    const char *cache = priv_image_cache();
    size_t count = 0, seen;
    if (count < max) {
        out[count++] = workspace;
    }
    /* N57: node-local platform state, right after the tree root. Compared
     * against the tree root only -- statement for statement what the Python
     * side's ``ControlPaths.roots`` does, so the two lists cannot drift. */
    if (node_state != NULL && strcmp(node_state, workspace) != 0 && count < max) {
        out[count++] = node_state;
    }
    /* A second root only when the state base *is* one: with no E2B_STATE_BASE
     * the two are the same directory, and naming one directory twice would
     * misreport the shape. */
    if (state != NULL && strcmp(state, workspace) != 0 && count < max) {
        out[count++] = state;
    }
    /* Named whenever the deployment names one -- even a shared root that
     * points at the workspace base is kept, because that is what the Python
     * side does and the two lists have to match. */
    if (shared != NULL && count < max) {
        out[count++] = shared;
    }
    /* C1's root, and the one entry with a dedupe of its own: every deployment
     * points it somewhere of its own, but two roots that resolve to the same
     * directory are still one root. */
    if (cache != NULL) {
        for (seen = 0; seen < count; seen++) {
            if (strcmp(out[seen], cache) == 0) {
                break;
            }
        }
        if (seen == count && count < max) {
            out[count++] = cache;
        }
    }
    return count;
}

/* The roots, as "a" or "a, b, c" -- what the refusal for a path outside all of
 * them names, so the operator sees which list to reconcile. */
void priv_roots_text(char *out, size_t outlen) {
    const char *roots[PRIV_MAX_ROOTS];
    size_t count = priv_root_paths(roots, PRIV_MAX_ROOTS);
    size_t index, used = 0;
    if (outlen == 0) {
        return;
    }
    out[0] = '\0';
    for (index = 0; index < count && used + 1 < outlen; index++) {
        int written = snprintf(out + used, outlen - used, "%s%s",
                               index == 0 ? "" : ", ", roots[index]);
        if (written < 0) {
            break;
        }
        used += (size_t)written;
        if (used >= outlen) {
            used = outlen - 1;
            break;
        }
    }
}

const char *priv_supervise_bin(void) {
    const char *value = getenv("E2B_SUPERVISE_BIN");
    return (value != NULL && *value != '\0') ? value : PRIV_DEFAULT_SUPERVISE_BIN;
}

/* The length of the sequence a lead byte starts, or 0 for a byte that cannot
 * start one (stranded continuation bytes, overlong leads, 5/6-byte forms). */

/* The length of the well-formed UTF-8 sequence at `data`, or 0 when the bytes
 * there are not one (RFC 3629: no overlongs, no surrogates, <= U+10FFFF). */

/* Append `text` to the buffer that ends at `out[outlen]`, or leave it as it
 * was when it does not fit (the caller sizes the buffer for the shape it
 * froze, so truncation is a bug, not a case to paper over). */

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
    const char *roots[PRIV_MAX_ROOTS];
    size_t root_count, index;
    if (real == NULL) {
        snprintf(err, errlen, "cannot resolve path %s: %s", path, strerror(errno));
        return -1;
    }
    if (strlen(real) + 1 > resolved_len) {
        snprintf(err, errlen, "resolved path is too long: %s", real);
        free(real);
        return -1;
    }
    root_count = priv_root_paths(roots, PRIV_MAX_ROOTS);
    for (index = 0; index < root_count && !matched; index++) {
        priv_check_root(roots[index], real, strict, &matched, err, errlen);
    }
    if (!matched) {
        char text[PRIV_ERR_LEN];
        priv_roots_text(text, sizeof(text));
        snprintf(err, errlen,
                 "path %s is outside the privileged helper roots (%s)", path,
                 text);
        free(real);
        return -1;
    }
    snprintf(resolved, resolved_len, "%s", real);
    free(real);
    return 0;
}

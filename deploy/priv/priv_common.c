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

void priv_report_refused(const char *message) {
    fprintf(stderr, "%s: refused: %s\n", priv_progname(), message);
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

/* The peer identity is the one place uid/gid 0 is legitimate (a root worker
 * exists in the compose/test shapes), so this is deliberately not
 * priv_parse_uid: "0" is a value here, not a refusal. */
static int priv_env_peer_id(const char *name, long fallback, long *out, char *err,
                            size_t errlen) {
    const char *text = getenv(name);
    char *end = NULL;
    long value;
    if (text == NULL) {
        *out = fallback;
        return 0;
    }
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value < 0) {
        snprintf(err, errlen, "%s must be a non-negative decimal integer (got '%s')",
                 name, text);
        return -1;
    }
    *out = value;
    return 0;
}

void priv_peer_identity(long *uid, long *gid) {
    long want_uid, want_gid;
    char err[PRIV_ERR_LEN];
    if (priv_env_peer_id("E2B_BROKER_PEER_UID", PRIV_DEFAULT_PEER_UID, &want_uid,
                         err, sizeof(err)) != 0 ||
        priv_env_peer_id("E2B_BROKER_PEER_GID", PRIV_DEFAULT_PEER_GID, &want_gid,
                         err, sizeof(err)) != 0) {
        /* A peer gate that cannot be read is a deployment defect, not a
         * request-level refusal: name it and refuse to serve at all. */
        priv_fail("invalid peer configuration: %s", err);
    }
    *uid = want_uid;
    *gid = want_gid;
}

int priv_peer_allowed(long uid, long gid, char *err, size_t errlen) {
    long want_uid, want_gid;
    priv_peer_identity(&want_uid, &want_gid);
    if (uid != want_uid) {
        snprintf(err, errlen, "peer uid %ld does not match E2B_BROKER_PEER_UID=%ld",
                 uid, want_uid);
        return -1;
    }
    if (gid != want_gid) {
        snprintf(err, errlen, "peer gid %ld does not match E2B_BROKER_PEER_GID=%ld",
                 gid, want_gid);
        return -1;
    }
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
    /* This is the Python side's list (PrivHelpers._root_paths) byte for byte,
     * including *which* of its entries are conditional: a consumer that reads
     * the diagnostic and one that walks the whitelist must not disagree, and
     * the hello handshake compares the two lists. */
    const char *workspace = priv_workspace_base();
    const char *state = priv_state_base();
    const char *shared = priv_shared_volume_root();
    const char *cache = priv_image_cache();
    size_t count = 0, seen;
    if (count < max) {
        out[count++] = workspace;
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

const char *priv_maint_bin(void) {
    const char *value = getenv("E2B_MAINT_BIN");
    return (value != NULL && *value != '\0') ? value : PRIV_DEFAULT_MAINT_BIN;
}

const char *priv_broker_socket(void) {
    const char *value = getenv("E2B_PRIV_HELPER_SOCKET");
    return (value != NULL && *value != '\0') ? value : PRIV_DEFAULT_BROKER_SOCKET;
}

size_t priv_json_escape(char *out, const char *data, size_t len) {
    static const char *hex = "0123456789abcdef";
    size_t index, used = 0;
    for (index = 0; index < len; index++) {
        unsigned char c = (unsigned char)data[index];
        switch (c) {
        case '"':
            out[used++] = '\\';
            out[used++] = '"';
            break;
        case '\\':
            out[used++] = '\\';
            out[used++] = '\\';
            break;
        case '\b':
        case '\f':
        case '\n':
        case '\r':
        case '\t':
            /* The short escapes, so a `walk` line stays readable on the wire. */
            out[used++] = '\\';
            out[used++] = c == '\b'   ? 'b'
                          : c == '\f' ? 'f'
                          : c == '\n' ? 'n'
                          : c == '\r' ? 'r'
                                      : 't';
            break;
        default:
            if (c < 0x20) {
                out[used++] = '\\';
                out[used++] = 'u';
                out[used++] = '0';
                out[used++] = '0';
                out[used++] = hex[(c >> 4) & 0xf];
                out[used++] = hex[c & 0xf];
            } else {
                /* Anything else goes through byte for byte: the protocol
                 * carries UTF-8, and re-encoding it here would mean inventing
                 * a charset the caller did not ask for. */
                out[used++] = (char)c;
            }
            break;
        }
    }
    return used;
}

/* Append `text` to the buffer that ends at `out[outlen]`, or leave it as it
 * was when it does not fit (the caller sizes the buffer for the shape it
 * froze, so truncation is a bug, not a case to paper over). */
static void priv_append_text(char *out, size_t outlen, size_t *used,
                             const char *text) {
    size_t text_len = strlen(text);
    if (*used + text_len + 1 > outlen) {
        return;
    }
    memcpy(out + *used, text, text_len);
    *used += text_len;
    out[*used] = '\0';
}

void priv_roots_json(char *out, size_t outlen) {
    const char *roots[PRIV_MAX_ROOTS];
    size_t count = priv_root_paths(roots, PRIV_MAX_ROOTS);
    size_t index, used = 0;
    /* A root is one path and the widest escape is 6 bytes per byte. */
    char escaped[6 * PATH_MAX];
    out[0] = '\0';
    priv_append_text(out, outlen, &used, "[");
    for (index = 0; index < count; index++) {
        size_t root_len = strlen(roots[index]);
        size_t escaped_len;
        /* A root that cannot fit in PATH_MAX can never match a resolved path
         * either (realpath() cannot produce one), so this is a deployment
         * defect rather than a shape to squeeze: answer with an empty array
         * instead of a truncated -- i.e. lying -- one. */
        if (root_len >= sizeof(escaped) / 6) {
            snprintf(out, outlen, "[]");
            return;
        }
        escaped_len = priv_json_escape(escaped, roots[index], root_len);
        escaped[escaped_len] = '\0';
        if (index > 0) {
            priv_append_text(out, outlen, &used, ",");
        }
        priv_append_text(out, outlen, &used, "\"");
        priv_append_text(out, outlen, &used, escaped);
        priv_append_text(out, outlen, &used, "\"");
    }
    priv_append_text(out, outlen, &used, "]");
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

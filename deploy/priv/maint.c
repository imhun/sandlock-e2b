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
 * link that makes the broker touch something outside the roots.
 *
 * usage:
 *   e2b-maint chown  (--uid U --gid G | --worker) [--recursive] --path P
 *   e2b-maint rm     --path P
 *   e2b-maint walk   --path P
 *   e2b-maint serve  [--socket P]          (C1) the node's socket broker
 *   e2b-maint ping   [--socket P]          (C1) one hello round-trip, exit 0/77
 *
 * C1 -- why the socket exists: this binary is the privileged thing in the
 * worker pod and the worker is not root. Each call so far *was* the broker
 * call (one `execv` per action by the Python transport). `serve` keeps one
 * root process per node and answers the frozen line-JSON protocol on
 * `/run/e2b-broker/broker.sock` (`E2B_PRIV_HELPER_SOCKET` overrides it):
 *
 *   {"v":1,"args":["chown","--uid","21000","--path","/..."],"timeout_s":300}
 *   -> {"v":1,"ok":true,"exit":0,"stdout":"...","stderr":"..."}
 *   {"v":1,"hello":true}
 *   -> {"v":1,"ok":true,"peer_uid":65534,"uid_pool":[10000,1000],"roots":[...]}
 *
 * `args` never carries argv[0]: the daemon execs **its own image**
 * (`/proc/self/exe`) with the request's argv, so the socket is the same broker
 * one hop further out -- never a general "run this as root" launcher. A request the
 * daemon refuses to run answers `ok:false`; a request the child refuses
 * answers with the child's own exit status (77 is the fail-closed refusal)
 * and its stderr, which is what the caller logs.
 *
 * Who may talk to it, and at what cost: the socket is `0660 root:<peer gid>`
 * (the same pair as `/var/lib/e2b-priv`, 0710), so a pool uid cannot even
 * `connect()`; the daemon then checks SO_PEERCRED **before it forks**, so an
 * unauthorized peer costs one refused answer and never a child. A fork error
 * or too many handlers (PRIV_MAX_HANDLERS) refuses that one connection and
 * leaves the daemon serving: nothing a local peer does may take the node's
 * broker down. That includes hanging up mid-answer: SIGPIPE is ignored and the
 * socket writes pass MSG_NOSIGNAL, because the refusal path writes from the
 * *parent* and a signal there would end the broker.
 *
 * Output is JSON, so every byte of it has to be. A path is not necessarily
 * UTF-8 (a Linux filename may be any byte but NUL and '/'), and one such name
 * must not cost the caller the whole response: an ill-formed byte is written
 * as `\udcXX` -- Python's surrogateescape -- so `json.loads` on the other side
 * produces exactly the string `os.fsdecode()` produced for the same name.
 */
#define _GNU_SOURCE

#include "priv_common.h"

#include <errno.h>
#include <fts.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* One request line and one response line, both bounded: the protocol is not a
 * stream of messages, so a caller that sends more than a line is refused
 * instead of parsed in pieces. */
#define PRIV_MAX_REQUEST (64 * 1024)
/* How much of an over-long request is read (and thrown away) before the
 * refusal is answered: a client that is still writing when the daemon closes
 * would get the kernel's RST instead of the answer. */
#define PRIV_DRAIN_LIMIT (4 * 1024 * 1024)
/* How long the *daemon* waits for the request of a connection it is refusing.
 * It is time the accept loop spends, so it is short: it only has to cover the
 * bytes already on their way down a local socket (which is what keeps the
 * answer from being destroyed by RST). */
#define PRIV_REFUSAL_WAIT_MS 50
/* Per stream, not per response: `walk` legitimately prints megabytes, and the
 * cap is where the daemon kills the producer. */
#define PRIV_MAX_OUTPUT ((unsigned long long)256 * 1024 * 1024)
#define PRIV_MAX_ARGS 32
/* Concurrent handlers. One process per in-flight request is the design, but a
 * number is still needed: without a cap, a *trusted* peer with a leak (or a
 * client that opens connections and says nothing) turns into unlimited forked
 * root processes. Over the cap the connection is refused by name, and the
 * daemon stays up. */
#define PRIV_MAX_HANDLERS 32
#define PRIV_DEFAULT_TIMEOUT_S 300L
#define PRIV_MAX_TIMEOUT_S 3600L
/* Resolved per child: the daemon only ever execs its own image. */
#define PRIV_SELF_EXE "/proc/self/exe"

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

/* ----------------------------------------------------------- serve / ping --
 *
 * The request is one line of JSON, so the daemon needs a JSON reader. A
 * library would be a new dependency inside a capability-carrying root binary,
 * so this is the smallest reader that accepts exactly the frozen shape and
 * refuses everything else: an unknown *value* is skipped, but an unknown
 * *structure* (a bare word, a float, a NUL escape, a truncated escape) is a
 * refusal, because those are the shapes that mean "this is not the protocol
 * the broker froze".
 */
struct json {
    const char *cur;
    const char *end;
};

struct priv_request {
    long version;
    int has_version;
    int hello;
    long timeout_s;
    int has_timeout;
    size_t nargs;
    char *args[PRIV_MAX_ARGS];
};

static void json_ws(struct json *j) {
    while (j->cur < j->end &&
           (*j->cur == ' ' || *j->cur == '\t' || *j->cur == '\r' ||
            *j->cur == '\n')) {
        j->cur++;
    }
}

static int json_error(char *err, size_t errlen, const char *what) {
    snprintf(err, errlen, "malformed request JSON: %s", what);
    return -1;
}

static int json_hex4(struct json *j, unsigned int *out) {
    unsigned int value = 0;
    int index;
    for (index = 0; index < 4; index++) {
        int digit;
        if (j->cur >= j->end) {
            return -1;
        }
        if (*j->cur >= '0' && *j->cur <= '9') {
            digit = *j->cur - '0';
        } else if (*j->cur >= 'a' && *j->cur <= 'f') {
            digit = *j->cur - 'a' + 10;
        } else if (*j->cur >= 'A' && *j->cur <= 'F') {
            digit = *j->cur - 'A' + 10;
        } else {
            return -1;
        }
        value = (value << 4) | (unsigned int)digit;
        j->cur++;
    }
    *out = value;
    return 0;
}

static size_t utf8_encode(char *out, unsigned int code) {
    if (code < 0x80) {
        out[0] = (char)code;
        return 1;
    }
    if (code < 0x800) {
        out[0] = (char)(0xc0 | (code >> 6));
        out[1] = (char)(0x80 | (code & 0x3f));
        return 2;
    }
    if (code < 0x10000) {
        out[0] = (char)(0xe0 | (code >> 12));
        out[1] = (char)(0x80 | ((code >> 6) & 0x3f));
        out[2] = (char)(0x80 | (code & 0x3f));
        return 3;
    }
    out[0] = (char)(0xf0 | (code >> 18));
    out[1] = (char)(0x80 | ((code >> 12) & 0x3f));
    out[2] = (char)(0x80 | ((code >> 6) & 0x3f));
    out[3] = (char)(0x80 | (code & 0x3f));
    return 4;
}

/* One JSON string, decoded. A decoded string is never longer than its source,
 * so the buffer is sized from the remaining input. */
static int json_string(struct json *j, char **out, size_t *out_len, char *err,
                       size_t errlen) {
    char *buffer;
    size_t used = 0;
    if (j->cur >= j->end || *j->cur != '"') {
        return json_error(err, errlen, "expected a string");
    }
    j->cur++;
    buffer = malloc((size_t)(j->end - j->cur) + 1);
    if (buffer == NULL) {
        snprintf(err, errlen, "out of memory reading the request");
        return -1;
    }
    while (j->cur < j->end && *j->cur != '"') {
        unsigned char c = (unsigned char)*j->cur++;
        if (c != '\\') {
            if (c < 0x20) {
                free(buffer);
                return json_error(err, errlen, "a raw control byte in a string");
            }
            buffer[used++] = (char)c;
            continue;
        }
        if (j->cur >= j->end) {
            break;
        }
        switch (*j->cur++) {
        case '"':
            buffer[used++] = '"';
            break;
        case '\\':
            buffer[used++] = '\\';
            break;
        case '/':
            buffer[used++] = '/';
            break;
        case 'b':
            buffer[used++] = '\b';
            break;
        case 'f':
            buffer[used++] = '\f';
            break;
        case 'n':
            buffer[used++] = '\n';
            break;
        case 'r':
            buffer[used++] = '\r';
            break;
        case 't':
            buffer[used++] = '\t';
            break;
        case 'u': {
            unsigned int code;
            if (json_hex4(j, &code) != 0) {
                free(buffer);
                return json_error(err, errlen, "a truncated \\u escape");
            }
            if (code >= 0xd800 && code <= 0xdbff) {
                unsigned int low;
                /* Python's json.dumps spells a non-ASCII path as a surrogate
                 * pair by default; anything else would arrive mangled. */
                if ((size_t)(j->end - j->cur) < 6 || j->cur[0] != '\\' ||
                    j->cur[1] != 'u') {
                    free(buffer);
                    return json_error(err, errlen, "a surrogate without its pair");
                }
                j->cur += 2;
                if (json_hex4(j, &low) != 0 || low < 0xdc00 || low > 0xdfff) {
                    free(buffer);
                    return json_error(err, errlen, "a surrogate without its pair");
                }
                code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
            } else if (code >= 0xdc80 && code <= 0xdcff) {
                /* Python's json.dumps spells a byte that is *not* UTF-8 as a
                 * lone low surrogate (`\udcXX`, surrogateescape). The argv
                 * entry has to carry that byte again, or a sandbox tree with
                 * such a name could never be torn down. A high surrogate plus
                 * a low one stays a real character (the branch above). */
                buffer[used++] = (char)(code & 0xff);
                break;
            } else if (code >= 0xdc00 && code <= 0xdfff) {
                free(buffer);
                return json_error(err, errlen,
                                  "a lone low surrogate that is not a byte");
            }
            if (code == 0) {
                /* A NUL cannot survive as an argv byte: the tail of that
                 * argument would be dropped silently, so refuse the shape. */
                free(buffer);
                return json_error(err, errlen, "a NUL escape in a string");
            }
            used += utf8_encode(buffer + used, code);
            break;
        }
        default:
            free(buffer);
            return json_error(err, errlen, "an unknown escape");
        }
    }
    if (j->cur >= j->end) {
        free(buffer);
        return json_error(err, errlen, "an unterminated string");
    }
    j->cur++; /* the closing quote */
    buffer[used] = '\0';
    *out = buffer;
    *out_len = used;
    return 0;
}

/* Integers only: the frozen shape has no floats, and rounding a timeout the
 * caller asked for is exactly the kind of quiet change this refuses. */
static int json_integer(struct json *j, long *out, char *err, size_t errlen) {
    int negative = 0;
    long value = 0;
    if (j->cur < j->end && (*j->cur == '-' || *j->cur == '+')) {
        negative = *j->cur == '-';
        j->cur++;
    }
    if (j->cur >= j->end || *j->cur < '0' || *j->cur > '9') {
        return json_error(err, errlen, "expected an integer");
    }
    while (j->cur < j->end && *j->cur >= '0' && *j->cur <= '9') {
        if (value > (LONG_MAX - 9) / 10) {
            return json_error(err, errlen, "an integer that does not fit");
        }
        value = value * 10 + (*j->cur - '0');
        j->cur++;
    }
    if (j->cur < j->end &&
        (*j->cur == '.' || *j->cur == 'e' || *j->cur == 'E')) {
        return json_error(err, errlen, "a non-integer number");
    }
    *out = negative ? -value : value;
    return 0;
}

static int json_literal(struct json *j, const char *text) {
    size_t len = strlen(text);
    if ((size_t)(j->end - j->cur) < len || strncmp(j->cur, text, len) != 0) {
        return -1;
    }
    j->cur += len;
    return 0;
}

static int json_skip_value(struct json *j, int depth, char *err,
                           size_t errlen) {
    char *text = NULL;
    size_t text_len = 0;
    if (depth > 8) {
        return json_error(err, errlen, "a value nested too deeply");
    }
    if (j->cur >= j->end) {
        return json_error(err, errlen, "a truncated value");
    }
    if (*j->cur == '"') {
        if (json_string(j, &text, &text_len, err, errlen) != 0) {
            return -1;
        }
        free(text);
        return 0;
    }
    if (*j->cur == '{' || *j->cur == '[') {
        char close = *j->cur == '{' ? '}' : ']';
        int object = *j->cur == '{';
        j->cur++;
        json_ws(j);
        if (j->cur < j->end && *j->cur == close) {
            j->cur++;
            return 0;
        }
        for (;;) {
            if (object) {
                if (json_string(j, &text, &text_len, err, errlen) != 0) {
                    return -1;
                }
                free(text);
                json_ws(j);
                if (j->cur >= j->end || *j->cur != ':') {
                    return json_error(err, errlen, "expected ':'");
                }
                j->cur++;
                json_ws(j);
            }
            if (json_skip_value(j, depth + 1, err, errlen) != 0) {
                return -1;
            }
            json_ws(j);
            if (j->cur < j->end && *j->cur == ',') {
                j->cur++;
                json_ws(j);
                continue;
            }
            if (j->cur < j->end && *j->cur == close) {
                j->cur++;
                return 0;
            }
            return json_error(err, errlen, "expected ',' or a closing bracket");
        }
    }
    if ((*j->cur == 't' && json_literal(j, "true") == 0) ||
        (*j->cur == 'f' && json_literal(j, "false") == 0) ||
        (*j->cur == 'n' && json_literal(j, "null") == 0)) {
        return 0;
    }
    if (*j->cur == 't' || *j->cur == 'f' || *j->cur == 'n') {
        return json_error(err, errlen, "an unknown value");
    }
    {
        long ignored = 0;
        return json_integer(j, &ignored, err, errlen);
    }
}

static int json_parse_args(struct json *j, struct priv_request *request,
                           char *err, size_t errlen) {
    char *value = NULL;
    size_t value_len = 0;
    json_ws(j);
    if (j->cur >= j->end || *j->cur != '[') {
        return json_error(err, errlen, "expected an array of strings");
    }
    j->cur++;
    json_ws(j);
    if (j->cur < j->end && *j->cur == ']') {
        j->cur++;
        return 0;
    }
    for (;;) {
        if (request->nargs >= PRIV_MAX_ARGS) {
            return json_error(err, errlen, "more arguments than the broker takes");
        }
        if (json_string(j, &value, &value_len, err, errlen) != 0) {
            return -1;
        }
        if (value_len == 0) {
            free(value);
            return json_error(err, errlen, "an empty argument");
        }
        request->args[request->nargs++] = value;
        json_ws(j);
        if (j->cur < j->end && *j->cur == ',') {
            j->cur++;
            json_ws(j);
            continue;
        }
        if (j->cur < j->end && *j->cur == ']') {
            j->cur++;
            return 0;
        }
        return json_error(err, errlen, "expected ',' or ']' in the argv list");
    }
}

static int json_parse_request(const char *data, size_t len,
                              struct priv_request *request, char *err,
                              size_t errlen) {
    struct json j;
    j.cur = data;
    j.end = data + len;
    json_ws(&j);
    if (j.cur >= j.end || *j.cur != '{') {
        return json_error(err, errlen, "expected an object");
    }
    j.cur++;
    for (;;) {
        char *key = NULL;
        size_t key_len = 0;
        json_ws(&j);
        if (j.cur < j.end && *j.cur == '}') {
            j.cur++;
            break;
        }
        if (json_string(&j, &key, &key_len, err, errlen) != 0) {
            return -1;
        }
        json_ws(&j);
        if (j.cur >= j.end || *j.cur != ':') {
            free(key);
            return json_error(err, errlen, "expected ':'");
        }
        j.cur++;
        json_ws(&j);
        if (strcmp(key, "v") == 0) {
            if (json_integer(&j, &request->version, err, errlen) != 0) {
                free(key);
                return -1;
            }
            request->has_version = 1;
        } else if (strcmp(key, "hello") == 0) {
            if (json_literal(&j, "true") == 0) {
                request->hello = 1;
            } else if (json_literal(&j, "false") != 0) {
                free(key);
                return json_error(err, errlen, "\"hello\" must be a boolean");
            }
        } else if (strcmp(key, "timeout_s") == 0) {
            if (json_integer(&j, &request->timeout_s, err, errlen) != 0) {
                free(key);
                return -1;
            }
            request->has_timeout = 1;
        } else if (strcmp(key, "args") == 0) {
            if (json_parse_args(&j, request, err, errlen) != 0) {
                free(key);
                return -1;
            }
        } else if (json_skip_value(&j, 0, err, errlen) != 0) {
            free(key);
            return -1;
        }
        free(key);
        json_ws(&j);
        if (j.cur < j.end && *j.cur == ',') {
            j.cur++;
            continue;
        }
        if (j.cur < j.end && *j.cur == '}') {
            j.cur++;
            break;
        }
        return json_error(err, errlen, "expected ',' or '}'");
    }
    json_ws(&j);
    if (j.cur != j.end) {
        return json_error(err, errlen, "trailing bytes after the request object");
    }
    return 0;
}

/* The response's boolean `name`, for `ping`'s exit code: anything that is not
 * a well-formed object carrying a boolean of that name is "not ok". */
static int json_find_bool(const char *data, size_t len, const char *name,
                          int *out) {
    struct json j;
    char error[PRIV_ERR_LEN];
    char *key = NULL;
    size_t key_len = 0;
    j.cur = data;
    j.end = data + len;
    json_ws(&j);
    if (j.cur >= j.end || *j.cur != '{') {
        return -1;
    }
    j.cur++;
    for (;;) {
        json_ws(&j);
        if (j.cur < j.end && *j.cur == '}') {
            return -1;
        }
        if (json_string(&j, &key, &key_len, error, sizeof(error)) != 0) {
            return -1;
        }
        json_ws(&j);
        if (j.cur >= j.end || *j.cur != ':') {
            free(key);
            return -1;
        }
        j.cur++;
        json_ws(&j);
        if (strcmp(key, name) == 0) {
            free(key);
            if (json_literal(&j, "true") == 0) {
                *out = 1;
                return 0;
            }
            if (json_literal(&j, "false") == 0) {
                *out = 0;
                return 0;
            }
            return -1;
        }
        free(key);
        if (json_skip_value(&j, 0, error, sizeof(error)) != 0) {
            return -1;
        }
        json_ws(&j);
        if (j.cur < j.end && *j.cur == ',') {
            j.cur++;
            continue;
        }
        return -1; /* the object ended without the key we are looking for */
    }
}

static void request_clear(struct priv_request *request) {
    size_t index;
    for (index = 0; index < request->nargs; index++) {
        free(request->args[index]);
    }
    request->nargs = 0;
}

/* --------------------------------------------------------------- writing -- */

struct output {
    char *data;
    size_t len;
    size_t cap;
    int overflow;
};

static int output_append(struct output *sink, const char *data, size_t len) {
    if (sink->overflow) {
        return 0; /* already over the cap: keep draining, keep discarding */
    }
    if (sink->len + len > PRIV_MAX_OUTPUT) {
        sink->overflow = 1;
        return 0;
    }
    if (sink->len + len + 1 > sink->cap) {
        size_t wanted = sink->len + len + 1;
        size_t cap = sink->cap ? sink->cap : 8192;
        char *grown;
        while (cap < wanted) {
            cap *= 2;
        }
        if (cap > PRIV_MAX_OUTPUT + 1) {
            cap = PRIV_MAX_OUTPUT + 1;
        }
        grown = realloc(sink->data, cap);
        if (grown == NULL) {
            return -1;
        }
        sink->data = grown;
        sink->cap = cap;
    }
    memcpy(sink->data + sink->len, data, len);
    sink->len += len;
    sink->data[sink->len] = '\0';
    return 0;
}

/* Everything or nothing: a half-written response would desynchronize a
 * protocol that has no framing of its own.
 *
 * `socket_fd` picks send(MSG_NOSIGNAL) over write(). The daemon ignores SIGPIPE
 * as well (see main), but the two together are what make "the peer hung up
 * mid-answer" an ordinary error here: a refusal is written by the *parent*, and
 * a signal that kills the parent takes the node's broker with it. */
static int sink_write_to(int fd, const char *data, size_t len, int socket_fd) {
    size_t used = 0;
    while (used < len) {
        ssize_t written = socket_fd
                              ? send(fd, data + used, len - used, MSG_NOSIGNAL)
                              : write(fd, data + used, len - used);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        used += (size_t)written;
    }
    return 0;
}

static int sink_write(int fd, const char *data, size_t len) {
    return sink_write_to(fd, data, len, 0);
}

static int conn_write(int fd, const char *data, size_t len) {
    return sink_write_to(fd, data, len, 1);
}

static int conn_write_literal(int fd, const char *literal) {
    return conn_write(fd, literal, strlen(literal));
}

/* `data` as a JSON string literal, escaped in chunks: a 256 MiB `walk` must
 * not need a second copy of itself to be answered. */
static int conn_write_json_string(int fd, const char *data, size_t len) {
    char escaped[6 * 4096];
    size_t used = 0;
    if (conn_write_literal(fd, "\"") != 0) {
        return -1;
    }
    while (used < len) {
        size_t chunk = len - used > 4096 ? 4096 : len - used;
        /* Never cut a UTF-8 sequence in half: the two halves would both look
         * like invalid bytes and come out as two surrogate escapes. */
        chunk = priv_json_escape_boundary(data + used, chunk);
        if (conn_write(fd, escaped, priv_json_escape(escaped, data + used,
                                                     chunk)) != 0) {
            return -1;
        }
        used += chunk;
    }
    return conn_write_literal(fd, "\"");
}

static void respond_error(int fd, const char *message) {
    if (conn_write_literal(fd, "{\"v\":1,\"ok\":false,\"error\":") != 0) {
        return;
    }
    if (conn_write_json_string(fd, message, strlen(message)) != 0) {
        return;
    }
    conn_write_literal(fd, "}\n");
}

static void respond_run(int fd, int code, const struct output *out,
                        const struct output *err_out) {
    char head[64];
    snprintf(head, sizeof(head), "{\"v\":1,\"ok\":true,\"exit\":%d,\"stdout\":",
             code);
    if (conn_write_literal(fd, head) != 0) {
        return;
    }
    if (conn_write_json_string(fd, out->data != NULL ? out->data : "", out->len) !=
        0) {
        return;
    }
    if (conn_write_literal(fd, ",\"stderr\":") != 0) {
        return;
    }
    if (conn_write_json_string(fd, err_out->data != NULL ? err_out->data : "",
                               err_out->len) != 0) {
        return;
    }
    conn_write_literal(fd, "}\n");
}

static void respond_hello(int fd, long peer_uid, long pool_start,
                          long pool_size) {
    const char *roots[PRIV_MAX_ROOTS];
    size_t count = priv_root_paths(roots, PRIV_MAX_ROOTS);
    size_t index, needed = 3; /* "[" + "]" + the terminator */
    char head[256];
    char *roots_json;
    for (index = 0; index < count; index++) {
        size_t root_len = strlen(roots[index]);
        if (root_len > PATH_MAX) {
            /* Fail closed and loud: the roots are what the caller validates
             * its own paths against, and a truncated list would lie. */
            respond_error(fd, "a whitelisted root is longer than PATH_MAX");
            return;
        }
        needed += 6 * root_len + 3; /* both quotes and a comma */
    }
    roots_json = malloc(needed);
    if (roots_json == NULL) {
        respond_error(fd, "out of memory building the hello response");
        return;
    }
    priv_roots_json(roots_json, needed);
    snprintf(head, sizeof(head),
             "{\"v\":1,\"ok\":true,\"peer_uid\":%ld,\"uid_pool\":[%ld,%ld],"
             "\"roots\":",
             peer_uid, pool_start, pool_size);
    if (conn_write_literal(fd, head) == 0 &&
        conn_write_literal(fd, roots_json) == 0) {
        conn_write_literal(fd, "}\n");
    }
    free(roots_json);
}

/* ------------------------------------------------------------------ exec -- */

static long long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int exit_code_from_status(int status) {
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 255;
}

/* Run one request in a grandchild that is *this* image, and collect its two
 * streams. Returns 0 with `*code` set when the child ran to completion, and
 * -1 with `message` set when the daemon itself had to stop it (timeout, output
 * cap, a broken pipe): those are the cases the caller must not read as an exit
 * status it can interpret. */
static int run_child(char *const *args, long timeout_s, struct output *out,
                     struct output *err_out, int *code, char *message,
                     size_t message_len) {
    int out_pipe[2];
    int err_pipe[2];
    struct pollfd fds[2];
    char chunk[65536];
    pid_t pid;
    long long deadline;
    int status = 0;
    int killed = 0;
    int timed_out = 0;
    int open_fds = 0;
    int index;

    if (pipe(out_pipe) != 0) {
        snprintf(message, message_len, "cannot open the stdout pipe: %s",
                 strerror(errno));
        return -1;
    }
    if (pipe(err_pipe) != 0) {
        close(out_pipe[0]);
        close(out_pipe[1]);
        snprintf(message, message_len, "cannot open the stderr pipe: %s",
                 strerror(errno));
        return -1;
    }
    pid = fork();
    if (pid < 0) {
        close(out_pipe[0]);
        close(out_pipe[1]);
        close(err_pipe[0]);
        close(err_pipe[1]);
        snprintf(message, message_len, "fork() failed: %s", strerror(errno));
        return -1;
    }
    if (pid == 0) {
        /* The grandchild: the two pipes are its stdout/stderr and the program
         * is *this* image -- resolved for the child itself, so nothing can be
         * swapped in between the daemon's check and this exec. */
        close(out_pipe[0]);
        close(err_pipe[0]);
        if (dup2(out_pipe[1], STDOUT_FILENO) < 0 ||
            dup2(err_pipe[1], STDERR_FILENO) < 0) {
            _exit(PRIV_EXIT_SYSTEM);
        }
        close(out_pipe[1]);
        close(err_pipe[1]);
        execv(PRIV_SELF_EXE, args);
        _exit(PRIV_EXIT_SYSTEM);
    }
    close(out_pipe[1]);
    close(err_pipe[1]);
    fds[0].fd = out_pipe[0];
    fds[0].events = POLLIN;
    fds[0].revents = 0;
    fds[1].fd = err_pipe[0];
    fds[1].events = POLLIN;
    fds[1].revents = 0;
    open_fds = 2;
    deadline = now_ms() + timeout_s * 1000;
    while (open_fds > 0) {
        long long remaining = deadline - now_ms();
        int poll_ms;
        int ready;
        if (remaining < 0) {
            remaining = 0;
        }
        poll_ms = remaining > INT_MAX ? INT_MAX : (int)remaining;
        ready = poll(fds, 2, poll_ms);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            snprintf(message, message_len, "poll() failed: %s", strerror(errno));
            kill(pid, SIGKILL);
            waitpid(pid, &status, 0);
            for (index = 0; index < 2; index++) {
                if (fds[index].fd >= 0) {
                    close(fds[index].fd);
                    fds[index].fd = -1;
                }
            }
            return -1;
        }
        if (ready == 0) {
            if (timed_out) {
                /* SIGKILL did not close the pipes (something deeper is still
                 * holding them): stop waiting and answer. */
                break;
            }
            timed_out = 1;
            kill(pid, SIGKILL);
            deadline = now_ms() + 5000;
            continue;
        }
        for (index = 0; index < 2; index++) {
            struct output *sink = index == 0 ? out : err_out;
            ssize_t got;
            if (fds[index].fd < 0) {
                continue;
            }
            if ((fds[index].revents & (POLLIN | POLLHUP | POLLERR | POLLNVAL)) ==
                0) {
                continue;
            }
            got = read(fds[index].fd, chunk, sizeof(chunk));
            if (got > 0) {
                if (output_append(sink, chunk, (size_t)got) != 0) {
                    snprintf(message, message_len,
                             "out of memory buffering the child output");
                    kill(pid, SIGKILL);
                    waitpid(pid, &status, 0);
                    for (index = 0; index < 2; index++) {
                        if (fds[index].fd >= 0) {
                            close(fds[index].fd);
                            fds[index].fd = -1;
                        }
                    }
                    return -1;
                }
                if (sink->overflow && !killed) {
                    /* The cap is a limit, not a target: stop the producer the
                     * moment it crosses it instead of accumulating it. */
                    killed = 1;
                    kill(pid, SIGKILL);
                    deadline = now_ms() + 5000;
                }
                continue;
            }
            if (got < 0 && errno == EINTR) {
                continue;
            }
            close(fds[index].fd);
            fds[index].fd = -1;
            open_fds--;
        }
    }
    for (index = 0; index < 2; index++) {
        if (fds[index].fd >= 0) {
            close(fds[index].fd);
            fds[index].fd = -1;
        }
    }
    waitpid(pid, &status, 0);
    if (timed_out) {
        snprintf(message, message_len,
                 "the request timed out after %lds and was killed with SIGKILL",
                 timeout_s);
        return -1;
    }
    if (killed) {
        snprintf(message, message_len,
                 "%s exceeded the 268435456-byte output cap and was killed",
                 out->overflow ? "stdout" : "stderr");
        return -1;
    }
    *code = exit_code_from_status(status);
    return 0;
}

/* --------------------------------------------------------------- reading -- */

/* One line, at most PRIV_MAX_REQUEST bytes. An over-long line keeps being read
 * (and thrown away) so the refusal arrives as a response instead of as the
 * kernel's RST on a connection closed with unread input. */
static int read_request(int fd, char **out, size_t *out_len, char *err,
                        size_t errlen) {
    char scratch[4096];
    char *buffer = NULL;
    size_t len = 0, cap = 0, drained = 0;
    int over_limit = 0;
    for (;;) {
        ssize_t got = read(fd, scratch, sizeof(scratch));
        char *newline;
        size_t chunk;
        if (got < 0) {
            if (errno == EINTR) {
                continue;
            }
            free(buffer);
            snprintf(err, errlen, "reading the request failed: %s",
                     strerror(errno));
            return -1;
        }
        if (got == 0) {
            break; /* EOF ends the line too */
        }
        newline = memchr(scratch, '\n', (size_t)got);
        chunk = newline != NULL ? (size_t)(newline - scratch) : (size_t)got;
        if (!over_limit) {
            if (len + chunk > (size_t)PRIV_MAX_REQUEST) {
                over_limit = 1;
            } else {
                if (len + chunk + 1 > cap) {
                    size_t wanted = len + chunk + 1;
                    size_t next = cap != 0 ? cap : 4096;
                    char *grown;
                    while (next < wanted) {
                        next *= 2;
                    }
                    grown = realloc(buffer, next);
                    if (grown == NULL) {
                        free(buffer);
                        snprintf(err, errlen, "out of memory reading the request");
                        return -1;
                    }
                    buffer = grown;
                    cap = next;
                }
                memcpy(buffer + len, scratch, chunk);
                len += chunk;
            }
        }
        if (newline != NULL) {
            break;
        }
        if (over_limit) {
            drained += (size_t)got;
            if (drained > PRIV_DRAIN_LIMIT) {
                break;
            }
        }
    }
    if (over_limit) {
        free(buffer);
        snprintf(err, errlen,
                 "the request line is longer than the %d-byte limit",
                 PRIV_MAX_REQUEST);
        return -1;
    }
    if (len == 0) {
        free(buffer);
        snprintf(err, errlen, "empty request");
        return -1;
    }
    buffer[len] = '\0';
    *out = buffer;
    *out_len = len;
    return 0;
}

/* ------------------------------------------------------------- the verbs -- */

static const char *socket_from_args(int argc, char **argv, int start) {
    const char *socket_path = NULL;
    int index;
    for (index = start; index < argc; index++) {
        if (strcmp(argv[index], "--socket") == 0) {
            if (index + 1 >= argc) {
                priv_usage("--socket needs a value");
            }
            socket_path = argv[++index];
            continue;
        }
        if (strncmp(argv[index], "--socket=", 9) == 0 && argv[index][9] != '\0') {
            socket_path = argv[index] + 9;
            continue;
        }
        priv_usage("unexpected argument '%s'", argv[index]);
    }
    return socket_path != NULL ? socket_path : priv_broker_socket();
}

static int request_timeout(const struct priv_request *request, char *err,
                           size_t errlen, long *out) {
    if (!request->has_timeout) {
        *out = PRIV_DEFAULT_TIMEOUT_S;
        return 0;
    }
    if (request->timeout_s <= 0) {
        snprintf(err, errlen, "timeout_s must be positive (got %ld)",
                 request->timeout_s);
        return -1;
    }
    /* Clamped, not refused: the caller naming an hour is not a protocol
     * violation, it is a request this broker will not wait longer than 1h for. */
    *out = request->timeout_s > PRIV_MAX_TIMEOUT_S ? PRIV_MAX_TIMEOUT_S
                                                   : request->timeout_s;
    return 0;
}

static void respond_to_request(int fd, long peer_uid) {
    struct priv_request request;
    struct output out, err_out;
    char err[PRIV_ERR_LEN];
    char *line = NULL;
    char *argv[PRIV_MAX_ARGS + 2];
    size_t line_len = 0;
    long timeout_s = PRIV_DEFAULT_TIMEOUT_S;
    long pool_start, pool_size;
    int code = 0;
    size_t index;

    memset(&request, 0, sizeof(request));
    memset(&out, 0, sizeof(out));
    memset(&err_out, 0, sizeof(err_out));
    if (read_request(fd, &line, &line_len, err, sizeof(err)) != 0) {
        goto refuse;
    }
    if (json_parse_request(line, line_len, &request, err, sizeof(err)) != 0) {
        goto refuse;
    }
    if (!request.has_version || request.version != 1) {
        snprintf(err, sizeof(err), "unsupported protocol version %ld (expected 1)",
                 request.version);
        goto refuse;
    }
    if (request.hello) {
        priv_uid_pool(&pool_start, &pool_size);
        respond_hello(fd, peer_uid, pool_start, pool_size);
        goto done;
    }
    if (request.nargs == 0) {
        snprintf(err, sizeof(err), "the request carries no argv");
        goto refuse;
    }
    if (strcmp(request.args[0], "chown") != 0 &&
        strcmp(request.args[0], "rm") != 0 &&
        strcmp(request.args[0], "walk") != 0) {
        /* The socket is this broker one hop further out, not a launcher. */
        snprintf(err, sizeof(err),
                 "args[0] '%s' is not one of the privileged helper verbs "
                 "(chown, rm, walk)",
                 request.args[0]);
        goto refuse;
    }
    if (request_timeout(&request, err, sizeof(err), &timeout_s) != 0) {
        goto refuse;
    }
    argv[0] = (char *)"e2b-maint";
    for (index = 0; index < request.nargs; index++) {
        argv[index + 1] = request.args[index];
    }
    argv[request.nargs + 1] = NULL;
    if (run_child(argv, timeout_s, &out, &err_out, &code, err, sizeof(err)) != 0) {
        goto refuse;
    }
    respond_run(fd, code, &out, &err_out);
    goto done;

refuse:
    priv_report_refused(err);
    respond_error(fd, err);
done:
    request_clear(&request);
    free(line);
    free(out.data);
    free(err_out.data);
}

/* The client sends exactly one line, but a refusal can be answered *before*
 * that line was read (the peer gate runs first, and it must). Closing a socket
 * that still holds unread input makes the kernel send RST, and RST destroys
 * the answer that was already written -- the caller would see "connection
 * reset" instead of the refusal. So: finish the output (FIN), read what is in
 * flight for a bounded while, and only then let the handler exit. */
static void finish_connection(int fd) {
    struct pollfd waiter;
    char scratch[4096];
    /* Short on purpose: what is being drained is a request that was already
     * sent down a local socket, so anything still in flight arrives at once.
     * A client that sits on an answered connection must not pin a handler. */
    long long deadline = now_ms() + 500;
    shutdown(fd, SHUT_WR);
    for (;;) {
        long long remaining = deadline - now_ms();
        ssize_t got;
        if (remaining <= 0) {
            break;
        }
        waiter.fd = fd;
        waiter.events = POLLIN;
        waiter.revents = 0;
        if (poll(&waiter, 1, (int)remaining) <= 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        got = read(fd, scratch, sizeof(scratch));
        if (got > 0) {
            continue; /* discarded on purpose: the request was refused */
        }
        if (got < 0 && errno == EINTR) {
            continue;
        }
        break; /* EOF (or a dead peer) */
    }
}

/* The peer gate for one accepted connection: SO_PEERCRED is fixed at connect
 * time, so this is the same answer in the daemon and in the handler that
 * inherits the fd. */
static int peer_gate(int fd, long *peer_uid, char *err, size_t errlen) {
    struct ucred peer;
    socklen_t peer_len = sizeof(peer);

    memset(&peer, 0, sizeof(peer));
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_len) != 0) {
        snprintf(err, errlen, "cannot read the peer credentials: %s",
                 strerror(errno));
        return -1;
    }
    if (priv_peer_allowed((long)peer.uid, (long)peer.gid, err, errlen) != 0) {
        return -1;
    }
    *peer_uid = (long)peer.uid;
    return 0;
}

/* Answer a refusal the daemon itself decided on and let the connection go,
 * **without ever forking for it**.
 *
 * The drain has to happen before the close (closing a socket that still holds
 * unread input makes the kernel send RST, and the RST destroys the refusal
 * that was just written), but it must not be able to hold the accept loop
 * either: an unprivileged peer that is allowed to `connect()` because of the
 * socket's mode could otherwise stall every other caller. So: one bounded wait
 * for the *first* byte (PRIV_REFUSAL_WAIT_MS), then non-blocking reads until
 * the request that is already in flight has been consumed -- never a wait for
 * more than that. */
static void refuse_connection(int fd, const char *message) {
    struct pollfd waiter;
    char scratch[4096];
    size_t drained = 0;
    respond_error(fd, message);
    shutdown(fd, SHUT_WR);
    waiter.fd = fd;
    waiter.events = POLLIN;
    waiter.revents = 0;
    while (drained < PRIV_DRAIN_LIMIT &&
           poll(&waiter, 1, PRIV_REFUSAL_WAIT_MS) > 0) {
        ssize_t got = recv(fd, scratch, sizeof(scratch), MSG_DONTWAIT);
        if (got > 0) {
            drained += (size_t)got;
            while (drained < PRIV_DRAIN_LIMIT &&
                   (got = recv(fd, scratch, sizeof(scratch), MSG_DONTWAIT)) > 0) {
                drained += (size_t)got;
            }
            break;
        }
        if (got < 0 && errno == EINTR) {
            continue;
        }
        break; /* EOF */
    }
    close(fd);
}

static void handle_connection(int fd) {
    long peer_uid = -1;
    char err[PRIV_ERR_LEN];

    /* SIGPIPE is already ignored (main) and the socket writes pass
     * MSG_NOSIGNAL, so a client that hangs up mid-answer makes those writes
     * return -1 instead of killing this handler. */
    /* Depth in defence: the daemon gated this connection before it forked, and
     * a handler must not serve a peer the daemon would have refused. */
    if (peer_gate(fd, &peer_uid, err, sizeof(err)) != 0) {
        priv_report_refused(err);
        respond_error(fd, err);
        finish_connection(fd);
        return;
    }
    respond_to_request(fd, peer_uid);
    finish_connection(fd);
}

/* The daemon: one root process per node. A handler per connection, and the
 * handler forks the grandchild that execs this same image, so the accepted fd
 * (and with it SO_PEERCRED) stays in a process that never becomes a sandbox
 * uid. */
static int serve(const char *socket_path) {
    struct sockaddr_un addr;
    char exe[PATH_MAX];
    char err[PRIV_ERR_LEN];
    long peer_uid, peer_gid, pool_start, pool_size;
    int live_handlers = 0;
    int fd;

    if (socket_path == NULL || *socket_path == '\0') {
        priv_fail("the broker socket path is empty");
    }
    /* Read the two gates the handler will apply *now*: a deployment that
     * cannot name its peer or its uid pool has to refuse to serve, not answer
     * every request with a message the caller cannot act on. */
    priv_peer_identity(&peer_uid, &peer_gid);
    priv_uid_pool(&pool_start, &pool_size);
    (void)peer_uid;
    (void)pool_start;
    (void)pool_size;
    if (strlen(socket_path) >= sizeof(addr.sun_path)) {
        priv_fail("the broker socket path is too long (max %zu bytes): %s",
                  sizeof(addr.sun_path) - 1, socket_path);
    }
    /* Fail closed at startup: a broker that is not the installed one is a
     * deployment defect (a copy of the binary with the file capability, in a
     * sandbox-reachable place) and must be named, never served from. The path
     * is fixed at compile time on purpose -- an environment variable naming
     * where the broker "really" lives would only move the trust, not the
     * check. */
    if (realpath(PRIV_SELF_EXE, exe) == NULL) {
        priv_fail("cannot resolve %s: %s", PRIV_SELF_EXE, strerror(errno));
    }
    if (strcmp(PRIV_DEFAULT_MAINT_BIN, exe) != 0) {
        priv_fail(
            "this broker must run from the installed path %s, but the running "
            "image is %s: not serving from a copy",
            PRIV_DEFAULT_MAINT_BIN, exe);
    }
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        priv_fail("socket() failed: %s", strerror(errno));
    }
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path);
    /* What a previous incarnation left behind; without this, every restart
     * would fail with EADDRINUSE forever. */
    if (unlink(socket_path) != 0 && errno != ENOENT) {
        priv_fail("cannot remove the stale socket %s: %s", socket_path,
                  strerror(errno));
    }
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        priv_fail("bind %s failed: %s", socket_path, strerror(errno));
    }
    /* Owner + peer group only (root:<worker gid>, the same pair the broker
     * directory uses): a pool uid cannot even `connect()`, which is the point
     * of a gate that must not be reachable by a tenant. SO_PEERCRED stays the
     * real check; this is the layer that keeps unauthorized traffic out of the
     * accept loop entirely. */
    if (chmod(socket_path, 0660) != 0) {
        priv_fail("chmod %s failed: %s", socket_path, strerror(errno));
    }
    if (chown(socket_path, 0, (gid_t)peer_gid) != 0) {
        priv_fail("chown %s to 0:%ld failed: %s", socket_path, peer_gid,
                  strerror(errno));
    }
    if (listen(fd, 16) != 0) {
        priv_fail("listen %s failed: %s", socket_path, strerror(errno));
    }
    fprintf(stderr, "%s: serving on %s\n", priv_progname(), socket_path);
    for (;;) {
        int conn;
        pid_t pid;
        while (waitpid(-1, NULL, WNOHANG) > 0) {
            /* Reap finished handlers: a node broker is long-lived, and a
             * zombie per request would be the leak. */
            if (live_handlers > 0) {
                live_handlers--;
            }
        }
        conn = accept4(fd, NULL, NULL, SOCK_CLOEXEC);
        if (conn < 0) {
            if (errno == EINTR) {
                continue;
            }
            priv_fail("accept() on %s failed: %s", socket_path, strerror(errno));
        }
        /* The gate runs here, in the daemon, *before* fork(): an unauthorized
         * local uid must not be able to make the root broker fork at all, and
         * reading the credential needs no child. */
        if (peer_gate(conn, &peer_uid, err, sizeof(err)) != 0) {
            priv_report_refused(err);
            refuse_connection(conn, err);
            continue;
        }
        if (live_handlers >= PRIV_MAX_HANDLERS) {
            snprintf(err, sizeof(err),
                     "the broker is already serving %d requests",
                     PRIV_MAX_HANDLERS);
            priv_report_refused(err);
            refuse_connection(conn, err);
            continue;
        }
        pid = fork();
        if (pid < 0) {
            /* EAGAIN/ENOMEM: refuse *this* connection. A fork failure is not a
             * reason to take the node's broker -- and with it every other
             * caller -- down. */
            snprintf(err, sizeof(err),
                     "cannot fork a handler for the connection: %s",
                     strerror(errno));
            priv_report_refused(err);
            refuse_connection(conn, err);
            continue;
        }
        if (pid == 0) {
            close(fd);
            handle_connection(conn);
            _exit(PRIV_EXIT_OK);
        }
        live_handlers++;
        close(conn);
    }
}

/* The probe: connect, say hello, print the answer verbatim and report it as an
 * exit code, so a liveness check needs no client at all. */
static int ping(const char *socket_path) {
    static const char hello[] = "{\"v\":1,\"hello\":true}\n";
    struct sockaddr_un addr;
    char *line = NULL;
    size_t line_len = 0;
    char err[PRIV_ERR_LEN];
    int found;
    int ok = 0;
    int fd;

    if (socket_path == NULL || *socket_path == '\0') {
        priv_fail("the broker socket path is empty");
    }
    if (strlen(socket_path) >= sizeof(addr.sun_path)) {
        priv_fail("the broker socket path is too long (max %zu bytes): %s",
                  sizeof(addr.sun_path) - 1, socket_path);
    }
    fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        priv_fail("socket() failed: %s", strerror(errno));
    }
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", socket_path);
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        priv_fail("cannot connect to the broker socket %s: %s", socket_path,
                  strerror(errno));
    }
    if (conn_write(fd, hello, sizeof(hello) - 1) != 0) {
        close(fd);
        priv_fail("cannot send the hello to %s: %s", socket_path,
                  strerror(errno));
    }
    if (read_request(fd, &line, &line_len, err, sizeof(err)) != 0) {
        close(fd);
        priv_fail("cannot read the hello response from %s: %s", socket_path, err);
    }
    close(fd);
    if (sink_write(STDOUT_FILENO, line, line_len) != 0 ||
        sink_write(STDOUT_FILENO, "\n", 1) != 0) {
        free(line);
        priv_fail("cannot write the hello response: %s", strerror(errno));
    }
    found = json_find_bool(line, line_len, "ok", &ok);
    free(line);
    return found == 0 && ok ? PRIV_EXIT_OK : PRIV_EXIT_REFUSED;
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
     * executed from here. A peer that hangs up mid-answer makes a write fail
     * with EPIPE, and the default disposition would kill whoever is writing --
     * which for `serve` is the *parent*, i.e. the node's broker. Handlers and
     * the exec'd child inherit this; the socket writes also pass
     * MSG_NOSIGNAL, so the crash needs both "ignored" and "done by hand" to be
     * wrong at once. */
    signal(SIGPIPE, SIG_IGN);
    if (argc < 2) {
        priv_usage("expected chown|rm|walk|serve|ping");
    }
    verb = argv[1];
    /* serve/ping take their own (socket-shaped) arguments; the maintenance
     * verbs' flag parser below has no meaning for either. */
    if (strcmp(verb, "serve") == 0) {
        return serve(socket_from_args(argc, argv, 2));
    }
    if (strcmp(verb, "ping") == 0) {
        return ping(socket_from_args(argc, argv, 2));
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

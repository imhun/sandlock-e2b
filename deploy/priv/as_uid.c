/* SPDX-License-Identifier: Apache-2.0
 *
 * as_uid -- face A of the C3 per-node agent: hand ONE identity to ONE pid.
 *
 * The worker forks the slot's process C, C calls `unshare(CLONE_NEWUSER)`
 * (both of those unprivileged, and both on the *worker* side of the fence),
 * and this program -- the only identity grantor in the system -- writes the
 * identity mapping `X X 1` into C's `/proc/<pid>/uid_map` and `gid_map`. The
 * pid given to it is C's **container** pid; turning that into the host pid is
 * the caller's job (Task 3's rendezvous: `NSpid` plus the worker pod's cgroup).
 *
 * Why that write works at all from a process that is neither root nor a
 * parent, and why it is the *cheap* way: `map_write()` refuses a writer that
 * does not hold CAP_SYS_ADMIN **in the target namespace**, and
 * `cap_capable()`'s owner rule hands that capability for free to a process
 * whose euid is the target namespace's owner -- the worker's 65534, which is
 * exactly what C unshared as. What is *not* free is naming a uid: that needs
 * `cap_setuid`/`cap_setgid`, which this binary carries as **file
 * capabilities** (`cap_setuid,cap_setgid+ep`). No root, no CAP_SYS_ADMIN, no
 * CAP_SYS_PTRACE. The measured matrix (and the failed arms that ruled the
 * other shapes out) is docs/c3-privilege-relocation.md §14.2.7.
 *
 * It is deliberately not a general "write a map" tool. The only mapping it can
 * produce is the identity one, for a uid from the configured pool, and every
 * way out of that shape is refused by name **before** anything is written:
 *
 *   1. the uid is outside `E2B_UID_POOL_START..+SIZE`, or is 0
 *      (`priv_validate_uid`, the same function both brokers use);
 *   2. the target's map already carries a mapping -- a user namespace's map is
 *      written exactly once, so a non-empty one means somebody else already
 *      granted this pid an identity;
 *   3. the target has not unshared at all: its map is the initial namespace's
 *      full range, so there is no new namespace to map;
 *   4. the bytes about to be handed to the kernel are not the identity
 *      `X X 1` that rule fixes -- checked on the write path, so a formatting
 *      bug becomes a refusal instead of a non-identity grant.
 *
 * Both maps are read and judged *before* either is written, so a target in a
 * bad state is refused without being half-granted. If the gid_map write does
 * fail after the uid_map write landed (`ENOSPC` is the realistic one), the
 * refusal says so and the pid has to be abandoned: a written map is permanent
 * and the uid half is already an identity grant.
 *
 * The read is a *pre*-check and not the guarantee. The guarantee is the
 * kernel's own "a namespace's map is written exactly once": a second grant
 * that races this one loses inside `map_write()` (`EPERM`) and is refused by
 * the write below, never silently absorbed.
 *
 * usage: as_uid --uid X --pid N
 * success prints exactly one line on stdout: C3-ASUID-OK pid=N uid=X
 *
 * `AS_UID_NO_MAIN` builds the pure decisions without the binary's `main`, the
 * shape `maint.c` already uses for A7's walk cap: tests/unit/test_priv_as_uid.py
 * drives the four rules on the host (no /proc, no CLONE_NEWUSER, no
 * capability), and the container lane pins the kernel-facing half. The switch
 * is absent from the shipped binary.
 */
#define _GNU_SOURCE

#include "priv_common.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

/* The initial user namespace's own map, as a reader inside it sees it:
 * `cat /proc/self/uid_map` -> "         0          0 4294967295". A pid whose
 * map reads like this is in the *initial* namespace, i.e. it never unshared
 * (refusal 3). */
#define AS_UID_INITIAL_FIRST 0L
#define AS_UID_INITIAL_LOWER 0L
#define AS_UID_INITIAL_COUNT 4294967295L
/* The kernel's own ceiling for any single field of a map (u32). */
#define AS_UID_ID_MAX 4294967295L

/* A map file is a handful of short lines; anything longer is not a shape this
 * program is willing to judge, and reading a prefix of it would be exactly the
 * kind of "parse what you can" this binary must not do. */
#define AS_UID_MAP_MAX 4096
/* How many extents are read before the map stops being "a map this program can
 * read". Real identities here are single-extent; the cap only bounds the work. */
#define AS_UID_MAX_EXTENTS 8

#define AS_UID_LINE_MAX 64
#define AS_UID_TEXT_MAX 256

enum as_uid_map_state {
    /* Empty: unshared and not yet mapped. The one state face A writes into. */
    AS_UID_MAP_READY = 0,
    /* The initial namespace's full range: not unshared (refusal 3). */
    AS_UID_MAP_INITIAL,
    /* Somebody already wrote an identity here (refusal 2). */
    AS_UID_MAP_WRITTEN,
    /* Not a map this program can read: refused, never treated as empty. */
    AS_UID_MAP_UNREADABLE,
};

/* Whitespace runs collapse to one space, ends trimmed: the form the refusals
 * quote a map in, because the kernel pads every field to ten columns and the
 * message is read by a human. */
static void as_uid_normalize(const char *text, char *out, size_t outlen) {
    size_t written = 0;
    int pending_space = 0;
    for (; *text != '\0'; text++) {
        if (*text == ' ' || *text == '\t' || *text == '\n' || *text == '\r') {
            pending_space = written > 0;
            continue;
        }
        if (pending_space && written + 1 < outlen) {
            out[written++] = ' ';
            pending_space = 0;
        }
        if (written + 1 >= outlen) {
            break;
        }
        out[written++] = *text;
    }
    out[written < outlen ? written : outlen - 1] = '\0';
}

/* One non-negative decimal field of a map, bounded by the kernel's u32 range.
 * Returns 0 and advances `*cursor` on success. */
static int as_uid_next_field(const char **cursor, long *out) {
    const char *p = *cursor;
    char *end = NULL;
    long value;
    while (*p == ' ' || *p == '\t') {
        p++;
    }
    if (*p < '0' || *p > '9') {
        return -1;
    }
    errno = 0;
    value = strtol(p, &end, 10);
    if (errno != 0 || end == p || value > AS_UID_ID_MAX) {
        return -1;
    }
    *cursor = end;
    *out = value;
    return 0;
}

/* Which of the four states this map's content is in. Pure: the caller has
 * already read the file (or is driving the rule directly). */
static enum as_uid_map_state as_uid_classify(const char *content) {
    long first = 0, lower = 0, count = 0;
    long seen_first[AS_UID_MAX_EXTENTS], seen_lower[AS_UID_MAX_EXTENTS],
        seen_count[AS_UID_MAX_EXTENTS];
    int extents = 0;
    const char *p = content;

    for (;;) {
        long a, b, c;
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        if (extents >= AS_UID_MAX_EXTENTS) {
            return AS_UID_MAP_UNREADABLE;
        }
        if (as_uid_next_field(&p, &a) != 0 || as_uid_next_field(&p, &b) != 0 ||
            as_uid_next_field(&p, &c) != 0) {
            return AS_UID_MAP_UNREADABLE;
        }
        /* A fourth number in the same group is not a map line. */
        if (*p != '\0' && *p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') {
            return AS_UID_MAP_UNREADABLE;
        }
        if (c == 0) {
            return AS_UID_MAP_UNREADABLE;
        }
        seen_first[extents] = a;
        seen_lower[extents] = b;
        seen_count[extents] = c;
        extents++;
    }

    if (extents == 0) {
        return AS_UID_MAP_READY;
    }
    if (extents == 1) {
        first = seen_first[0];
        lower = seen_lower[0];
        count = seen_count[0];
        if (first == AS_UID_INITIAL_FIRST && lower == AS_UID_INITIAL_LOWER &&
            count == AS_UID_INITIAL_COUNT) {
            return AS_UID_MAP_INITIAL;
        }
    }
    return AS_UID_MAP_WRITTEN;
}

/* Decide whether this map may be written. 0 when it is ready; -1 with the
 * refusal spelled into `err` otherwise. `name` is the map's own name
 * (`uid_map` / `gid_map`) because a half-granted namespace has to be reported
 * as the *half* it is, and `pid` is the container pid the caller asked about
 * (the caller resolved it, this program only reports it). */
int as_uid_check_map(const char *name, long pid, const char *content, char *err,
                     size_t errlen) {
    char text[AS_UID_TEXT_MAX];
    as_uid_normalize(content, text, sizeof(text));
    switch (as_uid_classify(content)) {
    case AS_UID_MAP_READY:
        return 0;
    case AS_UID_MAP_INITIAL:
        snprintf(err, errlen,
                 "%s for pid %ld is the initial namespace's full range: this "
                 "pid has not unshared a user namespace, so there is no new "
                 "identity to grant",
                 name, pid);
        return -1;
    case AS_UID_MAP_WRITTEN:
        snprintf(err, errlen,
                 "%s for pid %ld already carries a mapping ('%s'): a user "
                 "namespace's map is written exactly once, and face A never "
                 "rewrites one",
                 name, pid, text);
        return -1;
    case AS_UID_MAP_UNREADABLE:
        break;
    }
    snprintf(err, errlen,
             "%s for pid %ld is not a map this program can read ('%s'): a "
             "fresh namespace's map is empty, so this pid is not the one face "
             "A was asked to grant",
             name, pid, text);
    return -1;
}

/* The one mapping face A may write, for the uid it was asked for. Returns 0,
 * or -1 when it would not fit (a caller bug, not a policy decision). */
int as_uid_identity_line(long id, char *out, size_t outlen) {
    int written = snprintf(out, outlen, "%ld %ld 1\n", id, id);
    if (written < 0 || (size_t)written >= outlen) {
        return -1;
    }
    return 0;
}

/* Refuse every mapping that is not the identity `X X 1`, and uid 0 with it:
 * a container id may only ever be handed the *same* host id, and never root's.
 * Returns 0 and writes the identity's uid, or -1 with the refusal in `err`. */
int as_uid_check_identity_line(const char *line, long *id, char *err,
                               size_t errlen) {
    char text[AS_UID_TEXT_MAX];
    const char *p = line;
    long fields[4];
    int seen = 0;
    as_uid_normalize(line, text, sizeof(text));
    for (;;) {
        while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
            p++;
        }
        if (*p == '\0') {
            break;
        }
        if (seen >= 4 || as_uid_next_field(&p, &fields[seen]) != 0) {
            snprintf(err, errlen, "the mapping '%s' must map exactly one id",
                     text);
            return -1;
        }
        seen++;
    }
    if (seen != 3 || fields[2] != 1) {
        snprintf(err, errlen, "the mapping '%s' must map exactly one id", text);
        return -1;
    }
    if (fields[0] != fields[1]) {
        snprintf(err, errlen,
                 "the mapping '%s' is not the identity 'X X 1' face A writes",
                 text);
        return -1;
    }
    if (fields[0] == 0) {
        snprintf(err, errlen,
                 "the mapping '%s' names uid 0: face A hands a pooled uid, "
                 "never root's",
                 text);
        return -1;
    }
    *id = fields[0];
    return 0;
}

/* The one line a successful grant prints, and the only thing it prints. */
void as_uid_ok_line(long pid, long uid, char *out, size_t outlen) {
    snprintf(out, outlen, "C3-ASUID-OK pid=%ld uid=%ld\n", pid, uid);
}

#ifndef AS_UID_NO_MAIN

/* Read one of the target's maps whole. size_t because the read is bounded:
 * a map longer than AS_UID_MAP_MAX is refused by the caller, not truncated. */
static int as_uid_read_map(const char *name, long pid, char *out, size_t outlen,
                           char *err, size_t errlen) {
    char path[64];
    ssize_t got;
    int fd;
    snprintf(path, sizeof(path), "/proc/%ld/%s", pid, name);
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        snprintf(err, errlen, "cannot read %s for pid %ld: %s", name, pid,
                 strerror(errno));
        return -1;
    }
    got = read(fd, out, outlen - 1);
    if (got < 0) {
        snprintf(err, errlen, "cannot read %s for pid %ld: %s", name, pid,
                 strerror(errno));
        close(fd);
        return -1;
    }
    close(fd);
    out[got] = '\0';
    if ((size_t)got == outlen - 1) {
        snprintf(err, errlen,
                 "%s for pid %ld is longer than %zu bytes: this pid is not the "
                 "one face A was asked to grant",
                 name, pid, outlen - 1);
        return -1;
    }
    return 0;
}

static int as_uid_write_map(const char *name, long pid, const char *line,
                            char *err, size_t errlen) {
    char path[64];
    size_t len = strlen(line);
    ssize_t written;
    int fd;
    snprintf(path, sizeof(path), "/proc/%ld/%s", pid, name);
    fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0) {
        snprintf(err, errlen, "cannot write %s for pid %ld: %s", name, pid,
                 strerror(errno));
        return -1;
    }
    written = write(fd, line, len);
    if (written < 0 || (size_t)written != len) {
        snprintf(err, errlen, "cannot write %s for pid %ld: %s", name, pid,
                 written < 0 ? strerror(errno) : "short write");
        close(fd);
        return -1;
    }
    close(fd);
    return 0;
}


/* "--uid 10000" and "--uid=10000", the shape both brokers already parse. */
static const char *as_uid_flag_value(const char *arg, const char *name,
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

static const char *as_uid_need_value(int argc, char **argv, int *index,
                                     const char *name,
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

static long as_uid_parse_pid(const char *text) {
    char *end = NULL;
    long value;
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || value <= 0) {
        priv_usage("--pid must be a positive decimal integer (got '%s')", text);
    }
    return value;
}

int main(int argc, char **argv) {
    long uid = -1, pid = -1, mapped = -1;
    char err[PRIV_ERR_LEN];
    char line[AS_UID_LINE_MAX];
    char uid_map[AS_UID_MAP_MAX];
    char gid_map[AS_UID_MAP_MAX];
    char ok[AS_UID_LINE_MAX];
    int index = 1;

    priv_set_progname("as_uid");
    while (index < argc) {
        const char *inline_value = NULL;
        const char *value;
        if (as_uid_flag_value(argv[index], "--uid", &inline_value) != NULL) {
            value = as_uid_need_value(argc, argv, &index, "--uid", inline_value);
            if (uid >= 0) {
                priv_usage("--uid given twice");
            }
            if (priv_parse_uid(value, &uid, err, sizeof(err)) != 0) {
                priv_usage("--uid: %s", err);
            }
        } else if (as_uid_flag_value(argv[index], "--pid", &inline_value) !=
                   NULL) {
            value = as_uid_need_value(argc, argv, &index, "--pid", inline_value);
            if (pid >= 0) {
                priv_usage("--pid given twice");
            }
            pid = as_uid_parse_pid(value);
        } else {
            priv_usage("unexpected argument '%s'", argv[index]);
        }
        index += 1;
    }
    if (uid < 0 || pid < 0) {
        priv_usage("expected: as_uid --uid X --pid N");
    }

    /* 1. The identity is a pool uid, never root's. */
    if (priv_validate_uid(uid, err, sizeof(err)) != 0) {
        priv_fail("%s", err);
    }
    /* 4. The bytes about to be written are the identity, and only it. */
    if (as_uid_identity_line(uid, line, sizeof(line)) != 0) {
        priv_fail("cannot format the identity mapping for uid %ld", uid);
    }
    if (as_uid_check_identity_line(line, &mapped, err, sizeof(err)) != 0) {
        priv_fail("%s", err);
    }
    if (mapped != uid) {
        priv_fail(
            "the identity mapping carries uid %ld, not the validated uid %ld",
            mapped, uid);
    }
    /* 2 + 3. Both maps are judged before either is written. */
    if (as_uid_read_map("uid_map", pid, uid_map, sizeof(uid_map), err,
                        sizeof(err)) != 0 ||
        as_uid_check_map("uid_map", pid, uid_map, err, sizeof(err)) != 0 ||
        as_uid_read_map("gid_map", pid, gid_map, sizeof(gid_map), err,
                        sizeof(err)) != 0 ||
        as_uid_check_map("gid_map", pid, gid_map, err, sizeof(err)) != 0) {
        priv_fail("%s", err);
    }
    if (as_uid_write_map("uid_map", pid, line, err, sizeof(err)) != 0) {
        priv_fail("%s", err);
    }
    if (as_uid_write_map("gid_map", pid, line, err, sizeof(err)) != 0) {
        priv_fail("%s (the uid_map write already landed: this pid is "
                  "half-granted and must be abandoned, not retried)",
                  err);
    }
    as_uid_ok_line(pid, uid, ok, sizeof(ok));
    fputs(ok, stdout);
    return PRIV_EXIT_OK;
}

#endif /* AS_UID_NO_MAIN */

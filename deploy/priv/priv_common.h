/* SPDX-License-Identifier: Apache-2.0
 *
 * Shared validators for the two file-capability brokers (Track F / Task F1).
 *
 * Both brokers link this translation unit so the uid-pool range, the root
 * whitelist and the argument shapes cannot drift apart: a rule that exists in
 * only one of them is exactly the failure this file prevents.
 */
#ifndef E2B_PRIV_COMMON_H
#define E2B_PRIV_COMMON_H

#include <stddef.h>
#include <sys/types.h>

/* Exit codes. REFUSED is the fail-closed policy refusal (never "try harder"). */
#define PRIV_EXIT_OK 0
#define PRIV_EXIT_USAGE 2
#define PRIV_EXIT_SYSTEM 70
#define PRIV_EXIT_REFUSED 77

#define PRIV_ERR_LEN 512

/* Defaults mirror envd_service/config.py (E2B_UID_POOL_START/SIZE). */
#define PRIV_DEFAULT_UID_POOL_START 10000L
#define PRIV_DEFAULT_UID_POOL_SIZE 1000L
#define PRIV_DEFAULT_WORKSPACE_BASE "/var/lib/e2b-sandboxes"
#define PRIV_DEFAULT_SUPERVISE_BIN \
    "/usr/local/lib/python3.14/site-packages/sandlock/bin/sandlock-supervise"

/* The program name used in every diagnostic. */
const char *priv_progname(void);
void priv_set_progname(const char *name);

/* Print "e2b-...: <fmt>" to stderr and exit(PRIV_EXIT_REFUSED). */
void priv_fail(const char *fmt, ...) __attribute__((format(printf, 1, 2)))
    __attribute__((noreturn));

/* Print "e2b-...: <fmt>" to stderr and exit(PRIV_EXIT_USAGE). */
void priv_usage(const char *fmt, ...) __attribute__((format(printf, 1, 2)))
    __attribute__((noreturn));

/* Strict decimal parse: the whole string must be the number, > 0. */
int priv_parse_uid(const char *text, long *out, char *err, size_t errlen);

/* The configured uid pool (E2B_UID_POOL_START / E2B_UID_POOL_SIZE). */
void priv_uid_pool(long *start, long *size);

/* The uid-pool gate shared by every verb: pool membership, never uid 0. */
int priv_validate_uid(long uid, char *err, size_t errlen);

/* The chown group gate (fix round 1 / c1): a pooled uid, or the broker's own
 * gid -- a sandbox tree is `0770 owner=<sandbox uid> group=<worker gid>`, and
 * chgrp-to-own-gid is never a privilege widening. */
int priv_gid_allowed(long gid, char *err, size_t errlen);

/* N27: the platform state base -- `E2B_STATE_BASE` when the deployment names
 * one, the workspace base otherwise (one shape, one root). */
const char *priv_state_base(void);

/* realpath() + containment in <workspace_base>/, <state_base>/ (only when it
 * is a root of its own) or <shared_volume_root>/.
 * `strict` additionally refuses the roots themselves (delete/chown must never
 * target a whole managed root). Returns 0 on success and writes the resolved
 * absolute path into `resolved` (>= PATH_MAX bytes). */
int priv_resolve_allowed_path(const char *path, int strict, char *resolved,
                              size_t resolved_len, char *err, size_t errlen);

/* The roots, as "a" or "a, b, c" (for diagnostics). */
void priv_roots_text(char *out, size_t outlen);

/* The pinned sandlock-supervise path (E2B_SUPERVISE_BIN or the build default). */
const char *priv_supervise_bin(void);

#endif /* E2B_PRIV_COMMON_H */

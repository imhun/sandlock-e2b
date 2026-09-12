#!/usr/bin/env bash
# C4 maintenance window: turn the degraded per-sandbox quota into a real XFS
# project quota, and bring the stack up with redis auth + the quota-agent.
#
# WHY A WINDOW: the sandbox storage lives on the host's `/` (xfs, `noquota`).
# `mount -o remount,prjquota /` returns 0 but changes nothing -- XFS only
# applies quota options at the FIRST mount (measured twice on 2026-09-12: on
# the live `/` in tmp/c4-02-step1-followup.log and on a scratch loop fs in
# tmp/c4-06-scratch-remount.log, where remount left the fs at `noquota` with
# zero project-quota state while a clean `mount -o prjquota` turned
# Accounting/Enforcement ON).
#
# That matters because on a RHEL-family boot the root fs is mounted by the
# initramfs from `root=...` and fstab options are only re-applied afterwards
# with `mount -o remount` (systemd-remount-fs) -- which we just proved is a
# no-op for XFS quota options. So the window sets BOTH: `rootflags=prjquota`
# on the kernel command line (makes the first mount carry it) and
# `defaults,prjquota` in fstab (keeps later mounts and the operator's mental
# model right). The post-reboot check fails closed if either is missing.
#
# Usage (run from the repo root on the operator machine):
#   ./deploy/scripts/c4-prjquota-window.sh --dry-run          # read-only probe
#   ./deploy/scripts/c4-prjquota-window.sh --apply --yes-i-have-a-window
#   ./deploy/scripts/c4-prjquota-window.sh --apply --stage post   # resume after the reboot
#
# Stages: pre (backup + secrets + env + fstab + pre-pull) -> reboot (reboot +
# verify the mount) -> post (compose up --profile quota + warm) -> accept
# (quota ENOSPC / no-oversell / release, redis auth, secret master key, the
# three smokes, the C2.1 defect regression, log checks). Every step is
# fail-closed and every mutation is followed by a readback.
#
# Constraints honoured here: no secret value ever reaches the log (only
# sha256 prefixes of values), no git push, all remote work through the
# existing bastion channel, and rollback is prepared before anything is
# touched.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$SCRIPT_DIR/lib/helpers.sh"

#: The host's root filesystem must be this one -- fail closed otherwise.
EXPECT_ROOT_UUID="${C4_ROOT_UUID:-de890c86-56b9-427c-a659-81115c282d5a}"
EXPECT_ROOT_FSTYPE="${C4_ROOT_FSTYPE:-xfs}"
#: Quota-agent image that really exists in ACR (verified 2026-09-12:
#: index sha256:df35eabb..., linux/amd64 + linux/arm64).
QUOTA_AGENT_TAG="${C4_QUOTA_AGENT_TAG:-0.1.0-227-ge75b03a-20260912-084538}"
QUOTA_AGENT_URL="${C4_QUOTA_AGENT_URL:-http://quota-agent:49984}"
QUOTA_AGENT_IMAGE="$ACR_REGISTRY/$ACR_NAMESPACE/e2b-sandlock-quota-agent:$QUOTA_AGENT_TAG"
#: Small per-sandbox quota used by the acceptance test.
ACCEPT_QUOTA_MB="${C4_ACCEPT_QUOTA_MB:-64}"
REBOOT_WAIT_S="${C4_REBOOT_WAIT_S:-300}"

DRY_RUN=1
APPLY_OK=0
STAGE="all"
AUTO_ROLLBACK=1
ALLOW_LIVE=0
#: Which rollback the fail-closed handler must run. Set as the window
#: progresses: "" (nothing changed) -> env -> boot -> quota.
ROLLBACK_MODE=""
TS="${C4_TS:-$(date -u +%Y%m%dT%H%M%SZ)}"
BACKUP_DIR="/opt/sandlock/c4-backup-$TS"

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --apply) DRY_RUN=0 ;;
        --yes-i-have-a-window) APPLY_OK=1 ;;
        --stage) STAGE="$2"; shift ;;
        --active-window-ts) TS="$2"; BACKUP_DIR="/opt/sandlock/c4-backup-$TS"; shift ;;
        --no-auto-rollback) AUTO_ROLLBACK=0 ;;
        --allow-live-sandboxes) ALLOW_LIVE=1 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$DRY_RUN" = "0" ] && [ "$APPLY_OK" != "1" ]; then
    echo "refusing to --apply without --yes-i-have-a-window" >&2
    exit 2
fi

# The window does a reboot and long image pulls: give the bastion channel room.
export TASK_TIMEOUT="${TASK_TIMEOUT:-1800}"

say() { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

# ro <cmd>   -- read-only probe: runs in both dry-run and apply
# mut <cmd>  -- mutation: printed in dry-run, executed in apply
# mutq <cmd> -- mutation whose output may contain secrets: never echoed
ro() { run_target "$1" 2>&1 | tr -d '\r'; }
# ro1 <cmd> -- single-value read: the bastion channel prefixes the output with
# a blank line, so trim to one non-empty token before comparing.
ro1() { ro "$1" | sed -e '/^[[:space:]]*$/d' | head -1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'; }
mut() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '    [dry-run] WOULD RUN: %s\n' "$1"
        return 0
    fi
    run_target "$1" 2>&1 | tr -d '\r'
}
fail() {
    printf '\n!! FAIL-CLOSED: %s\n' "$*" >&2
    if [ -n "$ROLLBACK_MODE" ] && [ "$AUTO_ROLLBACK" = "1" ]; then
        rollback "$ROLLBACK_MODE"
    else
        printf '!! no automatic rollback needed/possible (%s)\n' "${ROLLBACK_MODE:-nothing changed yet}" >&2
    fi
    exit 1
}

# rollback <env|boot|quota|all> -- restore what the window has changed so far.
# Deliberately does NOT reboot: dropping `rootflags=prjquota` only takes effect
# on the next boot, and the operator decides when that is safe.
rollback() {
    local mode="$1"
    if [ "$DRY_RUN" = "1" ]; then
        printf '    [dry-run] WOULD ROLL BACK (%s): fstab+boot args and/or .env snapshot and/or quota switch\n' "$mode"
        return 0
    fi
    printf '\n==> ROLLBACK (%s)\n' "$mode" >&2
    case "$mode" in
        env|boot|all)
            if ro1 "test -f '$BACKUP_DIR/fstab' && echo yes" = "yes"; then
                ro "cp -p '$BACKUP_DIR/fstab' /etc/fstab && echo restored-fstab" | sed 's/^/    /' >&2
            fi
            ro "grubby --update-kernel=ALL --remove-args='rootflags=prjquota' >/dev/null && echo removed-rootflags" | sed 's/^/    /' >&2
            printf '    NOTE: prjquota drops off only after the next reboot (fstab + boot args restored)\n' >&2
            if ro1 "test -f '$BACKUP_DIR/env' && echo yes" = "yes"; then
                ro "cp -p '$BACKUP_DIR/env' '$REMOTE_DIR/.env' && chown $DEPLOY_USER:$DEPLOY_USER '$REMOTE_DIR/.env' && chmod 600 '$REMOTE_DIR/.env' && echo restored-env" | sed 's/^/    /' >&2
                ro "su - $DEPLOY_USER -c 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml up -d --no-build --remove-orphans' >/dev/null && echo stack-recreated-from-snapshot" | sed 's/^/    /' >&2
            fi
            ;;
        quota)
            ro "cd $REMOTE_DIR && sed -i 's|^QUOTA_AGENT_PROFILE=.*|QUOTA_AGENT_PROFILE=0|; s|^E2B_QUOTA_AGENT_URL=.*|E2B_QUOTA_AGENT_URL=|' .env && echo quota-switched-to-degraded" | sed 's/^/    /' >&2
            ro "su - $DEPLOY_USER -c 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml --profile quota rm -sf quota-agent' >/dev/null 2>&1; echo agent-removed" | sed 's/^/    /' >&2
            ro "su - $DEPLOY_USER -c 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml up -d --no-build --remove-orphans' >/dev/null && echo stack-back-to-degraded" | sed 's/^/    /' >&2
            ;;
    esac
}
expect_contains() { # expect_contains <what> <needle> <<< haystack
    local what="$1" needle="$2" haystack
    haystack="$(cat)"
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
        printf '    %s\n' "$haystack" | head -20
        fail "$what: expected to find '$needle'"
    fi
    printf '    OK: %s\n' "$what"
}

SKIP_PRE=0
case "$STAGE" in
    all|pre|reboot|post|accept) ;;
    *) echo "unknown stage: $STAGE" >&2; exit 2 ;;
esac

say "C4 window ($([ "$DRY_RUN" = 1 ] && echo dry-run || echo APPLY), stage=$STAGE, ts=$TS)"
note "repo: $REPO_DIR"
note "target: $TARGET_HOST via bastion $BASTION_HOST"
note "backup dir on target: $BACKUP_DIR"

# --------------------------------------------------------------------------
# PRE-1  preflight (read-only, fail closed)
# --------------------------------------------------------------------------
say "PRE-1 preflight"
ROOT_UUID="$(ro1 "findmnt -no UUID /")"
[ "$ROOT_UUID" = "$EXPECT_ROOT_UUID" ] \
    || fail "root UUID is '$ROOT_UUID', expected '$EXPECT_ROOT_UUID' (fstab target would be wrong)"
note "root UUID matches: $ROOT_UUID"

ROOT_LINE="$(ro1 "findmnt -no SOURCE,FSTYPE /")"
printf '%s' "$ROOT_LINE" | grep -q "$EXPECT_ROOT_FSTYPE" \
    || fail "root fs is '$ROOT_LINE', expected $EXPECT_ROOT_FSTYPE"
note "root fs: $ROOT_LINE"

CURRENT_OPTS="$(ro1 "findmnt -no OPTIONS /")"
note "current options: $CURRENT_OPTS"
if printf '%s' "$CURRENT_OPTS" | grep -q 'prjquota'; then
    note "prjquota is ALREADY active -> PRE/reboot steps will be skipped"
    SKIP_PRE=1
fi

ro "command -v xfs_quota mkfs.xfs losetup >/dev/null && echo 'quota tooling: present'" \
    | expect_contains "quota tooling on the target" "present"

LIVE="$(ro "docker exec -i sandlock-control-plane-1 python3 -c \"
import os, urllib.request
api = os.environ.get('E2B_API_KEYS', '').split(',')[0]
req = urllib.request.Request('http://127.0.0.1:3000/sandboxes', headers={'X-API-Key': api})
print(urllib.request.urlopen(req, timeout=5).read().decode())\"")"
if [ "$ALLOW_LIVE" != "1" ]; then
    printf '%s' "$LIVE" | grep -q '^\[\]$' \
        || fail "live sandboxes exist ($LIVE): reboot would kill them (use --allow-live-sandboxes only if that is intended)"
    note "no live sandboxes: $LIVE"
fi

say "PRE-1b current container/image inventory (recorded, not changed)"
ro "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | sort" | sed 's/^/    /'

if [ "$DRY_RUN" = "1" ]; then
    say "DRY-RUN: the mutations below would run in --apply (nothing was changed)"
fi

# --------------------------------------------------------------------------
# PRE-2  backups
# --------------------------------------------------------------------------
if [ "$SKIP_PRE" = "0" ] && { [ "$STAGE" = "all" ] || [ "$STAGE" = "pre" ]; }; then
    say "PRE-2 backup fstab + .env + compose into $BACKUP_DIR"
    mut "install -d -m 700 '$BACKUP_DIR' && cp -p /etc/fstab '$BACKUP_DIR/fstab' && cp -p '$REMOTE_DIR/.env' '$BACKUP_DIR/env' && cp -p '$REMOTE_DIR/docker-compose.prod.yml' '$BACKUP_DIR/docker-compose.prod.yml' && mkdir -p '$BACKUP_DIR/boot' && cp -a /boot/grub2/grub.cfg '$BACKUP_DIR/boot/grub.cfg' && cp -a /boot/loader/entries '$BACKUP_DIR/boot/entries' && cat /proc/cmdline > '$BACKUP_DIR/boot/cmdline' && chmod 600 '$BACKUP_DIR'/* && sha256sum '$BACKUP_DIR'/fstab '$BACKUP_DIR'/env '$BACKUP_DIR'/docker-compose.prod.yml"
    if [ "$DRY_RUN" = "0" ]; then
        ro "test -s '$BACKUP_DIR/fstab' && test -s '$BACKUP_DIR/env' && echo 'backups: present'" \
            | expect_contains "backup files written" "backups: present"
    fi

    # Local copies of the non-secret files (the .env stays on the machine; only
    # its key fingerprints are recorded).
    if [ "$DRY_RUN" = "0" ]; then
        mkdir -p "$REPO_DIR/tmp/c4-window-$TS"
        ro "cat /etc/fstab" > "$REPO_DIR/tmp/c4-window-$TS/fstab.before"
        ro "cat '$REMOTE_DIR/docker-compose.prod.yml'" > "$REPO_DIR/tmp/c4-window-$TS/compose.before.yml"
        note "saved local copies: tmp/c4-window-$TS/{fstab.before,compose.before.yml}"
    fi
fi

# --------------------------------------------------------------------------
# PRE-3  secrets (generated ON the target, never printed)
# --------------------------------------------------------------------------
if [ "$SKIP_PRE" = "0" ] && { [ "$STAGE" = "all" ] || [ "$STAGE" = "pre" ]; }; then
    say "PRE-3 secrets: fingerprint before (values never leave the target)"
    FP_CMD='cd /opt/sandlock && for k in E2B_API_KEYS E2B_INTERNAL_API_KEY E2B_INTERNAL_API_KEYS E2B_IMAGE_REGISTRY_USERNAME E2B_IMAGE_REGISTRY_PASSWORD E2B_REDIS_PASSWORD E2B_SECRET_MASTER_KEY E2B_SECRET_MASTER_KEYS E2B_QUOTA_AGENT_TOKEN; do v=$(grep -E "^$k=" .env | tail -1 | cut -d= -f2-); if [ -z "$v" ]; then echo "  $k unset/empty"; else echo "  $k sha256($(printf %s "$v" | sha256sum | cut -c1-16))"; fi; done'
    ro "$FP_CMD" | sed 's/^/  /'

    say "PRE-3b generate the three new secrets (only when the slot is empty)"
    GEN_CMD='cd /opt/sandlock && \
gen_if_empty() { k="$1"; bits="$2"; cur=$(grep -E "^$k=" .env | tail -1 | cut -d= -f2-); \
  if [ -n "$cur" ]; then echo "  $k kept (already set)"; return 0; fi; \
  val=$(openssl rand -hex "$bits"); \
  if grep -qE "^$k=" .env; then sed "s|^$k=.*|$k=$val|" .env > .env.tmp && mv .env.tmp .env; else printf "%s=%s\n" "$k" "$val" >> .env; fi; \
  echo "  $k generated"; }; \
gen_if_empty E2B_SECRET_MASTER_KEY 32; \
gen_if_empty E2B_REDIS_PASSWORD 24; \
gen_if_empty E2B_QUOTA_AGENT_TOKEN 24; \
chown deploy:deploy .env; chmod 600 .env; \
for k in E2B_SECRET_MASTER_KEY E2B_REDIS_PASSWORD E2B_QUOTA_AGENT_TOKEN; do \
  [ -n "$(grep -E "^$k=" .env | tail -1 | cut -d= -f2-)" ] || { echo "  $k FAILED to set"; exit 1; }; done; \
echo "  all three slots are set"'
    if [ "$DRY_RUN" = "1" ]; then
        printf '    [dry-run] WOULD RUN (output shows only the key name, never the value):\n'
        printf '%s\n' "$GEN_CMD" | sed 's/^/      /'
    else
        GEN_OUT="$(run_target "$GEN_CMD" 2>&1 | tr -d '\r')"
        printf '%s\n' "$GEN_OUT" | sed 's/^/  /'
        printf '%s' "$GEN_OUT" | grep -q 'all three slots are set' \
            || fail "secret generation did not report success"
    fi

    say "PRE-3c fingerprints AFTER (must match the keep-list, generation adds rows)"
    ro "$FP_CMD" | sed 's/^/  /'
    ROLLBACK_MODE="env"
fi

# --------------------------------------------------------------------------
# PRE-4  quota-agent wiring in .env (preserve everything else)
# --------------------------------------------------------------------------
if [ "$SKIP_PRE" = "0" ] && { [ "$STAGE" = "all" ] || [ "$STAGE" = "pre" ]; }; then
    say "PRE-4 pin QUOTA_AGENT_IMAGE / URL / profile"
    SET_CMD="cd /opt/sandlock && \
setkv() { k=\"\$1\"; v=\"\$2\"; if grep -qE \"^\$k=\" .env; then sed \"s|^\$k=.*|\$k=\$v|\" .env > .env.tmp && mv .env.tmp .env; else printf '%s=%s\n' \"\$k\" \"\$v\" >> .env; fi; }; \
setkv QUOTA_AGENT_IMAGE '$QUOTA_AGENT_IMAGE'; \
setkv E2B_QUOTA_AGENT_URL '$QUOTA_AGENT_URL'; \
setkv QUOTA_AGENT_PROFILE 1; \
chown deploy:deploy .env; chmod 600 .env; \
grep -E '^(QUOTA_AGENT_IMAGE|E2B_QUOTA_AGENT_URL|QUOTA_AGENT_PROFILE|E2B_QUOTA_AGENT_TOKEN)=' .env | sed -E 's/(E2B_QUOTA_AGENT_TOKEN=).*/\1<set>/'"
    mut "$SET_CMD"
    if [ "$DRY_RUN" = "0" ]; then
        ro "grep -E '^(QUOTA_AGENT_IMAGE|E2B_QUOTA_AGENT_URL|QUOTA_AGENT_PROFILE)=' /opt/sandlock/.env" \
            | expect_contains "quota-agent wiring in .env" "QUOTA_AGENT_PROFILE=1"
    fi

    say "PRE-4b quota-agent image exists (ACR manifest, local check)"
    if docker buildx imagetools inspect "$QUOTA_AGENT_IMAGE" >/dev/null 2>&1; then
        note "ACR has $QUOTA_AGENT_IMAGE"
    else
        note "WARNING: could not verify $QUOTA_AGENT_IMAGE from here (target pull will still be checked)"
    fi
fi

# --------------------------------------------------------------------------
# PRE-5  compose sync (redis restart policy) + config validation
# --------------------------------------------------------------------------
if [ "$SKIP_PRE" = "0" ] && { [ "$STAGE" = "all" ] || [ "$STAGE" = "pre" ]; }; then
    say "PRE-5 upload the compose (redis restart policy) and validate it"
    if [ "$DRY_RUN" = "0" ]; then
        upload_file "$REPO_DIR/deploy/stack/docker-compose.prod.yml" \
            "$REMOTE_DIR/docker-compose.prod.yml" "$DEPLOY_USER" | sed 's/^/  /'
        local_sum="$(sha256sum "$REPO_DIR/deploy/stack/docker-compose.prod.yml" | cut -d' ' -f1)"
        remote_sum="$(ro1 "sha256sum '$REMOTE_DIR/docker-compose.prod.yml'" | cut -d' ' -f1)"
        [ "$local_sum" = "$remote_sum" ] || fail "compose upload mismatch ($local_sum vs $remote_sum)"
        note "compose sha256 local == remote: $local_sum"
    else
        printf '    [dry-run] WOULD RUN: upload deploy/stack/docker-compose.prod.yml -> %s\n' "$REMOTE_DIR/docker-compose.prod.yml"
    fi
    mut "su - $DEPLOY_USER -c 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml --profile quota config --quiet && echo compose-config-ok'"
fi

# --------------------------------------------------------------------------
# PRE-6  fstab
# --------------------------------------------------------------------------
if [ "$SKIP_PRE" = "0" ] && { [ "$STAGE" = "all" ] || [ "$STAGE" = "pre" ]; }; then
    say "PRE-6a fstab: $EXPECT_ROOT_UUID / $EXPECT_ROOT_FSTYPE defaults,prjquota"
    CUR_FSTAB="$(ro "grep -E '^[^#].*[[:space:]]/[[:space:]]' /etc/fstab")"
    printf '%s\n' "$CUR_FSTAB" | sed 's/^/    today: /'
    if ! printf '%s' "$CUR_FSTAB" | grep -q "$EXPECT_ROOT_UUID"; then
        fail "no fstab line with the root UUID $EXPECT_ROOT_UUID -- refusing to edit"
    fi
    if printf '%s' "$CUR_FSTAB" | grep -q 'prjquota'; then
        note "fstab already carries prjquota"
    else
        FSTAB_CMD="python3 - <<'PYEOF'
import os, shutil, subprocess, sys
UUID = '$EXPECT_ROOT_UUID'
src = '/etc/fstab'
cand = '/etc/fstab.c4-candidate'
lines = open(src).read().splitlines(keepends=True)
out, hit = [], 0
for line in lines:
    if line.lstrip().startswith('#'):
        out.append(line); continue
    parts = line.split()
    # fstab fields: <spec> <mountpoint> <fstype> <options> <dump> <pass>
    if len(parts) >= 4 and parts[0] == 'UUID=' + UUID and parts[1] == '/' and parts[2] == '$EXPECT_ROOT_FSTYPE':
        opts = parts[3].split(',')
        if 'prjquota' not in opts:
            opts.append('prjquota')
        parts[3] = ','.join(opts)
        hit += 1
        out.append('\t'.join(parts[:3]) + '\t' + parts[3] + '\t' + '\t'.join(parts[4:]) + '\n')
    else:
        out.append(line)
if hit != 1:
    print(f'FAIL: matched {hit} root lines for UUID {UUID}'); sys.exit(1)
open(cand, 'w').write(''.join(out))
# dry-run parse of the candidate before installing it
rc = subprocess.run(['mount', '-a', '--fake', '--verbose', '--fstab', cand],
                    capture_output=True, text=True)
print(rc.stdout.strip()); print(rc.stderr.strip())
if rc.returncode != 0 and 'ignored' not in (rc.stdout + rc.stderr):
    print('FAIL: mount -a --fake rejected the candidate'); sys.exit(1)
shutil.copy2(cand, '/etc/fstab')
os.unlink(cand)
print('installed: /etc/fstab now has prjquota on /')
PYEOF
sha256sum /etc/fstab"
        mut "$FSTAB_CMD"
        if [ "$DRY_RUN" = "0" ]; then
            ro "grep -c prjquota /etc/fstab" | expect_contains "fstab prjquota count" "1"
            ro "grep -E '^[^#].*[[:space:]]/[[:space:]]' /etc/fstab" | sed 's/^/    now: /'
        fi
    fi

    say "PRE-6b kernel command line: rootflags=prjquota (first mount carries it)"
    CURRENT_ARGS="$(ro1 "grubby --info=DEFAULT 2>/dev/null | sed -n 's/^args=\"\\(.*\\)\"$/\\1/p'")"
    [ -n "$CURRENT_ARGS" ] || fail "could not read the current kernel args with grubby"
    note "current args: $CURRENT_ARGS"
    case " $CURRENT_ARGS " in
        *" rootflags="*)
            if printf '%s' "$CURRENT_ARGS" | grep -q 'rootflags=[^ ]*prjquota'; then
                note "rootflags already carries prjquota"
            else
                OLD_ROOTFLAGS="$(printf '%s' "$CURRENT_ARGS" | tr ' ' '\n' | grep '^rootflags=' | tail -1)"
                NEW_ROOTFLAGS="${OLD_ROOTFLAGS},prjquota"
                note "merging existing $OLD_ROOTFLAGS -> $NEW_ROOTFLAGS"
                mut "grubby --update-kernel=ALL --remove-args='$OLD_ROOTFLAGS' && grubby --update-kernel=ALL --args='$NEW_ROOTFLAGS'"
            fi
            ;;
        *)
        mut "grubby --update-kernel=ALL --args='rootflags=prjquota'"
            ;;
    esac
    if [ "$DRY_RUN" = "0" ]; then ROLLBACK_MODE="boot"; fi
    if [ "$DRY_RUN" = "0" ]; then
        # --update-kernel=ALL adds the arg to every kernel entry (this host has
        # several), so assert on the entry that will actually boot, and record
        # the total for the log.
        ro "grubby --info=DEFAULT 2>/dev/null | grep -c 'rootflags=[^ ]*prjquota' || true" \
            | expect_contains "rootflags=prjquota on the default boot entry" "1"
        ALL_ENTRIES="$(ro1 "grubby --info=ALL 2>/dev/null | grep -c 'rootflags=[^ ]*prjquota' || true")"
        note "kernel entries carrying rootflags=prjquota: $ALL_ENTRIES"
        [ "${ALL_ENTRIES:-0}" -ge 1 ] || fail "no boot entry carries rootflags=prjquota"
        ro "grubby --info=DEFAULT 2>/dev/null | sed -n 's/^args=\"\\(.*\\)\"$/    args: \\1/p'" | head -3
    fi

    say "PRE-7 pre-pull the images the post-reboot start needs"
    mut "su - $DEPLOY_USER -c 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml --profile quota pull --quiet && echo pull-ok'"
    if [ "$DRY_RUN" = "0" ]; then
        ro "docker images --format '{{.Repository}}:{{.Tag}}' | grep -F 'quota-agent:$QUOTA_AGENT_TAG' >/dev/null && echo 'quota-agent image present locally'" \
            | expect_contains "quota-agent image pulled" "present locally"
    fi
fi

# --------------------------------------------------------------------------
# REBOOT
# --------------------------------------------------------------------------
if [ "$SKIP_PRE" = "0" ] && { [ "$STAGE" = "all" ] || [ "$STAGE" = "reboot" ]; }; then
    say "REBOOT: issuing a detached reboot (session drop is expected)"
    mut "nohup sh -c 'sleep 3; /usr/sbin/reboot' >/dev/null 2>&1 & echo reboot-scheduled"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD wait for the host to come back and verify the mount"
    else
        note "waiting for the host to come back (max ${REBOOT_WAIT_S}s)"
        deadline=$(( $(date +%s) + REBOOT_WAIT_S ))
        up=0
        while [ "$(date +%s)" -lt "$deadline" ]; do
            if ro "echo alive" >/dev/null 2>&1; then up=1; break; fi
            sleep 5
        done
        [ "$up" = "1" ] || fail "host did not come back within ${REBOOT_WAIT_S}s"
        uptime_s="$(ro1 "cut -d. -f1 /proc/uptime")"
        [ -n "$uptime_s" ] && [ "$uptime_s" -lt 600 ] \
            || fail "host answers but uptime=${uptime_s}s: the reboot did not happen"
        note "host is back (uptime ${uptime_s}s)"
    fi

    say "REBOOT-verify: prjquota active + accounting/enforcement ON (both required)"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: findmnt -no OPTIONS / | grep prjquota"
        note "[dry-run] WOULD RUN: xfs_quota -x -c 'state' /  (needs Accounting: ON and Enforcement: ON)"
    else
        ro "findmnt -no OPTIONS /" | expect_contains "root mount options carry prjquota" "prjquota"
        STATE="$(ro "xfs_quota -x -c 'state' /")"
        printf '%s\n' "$STATE" | sed 's/^/    /'
        printf '%s' "$STATE" | grep -q 'Project quota state' \
            || fail "xfs_quota state has no project-quota section"
        printf '%s' "$STATE" | awk '/Project quota state/{f=1} f&&/Accounting: ON/{a=1} f&&/Enforcement: ON/{e=1} END{exit !(a&&e)}' \
            || fail "project quota is not Accounting+Enforcement ON"
        note "project quota: Accounting ON / Enforcement ON"
    fi
fi

# --------------------------------------------------------------------------
# POST: bring the stack up with the quota profile
# --------------------------------------------------------------------------
if [ "$STAGE" = "all" ] || [ "$STAGE" = "post" ]; then
    say "POST-1 docker compose --profile quota up -d"
    mut "su - $DEPLOY_USER -c 'cd $REMOTE_DIR && docker compose -f docker-compose.prod.yml --profile quota up -d --no-build --remove-orphans && echo up-ok'"
    if [ "$DRY_RUN" = "0" ]; then ROLLBACK_MODE="quota"; fi
    if [ "$DRY_RUN" = "0" ]; then
        sleep 10
        ro "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | sort" | sed 's/^/    /'
        ro "docker ps --format '{{.Names}}' | grep -q quota-agent && echo 'quota-agent running'" \
            | expect_contains "quota-agent container" "running"
    fi

    say "POST-2 wait for warm on both workers"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD wait for 'worker image warmed' on both workers"
    else
        deadline=$(( $(date +%s) + 300 ))
        while :; do
            ok=0
            for c in sandlock-worker-1-1 sandlock-worker-2-1; do
                [ "$(ro1 "docker logs $c 2>&1 | grep -c 'worker image warmed' || true")" -ge 1 ] && ok=$((ok+1))
            done
            [ "$ok" -eq 2 ] && { note "both workers warmed"; break; }
            [ "$(date +%s)" -ge "$deadline" ] && fail "workers did not warm within 300s ($ok/2)"
            sleep 5
        done
    fi

    say "POST-3 the agent itself must report the quota as usable"
    AGENT_CMD='docker exec sandlock-worker-1-1 python3 -c "
import os, httpx
tok = os.environ.get(\"E2B_QUOTA_AGENT_TOKEN\", \"\")
r = httpx.get(\"http://quota-agent:49984/detect\", params={\"mount\": \"/var/lib/e2b-sandboxes\"}, headers={\"X-Internal-Key\": tok}, timeout=10)
print(r.status_code, r.text)"'
    mut "$AGENT_CMD" | sed 's/^/    /'
    if [ "$DRY_RUN" = "0" ]; then
        ro "$AGENT_CMD" | expect_contains "agent /detect" '"prjquota": true'
    fi
fi

# --------------------------------------------------------------------------
# ACCEPT
# --------------------------------------------------------------------------
if [ "$STAGE" = "all" ] || [ "$STAGE" = "accept" ]; then
    say "ACCEPT-1 per-sandbox quota: ENOSPC on overshoot, no oversell, release"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: upload deploy/scripts/c4-accept.py -> $REMOTE_DIR/c4-accept.py and run it"
        note "           (creates a ${ACCEPT_QUOTA_MB}MB per-sandbox volume, 2 sandboxes: overshoot must ENOSPC,"
        note "            the second must still write, projid/limits must match report -p, then release)"
    else
        ACCEPT_LOCAL="$SCRIPT_DIR/c4-accept.py"
        [ -f "$ACCEPT_LOCAL" ] || fail "missing $ACCEPT_LOCAL"
        upload_file "$ACCEPT_LOCAL" "$REMOTE_DIR/c4-accept.py" "$DEPLOY_USER" | sed 's/^/    /'
        API_KEY="$(remote_env_value E2B_API_KEYS | cut -d, -f1)"
        INTERNAL_KEY="$(remote_env_value E2B_INTERNAL_API_KEY)"
        mkdir -p "$REPO_DIR/tmp/c4-window-$TS"
        ACCEPT_OUT="$(run_target "cd '$REMOTE_DIR' && E2B_API_URL=http://127.0.0.1:3000 E2B_SANDBOX_URL=http://127.0.0.1:3000 E2B_API_KEY='$API_KEY' E2B_INTERNAL_API_KEY='$INTERNAL_KEY' E2B_ACCEPT_QUOTA_MB='$ACCEPT_QUOTA_MB' /opt/sandlock/venv/bin/python -u c4-accept.py 2>&1 | tee /opt/sandlock/c4-accept.log" 2>&1 | tr -d '\r')"
        printf '%s\n' "$ACCEPT_OUT" | tee "$REPO_DIR/tmp/c4-window-$TS/accept.log" | sed 's/^/    /'
        printf '%s' "$ACCEPT_OUT" | grep -q 'C4-ACCEPT-OK' \
            || fail "quota acceptance failed (see tmp/c4-window-$TS/accept.log)"
    fi

    say "ACCEPT-2 host-side proof: xfs_quota report after the run"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: xfs_quota -x -c 'report -p -n -b' / (must list no project but #0)"
    else
        V="$(ro1 "docker volume inspect \$(docker volume ls -q | grep sandbox-shared | head -1) -f '{{.Mountpoint}}'")"
        ro "xfs_quota -x -c 'report -p -n -b' '$V' | head -20" | sed 's/^/    /'
        ro "xfs_quota -x -c 'report -p -n -b' '$V' | grep -c '^#[1-9]' || true" \
            | expect_contains "no leftover project entries after the acceptance run" "0"
    fi

    say "ACCEPT-3 redis now requires auth (and the stack still works)"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: redis-cli ping (expect NOAUTH) then authenticated ping (expect PONG)"
    else
        ro "docker exec sandlock-redis-1 redis-cli ping 2>&1 | head -2" \
            | expect_contains "unauthenticated redis ping is refused" "NOAUTH"
        REDIS_PW="$(remote_env_value E2B_REDIS_PASSWORD)"
        ro "docker exec sandlock-redis-1 redis-cli -a '$REDIS_PW' --no-auth-warning ping" \
            | expect_contains "authenticated redis ping" "PONG"
    fi

    say "ACCEPT-4 secret master key is active (no degraded warnings, real round trip)"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: control-plane log check + POST/GET/DELETE /secrets round trip + redis mirror check (e2b:secret:<id>)"
    else
        ro "docker logs sandlock-control-plane-1 2>&1 | grep -c 'is not configured' || true" \
            | expect_contains "no 'secret master key is not configured' warnings" "0"
        ro "docker logs sandlock-control-plane-1 2>&1 | grep -c 'not be mirrored to Redis' || true" \
            | expect_contains "no 'not mirrored to Redis' warnings" "0"
        SECRET_OUT="$(run_target "docker exec -i sandlock-control-plane-1 python3 - <<'PYEOF'
import json, os, urllib.request
api = os.environ.get('E2B_API_KEYS', '').split(',')[0]
body = json.dumps({'name': 'c4-window-probe', 'value': 'c4-window-probe-value'}).encode()
req = urllib.request.Request('http://127.0.0.1:3000/secrets', data=body,
                             headers={'X-API-Key': api, 'Content-Type': 'application/json'}, method='POST')
created = json.load(urllib.request.urlopen(req, timeout=10))
sid = created['secretID']
req = urllib.request.Request('http://127.0.0.1:3000/secrets/' + sid, headers={'X-API-Key': api})
fetched = json.load(urllib.request.urlopen(req, timeout=10))
print('secret round trip:', sid, fetched.get('name') == 'c4-window-probe')
req = urllib.request.Request('http://127.0.0.1:3000/secrets/' + sid, headers={'X-API-Key': api}, method='DELETE')
print('secret delete HTTP:', urllib.request.urlopen(req, timeout=10).status)
print('SECRET_ID', sid)
PYEOF" 2>&1 | tr -d '\r')"
        printf '%s\n' "$SECRET_OUT" | sed 's/^/    /'
        printf '%s' "$SECRET_OUT" | grep -q 'secret round trip: .* True' \
            || fail "secret create/get round trip failed (master key path)"
        SECRET_ID="$(printf '%s' "$SECRET_OUT" | awk '/^SECRET_ID/{print $2}' | tail -1)"
        REDIS_PW="$(remote_env_value E2B_REDIS_PASSWORD)"
        MIRRORED="$(ro1 "docker exec sandlock-redis-1 redis-cli -a '$REDIS_PW' --no-auth-warning --scan --pattern 'e2b:secret:$SECRET_ID' | head -1")"
        if [ "$MIRRORED" != "e2b:secret:$SECRET_ID" ]; then
            fail "secret $SECRET_ID is not mirrored to redis (E2B_SECRET_MASTER_KEY not active?)"
        fi
        note "secret mirrored to redis: $MIRRORED (master key + Fernet active)"
    fi

    say "ACCEPT-5 the three smokes"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: ./deploy/scripts/smoke.sh  (multinode + deployment on the target)"
        note "[dry-run] WOULD RUN: build a local test image from the deployed worker tag, then ./deploy/scripts/smoke-prod-worker.sh"
    else
        "$SCRIPT_DIR/smoke.sh" || fail "deploy/scripts/smoke.sh failed"
        if [ "${C4_SKIP_LOCAL_SMOKE:-0}" = "1" ]; then
            note "local smoke-prod-worker skipped (C4_SKIP_LOCAL_SMOKE=1)"
        else
            WORKER_TAG="$(ro1 "grep -E '^WORKER_IMAGE=' $REMOTE_DIR/.env | tail -1 | cut -d= -f2-")"
            note "deployed worker image: $WORKER_TAG"
            docker pull --platform linux/amd64 -q "$WORKER_TAG" >/dev/null 2>&1 || true
            ( cd "$REPO_DIR" && docker build -q -f - --build-arg WORKER_IMAGE="$WORKER_TAG" \
                -t e2b-sandlock-test:c4 . <<'DOCKERFILE'
ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
USER root
COPY requirements.txt requirements-test.txt /tmp/req/
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r /tmp/req/requirements-test.txt && rm -rf /tmp/req
WORKDIR /workspace
DOCKERFILE
            ) >/dev/null || fail "could not build e2b-sandlock-test:c4 from $WORKER_TAG"
            "$SCRIPT_DIR/smoke-prod-worker.sh" e2b-sandlock-test:c4 \
                || fail "smoke-prod-worker.sh failed (deployment shape)"
            note "smoke-prod-worker.sh (deployment shape): passed"
        fi
    fi

    say "ACCEPT-6 C2.1 defect regression (a volume must not break the sandbox list)"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: create volume -> GET /sandboxes,/v2/sandboxes,/internal/tenants must be 200 -> destroy"
    else
        REG_OUT="$(run_target "docker exec -i sandlock-control-plane-1 python3 - <<'PYEOF'
import os, urllib.request
api = os.environ.get('E2B_API_KEYS', '').split(',')[0]
internal = os.environ.get('E2B_INTERNAL_API_KEY', '')
for path, hdr, val in ((\"/sandboxes\", \"X-API-Key\", api), (\"/v2/sandboxes\", \"X-API-Key\", api), (\"/internal/tenants\", \"X-Internal-Key\", internal)):
    req = urllib.request.Request(\"http://127.0.0.1:3000\" + path, headers={hdr: val})
    print(path, urllib.request.urlopen(req, timeout=5).status)
PYEOF" 2>&1 | tr -d '\r')"
        printf '%s\n' "$REG_OUT" | sed 's/^/    /'
        [ "$(printf '%s' "$REG_OUT" | grep -c ' 200$')" = "3" ] \
            || fail "C2.1 regression: the three endpoints did not all answer 200"
    fi

    say "ACCEPT-7 log checks (route-B, per-uid warning, caps, the old degradation warning)"
    if [ "$DRY_RUN" = "1" ]; then
        note "[dry-run] WOULD RUN: route-B ready count / PER_UID_NONROOT_WARNING / CapEff / getcap / 'XFS project quota unavailable'"
    else
        for c in sandlock-worker-1-1 sandlock-worker-2-1; do
            echo "    $c:"
            RB="$(ro1 "docker logs $c 2>&1 | grep -c 'route-B instance ready' || true")"
            PU="$(ro1 "docker logs $c 2>&1 | grep -c 'PER_UID_NONROOT_WARNING' || true")"
            XQ="$(ro1 "docker logs --since 20m $c 2>&1 | grep -c 'XFS project quota unavailable' || true")"
            CAP="$(ro "docker exec $c sh -c 'grep ^CapEff /proc/1/status'")"
            echo "      route-B ready lines: $RB"
            echo "      PER_UID_NONROOT_WARNING: $PU"
            echo "      XFS quota degradation warnings (must be 0): $XQ"
            echo "      $CAP"
            ro "docker exec -u 0 $c getcap /var/lib/e2b-priv/e2b-slot-spawn /var/lib/e2b-priv/e2b-maint" | sed 's/^/      /'
            [ "${RB:-0}" -ge 1 ] 2>/dev/null || fail "$c: no route-B ready line since the restart"
            [ "${PU:-1}" = "0" ] || fail "$c: PER_UID_NONROOT_WARNING present"
            [ "${XQ:-1}" = "0" ] || fail "$c: still logging the XFS quota degradation warning"
            printf '%s' "$CAP" | grep -qi '0000000000000000' \
                || fail "$c: CapEff is not 0 (SYS_ADMIN check): $CAP"
        done
    fi
fi

say "done (stage=$STAGE, dry_run=$DRY_RUN)"
if [ "$DRY_RUN" = "1" ]; then
    note "nothing on the target was modified; re-run with --apply --yes-i-have-a-window in the window"
fi

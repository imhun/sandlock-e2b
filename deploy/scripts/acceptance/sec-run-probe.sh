#!/bin/sh
# Reusable runner: the prod-shaped capability set the sandlock create path needs.
# SECCOMP_PROFILE defaults to the *deployed* worker profile (the worker refuses
# to start unfiltered); set SECCOMP_PROFILE=unconfined to widen deliberately.
SECCOMP_PROFILE="${SECCOMP_PROFILE:-$(pwd)/deploy/seccomp/sandlock-worker.json}"
docker run --rm --security-opt seccomp="$SECCOMP_PROFILE" --cap-drop ALL \
  --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add NET_BIND_SERVICE --cap-add NET_RAW \
  --cap-add SYS_CHROOT --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID \
  --cap-add KILL --cap-add SETGID --cap-add SETUID --cap-add SETPCAP --cap-add AUDIT_WRITE \
  --cap-add SETFCAP --cap-add NET_ADMIN \
  --network host -e E2B_HOST_PROJECT="$(pwd)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(pwd):/workspace" -w /workspace e2b-sandlock-test:latest "$@"

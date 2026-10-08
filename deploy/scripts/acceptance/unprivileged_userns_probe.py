"""Can an own-identity slot (euid == the sandbox host uid) map 0 -> X in its own
userns, i.e. restore "root inside the sandbox, host uid outside" like the
in-process mediator does today?"""
import ctypes, os, subprocess, sys

libc = ctypes.CDLL("libc.so.6", use_errno=True)
UID = 21850
CLONE_NEWUSER = 0x10000000

code = r'''
import os, sys
uid = int(sys.argv[1])
print("in-ns euid:", os.geteuid(), "uid_map:", open("/proc/self/uid_map").read().strip())
'''

def main():
    print("sysctl unprivileged_userns_clone:",
          open("/proc/sys/kernel/unprivileged_userns_clone").read().strip()
          if os.path.exists("/proc/sys/kernel/unprivileged_userns_clone") else "(absent)")
    print("sysctl max_user_namespaces:",
          open("/proc/sys/user/max_user_namespaces").read().strip()
          if os.path.exists("/proc/sys/user/max_user_namespaces") else "(absent)")
    # As uid UID: unshare(CLONE_NEWUSER) then map 0 -> UID.
    helper = r'''
import ctypes, os, sys
libc = ctypes.CDLL("libc.so.6", use_errno=True)
uid = int(sys.argv[1])
if libc.unshare(0x10000000) != 0:
    print("unshare FAILED:", ctypes.get_errno(), os.strerror(ctypes.get_errno())); raise SystemExit(0)
try:
    open("/proc/self/setgroups", "w").write("deny")
except OSError as e:
    print("setgroups:", e)
open("/proc/self/uid_map", "w").write(f"0 {uid} 1\n")
open("/proc/self/gid_map", "w").write(f"0 {uid} 1\n")
print("in-ns euid:", os.geteuid(), "uid_map:", open("/proc/self/uid_map").read().strip())
'''
    out = subprocess.run(
        ["setpriv", f"--reuid={UID}", f"--regid={UID}", "--clear-groups", "--",
         sys.executable, "-B", "-c", helper, str(UID)],
        capture_output=True, text=True)
    print("as uid", UID, "->", (out.stdout + out.stderr).strip()[:400])

main()

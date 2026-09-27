"""What a directory really costs on this NAS: st_size vs st_blocks, by entry count."""
import time
from e2b import Sandbox

sb = Sandbox.create(timeout=900)
try:
    sb.commands.run("python3 -c \"import os;os.makedirs('/home/user/measure',exist_ok=True)\"", timeout=120)
    for n in (0, 10, 200, 1000, 2000):
        sb.commands.run(
            f"python3 -c \"import os;d='/home/user/measure/d{n}';os.makedirs(d,exist_ok=True);"
            f"[open(os.path.join(d,f'f{{i}}'),'w').close() for i in range({n})]\"",
            timeout=600,
        )
        out = sb.commands.run(
            f"stat -c '%s %b %B' /home/user/measure/d{n}; du -s -B1 /home/user/measure/d{n} | cut -f1",
            timeout=120,
        )
        size, blocks, bsize, du = out.stdout.split()
        print(
            f"entries={n:5d}  st_size={int(size):8d}  st_blocks*512={int(blocks)*int(bsize):8d}  du={int(du):8d}"
        )
finally:
    sb.kill()

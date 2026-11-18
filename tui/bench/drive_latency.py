# Input-to-paint probe (S295): drives rabble-tui in a pty with injected SGR wheel events, then read the --log CSV.
# Run from tui/: python3 bench/drive_latency.py /tmp/lat.csv   (needs target/release/rabble-tui built)
import os,pty,time,select,struct,fcntl,termios,sys
log=sys.argv[1]
pid,fd=pty.fork()
if pid==0:
    os.execv("target/release/rabble-tui",["rabble-tui","--log",log])
fcntl.ioctl(fd,termios.TIOCSWINSZ,struct.pack("HHHH",50,196,0,0))
def drain(t):
    end=time.time()+t
    while time.time()<end:
        if select.select([fd],[],[],0.01)[0]:
            try: os.read(fd,1<<20)
            except OSError: return
drain(0.5)
for _ in range(200):                       # 200 notches, ~8 ms apart (fast wheel)
    os.write(fd,b"\x1b[<65;10;10M"); drain(0.008)
drain(0.3)
for _ in range(40):                        # a burst of 20 queued at once
    os.write(fd,b"\x1b[<64;10;10M"*20); drain(0.02)
os.write(fd,b"q"); drain(0.5); os.waitpid(pid,0)

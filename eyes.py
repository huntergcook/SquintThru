# Reads Steam Frame eye openness straight from the eye server's shared memory
# (/dev/shm/eye-server.mmap, ABI v5), in this process, with no compiler needed.
# Same lock-and-request protocol as eyestream.c, which stays as a fallback.
import ctypes, ctypes.util, mmap, os, platform, struct, subprocess, time

SHM = "/dev/shm/eye-server.mmap"
ABI = 5
MUTEX, SEQ, REQ = 0x08, 0x38, 0x3C
OPENNESS = 0x157 + 0x79          # two floats: left, right
EOWNERDEAD = 130
FUTEX_WAIT = 0
SYS_FUTEX = {"x86_64": 202, "amd64": 202, "aarch64": 98, "arm64": 98}
STALE_AFTER = 3.0                # no new sample for this long: reopen the eye server
HERE = os.path.dirname(os.path.abspath(__file__))

class _timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

def _libc():
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    consistent = getattr(libc, "pthread_mutex_consistent", None)
    if consistent is None:  # glibc before 2.34 keeps it in libpthread
        consistent = ctypes.CDLL(ctypes.util.find_library("pthread") or "libpthread.so.0").pthread_mutex_consistent
    return libc, consistent

class EyeServer:
    """One open eye-server mapping. read() blocks for the next sample (up to 1 s)."""
    def __init__(self, path=SHM):
        nr = SYS_FUTEX.get(platform.machine().lower())
        if nr is None:
            raise RuntimeError("unsupported CPU " + platform.machine())
        fd = os.open(path, os.O_RDWR)
        try:
            self.ino = os.fstat(fd).st_ino
            self.mm = mmap.mmap(fd, os.fstat(fd).st_size)
        finally:
            os.close(fd)
        if struct.unpack_from("<I", self.mm, 0)[0] != ABI:
            raise RuntimeError("unexpected eye-server layout")
        self.path, self.nr = path, nr
        self._anchor = ctypes.c_char.from_buffer(self.mm)
        base = ctypes.addressof(self._anchor)
        self.mutex = ctypes.c_void_p(base + MUTEX)
        self.seq_addr = ctypes.c_void_p(base + SEQ)
        self.seq = ctypes.c_uint32.from_address(base + SEQ)
        self.req = ctypes.c_uint32.from_address(base + REQ)
        self.libc, self.consistent = _libc()
        self.timeout = _timespec(1, 0)

    def _lock(self):
        if self.libc.pthread_mutex_lock(self.mutex) == EOWNERDEAD:
            self.consistent(self.mutex)

    def read(self):
        """Returns (left, right), or None if the eye server sent nothing new in time."""
        self._lock()
        s = self.seq.value
        self.req.value = 1
        self.libc.pthread_mutex_unlock(self.mutex)
        self.libc.syscall(ctypes.c_long(self.nr), self.seq_addr, ctypes.c_long(FUTEX_WAIT),
                          ctypes.c_long(s), ctypes.byref(self.timeout), ctypes.c_void_p(0), ctypes.c_long(0))
        self._lock()
        l, r = struct.unpack_from("<2f", self.mm, OPENNESS)
        s2 = self.seq.value
        self.libc.pthread_mutex_unlock(self.mutex)
        return (l, r) if s2 != s else None

    def replaced(self):
        try:
            return os.stat(self.path).st_ino != self.ino
        except OSError:
            return True

def _from_binary():
    """Fallback: the compiled eyestream helper, if it was built."""
    exe = os.path.join(HERE, "eyestream")
    if not os.access(exe, os.X_OK):
        raise RuntimeError("in-process reader failed and eyestream isn't built")
    proc = subprocess.Popen([exe], stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            l, r = map(float, line.split())
            yield l, r
    finally:
        proc.kill()

def stream(log=print, path=SHM):
    """Yields (left, right) forever. Waits for the eye server and reconnects after restarts."""
    said = None
    def say(*a):  # log each problem once, not once a second
        nonlocal said
        if a != said:
            said = a
            log(*a)
    use_binary = False
    while True:
        try:
            if use_binary:
                yield from _from_binary()
                raise RuntimeError("eyestream exited")
            eye = EyeServer(path)
            say("eye tracking connected")
            last = time.monotonic()
            while True:
                sample = eye.read()
                now = time.monotonic()
                if sample is not None:
                    last = now
                    yield sample
                elif now - last > STALE_AFTER and eye.replaced():
                    raise RuntimeError("eye server restarted")
        except FileNotFoundError:
            say("waiting for eye tracking (is SteamVR running?)")
        except PermissionError as e:
            say("can't open eye tracking:", e)
        except (OSError, AttributeError) as e:  # ctypes or mmap trouble on this system
            if not use_binary:
                say("in-process eye reader unavailable:", e, "- trying eyestream")
                use_binary = True
                continue
            say("eye reader error:", e)
        except RuntimeError as e:
            say("eye reader:", e)
        time.sleep(1.0)

if __name__ == "__main__":
    for l, r in stream():
        print(f"{l:.3f} {r:.3f}", flush=True)

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
STALE_AFTER = 3.0                # no new sample for this long: check whether the eye server restarted
WAIT = 1.0                       # longest wait for a sample while eye tracking is running
WAIT_IDLE = 5.0                  # ...and once it has gone quiet (standby), so we wake up less
HERE = os.path.dirname(os.path.abspath(__file__))

class _timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

class EyeServer:
    """One open eye-server mapping. read() blocks until the next sample arrives.

    Runs ~60 times a second, so each frame is kept to three calls into libc:
    wait for the sample, then lock, read it, ask for the next one, unlock."""
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
        self.mutex, self.seq_addr = base + MUTEX, base + SEQ
        self.seq = ctypes.c_uint32.from_address(base + SEQ)
        self.req = ctypes.c_uint32.from_address(base + REQ)
        # Plain handle (no errno bookkeeping) with fixed argument types, so calls don't build objects.
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        vp = ctypes.c_void_p
        self._lock_fn, self._unlock_fn = libc.pthread_mutex_lock, libc.pthread_mutex_unlock
        for f in (self._lock_fn, self._unlock_fn):
            f.argtypes, f.restype = [vp], ctypes.c_int
        self._consistent = getattr(libc, "pthread_mutex_consistent", None)
        if self._consistent is None:  # glibc before 2.34 keeps it in libpthread
            self._consistent = ctypes.CDLL(ctypes.util.find_library("pthread") or "libpthread.so.0").pthread_mutex_consistent
        self._consistent.argtypes = [vp]
        self._futex = libc.syscall
        self._futex.argtypes = [ctypes.c_long, vp, ctypes.c_long, ctypes.c_uint32, vp, vp, ctypes.c_long]
        self._futex.restype = ctypes.c_long
        self._timeouts = {t: _timespec(int(t), int(t % 1 * 1e9)) for t in (WAIT, WAIT_IDLE)}
        self._timeout_ptrs = {t: ctypes.addressof(ts) for t, ts in self._timeouts.items()}
        self._lock()
        self.last = self.seq.value
        self.req.value = 1               # ask for the first sample
        self._unlock_fn(self.mutex)

    def _lock(self):
        if self._lock_fn(self.mutex) == EOWNERDEAD:
            self._consistent(self.mutex)

    def read(self, wait=WAIT):
        """Returns (left, right), or None if nothing new arrived (a timeout, or a wakeup
        without a new sample). A request for the next sample is always left pending."""
        self._futex(self.nr, self.seq_addr, FUTEX_WAIT, self.last, self._timeout_ptrs[wait], None, 0)
        if self.seq.value == self.last:
            return None
        self._lock()
        sample = struct.unpack_from("<2f", self.mm, OPENNESS)
        self.last = self.seq.value
        self.req.value = 1               # ask for the next one now, so it's ready the moment it exists
        self._unlock_fn(self.mutex)
        return sample

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
            wait, quiet_since = WAIT, None
            while True:
                sample = eye.read(wait)
                if sample is not None:
                    if quiet_since is not None:
                        wait, quiet_since = WAIT, None
                    yield sample
                    continue
                # Nothing new: only now look at the clock, so the per-frame path stays cheap.
                now = time.monotonic()
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since > STALE_AFTER:
                    if eye.replaced():
                        raise RuntimeError("eye server restarted")
                    wait = WAIT_IDLE   # headset idle: a new sample still wakes us instantly
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

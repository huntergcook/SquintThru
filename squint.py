# Turns on Steam Frame passthrough while you squint, and back off when you stop.
# Squint = the squint eye nearly shut while the other stays mostly open (from
# calibration: open L1.0/R1.0, squint L~0.85/R~0.03, closed L~0.21/R0.0).
# Serves the SquintThru app page at http://127.0.0.1:8765.
import json, os, statistics, sys, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import eyes, roomview

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "squintthru")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
OLD_SETTINGS_FILE = os.path.join(HERE, "settings.json")  # where settings lived before the installer
PORT = 8765
CALIB_SETTLE = 0.6   # seconds ignored at the start of each recording while you get into position
CALIB_SECONDS = 3.0  # seconds recorded after that

DEFAULTS = {
    "enabled": True,
    "mode": "hold",          # "hold": on while squinting; "toggle": each squint flips it
    "squint_eye": "right",   # the eye that nearly shuts when you squint
    "squint_below": 0.79,    # squint eye below this...
    "other_min": 0.0,        # ...while the other eye is in this band
    "other_max": 0.93,
    "release_above": 0.9,    # squint eye above this counts as "stopped squinting"
    "hold": 0.0,             # seconds a squint must last before acting; 0 = the first eye frame
    "release_hold": 0.0,     # seconds the eye must stay open before releasing
    "min_on": 0.02,          # passthrough stays on at least this long once triggered
    "ignore_blinks": True,   # don't let the start of a blink count as a squint
    "click_sound": False,    # play Steam's button click when passthrough switches
    "double_wink": False,    # two quick winks of one eye toggle passthrough
    "wink_eye": "right",     # "left", "right" or "either"
    "wink_below": 0.35,      # winking eye below this counts as shut
    "wink_max": 0.4,         # longest a single wink can last, in seconds
    "wink_gap": 0.6,         # most time allowed between the two winks
}
WINK_OTHER_MIN = 0.6  # the other eye must stay this open, so blinks never count as winks

# Blink guard. In a blink both eyes close together; in a squint the other eye
# barely moves. Over the last BLINK_WINDOW seconds, if the squint eye fell by at
# least BLINK_MIN_DROP and the other eye fell by more than BLINK_RATIO of that,
# it's a blink. A squint that turned on and then sees the other eye shut within
# BLINK_CANCEL seconds was a blink too, and is undone.
BLINK_WINDOW = 0.03
BLINK_MIN_DROP = 0.15
BLINK_RATIO = 0.6
BLINK_CANCEL = 0.12
BLINK_OTHER_SHUT = 0.45

def load_settings():
    s = dict(DEFAULTS)
    for path in (SETTINGS_FILE, OLD_SETTINGS_FILE):
        try:
            with open(path) as f:
                s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            break
        except (OSError, ValueError):
            continue
    # Older versions used 1 ms to mean "instant"; 0 now really is instant.
    for k in ("hold", "release_hold"):
        if s[k] < 0.002:
            s[k] = 0.0
    return s

def save_settings():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_FILE)

settings = load_settings()
live = {"l": 0.0, "r": 0.0, "squinting": False, "passthrough": None, "latency_ms": None,
        "eyes": False, "problem": ""}
events = deque(maxlen=12)
calib = {"phase": None, "start": 0.0, "until": 0.0, "samples": {"open": [], "squint": []}, "message": ""}

def log(*a):
    line = " ".join(map(str, a))
    events.appendleft(time.strftime("%H:%M:%S ") + line)
    print(time.strftime("%H:%M:%S"), line, flush=True)

class Passthrough:
    """Applies only the latest wanted state, so quick repeat squints never queue up.
    Each change is one round trip to Steam; the state is re-read every 2 s when idle."""
    def __init__(self, rv):
        self.rv = rv
        self.want = None   # hold mode: True/False
        self.flips = 0     # toggle mode, double wink and the page button
        self.ours = False  # True only if we turned it on (hold mode)
        self.asked = None  # when the newest request was made, for the response time
        self.problem = None
        self.cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def hold(self, on, at=None):
        with self.cv:
            self.want, self.asked = on, at or time.monotonic()
            self.cv.notify()

    def flip(self, at=None):
        with self.cv:
            self.flips += 1
            self.asked = at or time.monotonic()
            self.cv.notify()

    def _apply(self, want, flips):
        quiet = not settings["click_sound"]
        state = None
        if flips % 2:
            state = "off" if self.rv.flip(quiet) == "on" else "on"
            self.ours = False
            log("flip -> passthrough", state)
        if want is True:
            was = self.rv.set(True, quiet)
            state = "on"
            if was == "off":
                self.ours = True
                log("squint -> passthrough on")
            elif not self.ours:
                log("squint, passthrough already on; leaving it alone")
        elif want is False:
            if self.ours:
                self.rv.set(False, quiet)
                state = "off"
                log("released -> passthrough off")
            self.ours = False
        return state

    def _run(self):
        while True:
            with self.cv:
                if self.want is None and not self.flips:
                    self.cv.wait(timeout=2)
                want, flips, asked = self.want, self.flips, self.asked
                self.want, self.flips, self.asked = None, 0, None
            try:
                if want is None and not flips:
                    state = self.rv.state()   # idle: refresh the page and keep the connection warm
                else:
                    state = self._apply(want, flips)
                    if asked is not None:
                        live["latency_ms"] = round((time.monotonic() - asked) * 1000, 1)
                if state:
                    live["passthrough"] = state
                if self.problem:
                    log("Steam connection back")
                self.problem = live["problem"] = None
            except Exception as e:  # never let this thread die; the next request retries
                live["passthrough"] = None
                if str(e) != self.problem:
                    self.problem = live["problem"] = str(e)
                    log("room view:", e)

class Detector:
    """Turns eye readings into passthrough actions. Pure logic, so it can be tested."""
    def __init__(self, pt):
        self.pt = pt
        self.squinting = False
        self.started = 0.0      # when the current squint triggered
        self.since = None       # when the opposite state started
        self.blink = False      # a blink was seen; wait for the eye to reopen
        self.history = deque(maxlen=8)
        self.winks = [WinkDetector(0), WinkDetector(1)]

    def reset(self, s):
        self.since = None
        self.history.clear()
        if self.squinting:
            self._set(False, s, time.monotonic())

    def _set(self, on, s, now):
        self.squinting = live["squinting"] = on
        self.since = None
        if on:
            self.started = now
        if s["mode"] == "hold":
            self.pt.hold(on, now)
        elif on:
            self.pt.flip(now)

    def _looks_like_blink(self, eye, other, now):
        for t, e0, o0 in reversed(self.history):   # newest sample at least BLINK_WINDOW old
            if now - t >= BLINK_WINDOW:
                drop, other_drop = e0 - eye, o0 - other
                return drop >= BLINK_MIN_DROP and other_drop > BLINK_RATIO * drop
        return False

    def update(self, l, r, now, s):
        eye, other = (r, l) if s["squint_eye"] == "right" else (l, r)
        if s["double_wink"]:
            for w in self.winks:
                if s["wink_eye"] in ("either", ("left", "right")[w.eye]) and w.update((l, r), now, s):
                    log("double wink (" + ("left", "right")[w.eye] + ")")
                    self.pt.flip(now)
        blinkish = s["ignore_blinks"] and self._looks_like_blink(eye, other, now)
        self.history.append((now, eye, other))
        if self.blink and eye > s["release_above"]:
            self.blink = False
        if not self.squinting:
            if blinkish and eye < s["squint_below"] + 0.1:
                self.blink = True
            hit = (not self.blink and eye < s["squint_below"]
                   and s["other_min"] < other <= s["other_max"])
            # With double wink on, a squint must outlast a wink, or every wink would trigger it.
            wait = max(s["hold"], s["wink_max"]) if s["double_wink"] else s["hold"]
        else:
            if (s["ignore_blinks"] and now - self.started < BLINK_CANCEL
                    and other < BLINK_OTHER_SHUT):
                self.blink = True
                log("that was a blink; undoing")
                self._set(False, s, now)
                if s["mode"] == "toggle":
                    self.pt.flip(now)   # two flips cancel out if Steam hasn't applied the first yet
                return
            # Blinks and closed eyes don't end a squint; only the squint eye reopening does.
            hit = eye > s["release_above"]
            wait = s["release_hold"]
        if not hit:
            self.since = None
            return
        if self.since is None:
            self.since = now
        if now - self.since >= wait and (not self.squinting or now - self.started >= s["min_on"]):
            self._set(not self.squinting, s, now)

class WinkDetector:
    """Spots two quick winks of one eye. Returns True from update() on the second."""
    def __init__(self, eye):
        self.eye = eye           # 0 = left, 1 = right
        self.shut_since = None
        self.blinked = False     # the other eye closed too, so this isn't a wink
        self.last_wink = -1e9

    def update(self, eyes, now, s):
        x, other = eyes[self.eye], eyes[1 - self.eye]
        if x < s["wink_below"]:
            if self.shut_since is None:
                self.shut_since, self.blinked = now, False
            if other < WINK_OTHER_MIN:
                self.blinked = True
            return False
        # Reopening needs a little margin above the threshold so noise doesn't count.
        if self.shut_since is None or x < s["wink_below"] + 0.15:
            return False
        length = now - self.shut_since
        self.shut_since = None
        if self.blinked or not 0.03 <= length <= s["wink_max"]:
            return False
        if now - self.last_wink <= s["wink_gap"]:
            self.last_wink = -1e9
            return True
        self.last_wink = now
        return False

def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]

def apply_calibration():
    """Sets thresholds from the open and squint recordings. Returns a message."""
    global settings
    op, sq = calib["samples"]["open"], calib["samples"]["squint"]
    if len(op) < 30 or len(sq) < 30:
        return "Not enough eye data. Is eye tracking running? Try again."
    drop = [statistics.median(x[i] for x in op) - statistics.median(x[i] for x in sq) for i in (0, 1)]
    e = 0 if drop[0] > drop[1] else 1   # the eye that closes more is the squint eye
    o = 1 - e
    e_open, e_sq = pct([x[e] for x in op], 0.1), pct([x[e] for x in sq], 0.9)
    gap = e_open - e_sq
    if gap < 0.1:
        return "Squinting and open eyes looked almost the same. Try a firmer squint."
    o_sq_lo, o_sq_hi = pct([x[o] for x in sq], 0.1), pct([x[o] for x in sq], 0.9)
    o_open = pct([x[o] for x in op], 0.1)
    new = dict(settings)
    new.update({
        "squint_eye": "left" if e == 0 else "right",
        # Trigger early in the squint (60% of the way from your squint to open) for speed.
        "squint_below": round(e_sq + gap * 0.6, 2),
        "release_above": round(e_sq + gap * 0.8, 2),
        "other_min": round(max(0.0, o_sq_lo - 0.15), 2),
        # A gap between the other eye's squint and open readings lets winks be ignored.
        "other_max": round((o_sq_hi + o_open) / 2, 2) if o_open - o_sq_hi > 0.05 else 1.0,
    })
    settings = new
    save_settings()
    name = "Left" if e == 0 else "Right"
    wink = "" if new["other_max"] < 1.0 else " Your other eye barely moves, so winks will count too."
    return f"Done. {name} eye is your squint eye.{wink}"

STATIC = {
    "/": ("tuner.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _trusted(self):
        # Only this machine's own pages: blocks other websites (DNS rebinding) from poking the API.
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            self.send_error(403)
            return False
        return True

    def do_GET(self):
        if not self._trusted():
            return
        path = self.path.split("?")[0]
        if path in STATIC:
            name, ctype = STATIC[path]
            with open(os.path.join(HERE, name), "rb") as f:
                self._send(f.read(), ctype)
        elif path == "/state":
            total = CALIB_SETTLE + CALIB_SECONDS
            left = max(0.0, calib["until"] - time.monotonic()) if calib["phase"] else 0.0
            self._send(json.dumps({**live, "settings": settings, "defaults": DEFAULTS,
                                   "events": list(events),
                                   "calib": {"phase": calib["phase"], "left": left,
                                             "seconds": total, "message": calib["message"]}}).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        global settings
        # A JSON content type forces a CORS preflight, which this server never answers,
        # so other origins can't send these.
        if not self._trusted():
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_error(415)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            self.send_error(400)
            return
        if self.path == "/settings":
            new = dict(settings)
            for k, v in body.items():
                if k in DEFAULTS and type(v) in (type(DEFAULTS[k]), int):
                    new[k] = type(DEFAULTS[k])(v)
            settings = new
            save_settings()
        elif self.path == "/reset":
            settings = dict(DEFAULTS)
            try: os.remove(SETTINGS_FILE)
            except OSError: pass
        elif self.path == "/flip":
            pt.flip()
        elif self.path == "/calibrate":
            phase = body.get("phase")
            if phase in ("open", "squint"):
                calib["samples"][phase] = []
                calib["message"] = ""
                calib["start"] = time.monotonic() + CALIB_SETTLE
                calib["until"] = calib["start"] + CALIB_SECONDS
                calib["phase"] = phase
            elif phase == "apply":
                calib["phase"] = None
                calib["message"] = apply_calibration()
                log("calibration:", calib["message"])
            else:
                calib["phase"] = None
                calib["message"] = ""
        self._send(b"{}")

pt = None

def main():
    global pt
    ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"port {PORT} is in use; SquintThru is probably already running (squintthru status)")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    rv = roomview.RoomView()
    pt = Passthrough(rv)
    try:
        live["passthrough"] = rv.state()   # also warms up the Steam connection before the first squint
    except Exception as e:
        log("room view:", e)
    detector = Detector(pt)
    log(f"watching for squints; app at http://127.0.0.1:{PORT}")
    for l, r in eyes.stream(log):
        live["l"], live["r"], live["eyes"] = l, r, True
        now = time.monotonic()
        if calib["phase"]:
            if now >= calib["until"]:
                calib["phase"] = None
            elif now >= calib["start"]:
                calib["samples"][calib["phase"]].append((l, r))
        s = settings
        # Calibrating means squinting on purpose, so don't act on it.
        if not s["enabled"] or calib["phase"]:
            detector.reset(s)
            continue
        detector.update(l, r, now, s)

if __name__ == "__main__":
    main()

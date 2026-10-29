# Turns on Steam Frame passthrough while you squint, and back off when you stop.
# Squint = the squint eye nearly shut while the other stays mostly open (from
# calibration: open L1.0/R1.0, squint L~0.85/R~0.03, closed L~0.21/R0.0).
# Serves a live tuning page at http://127.0.0.1:8765.
import json, os, statistics, subprocess, sys, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import roomview

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")
PORT = 8765
CALIB_SECONDS = 3.0

DEFAULTS = {
    "enabled": True,
    "mode": "hold",          # "hold": on while squinting; "toggle": each squint flips it
    "squint_eye": "right",   # the eye that nearly shuts when you squint
    "squint_below": 0.79,    # squint eye below this...
    "other_min": 0.0,        # ...while the other eye is in this band
    "other_max": 0.93,
    "release_above": 0.9,    # squint eye above this counts as "stopped squinting"
    "hold": 0.02,            # seconds a squint must last before acting
    "release_hold": 0.02,    # seconds the eye must stay open before releasing
    "min_on": 0.02,          # passthrough stays on at least this long once triggered
    "double_wink": False,    # two quick winks of one eye toggle passthrough
    "wink_eye": "right",     # "left", "right" or "either"
    "wink_below": 0.35,      # winking eye below this counts as shut
    "wink_max": 0.4,         # longest a single wink can last, in seconds
    "wink_gap": 0.6,         # most time allowed between the two winks
}
WINK_OTHER_MIN = 0.6  # the other eye must stay this open, so blinks never count as winks

def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE) as f:
            s.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return s

def save_settings():
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

settings = load_settings()
live = {"l": 0.0, "r": 0.0, "squinting": False, "passthrough": None}
events = deque(maxlen=8)
calib = {"phase": None, "until": 0.0, "samples": {"open": [], "squint": []}, "message": ""}

def log(*a):
    line = " ".join(map(str, a))
    events.appendleft(time.strftime("%H:%M:%S ") + line)
    print(time.strftime("%H:%M:%S"), line, flush=True)

class Passthrough:
    """Applies only the latest wanted state, so quick repeat squints never queue up."""
    def __init__(self):
        self.want = None   # hold mode: True/False
        self.flips = 0     # toggle mode and the page button
        self.ours = False  # True only if we turned it on (hold mode)
        self.cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def hold(self, on):
        with self.cv:
            self.want = on
            self.cv.notify()

    def flip(self):
        with self.cv:
            self.flips += 1
            self.cv.notify()

    def _run(self):
        while True:
            with self.cv:
                if self.want is None and not self.flips:
                    self.cv.wait(timeout=2)  # also refreshes the state shown on the page
                want, flips = self.want, self.flips
                self.want, self.flips = None, 0
            try:
                state = roomview.active()
                if flips % 2:
                    state = roomview.toggle(); self.ours = False
                    log("flip -> passthrough", state)
                if want is True and state == "off":
                    state = roomview.toggle(); self.ours = True
                    log("squint -> passthrough", state)
                elif want is True and not self.ours:
                    log("squint, passthrough already on; leaving it alone")
                elif want is False and self.ours and state == "on":
                    state = roomview.toggle()
                    log("released -> passthrough", state)
                if want is False:
                    self.ours = False
                live["passthrough"] = state
            except Exception as e:
                live["passthrough"] = None
                log("room view error:", e)

pt = Passthrough()

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
        return "Not enough eye data. Try again."
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
        "squint_below": round(e_sq + gap * 0.4, 2),
        "release_above": round(e_sq + gap * 0.7, 2),
        "other_min": round(max(0.0, o_sq_lo - 0.15), 2),
        # A gap between the other eye's squint and open readings lets winks be ignored.
        "other_max": round((o_sq_hi + o_open) / 2, 2) if o_open - o_sq_hi > 0.05 else 1.0,
    })
    settings = new
    save_settings()
    name = "Left" if e == 0 else "Right"
    wink = "" if new["other_max"] < 1.0 else " Your other eye barely moves, so winks will count too."
    return f"Done. {name} eye is your squint eye.{wink}"

def page():
    with open(os.path.join(HERE, "tuner.html"), "rb") as f:
        return f.read()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(page(), "text/html; charset=utf-8")
        elif self.path == "/state":
            left = max(0.0, calib["until"] - time.monotonic()) if calib["phase"] else 0.0
            self._send(json.dumps({**live, "settings": settings, "defaults": DEFAULTS,
                                   "events": list(events),
                                   "calib": {"phase": calib["phase"], "left": left,
                                             "seconds": CALIB_SECONDS, "message": calib["message"]}}).encode())
        else:
            self.send_error(404)

    def do_POST(self):
        global settings
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
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
                calib["until"] = time.monotonic() + CALIB_SECONDS
                calib["phase"] = phase
            elif phase == "apply":
                calib["phase"] = None
                calib["message"] = apply_calibration()
                log("calibration:", calib["message"])
            else:
                calib["phase"] = None
                calib["message"] = ""
        self._send(b"{}")

def main():
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    eyes = subprocess.Popen([os.path.join(HERE, "eyestream")], stdout=subprocess.PIPE, text=True)
    pt.hold(False)
    squinting = False
    started = 0.0  # when the current squint triggered
    since = None   # when the opposite state started
    winks = [WinkDetector(0), WinkDetector(1)]
    log(f"watching for squints; tuner at http://127.0.0.1:{PORT}")
    for line in eyes.stdout:
        l, r = map(float, line.split())
        live["l"], live["r"] = l, r
        now = time.monotonic()
        if calib["phase"]:
            if now < calib["until"]:
                calib["samples"][calib["phase"]].append((l, r))
            else:
                calib["phase"] = None
        s = settings
        eye, other = (r, l) if s["squint_eye"] == "right" else (l, r)
        # Calibrating means squinting on purpose, so don't act on it.
        if not s["enabled"] or calib["phase"]:
            since = None
            if squinting:
                squinting = live["squinting"] = False
                if s["mode"] == "hold": pt.hold(False)
            continue
        if s["double_wink"]:
            for w in winks:
                if s["wink_eye"] in ("either", ("left", "right")[w.eye]) and w.update((l, r), now, s):
                    log("double wink (" + ("left", "right")[w.eye] + ")")
                    pt.flip()
        if not squinting:
            hit = eye < s["squint_below"] and s["other_min"] < other <= s["other_max"]
            # With double wink on, a squint must outlast a wink, or every wink would trigger it.
            wait = max(s["hold"], s["wink_max"]) if s["double_wink"] else s["hold"]
        else:
            # Blinks and closed eyes don't end a squint; only the squint eye reopening does.
            hit = eye > s["release_above"]
            wait = s["release_hold"]
        if not hit:
            since = None
        elif since is None:
            since = now
        elif now - since >= wait and (not squinting or now - started >= s["min_on"]):
            squinting = live["squinting"] = not squinting
            since = None
            if squinting:
                started = now
            if s["mode"] == "hold":
                pt.hold(squinting)
            elif squinting:
                pt.flip()
    sys.exit("eye stream ended")

if __name__ == "__main__":
    main()

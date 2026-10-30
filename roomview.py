# Reads or switches Steam Frame Room View (passthrough) by driving the SteamVR
# dashboard bar's button over Steam's CEF debug port. Approach from KnockThru.
#
# Built for speed: one websocket to the bar page stays open, a small helper is
# installed in the page once, and each change is a single Runtime.evaluate
# round trip that both checks the state and clicks.
import json, os, select, socket, sys, threading, urllib.request

PORT = 8080

# Installed into the bar page as window.__squint3 (bump the name when this changes:
# the page outlives SquintThru, so an old helper stays installed). It caches the Room View button
# and Steam's nav sound controller, and remembers a state it just asked for, so
# a quick on-then-off never reads React's not-yet-updated props.
HELPER = r"""
window.__squint3 = (() => {
  const isRoomView = (a) => a?.invocation === 2 && a?.icon?.enum === 2 && a?.icon_active?.enum === 1;
  const actionOf = (button) => {
    const key = Object.keys(button).find((k) => k.startsWith("__reactFiber"));
    for (let f = key && button[key], depth = 0; f && depth < 16; f = f.return, depth++) {
      const a = f.memoizedProps?.action;
      if (isRoomView(a)) return a;
    }
  };
  let button = null;
  const find = () => {
    if (button && button.isConnected && actionOf(button)) return button;
    button = [...document.querySelectorAll('[role="button"]')].find(actionOf) || null;
    return button;
  };
  // Steam plays its click sound after every dashboard action. The click queues
  // it on a 1 ms timer, and SuppressImminentNavSound() cancels it. The module
  // is found by its source, since webpack ids change between Steam builds.
  const nav = () => {
    const w = opener;
    if (!w.__squintNav) {
      let req; w.webpackChunksteamui.push([[Symbol("squint")], {}, (r) => { req = r; }]);
      for (const id of Object.keys(req.m)) {
        if (!String(req.m[id]).includes("SuppressImminentNavSound(){")) continue;
        const n = Object.values(req(id)).find((v) => v && typeof v.SuppressImminentNavSound === "function");
        if (n) { w.__squintNav = n; break; }
      }
    }
    return w.__squintNav;
  };
  const quiet = () => { try { nav().SuppressImminentNavSound(); } catch (e) {} };
  // Steam's active prop can lag a press by a second or more, and can match the
  // wanted state by chance before going stale, so trust the press for the full window.
  let assumed = null, assumedAt = 0;
  const current = (b) => {
    if (assumed !== null) {
      if (performance.now() - assumedAt < 2000) return assumed;
      assumed = null;
    }
    return !!actionOf(b).active;
  };
  const press = (b, on, silent) => {
    b.click();
    if (silent) quiet();
    assumed = on; assumedAt = performance.now();
  };
  return {
    // Each returns the state before the call: "on", "off", or "missing".
    state() { const b = find(); return b ? (current(b) ? "on" : "off") : "missing"; },
    set(on, silent) {
      const b = find(); if (!b) return "missing";
      const was = current(b);
      if (was !== on) press(b, on, silent);
      return was ? "on" : "off";
    },
    flip(silent) {
      const b = find(); if (!b) return "missing";
      const was = current(b);
      press(b, !was, silent);
      return was ? "on" : "off";
    },
  };
})();
"""

class RoomViewError(RuntimeError):
    pass

def bar_path(port=PORT):
    try:
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
    except OSError as e:
        raise RoomViewError("Steam's debug port isn't open yet. Restart your Steam Frame once after installing.") from e
    for t in targets:
        if t.get("url", "").endswith("vrOverlayKey=valve.steam.gamepadui.bar"):
            ws = t["webSocketDebuggerUrl"]
            return ws[ws.index("/", ws.index("://") + 3):]
    raise RoomViewError("SteamVR dashboard bar not found (is SteamVR running?)")

class RoomView:
    """A persistent connection to the dashboard bar. Thread-safe; reconnects on its own."""
    def __init__(self, port=PORT):
        self.port = port
        self.sock = self.rf = None
        self.lock = threading.Lock()
        self.next_id = 0

    # --- public API: each returns the state before the call ---
    def state(self):
        return self._helper("state()")

    def set(self, on, quiet=True):
        return self._helper(f"set({'true' if on else 'false'}, {'true' if quiet else 'false'})")

    def flip(self, quiet=True):
        return self._helper(f"flip({'true' if quiet else 'false'})")

    def check(self):
        """Makes sure there's a working connection, without sending Steam anything if there
        is one: a closed socket shows up as readable with no data. Reconnects if needed."""
        with self.lock:
            if self.sock:
                try:
                    readable = select.select([self.sock], [], [], 0)[0]
                    if not readable or self.sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        return   # still connected (any unread message is skipped by _evaluate)
                except BlockingIOError:
                    return
                except OSError:
                    pass
                self.close()
        self.state()   # connect, install the helper, and read the state

    def connect(self):
        """Opens the connection and installs the helper ahead of time, so the first squint is fast."""
        self.state()

    def close(self):
        if self.sock:
            try: self.sock.close()
            except OSError: pass
        self.sock = self.rf = None

    # --- internals ---
    def _helper(self, call):
        # Page reloads (Steam UI restarts) drop the helper, so reinstall when it's gone.
        expr = f"window.__squint3 ? window.__squint3.{call} : 'nohelper'"
        with self.lock:
            for attempt in (0, 1):
                try:
                    if not self.sock:
                        self._open()
                    value = self._evaluate(expr)
                    if value == "nohelper":
                        self._evaluate(HELPER + "'ok'")
                        value = self._evaluate(expr)
                    if value == "missing":
                        raise RoomViewError("Room View button not found on the dashboard bar")
                    return value
                except (OSError, ValueError, ConnectionError) as e:
                    self.close()
                    if attempt:
                        raise RoomViewError(f"lost the Steam debug connection: {e}") from e

    def _open(self):
        path = bar_path(self.port)
        s = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: c3F1aW50LXBhc3N0aHJ1MQ==\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        rf = s.makefile("rb")
        status = rf.readline()
        if not status.startswith(b"HTTP/1.1 101"):
            s.close()
            raise ConnectionError("debug port refused websocket")
        while rf.readline() not in (b"\r\n", b""):
            pass
        self.sock, self.rf = s, rf

    def _send(self, opcode, payload):
        n = len(payload)
        if n < 126: head = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536: head = bytes([0x80 | opcode, 0xFE]) + n.to_bytes(2, "big")
        else: head = bytes([0x80 | opcode, 0xFF]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        keystream = (mask * (n // 4 + 1))[:n]
        masked = (int.from_bytes(payload, "big") ^ int.from_bytes(keystream, "big")).to_bytes(n, "big") if n else b""
        self.sock.sendall(head + mask + masked)

    def _read(self, k):
        d = self.rf.read(k)
        if len(d) < k:
            raise ConnectionError("debug connection closed")
        return d

    def _frame(self):
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126: n = int.from_bytes(self._read(2), "big")
        elif n == 127: n = int.from_bytes(self._read(8), "big")
        return b0 & 0x0F, self._read(n)

    def _evaluate(self, expr):
        self.next_id += 1
        msg_id = self.next_id
        self._send(0x1, json.dumps({"id": msg_id, "method": "Runtime.evaluate",
                                    "params": {"expression": expr, "returnByValue": True}}).encode())
        message = b""
        while True:
            opcode, data = self._frame()
            if opcode == 0x8:
                raise ConnectionError("debug connection closed")
            if opcode == 0x9:
                self._send(0xA, data)
                continue
            if opcode in (0x1, 0x2, 0x0):
                message += data
                # Steam doesn't fragment replies in practice; a partial JSON just waits for more.
                try:
                    reply = json.loads(message)
                except ValueError:
                    continue
                message = b""
                if reply.get("id") != msg_id:
                    continue
                result = reply.get("result", {})
                if "exceptionDetails" in result:
                    raise RoomViewError("page script failed: " + json.dumps(result["exceptionDetails"])[:200])
                return result.get("result", {}).get("value")

_shared = None
def _rv():
    global _shared
    _shared = _shared or RoomView()
    return _shared

def active():
    return _rv().state()

def toggle(quiet=True):
    """Flips Room View and returns the new state."""
    return "off" if _rv().flip(quiet) == "on" else "on"

if __name__ == "__main__":
    try:
        cmd = sys.argv[1:]
        if cmd == ["toggle"]: print(toggle())
        elif cmd in (["on"], ["off"]):
            _rv().set(cmd == ["on"]); print(cmd[0])
        else: print(active())
    except RoomViewError as e:
        sys.exit(str(e))

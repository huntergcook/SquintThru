# Reads or toggles Steam Frame Room View (passthrough) by driving the SteamVR
# dashboard bar's button over Steam's CEF debug port. Approach from KnockThru.
import json, os, socket, sys, time, urllib.request

PORT = 8080
FIND = """
  const roomViewAction = (button) => {
    const key = Object.keys(button).find(k => k.startsWith("__reactFiber"));
    for (let f = key && button[key], depth = 0; f && depth < 16; f = f.return, depth++) {
      const a = f.memoizedProps?.action;
      if (a?.invocation === 2 && a?.icon?.enum === 2 && a?.icon_active?.enum === 1) return a;
    }
  };
  const button = [...document.querySelectorAll('[role="button"]')].find(roomViewAction);
  if (!button) return 'missing';
"""

def bar_path():
    for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=3)):
        if t.get("url", "").endswith("vrOverlayKey=valve.steam.gamepadui.bar"):
            ws = t["webSocketDebuggerUrl"]
            return ws[ws.index("/", ws.index("://") + 3):]
    raise SystemExit("SteamVR dashboard bar not found")

def evaluate(expr):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=3)
    s.sendall((f"GET {bar_path()} HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: c3F1aW50LXBhc3N0aHJ1MQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf: buf += s.recv(1)
    if not buf.startswith(b"HTTP/1.1 101"): raise SystemExit("debug port refused websocket")
    msg = json.dumps({"id": 1, "method": "Runtime.evaluate",
                      "params": {"expression": expr, "returnByValue": True}}).encode()
    mask = os.urandom(4)
    n = len(msg)
    head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + n.to_bytes(2, "big"))
    s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(msg)))
    def recvn(k):
        d = b""
        while len(d) < k: d += s.recv(k - len(d))
        return d
    while True:
        h = recvn(2); ln = h[1] & 0x7F
        if ln == 126: ln = int.from_bytes(recvn(2), "big")
        elif ln == 127: ln = int.from_bytes(recvn(8), "big")
        reply = json.loads(recvn(ln))
        if reply.get("id") == 1:
            s.close()
            return reply["result"]["result"].get("value")

def active():
    return evaluate("(() => {" + FIND + " return roomViewAction(button).active ? 'on' : 'off'; })()")

# Steam plays its button-click sound after every dashboard action. Its nav sound
# controller has SuppressImminentNavSound(), which cancels a queued one. The
# module is found by its source, since webpack ids change between Steam builds.
SUPPRESS = """
  try {
    const w = opener;
    if (!w.__squintNav) {
      let req; w.webpackChunksteamui.push([[Symbol("squint")], {}, r => { req = r; }]);
      for (const id of Object.keys(req.m)) {
        if (!String(req.m[id]).includes("SuppressImminentNavSound(){")) continue;
        const nav = Object.values(req(id)).find(v => v && typeof v.SuppressImminentNavSound === "function");
        if (nav) { w.__squintNav = nav; break; }
      }
    }
    w.__squintNav.SuppressImminentNavSound();
  } catch (e) {}
"""

def toggle(quiet=True):
    before = active()
    # The click queues the sound on a 1 ms timer; suppressing right after cancels it.
    evaluate("(() => {" + FIND + " button.click();" + (SUPPRESS if quiet else "") + " return 'clicked'; })()")
    for _ in range(10):
        time.sleep(0.1)
        if (now := active()) != before: return now
    return before + " (unchanged)"

if __name__ == "__main__":
    print(toggle() if sys.argv[1:] == ["toggle"] else active())

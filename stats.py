# Measures what SquintThru costs on this headset.
#   squintthru stats             10 s snapshot: SquintThru's CPU and wakeups, the eye-tracking
#                                process's CPU, and the battery draw if the system reports it
#   squintthru stats --compare   alternates SquintThru on and off to show the difference it makes
import glob, os, subprocess, sys, time

TICK = os.sysconf("SC_CLK_TCK")
UNIT = "squintthru.service"

def pids_running(match):
    found = []
    for d in glob.glob("/proc/[0-9]*"):
        try:
            if match(int(d[6:]), open(d + "/cmdline", "rb").read()):
                found.append(int(d[6:]))
        except OSError:
            pass
    return found

def squintthru_pids():
    def is_service(pid, cmd):
        args = cmd.split(b"\0")
        return (pid != os.getpid() and os.path.basename(args[0]).startswith(b"python")
                and any(a.endswith(b"squint.py") for a in args[1:]))
    return pids_running(is_service)

def eye_server_pids(exclude):
    """Processes that map the eye server's shared memory, other than SquintThru."""
    def maps_it(pid, cmd):
        if pid in exclude or pid == os.getpid():
            return False
        try:
            return "eye-server.mmap" in open(f"/proc/{pid}/maps").read()
        except OSError:
            return False   # another user's process: not visible without root
    return pids_running(maps_it)

def name(pid):
    try:
        return open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        return str(pid)

def sample(pids):
    """Total CPU seconds and context switches (wakeups) across all threads of these processes."""
    cpu = switches = 0
    for pid in pids:
        for task in glob.glob(f"/proc/{pid}/task/*"):
            try:
                fields = open(task + "/stat").read().rsplit(")", 1)[1].split()
                cpu += (int(fields[11]) + int(fields[12])) / TICK
                for line in open(task + "/status"):
                    if line.startswith(("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")):
                        switches += int(line.split()[1])
            except (OSError, IndexError, ValueError):
                pass
    return cpu, switches

def battery_watts():
    """Battery discharge in watts, if the kernel reports it; None otherwise."""
    for bat in glob.glob("/sys/class/power_supply/*"):
        try:
            if open(bat + "/type").read().strip() != "Battery":
                continue
            if os.path.exists(bat + "/power_now"):
                return abs(int(open(bat + "/power_now").read())) / 1e6
            current = abs(int(open(bat + "/current_now").read()))
            voltage = int(open(bat + "/voltage_now").read())
            return current * voltage / 1e12
        except (OSError, ValueError):
            continue
    return None

def measure(seconds, ours, eye):
    a, b = sample(ours), sample(eye)
    watts, t0 = [], time.monotonic()
    while time.monotonic() - t0 < seconds:
        w = battery_watts()
        if w is not None:
            watts.append(w)
        time.sleep(1)
    dt = time.monotonic() - t0
    a2, b2 = sample(ours), sample(eye)
    return {"cpu": 100 * (a2[0] - a[0]) / dt, "wakeups": (a2[1] - a[1]) / dt,
            "eye_cpu": 100 * (b2[0] - b[0]) / dt if eye else None,
            "watts": sum(watts) / len(watts) if watts else None}

def fmt(r, label):
    parts = [f"{label:16s}"]
    if r["cpu"] is not None:
        parts.append(f"SquintThru {r['cpu']:5.2f}% of one core, {r['wakeups']:5.1f} wakeups/s")
    if r["eye_cpu"] is not None:
        parts.append(f"eye tracking {r['eye_cpu']:5.2f}%")
    if r["watts"] is not None:
        parts.append(f"battery {r['watts']:5.2f} W")
    return "   ".join(parts)

def service(action):
    return subprocess.run(["systemctl", "--user", action, UNIT], capture_output=True).returncode == 0

def main():
    compare = "--compare" in sys.argv
    ours = squintthru_pids()
    if not ours:
        sys.exit("SquintThru isn't running. Start it with: squintthru start")
    eye = eye_server_pids(set(ours))
    print("SquintThru:", ", ".join(f"{name(p)} ({p})" for p in ours))
    print("Eye tracking:", ", ".join(f"{name(p)} ({p})" for p in eye) if eye
          else "not visible to this user (it may run as another user); skipping its CPU")
    if battery_watts() is None:
        print("Battery draw: not reported by this system; skipping it")
    print("Put the headset on and keep still (or leave it in one scene) while this runs.\n")
    if not compare:
        print(fmt(measure(10, ours, eye), "now (10 s)"))
        print("\nFor the difference SquintThru makes, run: squintthru stats --compare")
        return
    rounds, seconds = 2, 30
    print(f"Comparing: {rounds} rounds of {seconds} s with SquintThru on, then off. About {rounds * seconds * 2 // 60} minutes.")
    on, off = [], []
    try:
        for i in range(rounds):
            ours = squintthru_pids()
            on.append(measure(seconds, ours, eye))
            print(fmt(on[-1], f"round {i + 1}: on"))
            if not service("stop"):
                sys.exit("Couldn't stop the SquintThru service (is it installed with install.sh?)")
            time.sleep(2)
            off.append(measure(seconds, [], eye))
            off[-1]["cpu"] = None
            print(fmt(off[-1], f"round {i + 1}: off"))
            service("start")
            time.sleep(3)
    finally:
        service("start")
    def avg(rs, k):
        vals = [r[k] for r in rs if r[k] is not None]
        return sum(vals) / len(vals) if vals else None
    print("\nDifference SquintThru makes:")
    print(f"  SquintThru itself:  {avg(on, 'cpu'):.2f}% of one core, {avg(on, 'wakeups'):.0f} wakeups/s")
    if avg(on, "eye_cpu") is not None:
        print(f"  Eye tracking:       {avg(on, 'eye_cpu') - avg(off, 'eye_cpu'):+.2f}% of one core while SquintThru runs")
    if avg(on, "watts") is not None and avg(off, "watts") is not None:
        print(f"  Battery draw:       {avg(on, 'watts') - avg(off, 'watts'):+.2f} W "
              "(the headset's own changes from second to second are usually bigger than this)")

if __name__ == "__main__":
    main()

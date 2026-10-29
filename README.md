# SquintThru

Turns on Steam Frame passthrough (Room View) while you squint, and turns it back off when you open your eye again. Runs on the Steam Frame itself.

## How it works

- `eyestream` (built from `eyestream.c`) reads eye openness from the headset's eye server (`/dev/shm/eye-server.mmap`) and prints `<left> <right>` about 60 times a second.
- `squint.py` watches those values. When your squint eye drops below a threshold and the other eye stays open, it flips Room View on.
- `roomview.py` clicks the Room View button on the SteamVR dashboard bar through Steam's CEF debug port (`127.0.0.1:8080`).
- `tuner.html` is a live tuning page served by `squint.py` at http://127.0.0.1:8765.

## Requirements

- A Steam Frame with eye tracking, with SteamVR running.
- Steam running with CEF remote debugging on, so port 8080 is open. Here Steam runs with `-cef-enable-debugging`. Check it with `curl -s http://127.0.0.1:8080/json`.
- Python 3 (standard library only) and `gcc`.

## Setup

```sh
git clone https://github.com/huntergcook/SquintThru.git
cd SquintThru
gcc -O2 -o eyestream eyestream.c -lpthread
```

## Running

```sh
nohup python3 -u squint.py >> squint.log 2>&1 &
xdg-open http://127.0.0.1:8765      # the tuner page
```

The log shows `watching for squints; tuner at http://127.0.0.1:8765` once it's running, plus one line for each squint or release.

To stop it:

```sh
pkill -f "^python3 -u squint.py"
```

`eyestream` exits along with it. To check Room View by hand, run `python3 roomview.py` (prints `on` or `off`) or `python3 roomview.py toggle`.

## Settings

Changes in the tuner save to `settings.json` right away. **Reset to defaults** deletes that file and goes back to these defaults, which work well in practice:

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `hold` | `hold`: on while squinting. `toggle`: each squint flips it. |
| `squint_eye` | `right` | The eye that nearly shuts when you squint |
| `squint_below` | 0.79 | The squint eye must be below this... |
| `other_min` / `other_max` | 0.0 / 0.93 | ...while the other eye is in this band |
| `release_above` | 0.9 | The squint eye above this counts as open again |
| `hold` | 20 ms | How long a squint must last before passthrough turns on (tuner: 1–100 ms) |
| `release_hold` | 20 ms | How long the eye must stay open before it turns off (tuner: 1–100 ms) |
| `min_on` | 20 ms | Once on, passthrough stays on at least this long |
| `double_wink` | off | Two quick winks of `wink_eye` toggle passthrough |
| `wink_eye` / `wink_below` / `wink_max` / `wink_gap` | right / 0.35 / 0.4 s / 0.6 s | Double-wink tuning |

Eye data arrives about every 17 ms, so timings below that act on the next reading.

## Known issues

- **Calibration doesn't work well yet.** The tuner's **Start calibration** button records your open and squint eyes and sets thresholds from them, but the results aren't reliable. For now, set the thresholds by dragging the handles on the eye bars. Improving calibration is future work.

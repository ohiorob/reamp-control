# REAMP-Control

Unofficial Python control, logging and a live browser app for the RigExpert REAMP power-consumption logger (USB HID "uCurrentMeter", VID 0483 / PID A1DE, firmware "RigExpert Current logger v1.09"). Not affiliated with or endorsed by RigExpert.
The protocol was reverse-engineered from REAMP-Logger 2.0.7 (full packet map at the top of `reamp.py`).

## Requirements

- Windows. The USB layer talks to Windows' built-in HID driver through ctypes, so there is no driver to install.
- Python 3 (developed with 3.14), plus numpy for the live app and matplotlib for plots:

```powershell
# PowerShell
py -3 -m pip install -r requirements.txt
```

Get the code with the green **Code → Download ZIP** button on GitHub, or:

```powershell
# PowerShell
git clone https://github.com/ohiorob/reamp-control.git
```

## Live app (REAMP-Logger replacement)

Double-click **`REAMP App.bat`**, or:

```powershell
# PowerShell
cd reamp-control              # the folder you cloned or unzipped
py -3 reamp_app.py            # opens http://127.0.0.1:8765/ in your browser
py -3 reamp_app.py --demo     # simulated REAMP, for trying the UI without hardware
```

Close REAMP-Logger first; only one program can hold the REAMP. Close the app with Ctrl+C in its window.

The page copies the REAMP-Logger layout: toolbar (Save CSV/PNG, Open, View, Settings, Show N sec, Report, Markers, Battery Calculator, Measuring time), Control dock (power output, Vin source, sync, sample rate, channels and multipliers, TRIG / TRIG-level / Start / Stop), capture tabs, and a chart with the current axis on the left and voltage axes on the right, plus a navigation bar with event marks.
It plots live while measuring. Each capture is saved automatically to `logs\reamp_<date>_<time>.csv` with timebase-corrected timestamps.

- Start / stop: big Start, Stop, TRIG and TRIG level buttons (solid green while running, orange while waiting for TRIG), a Start/Stop toggle and a Restart button at the lower right of the chart, and keys **Space** (start/stop), **R** (restart), **F** (fit).
- Output voltage: per-digit setter (arrows, mouse wheel over a digit, or keyboard), a slider, **Apply instantly**, and a **Limit** (default 3300 mV) that the controls cannot exceed. Voltage and output on/off also work during a capture.
- Big readouts across the top: status, current, Vin1, Vin2, output setpoint and state (ON green, DELAYED amber, OFF or ERROR red), REAMP supply, and Signals (TRIG, SYNC, OK/ERROR: green = active or OK, grey = inactive, red = fault). These replace the original's Status pane and its LEDs.
- Ripple: while measuring, each readout also shows the AC rms of its last 0.3 s; the report tables and the marker tip add p-p (max − min) and AC rms (standard deviation = ripple + noise with the DC removed). Only changes slower than half the sample rate are seen, so switching-regulator ripple at hundreds of kHz is out of reach.
- Trace smoothing: 10 levels per channel (moving average of 3 to 1025 samples), display only.
- Interface size: A− / 100% / A+ in the toolbar (80–200 %), or the browser's Ctrl + / Ctrl −.
- Help: hover over any control, click a **?**, or press Help for the shortcuts.
- Zoom: drag across the chart, or use the mouse wheel; double-click or the Fit button resets the zoom. While measuring, zoom is off and the view follows the newest data.
- Markers: drag the green T1/T2 flags or lines; the yellow tip and the right-hand report table give min/max/average between them.
- Y axes: automatic by default. Drag up on an axis to zoom that trace in, down to zoom out (the mouse wheel over an axis does the same); Shift + drag moves it; double-click the axis or press Fit to go back to automatic. Each channel's scale can go on the left or the right (all on one side works too): drag the axis sideways to either half of the chart, click the arrow button under it, or use Settings → Y axis side.
- Battery Calculator: battery life = capacity ÷ average current, with the average taken from the marker window, the visible area, the whole capture, a working cycle, or a current you type in. The dialog explains what it's for and how to get a trustworthy average.
- Voltages are DC: each sample is the Vin pin's voltage against GND at that moment, and every "average" is the arithmetic mean of samples, not RMS.
- Open: reloads any CSV in `logs\`. `logs\sample_delayed_switch_on.csv` is included so you can try it without a REAMP: 7 s of current and Vin1 at 20 Hz, with the output in Delayed mode switching on at the start.

Tested on one REAMP (firmware v1.09) on 11/25/2026: 1 kHz on two channels for 50 s gave 50,288 samples, none lost; 10 kHz on one channel gave 8,999 samples/s, none lost.

## First run: calibrate the timebase

Each REAMP's sample clock is a little off (see below). Run this once with REAMP-Logger closed; it takes about 80 s and saves the measured rates for your unit's serial number in `reamp_timebase.json`. Without it, timestamps use the nominal rates.

```powershell
# PowerShell
py reamp_cli.py calibrate
```

## Quick start (PowerShell)

```powershell
# PowerShell
cd reamp-control

# readout (works with REAMP-Logger open or closed)
py reamp_cli.py status
py reamp_cli.py watch                          # live, ~10 readings/s, Ctrl+C to stop
py reamp_cli.py watch -i 0.05 --csv run1.csv   # log the slow readout to CSV

# high-rate logging (close REAMP-Logger first) -> logs\reamp_<timestamp>.csv
py reamp_cli.py log -t 60 --plot                         # current, 1 kHz, 60 s, with PNG plot
py reamp_cli.py log -r 10000 -t 600 --plot --battery-mah 55   # fastest rate, 10 min, battery-life estimate
py reamp_cli.py log -c 0 1 2 -r 1000 -t 30 --plot        # current + Vin1 + Vin2

# supply control
py reamp_cli.py set 3100            # output setpoint in mV (1800..5200, exact)
py reamp_cli.py on                  # DUT power on (permanent)
py reamp_cli.py off
py reamp_cli.py source bat          # battery source for loads over ~500 mA
py reamp_cli.py reset-errors

# re-measure the true sample rates (about 80 s, REAMP-Logger closed)
py reamp_cli.py calibrate
```

## What the readings mean

| Field | Source | Unit |
|---|---|---|
| I (status/watch) | channel #0, DUT current | mA (shown as uA when < 1 mA) |
| current_uA (log) | channel #0 | uA, auto-ranging 2 mA / 20 mA / 200 mA / 2 A full scale, 0.12 uA steps on the lowest range |
| Vin1, Vin2 | channels #1, #2 | mV |
| Vout set | the setpoint the REAMP is holding (not measured) | mV |
| REAMP supply | USB or battery input to the REAMP | V |

**To measure the real output voltage**, jumper Vout (pin 1) to Vin1 and read Vin1.

## Things the vendor software doesn't tell you

- **The output moves in 25 mV steps, rounded down** (measured 11/25/2026 with a watch on Vout and Vin1): setpoints 3100, 3110 and 3121 mV all gave 3.098 V; 3125–3149 mV gave 3.123 V; 3150 mV gave 3.147 V. The manual says 50 mV steps. So 3121 mV really means about 3.10 V.
- **Sample-rate error.** The development unit's clock runs about 0.53% fast, and its "10 kHz" setting really delivers about 9,002 Hz (5 kHz gives about 4,932 Hz). REAMP-Logger timestamps assume the nominal rate. These tools use the rates measured by `py reamp_cli.py calibrate`, which were checked against both the PC clock and the 1.000 s tick of a quartz watch.
- **Below 10 uA (the rated floor),** treat averages with caution as well as single samples. In a 10-minute log of a ~2 uA watch, 35.6% of samples read exactly 0 and the median was 0.61 uA, against a mean of 2.18 uA. The current reading can't go below zero, so the true average may be lower (if noise is being clipped) or higher (if the REAMP reads small currents as 0). Check against a DMM in series, plus a log with the DUT disconnected, before quoting a battery-life figure for very low-current devices.
- **Lost data** is detected from each packet's sample index and reported as a warning. Timestamps stay true to the sample index, so a gap shows up as a jump in `t_s` rather than being hidden. To avoid gaps at 10 kHz, don't run `watch` at the same time and keep the REAMP off busy USB hubs.

## If the REAMP stops responding

Sometimes the REAMP still shows up in Windows but stops replying: `status` prints "No reply from REAMP" and `log` says it isn't responding. Only a power cycle clears this. The output voltage the REAMP comes back up with hasn't been checked yet, so protect the DUT:

1. Disconnect the DUT, or pull the 6-pin connector.
2. Unplug the REAMP's USB cable, wait 5 s, then plug it back in.
3. `py reamp_cli.py status` and check "Vout set" and "output".
4. `py reamp_cli.py set 3100`, then `py reamp_cli.py on` (use your DUT's voltage).
5. Check `status` again, then reconnect the DUT.

## Using it in your own scripts

```python
from reamp import REAMP
with REAMP() as r:
    print(r.status())                      # Vout set, output state, I, Vin1, Vin2...
    r.set_voltage(3100); r.output_on()
    for i0, dt, block in r.stream_blocks(channels=(0, 1), rate_hz=1000, duration=10):
        currents_uA, vin1_mV = block[0], block[1]   # one USB packet per block; t = (i0 + k) * dt
```

## Developer tools

- `probe.py`: listens passively, then sends three read-only status polls and prints the raw packets. Safe to run while REAMP-Logger is open.
- `analyze_ticks.py`: characterizes a periodic load in a current log, such as a quartz watch's 1 s tick: averaged pulse shape, charge per pulse, once-a-minute events, and the measured pulse period as a check on the timebase. `py analyze_ticks.py logs\reamp_XXXX.csv`

## Files

| File | What it is |
|---|---|
| `reamp_app.py` + `reamp_ui.html` | the live browser app (local web server on 127.0.0.1:8765 and its page) |
| `REAMP App.bat` | double-click launcher for the app |
| `reamp.py` | the REAMP driver and protocol (no third-party packages) |
| `reamp_cli.py` | command-line status, watch, logging, supply control and timebase calibration |
| `probe.py`, `analyze_ticks.py` | developer tools (above) |
| `logs\` | captures land here; ships with one sample |

## Not done yet

- TRIG-armed starts (the app's TRIG and TRIG-level buttons, `start(trig=...)`) are built from the decoded protocol but haven't been tested on hardware with a real trigger signal.
- The meaning of the data-packet status byte (it shows 0x80 during long runs) isn't decoded yet.
- Current and voltage accuracy haven't been checked against a reference meter.

## License

MIT; see `LICENSE`. RigExpert and REAMP are RigExpert's names for its company and product; this project is independent of them.

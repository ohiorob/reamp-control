"""
reamp_cli.py - command-line control and logging for the RigExpert REAMP.

  py reamp_cli.py status                      one reading
  py reamp_cli.py watch [-i 0.1] [--csv f.csv] [-t 60]   live readout (+ optional CSV), Ctrl+C to stop
  py reamp_cli.py log [-c 0 1 2] [-r 1000] [-t 60] [--plot] [--battery-mah 55]
                                              high-rate logging (10 Hz..10 kHz) to CSV in .\\logs
  py reamp_cli.py set 3300                    set output voltage in mV (1800..5200, exact - not rounded)
  py reamp_cli.py on | on --delayed | off     switch the DUT output
  py reamp_cli.py source usb | bat            power source (use bat for DUT loads over ~500 mA)
  py reamp_cli.py reset-errors                clear the device's error flags
  py reamp_cli.py calibrate                   re-measure the true sample rates (the REAMP's clock is ~0.5% fast
                                              and "10 kHz" really runs ~9 kHz); saved per serial number

status/watch work with REAMP-Logger open or closed; `log` needs REAMP-Logger closed.
Units: status current in mA (shown as uA when small); log current in uA; voltages in mV.
"Vout set" is the device's setpoint - jumper Vout to Vin1 for the measured output voltage.
"""
import argparse, csv, datetime, os, sys, time
from reamp import REAMP

HERE = os.path.dirname(os.path.abspath(__file__))
COLNAME = {0: 'current_uA', 1: 'vin1_mV', 2: 'vin2_mV'}
LABEL = {0: 'Current (uA)', 1: 'Vin1 (mV)', 2: 'Vin2 (mV)'}


def cmd_watch(r, a):
    w = None
    if a.csv:
        f = open(a.csv, 'w', newline='')
        w = csv.writer(f)
        w.writerow(['timestamp', 'elapsed_s', 'current_mA', 'vin1_mV', 'vin2_mV', 'vout_set_mV',
                    'supply_mV', 'output', 'source', 'flags'])
    t0 = time.monotonic(); n = 0
    try:
        while not a.t or time.monotonic() - t0 < a.t:
            tick = time.monotonic()
            s = r.status()
            if s:
                el = tick - t0; n += 1
                print(f'\r{el:8.2f}s  {s}   ', end='', flush=True)
                if w:
                    w.writerow([datetime.datetime.now().isoformat(timespec='milliseconds'), f'{el:.3f}',
                                f'{s.current_mA:.6f}', f'{s.vin1_mV:.3f}', f'{s.vin2_mV:.3f}', s.vout_mV,
                                f'{s.supply_mV:.1f}', s.output, s.source, f'0x{s.flags:02X}'])
            time.sleep(max(0.0, a.interval - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    print(f'\n{n} readings' + (f' saved to {a.csv}' if a.csv else ''))
    if w:
        f.close()


class Envelope:
    """Bounded-memory min/max/mean envelope of a long trace (keeps spikes visible in plots)."""

    def __init__(self, max_bins=4000):
        self.max_bins, self.size, self.bins, self.cur = max_bins, 1, [], None

    def add(self, t, v):
        self.add_block(t, v, v, v, 1)

    def add_block(self, t, mn, mx, s, n):
        c = self.cur
        if c is None:
            self.cur = c = [t, mn, mx, 0.0, 0]
        if mn < c[1]: c[1] = mn
        if mx > c[2]: c[2] = mx
        c[3] += s; c[4] += n
        if c[4] >= self.size:
            self.bins.append(c); self.cur = None
            if len(self.bins) > self.max_bins:
                b = self.bins
                self.bins = [[b[i][0], min(b[i][1], b[i + 1][1]), max(b[i][2], b[i + 1][2]),
                              b[i][3] + b[i + 1][3], b[i][4] + b[i + 1][4]] for i in range(0, len(b) - 1, 2)] \
                    + ([b[-1]] if len(b) % 2 else [])
                self.size *= 2

    def arrays(self):
        b = self.bins + ([self.cur] if self.cur else [])
        return ([x[0] for x in b], [x[1] for x in b], [x[2] for x in b], [x[3] / x[4] for x in b])


def human_time(h):
    if h < 48: return f'{h:.1f} hours'
    if h < 24 * 365 * 2: return f'{h / 24:.1f} days'
    return f'{h / 24 / 365.25:.1f} years'


def cmd_log(r, a):
    chans = sorted(set(a.channels))
    if len(chans) > 1 and a.rate > 1000:
        print('Note: 10 kHz is single-channel only; using 1000 Hz for multi-channel logging.')
        a.rate = 1000
    os.makedirs(os.path.join(HERE, 'logs'), exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    path = a.csv or os.path.join(HERE, 'logs', f'reamp_{stamp}.csv')
    started = datetime.datetime.now()
    env = {c: Envelope() for c in chans}
    tot = {c: [0.0, float('inf'), float('-inf'), 0] for c in chans}  # sum, min, max, n
    sec = {c: [0.0, float('-inf'), 0] for c in chans}
    period = max(1, min(1000, round(10000 / a.rate)))
    true_hz = 1.0 / r.true_dt(period)
    tbnote = (f'true rate {true_hz:.1f} Hz (calibrated timebase)' if r.timebase_calibrated
              else 'NOMINAL timebase - run `py reamp_cli.py calibrate` for accurate timestamps')
    print(f'Logging channels {chans} at {10000 / period:g} Hz setting, {tbnote}')
    print(f'  -> {path}' + (f' for {a.t:g} s' if a.t else ' (Ctrl+C to stop)'))
    f = open(path, 'w', newline='')
    w = csv.writer(f)
    w.writerow(['t_s'] + [COLNAME[c] for c in chans])
    nextprint, t = 1.0, 0.0
    write = f.write
    gen = r.stream_blocks(chans, a.rate)
    try:
        for i0, dt, blk in gen:
            if a.t and i0 * dt >= a.t:
                break
            cols = [blk[c] for c in chans]
            n = len(cols[0])
            if len(cols) == 1:
                v0 = cols[0]
                write(''.join([f'{(i0 + k) * dt:.6f},{v0[k]:.4f}\n' for k in range(n)]))
            else:
                write(''.join([f'{(i0 + k) * dt:.6f},' + ','.join([f'{col[k]:.4f}' for col in cols]) + '\n'
                               for k in range(n)]))
            for c, vals in zip(chans, cols):
                s, mn, mx = sum(vals), min(vals), max(vals)
                T = tot[c]; S = sec[c]
                T[0] += s; T[3] += n
                if mn < T[1]: T[1] = mn
                if mx > T[2]: T[2] = mx
                S[0] += s; S[2] += n
                if mx > S[1]: S[1] = mx
                env[c].add_block(i0 * dt, mn, mx, s, n)
            t = (i0 + n) * dt
            if t >= nextprint:
                parts = [f'{LABEL[c].split()[0]} avg {sec[c][0] / max(1, sec[c][2]):9.3f} pk {sec[c][1]:9.3f}' for c in chans]
                print(f'{t:8.1f}s  ' + '  |  '.join(parts), flush=True)
                sec = {c: [0.0, float('-inf'), 0] for c in chans}
                nextprint += 1.0
    except KeyboardInterrupt:
        pass
    finally:
        gen.close()  # sends STOP to the REAMP
        f.close()
    st = getattr(r, 'stream_stats', {})
    dur = t
    print(f'\nSaved {tot[chans[0]][3]:,} samples ({dur:.2f} s) to {path}')
    if st.get('lost_frames'):
        print(f'WARNING: {st["lost_frames"]} samples lost in {st["gaps"]} gaps (USB could not keep up)')
    if st.get('status_flags'):
        print(f'  (device status byte showed flags 0x{st["status_flags"]:02X} - meaning not decoded yet; '
              f'lost-sample detection above is independent of it)')
    for c in chans:
        s, mn, mx, n = tot[c]
        if n:
            print(f'  {LABEL[c]:13} mean {s / n:10.3f}   min {mn:10.3f}   max {mx:10.3f}')
    if 0 in chans and tot[0][3]:
        mean_uA = tot[0][0] / tot[0][3]
        print(f'  Charge used: {mean_uA * dur / 3600:.4f} uAh over {dur:.1f} s  (avg {mean_uA:.3f} uA)')
        if a.battery_mah and mean_uA > 0:
            hours = a.battery_mah * 1000 / mean_uA
            print(f'  Estimated life on {a.battery_mah:g} mAh: {human_time(hours)} '
                  f'(at this average; ignores self-discharge and end-of-life voltage)')
    if a.plot and dur > 0:
        png = os.path.splitext(path)[0] + '.png'
        plot(env, chans, png, f'REAMP log {started:%m/%d/%Y %H:%M:%S}  -  {true_hz:.0f} Hz, {dur:.1f} s')
        print(f'Plot saved to {png}')


def plot(env, chans, png, title):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(chans), 1, figsize=(12, 3.2 * len(chans) + 0.6), sharex=True, squeeze=False)
    for ax, c in zip(axes[:, 0], chans):
        ts, lo, hi, mean = env[c].arrays()
        ax.fill_between(ts, lo, hi, step='post', color='#2e6fd4', alpha=0.35, linewidth=0, label='min-max')
        ax.step(ts, mean, where='post', color='#1a3a6b', linewidth=0.8, label='mean')
        ax.set_ylabel(LABEL[c]); ax.grid(True, alpha=0.3)
        if c == 0:
            ax.set_ylim(bottom=0)
    axes[-1, 0].set_xlabel('Time (s)')
    axes[0, 0].set_title(title, fontsize=11)
    axes[0, 0].legend(loc='upper right', fontsize=8)
    fig.tight_layout()
    fig.savefig(png, dpi=150)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description='RigExpert REAMP control')
    sp = p.add_subparsers(dest='cmd', required=True)
    sp.add_parser('status')
    pw = sp.add_parser('watch'); pw.add_argument('-i', '--interval', type=float, default=0.1)
    pw.add_argument('--csv'); pw.add_argument('-t', type=float, default=0, help='stop after N seconds')
    pl = sp.add_parser('log')
    pl.add_argument('-c', '--channels', type=int, nargs='+', default=[0], choices=[0, 1, 2],
                    help='0 = current, 1 = Vin1, 2 = Vin2 (default 0)')
    pl.add_argument('-r', '--rate', type=int, default=1000, help='samples/s, 10..10000 (default 1000)')
    pl.add_argument('-t', type=float, default=0, help='stop after N seconds (default: Ctrl+C)')
    pl.add_argument('--csv', help='output CSV (default logs\\reamp_<timestamp>.csv)')
    pl.add_argument('--plot', action='store_true', help='also save a PNG plot next to the CSV')
    pl.add_argument('--battery-mah', type=float, help='estimate battery life for this capacity')
    ps = sp.add_parser('set'); ps.add_argument('mv', type=int)
    po = sp.add_parser('on'); po.add_argument('--delayed', action='store_true')
    sp.add_parser('off')
    pc = sp.add_parser('source'); pc.add_argument('which', choices=['usb', 'bat'])
    sp.add_parser('reset-errors')
    sp.add_parser('calibrate', help='measure the true sample rates against the PC clock (~80 s)')
    a = p.parse_args()

    if a.cmd == 'calibrate':
        with REAMP() as r:
            print(f'Measuring true sample rates for REAMP S/N {r.dev.serial} (about 80 s; REAMP-Logger must be closed)...')
            cal = r.calibrate_timebase()
            print(f'Clock factor {cal["clock_factor"]:.4f} ({100 * (cal["clock_factor"] - 1):+.2f}%). Saved to reamp_timebase.json')
        return 0

    with REAMP() as r:
        if a.cmd == 'status':
            s = r.status()
        elif a.cmd == 'watch':
            return cmd_watch(r, a)
        elif a.cmd == 'log':
            return cmd_log(r, a)
        elif a.cmd == 'set':
            s = r.set_voltage(a.mv)
        elif a.cmd == 'on':
            s = r.output_on(a.delayed)
        elif a.cmd == 'off':
            s = r.output_off()
        elif a.cmd == 'source':
            s = r.set_source(a.which == 'bat')
        elif a.cmd == 'reset-errors':
            s = r.reset_errors()
        print(s if s else 'No reply from REAMP')
        return 0 if s else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, OSError, TimeoutError) as e:
        print(f'Error: {e}')
        sys.exit(1)

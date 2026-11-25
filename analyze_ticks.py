"""analyze_ticks.py - characterize a periodic load (e.g. a watch's 1 s tick) in a REAMP current log.

  py analyze_ticks.py logs\\reamp_XXXX.csv [--period 1.0]

Pulses are aligned to a straight-line fit of all pulse times (no per-pulse alignment bias), then
averaged (noise drops ~sqrt(N)). Reports baseline, pulse height/width, charge per pulse, the pulse share
of total draw, any once-a-minute event, and the measured pulse period (a check on the REAMP timebase).
Saves <csv>_ticks.png.
"""
import argparse, os, numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('csv'); ap.add_argument('--period', type=float, default=1.0)
a = ap.parse_args()
P0 = a.period

_txt = open(a.csv).read()                                   # much faster than np.loadtxt for big logs
d = np.array(_txt.replace('\n', ',').split(',')[2:-1], dtype=float).reshape(-1, 2)
del _txt
t, x = d[:, 0], d[:, 1]
print(f'exact-zero samples: {np.mean(x == 0) * 100:.1f}%  (high values mean the REAMP is near its floor; '
      f'treat averages with caution)')
dt = (t[-1] - t[0]) / (len(t) - 1); fs = 1 / dt
gaps = int(np.sum(np.diff(t) > 1.5 * dt))
print(f'{len(t):,} samples, {t[-1]:.2f} s, {fs:.1f} Hz, gaps {gaps}, mean {x.mean():.4f} uA')

# rough pulse detection (20 ms moving average), then a straight-line fit of pulse number -> time
w = max(3, int(fs * 0.02))
s = np.convolve(x, np.ones(w) / w, mode='same')
thr = np.median(s) + 0.5 * (np.percentile(s, 99.9) - np.median(s))
above = np.flatnonzero(s > thr)
starts = above[np.insert(np.diff(above) > fs * P0 * 0.3, 0, True)]
ts = t[starts]
k = np.round((ts - ts[0]) / P0)
P, T0 = np.polyfit(k, ts, 1)
resid = ts - (T0 + P * k)
keep = np.abs(resid) < 0.02
P, T0 = np.polyfit(k[keep], ts[keep], 1)
print(f'pulses {len(ts)}, fitted period {P:.6f} s ({(P / P0 - 1) * 1e6:+.0f} ppm vs {P0} s), '
      f'detection jitter {resid[keep].std() * 1000:.2f} ms')

# fold on the fitted grid, window -100 ms .. +400 ms
pre, post = int(0.1 * fs), int(0.4 * fs)
idx = np.round((T0 + P * np.arange(int(k.max()) + 1) - t[0]) / dt).astype(int)
segs = np.array([x[i - pre:i + post] for i in idx if i - pre >= 0 and i + post <= len(x)])
avg = segs.mean(axis=0); tt = np.arange(-pre, post) * dt * 1000
sm = np.convolve(avg, np.ones(int(fs * 0.002)) / int(fs * 0.002), mode='same')   # 2 ms smoothing for shape
base = np.median(np.concatenate([avg[:int(0.08 * fs)], avg[-int(0.15 * fs):]]))
ex = sm - base
pk = ex.max(); over = np.flatnonzero(ex > 0.5 * pk)
fwhm = (over[-1] - over[0]) * dt * 1000
win = (tt > -40) & (tt < 200)
q = (avg[win] - base).sum() * dt                       # uA*s per pulse
share = q / P / x.mean() * 100
print(f'baseline {base:.4f} uA | pulse +{pk:.3f} uA for {fwhm:.1f} ms (FWHM) | '
      f'charge/pulse {q * 1000:.2f} nA*s | pulses = {share:.1f}% of total draw')

# once-a-minute check: per-second means folded at 60 s
nsec = int(t[-1])
sec = np.array([x[(t >= j) & (t < j + 1)].mean() for j in range(nsec)])
nfull = (nsec // 60) * 60
fold = sec[:nfull].reshape(-1, 60).mean(axis=0)
ph = int(fold.argmax()); extra = (fold[ph] - np.median(fold))
print(f'minute fold: max at second {ph} of each minute, +{extra:.4f} uA over that second '
      f'(= {extra * 1000:.1f} nA*s per minute, {extra / 60 / x.mean() * 100:.2f}% of total draw)')

import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(12, 10))
a1.plot(tt, avg, color='#7a90b0', lw=0.5, label='average (raw)')
a1.plot(tt, sm, color='#1a3a6b', lw=1.2, label='2 ms smoothed')
a1.axhline(base, color='#9b1515', lw=0.8, ls='--', label=f'baseline {base:.3f} uA')
a1.set_xlim(-95, 395); a1.set_xlabel('Time from tick (ms)'); a1.set_ylabel('Current (uA)'); a1.grid(alpha=0.3)
a1.set_title(f'Average of {len(segs)} ticks: +{pk:.2f} uA for {fwhm:.0f} ms, {q * 1000:.1f} nA*s each '
             f'({share:.1f}% of total draw)', fontsize=11)
a1.legend(loc='upper right', fontsize=8)
a2.plot(np.arange(nsec), sec, color='#2e6fd4', lw=0.8)
a2.set_xlabel('Time (s)'); a2.set_ylabel('1-s mean (uA)'); a2.grid(alpha=0.3)
a2.set_title(f'Per-second average current - overall mean {x.mean():.3f} uA', fontsize=11)
a3.bar(np.arange(60), fold, color='#2e6fd4')
a3.set_ylim(fold.min() * 0.995, fold.max() * 1.003)
a3.set_xlabel('Second within each minute (log time)'); a3.set_ylabel('mean (uA)'); a3.grid(alpha=0.3, axis='y')
a3.set_title(f'Folded at 60 s ({nfull // 60} minutes): once-a-minute event at second {ph}, '
             f'+{extra * 1000:.0f} nA*s', fontsize=11)
fig.tight_layout()
png = os.path.splitext(a.csv)[0] + '_ticks.png'
fig.savefig(png, dpi=150); print('saved', png)

#!/usr/bin/env python3
"""
reamp_app.py - live, browser-based front end for the RigExpert REAMP (a working stand-in for REAMP-Logger).

  py reamp_app.py              connect to the REAMP and open the app in your browser
  py reamp_app.py --demo       simulated REAMP (no hardware) to try the interface
  py reamp_app.py --port 8765 --no-browser

The page (reamp_ui.html) is served at http://127.0.0.1:<port>/ - only this PC can reach it.
Close REAMP-Logger first: only one program should log at a time. Needs numpy (already installed).
"""
import argparse
import array
import datetime
import json
import math
import os
import queue
import random
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, 'logs')
UI_FILE = os.path.join(HERE, 'reamp_ui.html')
COLS = {0: 'current_uA', 1: 'vin1_mV', 2: 'vin2_mV'}
COL2CH = {v: k for k, v in COLS.items()}


def _clean(a):
    """numpy array -> JSON-safe list (NaN -> None), 4 decimals."""
    a = np.round(np.asarray(a, dtype=np.float64), 4)
    lst = a.tolist()
    if np.isnan(a).any():
        lst = [None if v != v else v for v in lst]
    return lst


# ------------------------------------------------------------------------------------------------
# Captures (one per tab): full-resolution samples kept in memory for zooming and statistics
# ------------------------------------------------------------------------------------------------
class Capture:
    def __init__(self, cid, channels, dt, period, name=None, path=None, live=True):
        self.id, self.channels, self.dt, self.period = cid, list(channels), dt, period
        self.name = name or f'Capture {cid}'
        self.path, self.live, self.running = path, live, live
        self.started = datetime.datetime.now()
        self.data = {c: array.array('f') for c in self.channels}
        # sum, min, max, n, shift K, sum(v-K), sum((v-K)^2)  - the shift keeps the variance exact for a 3 V signal
        self.acc = {c: [0.0, math.inf, -math.inf, 0, None, 0.0, 0.0] for c in self.channels}
        self.lost, self.note, self.mode = 0, '', ''
        self.lock = threading.Lock()
        self._memo = {}

    @property
    def n(self):
        return len(self.data[self.channels[0]])

    @property
    def duration(self):
        return self.n * self.dt

    def append(self, block):
        with self.lock:
            for c, vals in block.items():
                self.data[c].extend(vals)
                a = self.acc[c]
                a[0] += sum(vals); a[3] += len(vals)
                m, M = min(vals), max(vals)
                if m < a[1]: a[1] = m
                if M > a[2]: a[2] = M
                if a[4] is None:
                    a[4] = vals[0]
                K = a[4]
                d = [v - K for v in vals]
                a[5] += sum(d); a[6] += sum(x * x for x in d)

    def append_gap(self, k):
        """Lost frames: keep the time axis true by inserting NaN placeholders."""
        with self.lock:
            for c in self.channels:
                self.data[c].extend(array.array('f', [math.nan]) * k)
            self.lost += k

    def _arr(self, c, i0, i1):
        if i1 <= i0:
            return np.zeros(0, np.float32)
        with self.lock:
            v = np.frombuffer(self.data[c], dtype=np.float32)
            out = v[i0:i1].copy()
            del v
        return out

    def _idx(self, t0, t1):
        n = self.n
        i0 = max(0, min(n, int(math.floor(t0 / self.dt))))
        i1 = max(0, min(n, int(math.ceil(t1 / self.dt)) + 1))
        return i0, i1

    def _series(self, c, i0, i1, w=1):
        """Samples i0..i1 of channel c, optionally smoothed by a centred w-sample moving average
        (display only - statistics and the CSV always use the raw samples)."""
        if w <= 1:
            return self._arr(c, i0, i1)
        h = w // 2
        a0, a1 = max(0, i0 - h), min(self.n, i1 + h)
        x = self._arr(c, a0, a1).astype(np.float64)
        ok = ~np.isnan(x)
        cs = np.concatenate(([0.0], np.cumsum(np.where(ok, x, 0.0))))
        cn = np.concatenate(([0], np.cumsum(ok)))
        pos = np.arange(i0, i1) - a0
        lo, hi = np.maximum(pos - h, 0), np.minimum(pos + h + 1, len(x))
        k = cn[hi] - cn[lo]
        y = np.where(k > 0, (cs[hi] - cs[lo]) / np.maximum(k, 1), np.nan)
        y[~ok[pos]] = np.nan
        return y.astype(np.float32)

    def envelope(self, t0, t1, npx, sm=None):
        npx = max(10, min(5000, int(npx)))
        sm = sm or {}
        i0, i1 = self._idx(t0, t1)
        out = {'i0': i0, 'i1': i1, 'dt': self.dt, 'ch': {}}
        if i1 - i0 < 2:
            out['mode'] = 'empty'
            return out
        if i1 - i0 <= npx * 2:
            out['mode'] = 'raw'
            for c in self.channels:
                out['ch'][str(c)] = _clean(self._series(c, i0, i1, sm.get(c, 1)))
            return out
        out['mode'] = 'env'
        edges = np.unique(np.linspace(i0, i1, npx + 1).astype(np.int64))
        rel = edges[:-1] - i0
        out['edges'] = edges[:-1].tolist()
        out['last'] = int(edges[-1])
        for c in self.channels:
            x = self._series(c, i0, i1, sm.get(c, 1))
            ok = ~np.isnan(x)
            cnt = np.add.reduceat(ok.astype(np.int32), rel)
            s = np.add.reduceat(np.where(ok, x, 0).astype(np.float64), rel)
            mn = np.fmin.reduceat(x, rel)
            mx = np.fmax.reduceat(x, rel)
            mean = np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)
            out['ch'][str(c)] = [_clean(mn), _clean(mx), _clean(mean)]
        return out

    def stats(self, t0=None, t1=None):
        res = {}
        if t0 is None:
            with self.lock:
                for c in self.channels:
                    s, mn, mx, k, _K, s1, s2 = self.acc[c]
                    res[str(c)] = [mn, mx, s / k, math.sqrt(max(0.0, s2 / k - (s1 / k) ** 2))] if k else None
            return res
        key = (round(t0, 6), round(t1, 6))
        if not self.running and key in self._memo:
            return self._memo[key]
        i0, i1 = self._idx(t0, t1)
        for c in self.channels:
            x = self._arr(c, i0, i1)
            x = x[~np.isnan(x)]
            res[str(c)] = [float(x.min()), float(x.max()), float(x.mean(dtype=np.float64)),
                           float(x.std(dtype=np.float64))] if len(x) else None
        if not self.running:
            self._memo[key] = res
        return res

    def summary(self):
        return {'id': self.id, 'name': self.name, 'channels': self.channels, 'dt': self.dt, 'period': self.period,
                'n': self.n, 'dur': self.duration, 'running': self.running, 'lost': self.lost,
                'path': self.path, 'file': os.path.basename(self.path) if self.path else None,
                'started': self.started.strftime('%m/%d/%Y %H:%M:%S'), 'note': self.note, 'live': self.live,
                'mode': self.mode}


def load_csv(path, cid):
    """Open a log written by this app or reamp_cli.py (t_s + current_uA/vin1_mV/vin2_mV columns)."""
    with open(path) as f:
        header = f.readline().strip().split(',')
        body = f.read()
    if header[0] != 't_s' or not all(h in COL2CH for h in header[1:]):
        raise ValueError('not a REAMP log (expected t_s,current_uA,... columns)')
    chans = [COL2CH[h] for h in header[1:]]
    d = np.array(body.replace('\n', ',').split(',')[:-1], dtype=np.float64).reshape(-1, len(header))
    t = d[:, 0]
    dt = float(np.median(np.diff(t[:2000]))) if len(t) > 2 else 1e-3
    if len(t) > 1:
        dt = float((t[-1] - t[0]) / round((t[-1] - t[0]) / dt)) if dt > 0 else dt
    cap = Capture(cid, chans, dt, round(dt * 1e4), name=os.path.basename(path), path=path, live=False)
    idx = np.round((t - t[0]) / dt).astype(np.int64)
    for j, c in enumerate(chans):
        col = np.full(int(idx[-1]) + 1, np.nan, np.float32)
        col[idx] = d[:, j + 1]
        cap.data[c] = array.array('f', col.tobytes())
        x = col[~np.isnan(col)]
        if len(x):
            dd = x.astype(np.float64) - float(x[0])
            cap.acc[c] = [float(x.sum(dtype=np.float64)), float(x.min()), float(x.max()), len(x),
                          float(x[0]), float(dd.sum()), float((dd * dd).sum())]
        else:
            cap.acc[c] = [0, 0, 0, 0, None, 0.0, 0.0]
    cap.lost = int(np.isnan(col).sum())
    m = os.path.getmtime(path)
    cap.started = datetime.datetime.fromtimestamp(m)
    return cap


# ------------------------------------------------------------------------------------------------
# Simulated REAMP for --demo (watch-like load: ~2.1 uA baseline, 1.6 uA x 43 ms tick every second)
# ------------------------------------------------------------------------------------------------
class _FakeStatus:
    def __init__(self, f):
        self.vout_mV, self.output_state, self.flags = f.vout, f.out, f.flags
        self.source = 'battery' if f.bat else 'USB'
        on = f.out in (0x08, 0x0C)
        t = time.monotonic()
        self.current_mA = max(0.0, f._cur(t) / 1000 + random.gauss(0, 0.0004)) if on else 0.0
        self.vin1_mV = (f.vout - 22 + random.gauss(0, 1.2)) if on else random.gauss(5, 1.5)
        self.vin2_mV = 20 + random.gauss(0, 1.5)
        self.supply_mV = (3900 if f.bat else 4910) + random.gauss(0, 5)
        pins = (0x40 if f.bat else 0x80) | (0x08 if on else 0) | 0x10 | (0x04)
        self.raw = bytes([6, 0, 0, 0, f.flags, pins]) + bytes(58)


class FakeREAMP:
    timebase_calibrated = True

    class _Dev:
        serial = 'DEMO-0001'

        def close(self):
            pass

    class _CI:
        def __init__(self, name, unit):
            self.name, self.out_unit = name, unit

    def __init__(self):
        self.vout, self.out, self.bat, self.flags = 3121, 0x08, False, 0
        self.last_power = None
        self.dev = self._Dev()
        self.stream_stats = {}

    def device_info(self):
        return 'RigExpert Current logger v1.09 (simulated)'

    def channel_info(self):
        return {0: self._CI('Current', 'uA'), 1: self._CI('Voltage', 'mV'), 2: self._CI('Voltage', 'mV')}

    def true_dt(self, period):
        return period * 1e-4

    def _cur(self, t):
        ph = t % 1.0
        c = 2.1 + (1.6 if 0.0 < ph < 0.043 else 0.0) + (0.4 if 0.043 <= ph < 0.08 else 0.0)
        if int(t) % 60 == 50 and ph < 0.15:
            c += 0.4
        return c

    def status(self, timeout=1.0):
        time.sleep(0.005)
        return _FakeStatus(self)

    def _power(self, ctrl, mv=0, sync_delay=0, timeout=1.0):
        if ctrl & 0x10: self.vout = mv
        if ctrl & 0x0C: self.out = ctrl & 0x0C
        if ctrl & 0x80: self.bat = bool(ctrl & 0x40)
        if ctrl & 0x20: self.flags = 0
        return _FakeStatus(self)

    def reset_errors(self):
        return self._power(0x20)

    def power_nowait(self, ctrl, mv=0, sync_delay=0):
        self.last_power = self._power(ctrl, mv, sync_delay)

    def stream_blocks(self, channels=(0,), rate_hz=1000, duration=None, raw=False, trig=None, trig_delay=0,
                      yield_idle=False):
        chans = sorted(set(channels))
        period = max(1, min(1000, round(10000 / rate_hz)))
        dt, nc = period * 1e-4, len(chans)
        if nc > 1 and period < 2:
            raise RuntimeError('REAMP refused to start measuring (error code 3)')
        per = 28 // nc
        on = self.out in (0x08, 0x0C)
        self.stream_stats = dict(packets=0, frames=0, lost_frames=0, gaps=0, status_flags=0)
        if trig:
            t_arm = time.monotonic() + 2.0 + trig_delay / 1000
            while time.monotonic() < t_arm:
                time.sleep(0.1)
                if yield_idle:
                    yield None
        t0, idx = time.monotonic(), 0
        while True:
            now = time.monotonic() - t0
            if idx * dt > now:
                time.sleep(min(0.02, idx * dt - now))
                continue
            blk = {c: [] for c in chans}
            for k in range(per):
                tt = t0 + (idx + k) * dt
                for c in chans:
                    if c == 0:
                        v = max(0.0, (self._cur(tt) if on else 0) + random.gauss(0, 3.2))
                        v = round(v / 0.1221) * 0.1221
                    elif c == 1:
                        v = (self.vout - 22 if on else 4) + random.gauss(0, 8)
                    else:
                        v = 20 + random.gauss(0, 10)
                    blk[c].append(v)
            self.stream_stats['frames'] += per
            yield idx, dt, blk
            idx += per

    def close(self):
        pass


# ------------------------------------------------------------------------------------------------
# Device thread: the only code that touches the REAMP
# ------------------------------------------------------------------------------------------------
def status_dict(s):
    return {'vout_mV': s.vout_mV, 'out': s.output_state, 'source': s.source, 'current_mA': s.current_mA,
            'vin1_mV': s.vin1_mV, 'vin2_mV': s.vin2_mV, 'supply_mV': s.supply_mV, 'flags': s.flags,
            'pins': s.raw[5], 't': time.time()}


class Device(threading.Thread):
    def __init__(self, app, demo):
        super().__init__(daemon=True)
        self.app, self.demo = app, demo
        self.r = None
        self.cmdq = queue.Queue()
        self.info = self.serial = self.chinfo = None
        self.status = None
        self.connected = self.responding = False
        self.misses = 0
        self.logging = None
        self.armed = False
        self.calibrated = False
        self.message = 'Connecting to REAMP...'

    # called from HTTP threads
    def call(self, action, timeout=8.0, **kw):
        fut = {'ev': threading.Event(), 'res': None, 'err': None}
        self.cmdq.put((action, kw, fut))
        if not fut['ev'].wait(timeout):
            raise TimeoutError('the REAMP did not answer in time')
        if fut['err']:
            raise RuntimeError(fut['err'])
        return fut['res']

    @staticmethod
    def _done(fut, res=None, err=None):
        if fut and not fut['ev'].is_set():
            fut['res'], fut['err'] = res, err
            fut['ev'].set()

    def _drain(self, err):
        while True:
            try:
                _a, _k, fut = self.cmdq.get_nowait()
            except queue.Empty:
                return
            self._done(fut, err=err)

    def _connect(self):
        if self.demo:
            self.r = FakeREAMP()
        else:
            import reamp
            self.r = reamp.REAMP()
        self.info = self.r.device_info()
        if not self.info:
            raise RuntimeError('REAMP is not responding - unplug and replug its USB cable')
        self.serial = self.r.dev.serial
        ci = self.r.channel_info()
        self.chinfo = {str(c): {'name': i.name, 'unit': i.out_unit} for c, i in ci.items()}
        self.calibrated = bool(getattr(self.r, 'timebase_calibrated', False))
        self.connected = self.responding = True
        self.misses = 0
        self.message = 'Device connected'

    def _drop(self, why):
        try:
            if self.r:
                self.r.close()
        except Exception:
            pass
        self.r = None
        self.connected = self.responding = False
        self.status = None
        self.message = why

    def run(self):
        next_poll = 0.0
        while True:
            if self.r is None:
                try:
                    self._connect()
                except Exception as e:
                    self._drop(f'REAMP not available: {e}')
                    self._drain('REAMP not connected')
                    time.sleep(1.5)
                    continue
            try:
                action, kw, fut = self.cmdq.get(timeout=0.05)
            except queue.Empty:
                action = None
            try:
                if action:
                    self._do(action, kw, fut)
                if self.r and time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 0.25
                    self._poll()
            except OSError as e:  # unplugged
                self._done(fut if action else None, err=str(e))
                self._drop(f'REAMP disconnected ({e}). Waiting for it...')
            except Exception as e:
                self._done(fut if action else None, err=str(e))
                self.message = f'Error: {e}'

    def _poll(self):
        s = self.r.status(timeout=0.4)
        if s:
            self.status = status_dict(s)
            self.misses = 0
            if not self.responding:
                self.message = 'Device connected'
            self.responding = True
        else:
            self.misses += 1
            if self.misses >= 6:
                self.responding = False
                self.message = 'REAMP is not responding - unplug and replug its USB cable'
            if self.misses >= 12:  # reopen (picks the device up again after a replug)
                self._drop(self.message)

    def _do(self, action, kw, fut):
        r = self.r
        if action == 'power':
            s = r._power(int(kw['ctrl']), int(kw.get('mv', 0)), int(kw.get('delay', 0)))
            if not s:
                return self._done(fut, err='no reply from the REAMP')
            self.status = status_dict(s)
            return self._done(fut, {'status': self.status})
        if action == 'reset_errors':
            s = r.reset_errors()
            if s:
                self.status = status_dict(s)
            return self._done(fut, {'status': self.status})
        if action == 'start':
            return self._log(kw, fut)
        if action == 'stop':
            return self._done(fut, {'note': 'not logging'})
        self._done(fut, err=f'unknown action {action}')

    def _power_while_logging(self, action, kw, fut):
        """Output voltage / output mode / error reset during a measurement. The command is written without
        waiting for the answer (waiting would throw data packets away); the reply is picked up by the stream."""
        try:
            ctrl = 0x20 if action == 'reset_errors' else int(kw['ctrl'])
            if ctrl & 0x80 or ctrl & 0x02:
                return self._done(fut, err='Vin source and SYNC settings can only be applied when stopped')
            mv = int(kw.get('mv', 0))
            if ctrl & 0x10 and not 1800 <= mv <= 5200:
                return self._done(fut, err='output voltage must be 1800..5200 mV')
            self.r.power_nowait(ctrl, mv, int(kw.get('delay', 0)))
            st = dict(self.status or {})
            if ctrl & 0x10:
                st['vout_mV'] = mv
            if ctrl & 0x0C:
                st['out'] = ctrl & 0x0C
            if st:
                self.status = st
            self._done(fut, {'status': self.status, 'pending': True})
        except Exception as e:
            self._done(fut, err=str(e))

    def _log(self, kw, fut):
        chans = sorted(set(int(c) for c in (kw.get('channels') or [0])))
        period = max(1, min(1000, int(kw.get('period', 10))))
        trig = kw.get('trig') or None
        delay = int(kw.get('delay') or 0)
        duration = kw.get('duration')
        duration = float(duration) if duration else None
        dt = self.r.true_dt(period)
        cap = self.app.new_capture(chans, dt, period)
        cap.mode = {None: 'Start', 'edge': 'TRIG', 'delay': f'TRIG + {delay} ms', 'level': 'TRIG level'}[trig]
        f = None
        if kw.get('save', True):
            os.makedirs(LOGDIR, exist_ok=True)
            cap.path = os.path.join(LOGDIR, f'reamp_{cap.started:%Y%m%d_%H%M%S}.csv')
            f = open(cap.path, 'w', newline='')
            f.write('t_s,' + ','.join(COLS[c] for c in chans) + '\n')
        gen = self.r.stream_blocks(chans, 10000 / period, raw=False, trig=trig, trig_delay=delay, yield_idle=True)
        self.logging, self.armed = cap, bool(trig)
        self.message = 'Waiting for TRIG...' if trig else 'Measuring...'
        started, base, expected = False, None, None
        try:
            for item in gen:
                if not started:
                    started = True
                    self._done(fut, {'cap': cap.id})
                lp = getattr(self.r, 'last_power', None)
                if lp is not None:  # the REAMP's reply to a command sent while logging
                    self.r.last_power = None
                    self.status = status_dict(lp)
                if item is not None:
                    i0, _dt, blk = item
                    if base is None:
                        base = i0
                        self.armed = False
                        self.message = 'Measuring...'
                    rel = i0 - base
                    if expected is not None and rel > expected:
                        cap.append_gap(rel - expected)
                    k = len(blk[chans[0]])
                    expected = rel + k
                    cap.append(blk)
                    if f:
                        cols = [blk[c] for c in chans]
                        f.write(''.join(
                            f'{(rel + j) * dt:.6f},' + ','.join(f'{col[j]:.4f}' for col in cols) + '\n'
                            for j in range(k)))
                    if duration and cap.duration >= duration:
                        break
                try:
                    a2, _k2, fut2 = self.cmdq.get_nowait()
                except queue.Empty:
                    continue
                if a2 == 'stop':
                    self._done(fut2, {'cap': cap.id})
                    break
                if a2 in ('power', 'reset_errors'):
                    self._power_while_logging(a2, _k2, fut2)
                    continue
                self._done(fut2, err='Stop measuring first')
        except Exception as e:
            if not started:
                self._done(fut, err=str(e))
                self.app.remove_capture(cap.id, force=True)
                if f:
                    f.close(); f = None
                    try:
                        os.remove(cap.path)
                    except OSError:
                        pass
                self.message = f'Could not start: {e}'
            else:
                cap.note = str(e)
                self.message = f'Logging stopped: {e}'
        finally:
            try:
                gen.close()  # sends STOP
            except Exception:
                pass
            if f:
                f.close()
            cap.running = False
            st = getattr(self.r, 'stream_stats', {}) if self.r else {}
            cap.lost = max(cap.lost, st.get('lost_frames', 0))
            self.logging, self.armed = None, False
            if started and not self.message.startswith(('Logging stopped', 'Could not')):
                self.message = f'Stopped. {cap.n:,} samples ({cap.duration:.2f} s)' + \
                               (f' saved to logs\\{os.path.basename(cap.path)}' if cap.path else '')
            self._done(fut, err='measurement ended before it started')


# ------------------------------------------------------------------------------------------------
# Application state + HTTP API
# ------------------------------------------------------------------------------------------------
class App:
    def __init__(self, demo):
        self.demo = demo
        self.caps = {}
        self.next_id = 1
        self.lock = threading.Lock()
        self.dev = Device(self, demo)

    def new_capture(self, chans, dt, period, **kw):
        with self.lock:
            cap = Capture(self.next_id, chans, dt, period, **kw)
            self.caps[cap.id] = cap
            self.next_id += 1
            return cap

    def remove_capture(self, cid, force=False):
        with self.lock:
            cap = self.caps.get(cid)
            if cap and (force or not cap.running):
                del self.caps[cid]
                return True
            return False

    def frame(self, q):
        d = self.dev
        out = {'dev': {'connected': d.connected, 'responding': d.responding, 'info': d.info, 'serial': d.serial,
                       'msg': d.message, 'logging': d.logging.id if d.logging else None, 'armed': d.armed,
                       'demo': self.demo, 'calibrated': d.calibrated, 'chinfo': d.chinfo},
               'status': d.status,
               'caps': []}
        with self.lock:
            caps = sorted(self.caps.values(), key=lambda c: c.id)
        out['caps'] = [c.summary() for c in caps]
        cid = q.get('cap')
        cap = self.caps.get(int(cid)) if cid not in (None, '', 'null') else None
        if cap:
            if 't0' in q:
                t0, t1 = float(q['t0']), float(q['t1'])
                sm = {}
                for part in (q.get('sm') or '').split(','):
                    if ':' in part:
                        c, w = part.split(':')
                        sm[int(c)] = max(1, min(4097, int(w)))
                out['env'] = cap.envelope(t0, t1, int(q.get('n', 1000)), sm)
                if q.get('vis'):
                    out['vis'] = cap.stats(t0, t1)
            if q.get('ov'):
                out['ov'] = cap.envelope(0, max(cap.duration, cap.dt * 2), int(q['ov']))
            out['full'] = cap.stats()
            if 'm1' in q:
                a, b = sorted((float(q['m1']), float(q['m2'])))
                out['win'] = cap.stats(a, b)
            if q.get('cyc'):
                a = float(q.get('cyc0', 0))
                out['cyc'] = cap.stats(a, a + float(q['cyc']))
        lc = d.logging
        if lc is not None and lc.n:  # live readout of the running capture, whichever tab is selected
            out['recent'] = lc.stats(max(0.0, lc.duration - 0.3), lc.duration)
        return out

    def logs(self):
        if not os.path.isdir(LOGDIR):
            return []
        items = []
        for fn in os.listdir(LOGDIR):
            if fn.lower().endswith('.csv'):
                p = os.path.join(LOGDIR, fn)
                items.append({'file': fn, 'size': os.path.getsize(p),
                              'date': datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime('%m/%d/%Y %H:%M')})
        return sorted(items, key=lambda x: x['file'], reverse=True)

    def command(self, b):
        a = b.get('action')
        if a in ('power', 'reset_errors', 'start'):
            return self.dev.call(a, **{k: v for k, v in b.items() if k != 'action'})
        if a == 'stop':
            if not self.dev.logging:
                return {'note': 'not logging'}
            return self.dev.call('stop')
        if a == 'open':
            fn = os.path.basename(b['file'])
            with self.lock:
                cid = self.next_id
                self.next_id += 1
            cap = load_csv(os.path.join(LOGDIR, fn), cid)
            with self.lock:
                self.caps[cap.id] = cap
            return {'cap': cap.id}
        if a == 'close_capture':
            return {'closed': self.remove_capture(int(b['id']))}
        raise ValueError(f'unknown action {a}')


APP = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, separators=(',', ':'), allow_nan=False).encode(), 'application/json', code)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ('/', '/index.html'):
                with open(UI_FILE, 'rb') as f:
                    self._send(f.read(), 'text/html; charset=utf-8')
            elif u.path == '/api/frame':
                self._json(APP.frame(q))
            elif u.path == '/api/logs':
                self._json({'logs': APP.logs()})
            elif u.path == '/api/export':
                cap = APP.caps.get(int(q['cap']))
                if not cap or not cap.path or not os.path.exists(cap.path):
                    return self._json({'error': 'no saved file for this capture'}, 404)
                with open(cap.path, 'rb') as f:
                    data = f.read()
                self._send(data, 'text/csv', extra={
                    'Content-Disposition': f'attachment; filename="{os.path.basename(cap.path)}"'})
            elif u.path == '/favicon.ico':
                self._send(b'', 'image/x-icon', 204)
            else:
                self._json({'error': 'not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({'error': str(e)}, 500)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n) or b'{}')
            if urlparse(self.path).path != '/api/cmd':
                return self._json({'error': 'not found'}, 404)
            res = APP.command(body) or {}
            self._json({'ok': True, **res})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({'ok': False, 'error': str(e)})


def main():
    global APP
    ap = argparse.ArgumentParser(description='Live browser GUI for the RigExpert REAMP')
    ap.add_argument('--demo', action='store_true', help='simulated REAMP, no hardware needed')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    APP = App(a.demo)
    APP.dev.start()
    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                return  # browser closed or reloaded the page
            super().handle_error(request, client_address)
    srv = QuietServer(('127.0.0.1', a.port), Handler)
    srv.daemon_threads = True
    url = f'http://127.0.0.1:{a.port}/'
    print(f'REAMP app running at {url}' + ('  (DEMO - simulated device)' if a.demo else ''))
    print('Leave this window open while you use the app; press Ctrl+C here to quit.')
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if APP.dev.logging:
            try:
                APP.dev.call('stop', timeout=3)
            except Exception:
                pass
        print('Stopped.')


if __name__ == '__main__':
    sys.exit(main())

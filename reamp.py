"""
reamp.py - Python control for the RigExpert REAMP power-consumption logger.

USB HID device "uCurrentMeter" (RigExpert), VID 0x0483 / PID 0xA1DE, 64-byte reports, no report IDs.
Protocol reverse-engineered from REAMP-Logger 2.0.7 (Windows build). Windows only; pure ctypes,
no third-party packages.

Packet = 64 bytes, byte 0 = descriptor. Multi-byte integers are big-endian; the status floats are
little-endian IEEE-754.

  0x01  device info        host sends [01]; reply [1] id, [2] fw minor, [3] channels, [4:8] S/N (BE),
                           [8:] ASCII 'RigExpert Current logger v1.09'
  0x02  measurement cmd    host -> dev: [02, cmd, 0, err, maskH, maskL, periodH, periodL, 01, delayH, delayL]
                           cmd 0x80 start now, 0x40 stop, 0x20 arm on TRIG, 0x10 arm on TRIG after delay
                           period in 100 us units (10 kHz = 1, 1 kHz = 10, 100 Hz = 100, 10 Hz = 1000)
                           channel mask bit0 = #0 current, bit1 = #1 Vin1, bit2 = #2 Vin2
                           dev -> host reply echoes it; byte 3 = error code (0 = OK)
  0x04  channel info       host sends [04, 0, 0]; device replies once per channel:
                           [4] channel, [5] data type, [6] base, [7] min exponent (signed), [12:] 'mA,Current,'
  0x05  sample data        [1:4] index of first sample frame (BE 24-bit), [4] status flags, [5] TRIG/SYNC pins,
                           [6] frames in packet (28 / channels), [7:] BE uint16 samples, channels interleaved.
                           Current (type 1): top 2 bits = range, low 14 = mantissa;
                             uA = base * 10^(range + minexp) * mantissa / 16383 * 1000  (2 mA/20 mA/200 mA/2 A FS)
                           Voltage (type 0): signed count = mV
  0x06  power control      host -> dev: [06, ctrl, 0, mV_H, mV_L, syncdelayH, syncdelayL]
                           ctrl bits: 0x10 apply voltage | 0x08 output ON (permanent)
                                      0x0C output delayed | 0x04 output OFF
                                      0x80 apply power source (+0x40 = battery, else USB)
                                      0x02 apply sync config (+0x01 = sync linked to logging, else TRIG)
                                      0x20 reset error flags
                                      0x00 = pure status poll (what the vendor app's timer sends)
        power status       dev -> host reply:
                           [1] ctrl echo, [2] bit7 'injected' (unsolicited), [4] error/status flags,
                           [5] bit6 = source Vbat, bit7 = source Vusb,
                           [6:10]  float #0  current (mA)
                           [10:14] float #1  Vin1 (mV)
                           [14:18] float #2  Vin2 (mV)
                           [18:22] float Vin  REAMP supply input (mV)
                           [22:24] int16 BE  Vout SETPOINT held by the device (mV) - not a measurement;
                                             to measure the real output, jumper Vout to Vin1 and read vin1_mV
                           [24]    output state 0x04 off / 0x08 permanent / 0x0C delayed
  0x07  text / event messages (not needed for logging)
"""
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import struct
import time

TIMEBASE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reamp_timebase.json')

VID, PID = 0x0483, 0xA1DE
REPORT_LEN = 64

D_INFO, D_CMD, D_CHINFO, D_DATA, D_POWER, D_MSG = 1, 2, 4, 5, 6, 7

# power-control bits
P_APPLY_V = 0x10
P_OUT_OFF, P_OUT_ON, P_OUT_DELAYED = 0x04, 0x08, 0x0C
P_RESET_ERR = 0x20
P_APPLY_SRC, P_SRC_BAT = 0x80, 0x40
P_APPLY_SYNC, P_SYNC_LOGGING = 0x02, 0x01
OUT_STATE = {0x04: 'OFF', 0x08: 'ON (permanent)', 0x0C: 'ON (delayed)'}

# measurement commands
C_START, C_STOP, C_ARM_TRIG, C_ARM_TRIG_DELAY, C_ARM_LEVEL = 0x80, 0x40, 0x20, 0x10, 0x08
TRIG_CMD = {None: C_START, False: C_START, True: C_ARM_TRIG, 'edge': C_ARM_TRIG, 'delay': C_ARM_TRIG_DELAY,
            'level': C_ARM_LEVEL}

# ----------------------------------------------------------------------------------------------
# Windows HID layer (ctypes)
# ----------------------------------------------------------------------------------------------
_hid = ctypes.WinDLL('hid')
_setupapi = ctypes.WinDLL('setupapi', use_last_error=True)
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)


class _GUID(ctypes.Structure):
    _fields_ = [('Data1', wt.DWORD), ('Data2', wt.WORD), ('Data3', wt.WORD), ('Data4', ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('InterfaceClassGuid', _GUID), ('Flags', wt.DWORD),
                ('Reserved', ctypes.c_void_p)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Size', wt.ULONG), ('VendorID', wt.USHORT), ('ProductID', wt.USHORT),
                ('VersionNumber', wt.USHORT)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.c_void_p), ('InternalHigh', ctypes.c_void_p),
                ('Offset', wt.DWORD), ('OffsetHigh', wt.DWORD), ('hEvent', wt.HANDLE)]


_INVALID = ctypes.c_void_p(-1).value
_setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
_setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND, wt.DWORD]
_setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                  wt.DWORD, ctypes.c_void_p]
_setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                       wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
_k32.CreateFileW.restype = wt.HANDLE
_k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE]
_k32.CreateEventW.restype = wt.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
_k32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(wt.DWORD), wt.BOOL]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.CancelIo.argtypes = [wt.HANDLE]
_k32.ResetEvent.argtypes = [wt.HANDLE]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_hid.HidD_GetAttributes.argtypes = [wt.HANDLE, ctypes.POINTER(_HIDD_ATTRIBUTES)]
_hid.HidD_SetNumInputBuffers.argtypes = [wt.HANDLE, wt.ULONG]
_hid.HidD_FlushQueue.argtypes = [wt.HANDLE]
for _fn in ('HidD_GetProductString', 'HidD_GetManufacturerString', 'HidD_GetSerialNumberString'):
    getattr(_hid, _fn).argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]


def find_paths(vid=VID, pid=PID):
    """Return device-interface paths of all attached REAMPs."""
    g = _GUID()
    _hid.HidD_GetHidGuid(ctypes.byref(g))
    hdev = _setupapi.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x12)  # PRESENT | DEVICEINTERFACE
    out, i = [], 0
    try:
        while True:
            d = _SP_DEVICE_INTERFACE_DATA()
            d.cbSize = ctypes.sizeof(d)
            if not _setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(g), i, ctypes.byref(d)):
                break
            i += 1
            req = wt.DWORD()
            _setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(d), None, 0, ctypes.byref(req), None)
            buf = ctypes.create_string_buffer(req.value)
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if _setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(d), buf, req, None, None):
                p = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                if f'vid_{vid:04x}&pid_{pid:04x}' in p.lower():
                    out.append(p)
    finally:
        _setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


class HidDevice:
    """Overlapped HID handle opened in shared mode (coexists with the vendor app if it is running)."""

    def __init__(self, path):
        self.path = path
        self.h = _k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)  # R/W, share R/W, OVERLAPPED
        if self.h in (None, _INVALID):
            raise OSError(f'Cannot open REAMP (Windows error {ctypes.get_last_error()})')
        _hid.HidD_SetNumInputBuffers(self.h, 512)  # deep queue so fast sample streams are not dropped
        self._rev = _k32.CreateEventW(None, True, False, None)
        self._wev = _k32.CreateEventW(None, True, False, None)
        self._rbuf = ctypes.create_string_buffer(REPORT_LEN + 1)

    def _str(self, fn):
        b = ctypes.create_unicode_buffer(128)
        getattr(_hid, fn)(self.h, b, 256)
        return b.value

    @property
    def product(self):
        return self._str('HidD_GetProductString')

    @property
    def serial(self):
        return self._str('HidD_GetSerialNumberString')

    def flush(self):
        _hid.HidD_FlushQueue(self.h)

    def write(self, payload):
        data = bytes(payload)[:REPORT_LEN].ljust(REPORT_LEN, b'\0')
        buf = ctypes.create_string_buffer(b'\0' + data, REPORT_LEN + 1)  # report ID 0 + 64 bytes
        ov = _OVERLAPPED(hEvent=self._wev)
        _k32.ResetEvent(self._wev)
        n = wt.DWORD()
        if not _k32.WriteFile(self.h, buf, REPORT_LEN + 1, ctypes.byref(n), ctypes.byref(ov)):
            if ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                raise OSError(f'HID write failed ({ctypes.get_last_error()})')
            if _k32.WaitForSingleObject(self._wev, 1000) != 0:
                _k32.CancelIo(self.h)
                raise TimeoutError('HID write timed out')
            _k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), False)

    def read(self, timeout=1.0):
        """Return one 64-byte report (bytes) or None on timeout."""
        ov = _OVERLAPPED(hEvent=self._rev)
        _k32.ResetEvent(self._rev)
        n = wt.DWORD()
        ok = _k32.ReadFile(self.h, self._rbuf, REPORT_LEN + 1, ctypes.byref(n), ctypes.byref(ov))
        if not ok:
            if ctypes.get_last_error() != 997:
                raise OSError(f'HID read failed ({ctypes.get_last_error()})')
            if _k32.WaitForSingleObject(self._rev, int(timeout * 1000)) != 0:
                _k32.CancelIo(self.h)
                _k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), True)
                return None
            _k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(n), False)
        return self._rbuf.raw[1:1 + REPORT_LEN]

    def close(self):
        if self.h:
            _k32.CloseHandle(self.h)
            _k32.CloseHandle(self._rev)
            _k32.CloseHandle(self._wev)
            self.h = None


# ----------------------------------------------------------------------------------------------
# Protocol
# ----------------------------------------------------------------------------------------------
class PowerStatus:
    """Decoded 0x06 power-status reply."""

    def __init__(self, raw):
        self.raw = raw
        self.ctrl_echo = raw[1]
        self.unsolicited = bool(raw[2] & 0x80)
        self.flags = raw[4]
        self.source = 'battery' if raw[5] & 0x40 else ('USB' if raw[5] & 0x80 else '?')
        # units confirmed from the vendor display code: current in mA, voltages in mV
        self.current_mA, self.vin1_mV, self.vin2_mV, self.supply_mV = struct.unpack_from('<4f', raw, 6)
        self.vout_mV = struct.unpack_from('>h', raw, 22)[0]
        self.output_state = raw[24]

    @property
    def output(self):
        return OUT_STATE.get(self.output_state, f'0x{self.output_state:02X}')

    @property
    def errors(self):
        return self.flags & 0x7B  # the vendor app shows "Reset errors" when any of these are set

    def as_dict(self):
        return dict(vout_mV=self.vout_mV, output=self.output, source=self.source,
                    current_mA=self.current_mA, vin1_mV=self.vin1_mV, vin2_mV=self.vin2_mV,
                    supply_mV=self.supply_mV, flags=self.flags)

    def __str__(self):
        err = f'  ERRORS 0x{self.errors:02X}' if self.errors else ''
        i = self.current_mA
        istr = f'{i * 1000:.1f} uA' if abs(i) < 1 else f'{i:.3f} mA'
        return (f'Vout set {self.vout_mV} mV | output {self.output} | source {self.source} | '
                f'I {istr} | Vin1 {self.vin1_mV:.1f} mV | Vin2 {self.vin2_mV:.1f} mV | '
                f'REAMP supply {self.supply_mV / 1000:.3f} V{err}')


def _pkt(*b):
    return bytes(b)


class ChannelInfo:
    """Decoded 0x04 channel-info reply: [4] channel, [5] data type, [6] base, [7] min exponent (signed),
    [12:] 'unit,name,' text. Data type 1 = auto-ranging current, 0 = voltage in mV."""

    def __init__(self, raw):
        self.channel, self.dtype, self.base = raw[4], raw[5], raw[6]
        self.minexp = raw[7] - 256 if raw[7] > 127 else raw[7]
        txt = raw[12:].split(b'\0')[0].decode('ascii', 'replace').split(',')
        self.unit, self.name = (txt + ['', ''])[:2]

    def convert(self, raw):
        """Raw 16-bit sample -> current in uA (type 1) or voltage in mV (type 0)."""
        if self.dtype == 1:
            # top 2 bits = range (2 mA / 20 mA / 200 mA / 2 A full scale with base 2), low 14 bits = mantissa
            return self.base * 10.0 ** ((raw >> 14) + self.minexp) * (raw & 0x3FFF) / 16383.0 * 1000.0
        v = raw - 65536 if raw > 32767 else raw
        return float(max(-100, v)) if self.dtype == 0 else float(v)

    @property
    def out_unit(self):
        return 'uA' if self.dtype == 1 else ('mV' if self.dtype == 0 else self.unit)

    def __repr__(self):
        return f'<ch{self.channel} {self.name} {self.out_unit} type={self.dtype} base={self.base} minexp={self.minexp}>'


class REAMP:
    def __init__(self, path=None):
        paths = [path] if path else find_paths()
        if not paths:
            raise RuntimeError('No REAMP (uCurrentMeter 0483:A1DE) found')
        self.dev = HidDevice(paths[0])
        self._last_cmd = None
        self.last_power = None  # PowerStatus reply seen inside stream_blocks (see power_nowait)

    # -- low level ---------------------------------------------------------------------------
    def send(self, pkt):
        self.dev.write(pkt)

    def recv(self, timeout=1.0):
        return self.dev.read(timeout)

    def wait_for(self, desc, timeout=1.0):
        """Read until a packet with descriptor `desc` arrives (others are discarded)."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            r = self.dev.read(left)
            if r and r[0] == desc:
                return r

    def _power(self, ctrl, mv=0, sync_delay=0, timeout=1.0):
        self.send(_pkt(D_POWER, ctrl, 0, (mv >> 8) & 0xFF, mv & 0xFF, (sync_delay >> 8) & 0xFF, sync_delay & 0xFF))
        r = self.wait_for(D_POWER, timeout)
        return PowerStatus(r) if r else None

    def power_nowait(self, ctrl, mv=0, sync_delay=0):
        """Send a power command without waiting for the reply - for use while stream_blocks() is running.
        The reply is stored in self.last_power when the stream loop reads it."""
        self.send(_pkt(D_POWER, ctrl, 0, (mv >> 8) & 0xFF, mv & 0xFF, (sync_delay >> 8) & 0xFF, sync_delay & 0xFF))

    # -- power supply ------------------------------------------------------------------------
    def status(self, timeout=1.0):
        """Read Vout, output state, source and the instantaneous channel values (no side effects)."""
        return self._power(0x00, 1800, 0, timeout)

    def set_voltage(self, mv):
        if not 1800 <= mv <= 5200:
            raise ValueError('REAMP output range is 1800..5200 mV')
        return self._power(P_APPLY_V, int(mv))

    def output_on(self, delayed=False):
        return self._power(P_OUT_DELAYED if delayed else P_OUT_ON)

    def output_off(self):
        return self._power(P_OUT_OFF)

    def set_source(self, battery):
        return self._power(P_APPLY_SRC | (P_SRC_BAT if battery else 0))

    def reset_errors(self):
        return self._power(P_RESET_ERR)

    # -- logging -----------------------------------------------------------------------------
    def start(self, channels=(0,), rate_hz=1000, trig=None, trig_delay=0):
        """channels: iterable of 0 (current), 1 (Vin1), 2 (Vin2). rate 10..10000 Hz (10 kHz single channel only)."""
        mask = 0
        for c in channels:
            mask |= 1 << c
        period = max(1, min(1000, round(10000 / rate_hz)))
        # trig: None = start now, 'edge' = start on TRIG, 'delay' = start on TRIG after trig_delay ms,
        #       'level' = measure while TRIG is active (REAMP-Logger's three TRIG buttons)
        cmd = TRIG_CMD.get(trig, C_ARM_TRIG)
        if cmd == C_ARM_TRIG and trig_delay:
            cmd = C_ARM_TRIG_DELAY
        pkt = _pkt(D_CMD, cmd, 0, 0, mask >> 8, mask & 0xFF, period >> 8, period & 0xFF, 1,
                   (trig_delay >> 8) & 0xFF, trig_delay & 0xFF)
        self._last_cmd = pkt
        self.send(pkt)
        return self.wait_for(D_CMD, 1.0)

    def stop(self):
        base = bytearray(self._last_cmd or _pkt(D_CMD, 0, 0, 0, 0, 1, 0, 10, 1))
        base[1] = C_STOP
        self.send(bytes(base))
        return self.wait_for(D_CMD, 1.0)

    # -- device / channel info ---------------------------------------------------------------
    def device_info(self):
        """Return the firmware's ID string, e.g. 'RigExpert Current logger v1.09'."""
        self.send(_pkt(D_INFO))
        r = self.wait_for(D_INFO, 1.0)
        return r[8:].split(b'\0')[0].decode('ascii', 'replace') if r else None

    def channel_info(self):
        """Ask for all channel descriptors (one request returns all three). Cached."""
        if getattr(self, '_chinfo', None):
            return self._chinfo
        self.send(_pkt(D_CHINFO, 0, 0))
        info, end = {}, time.monotonic() + 1.0
        while len(info) < 3 and time.monotonic() < end:
            r = self.dev.read(0.3)
            if r and r[0] == D_CHINFO:
                ci = ChannelInfo(r)
                info[ci.channel] = ci
        if not info:
            raise RuntimeError('REAMP is not responding (no channel info). If `status` also gets no reply, '
                               'unplug and replug the REAMP USB cable (see README, "If the REAMP stops responding").')
        self._chinfo = info
        return info

    # -- timebase correction -----------------------------------------------------------------
    # The REAMP's sample clock is not exact: on the development unit it runs ~0.53% fast, and the 10 kHz
    # setting actually delivers ~9.0 kHz. Measured true intervals are kept per serial number in
    # reamp_timebase.json (create/refresh with `py reamp_cli.py calibrate`).
    def _timebase(self):
        if not hasattr(self, '_tb'):
            try:
                self._tb = json.load(open(TIMEBASE_FILE)).get(self.dev.serial)
            except (OSError, ValueError):
                self._tb = None
        return self._tb

    def true_dt(self, period):
        """True seconds per sample for a period setting (100 us units); nominal if not calibrated."""
        tb = self._timebase()
        if tb:
            if str(period) in tb.get('dt_us', {}):
                return tb['dt_us'][str(period)] / 1e6
            if period >= 5 and tb.get('clock_factor'):
                return period * 1e-4 / tb['clock_factor']
        return period * 1e-4

    @property
    def timebase_calibrated(self):
        return bool(self._timebase())

    def calibrate_timebase(self, periods=(1, 2, 3, 4, 5, 10, 20, 100), seconds=8.0, report=print):
        """Measure true sample intervals against the PC clock and save them to reamp_timebase.json."""
        res = {}
        for period in periods:
            xs, ys, t_start = [], [], time.perf_counter()
            for i0, _dt, blk in self.stream_blocks((0,), 10000 / period, seconds + 1.0, raw=True):
                now = time.perf_counter()
                if now - t_start > 1.0:  # skip startup latency
                    xs.append(now); ys.append(i0 + len(blk[0]))
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            res[period] = 1e6 / slope
            report(f'  period {period:4d} (nominal {10000 / period:7.1f} Hz): true {slope:9.1f} Hz, '
                   f'{res[period]:8.2f} us/sample ({100 * (slope * period / 10000 - 1):+.2f}%)')
            time.sleep(0.3)
        factors = [p * 100.0 / us for p, us in res.items() if p >= 5]
        cf = sorted(factors)[len(factors) // 2] if factors else 1.0
        try:
            allcal = json.load(open(TIMEBASE_FILE))
        except (OSError, ValueError):
            allcal = {}
        allcal[self.dev.serial] = {'measured': datetime.date.today().isoformat(), 'clock_factor': round(cf, 6),
                                   'dt_us': {str(p): round(us, 3) for p, us in res.items()}}
        json.dump(allcal, open(TIMEBASE_FILE, 'w'), indent=2)
        if hasattr(self, '_tb'):
            del self._tb
        return allcal[self.dev.serial]

    # -- high-rate streaming -----------------------------------------------------------------
    def lut(self, ch):
        """65536-entry raw->value lookup table for channel `ch` (uA for current, mV for voltage)."""
        cache = self.__dict__.setdefault('_luts', {})
        if ch not in cache:
            cv = self.channel_info()[ch].convert
            cache[ch] = [cv(x) for x in range(65536)]
        return cache[ch]

    def stream(self, channels=(0,), rate_hz=1000, duration=None):
        """Per-sample convenience wrapper: yields (t_seconds, {channel: value})."""
        for i0, dt, block in self.stream_blocks(channels, rate_hz, duration):
            chans = list(block)
            for k, vals in enumerate(zip(*block.values())):
                yield (i0 + k) * dt, dict(zip(chans, vals))

    def stream_blocks(self, channels=(0,), rate_hz=1000, duration=None, raw=False, trig=None, trig_delay=0,
                      yield_idle=False):
        """Start logging and yield one block per USB packet: (first_sample_index, dt_seconds,
        {channel: [values...]}). Values are uA (current) / mV (voltage), or raw 16-bit codes if raw=True.
        Logging is always stopped on exit. Lost data is counted in self.stream_stats."""
        chans = sorted(set(channels))
        luts = [None if raw else self.lut(c) for c in chans]
        nc = len(chans)
        period = max(1, min(1000, round(10000 / rate_hz)))
        dt = self.true_dt(period)
        self.dev.flush()
        ack = self.start(chans, rate_hz, trig, trig_delay)
        if ack is None:
            self.stop()
            raise RuntimeError('REAMP did not acknowledge the start command')
        if ack[3]:
            self.stop()
            raise RuntimeError(f'REAMP refused to start measuring (error code {ack[3]})')
        st = self.stream_stats = dict(packets=0, frames=0, lost_frames=0, gaps=0, status_flags=0)
        expected, epoch = None, 0
        t_end = time.monotonic() + duration if duration else None
        last_data = time.monotonic()
        try:
            while t_end is None or time.monotonic() < t_end:
                p = self.dev.read(0.5)
                if p and p[0] == D_POWER:
                    self.last_power = PowerStatus(p)
                if not p or p[0] != D_DATA:
                    # while armed on TRIG, silence is normal; otherwise 3 s of silence means trouble
                    if not trig and time.monotonic() - last_data > 3.0:
                        raise RuntimeError('REAMP stopped sending data (was it stopped by another program?)')
                    if yield_idle:
                        yield None  # lets the caller check for a Stop request
                    continue
                last_data = time.monotonic()
                idx24, n = int.from_bytes(p[1:4], 'big'), p[6]
                if expected is not None:
                    if idx24 < (expected & 0xFFFFFF) and (expected & 0xFFFFFF) - idx24 > 0x800000:
                        epoch += 1 << 24  # 24-bit index wrapped
                    idx = epoch + idx24
                    if idx != expected:
                        st['gaps'] += 1
                        st['lost_frames'] += max(0, idx - expected)
                else:
                    idx = idx24
                expected = idx + n
                st['packets'] += 1
                st['frames'] += n
                st['status_flags'] |= p[4]
                words = struct.unpack_from(f'>{n * nc}H', p, 7)
                block = {}
                for j, c in enumerate(chans):
                    w = words[j::nc]
                    block[c] = list(w) if raw else [luts[j][x] for x in w]
                yield idx, dt, block
        finally:
            self.stop()
            self.dev.flush()

    def close(self):
        self.dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

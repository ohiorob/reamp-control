"""probe.py - passive listen, then one read-only status poll. Safe to run while REAMP-Logger is open."""
import sys, time, collections
from reamp import REAMP, PowerStatus, D_POWER

listen_s = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
with REAMP() as r:
    print('Device:', r.dev.product, 'S/N', r.dev.serial)
    seen = collections.Counter(); samples = {}
    t0 = time.monotonic()
    while time.monotonic() - t0 < listen_s:
        p = r.recv(0.25)
        if p:
            seen[p[0]] += 1
            samples.setdefault(p[0], []).append(p)
    print(f'Passive {listen_s:.0f}s: descriptors seen', {f'0x{k:02X}': v for k, v in seen.items()} or 'none')
    for d, ps in samples.items():
        for p in ps[:2]:
            print(f'  0x{d:02X}:', p[:32].hex(' '))
            if d == D_POWER:
                print('     ->', PowerStatus(p))
    print('Polling status (ctrl 0x00)...')
    for i in range(3):
        s = r.status()
        print('  ', s if s else 'no reply')
        if s:
            print('     raw', s.raw[:26].hex(' '))
        time.sleep(0.3)

import serial, time

PORT     = '/dev/ttyUSB0'
BAUD     = 115200
DURATION = 3 * 3600
REPORT   = 3 * 60

# Open WITHOUT asserting DTR/RTS so we don't reset the device
s = serial.Serial(PORT, BAUD, timeout=0.5, dsrdtr=False, rtscts=False)
s.dtr = False
s.rts = False

start       = time.time()
buf         = b''
health      = []
crashes     = 0
bods        = 0
last_report = start

print("=== 3-HOUR MONITOR STARTED {} ===".format(time.strftime('%H:%M:%S')), flush=True)

def report(health, crashes, bods, elapsed):
    print("\n--- T+{}m {} ---".format(int(elapsed/60), time.strftime('%H:%M:%S')), flush=True)
    print("  BOD events      : {}".format(bods), flush=True)
    print("  Crashes/reboots : {}".format(crashes), flush=True)
    if health:
        last = health[-1]
        mems = [h['mem'] for h in health]
        print("  Latest reading  : temp={} hum={} bat={}% wifi={}".format(
            last['temp'], last['hum'], last['bat'], last['wifi']), flush=True)
        print("  RAM (first/cur) : {} / {} (drift {:+d})".format(
            mems[0], mems[-1], mems[-1]-mems[0]), flush=True)
        if mems[-1] - mems[0] < -5000:
            print("  *** MEMORY LEAK SUSPECTED ***", flush=True)
    else:
        print("  No HEALTH logs yet", flush=True)
    print(flush=True)

while time.time() - start < DURATION:
    d = s.read(300)
    if d:
        buf += d
        txt = d.decode('utf-8', errors='replace')
        if txt.strip():
            print(txt, end='', flush=True)

        if b'BOD' in d:
            bods += 1
        for sig in [b'rst:0x', b'Guru Meditation', b'ets Jun  8 2016']:
            if sig in d:
                crashes += 1
                print("\n!!! CRASH #{} at T+{}s !!!".format(
                    crashes, int(time.time()-start)), flush=True)

        while b'\n' in buf:
            line, buf = buf.split(b'\n', 1)
            txt = line.decode('utf-8', errors='replace').strip()
            if txt.startswith('HEALTH'):
                parts = {}
                for tok in txt.split()[1:]:
                    if '=' in tok:
                        k, v = tok.split('=', 1)
                        parts[k] = v
                try:
                    health.append({
                        'mem' : int(parts.get('mem', '0')),
                        'wifi': parts.get('wifi', '?'),
                        'temp': parts.get('temp', '?'),
                        'hum' : parts.get('hum', '?'),
                        'bat' : parts.get('bat', '?'),
                    })
                except:
                    pass

    now = time.time()
    if now - last_report >= REPORT:
        report(health, crashes, bods, now - start)
        last_report = now

    time.sleep(0.3)

s.close()
print("\n\n=== FINAL REPORT ===", flush=True)
report(health, crashes, bods, DURATION)
print("RESULT: {} crash(es), {} BOD(s), {} health logs".format(
    crashes, bods, len(health)), flush=True)
if health:
    mems = [h['mem'] for h in health]
    print("MEMORY DRIFT: {:+d} bytes over 3h ({})".format(
        mems[-1]-mems[0],
        "LEAK?" if mems[-1]-mems[0] < -5000 else "OK"), flush=True)
print("=== END ===", flush=True)

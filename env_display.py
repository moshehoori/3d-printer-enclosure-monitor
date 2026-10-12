from machine import SPI, Pin, I2C, WDT, PWM
import machine
machine.freq(80000000)
import framebuf, time, struct, gc, network, socket, ssl

_wdt = WDT(timeout=120000)   # 2 minutes — wider than worst-case SSL/MQTT compound

# ── Config ────────────────────────────────────────────────────────────────────
from config import WIFI_SSID, WIFI_PASS, TG_TOKEN, TG_CHAT_ID, BAMBU_ACCESS, BAMBU_SERIAL  # secrets live in config.py (gitignored)
TG_HOST    = "api.telegram.org"
BUZZ_TEMP  = 40.0           # buzz when sensor temp goes over this

# Bambu Lab printer credentials (LAN mode must be enabled on printer)
BAMBU_IP     = None          # optional override; auto-discovered via SSDP if None

# ── Colors (RGB565) ──────────────────────────────────────────────────────────
def rgb(r,g,b): return ((r&0xF8)<<8)|((g&0xFC)<<3)|(b>>3)
def swap(c):    return ((c&0xFF)<<8)|(c>>8)

BG          = rgb(8, 12, 24)
PANEL_BG    = rgb(18, 24, 42)
DIV         = rgb(50, 55, 80)
WHITE       = rgb(240,240,255)
DIM         = rgb(120,130,160)

T_LBL       = rgb(255,160, 80)
T_VAL       = rgb(255,210,100)
H_LBL       = rgb( 80,200,160)
H_VAL       = rgb(100,230,180)
P_LBL       = rgb(140,180,255)
P_VAL       = rgb(180,210,255)
P_OK        = rgb( 80,230,140)
P_ERR       = rgb(255,100,100)
BAT_OK      = rgb(100,220,140)
BAT_LOW     = rgb(255,180, 60)
BAT_CRIT    = rgb(255, 80, 80)
MENU_HI     = rgb(255,200, 80)
HOT_RED     = rgb(255, 60, 60)

# ── AXP192 ────────────────────────────────────────────────────────────────────
_axp_i2c = None

def axp192_init():
    global _axp_i2c
    try:
        _axp_i2c = I2C(1, scl=Pin(22), sda=Pin(21), freq=400000)
        _axp_i2c.writeto_mem(0x34, 0x28, b'\xCC')   # LDO2/LDO3 = 3.0V
        _axp_i2c.writeto_mem(0x34, 0x12, b'\x4d')   # M5Stack official power outputs
        _axp_i2c.writeto_mem(0x34, 0x82, b'\xFF')   # ADC enable
        _axp_i2c.writeto_mem(0x34, 0x90, b'\x03')   # GPIO0 = LDOIO (backlight)
    except:
        pass

def battery_pct():
    if _axp_i2c is None: return None
    try:
        h = _axp_i2c.readfrom_mem(0x34, 0x78, 1)[0]
        l = _axp_i2c.readfrom_mem(0x34, 0x79, 1)[0]
        mv = ((h << 4) | (l >> 4)) * 1.1
        return max(0, min(100, int((mv - 3000) / 12)))
    except:
        return None

# ── ST7789 (LANDSCAPE 240x135) ───────────────────────────────────────────────
class ST7789:
    def __init__(self):
        axp192_init()
        self._spi = SPI(1, baudrate=20000000, polarity=0, phase=0,
                        sck=Pin(13), mosi=Pin(15))
        self._cs  = Pin(5,  Pin.OUT, value=1)
        self._dc  = Pin(23, Pin.OUT, value=0)
        self._rst = Pin(18, Pin.OUT, value=1)
        Pin(27, Pin.OUT, value=1)
        # LANDSCAPE: rotate 90°, swap dimensions and offsets
        self.w, self.h = 240, 135
        self._ox, self._oy = 40, 52
        self._rst.value(0); time.sleep_ms(50)
        self._rst.value(1); time.sleep_ms(120)
        for c, d in [(0x01,None),(0x11,None),(0x3A,[0x55]),
                     (0x36,[0x60]),    # MADCTL: MV=1, MY=1 = landscape
                     (0x21,None),(0x29,None)]:
            self._cmd(c, d); time.sleep_ms(10)

    def _cmd(self, c, d=None):
        self._dc.value(0); self._cs.value(0)
        self._spi.write(bytes([c])); self._cs.value(1)
        if d is not None:
            self._dc.value(1); self._cs.value(0)
            self._spi.write(bytes(d)); self._cs.value(1)

    def _win(self, x0, y0, x1, y1):
        x0+=self._ox; x1+=self._ox; y0+=self._oy; y1+=self._oy
        self._cmd(0x2A,[x0>>8,x0&0xFF,x1>>8,x1&0xFF])
        self._cmd(0x2B,[y0>>8,y0&0xFF,y1>>8,y1&0xFF])
        self._dc.value(0); self._cs.value(0)
        self._spi.write(bytes([0x2C])); self._cs.value(1)
        self._dc.value(1); self._cs.value(0)

    def rect(self, x, y, w, h, color):
        if w <= 0 or h <= 0: return
        self._win(x, y, x+w-1, y+h-1)
        chunk = struct.pack('>H', color)*64
        n = w*h
        for _ in range(n//64): self._spi.write(chunk)
        r = n%64
        if r: self._spi.write(struct.pack('>H', color)*r)
        self._cs.value(1)

    def blit(self, buf, x, y, w, h):
        self._win(x, y, x+w-1, y+h-1)
        self._spi.write(buf)
        self._cs.value(1)

    def fill(self, color): self.rect(0, 0, self.w, self.h, color)

# ── Text rendering — one-shot blit, no flash ─────────────────────────────────
def render(s, fg, bg, scale):
    w = len(s)*8*scale; h = 8*scale
    buf = bytearray(w*h*2)
    fb  = framebuf.FrameBuffer(buf, w, h, framebuf.RGB565)
    fgs = swap(fg); bgs = swap(bg)
    fb.fill(bgs)
    cb  = bytearray(128)
    cfb = framebuf.FrameBuffer(cb, 8, 8, framebuf.RGB565)
    for i, ch in enumerate(s):
        cfb.fill(bgs); cfb.text(ch, 0, 0, fgs)
        cx = i*8*scale
        for row in range(8):
            for col in range(8):
                idx = (row*8+col)*2
                px  = cb[idx]|(cb[idx+1]<<8)
                fb.fill_rect(cx+col*scale, row*scale, scale, scale, px)
    return buf, w, h

def draw_text(d, s, x, y, fg, bg, scale=1):
    buf, w, h = render(s, fg, bg, scale)
    d.blit(buf, x, y, w, h)
    gc.collect()
    return w

# ── BME680 ────────────────────────────────────────────────────────────────────
class BME680:
    def __init__(self, i2c, addr=0x77):
        self._i2c = i2c; self._addr = addr; self._t_fine = 0
        c1 = i2c.readfrom_mem(addr, 0x89, 25)
        c2 = i2c.readfrom_mem(addr, 0xE1, 16)
        self._pT1 = struct.unpack('<H', c2[8:10])[0]
        self._pT2 = struct.unpack('<h', c1[1:3])[0]
        self._pT3 = struct.unpack('<b', c1[3:4])[0]
        self._pH  = [(c2[2]<<4)|(c2[1]&0xF),(c2[0]<<4)|(c2[1]>>4),
                     struct.unpack('<b',c2[3:4])[0],struct.unpack('<b',c2[4:5])[0],
                     struct.unpack('<b',c2[5:6])[0],c2[6],
                     struct.unpack('<b',c2[7:8])[0]]

    def measure(self):
        self._i2c.writeto_mem(self._addr, 0x72, b'\x01')
        self._i2c.writeto_mem(self._addr, 0x74, b'\x41')
        time.sleep_ms(100)
        d  = self._i2c.readfrom_mem(self._addr, 0x22, 4)
        d2 = self._i2c.readfrom_mem(self._addr, 0x25, 2)
        adc_T = (d[0]<<12)|(d[1]<<4)|(d[2]>>4)
        adc_H = (d2[0]<<8)|d2[1]
        v1=(adc_T>>3)-(self._pT1<<1); v2=(v1*self._pT2)>>11
        v3=((v1>>1)*(v1>>1))>>12; v3=(v3*(self._pT3<<4))>>14
        self._t_fine = v2+v3
        temp = ((self._t_fine*5+128)>>8)/100.0
        ts=(self._t_fine*5+128)>>8; H=self._pH
        v1=adc_H-(H[0]*16)-(((ts*H[2])//100)>>1)
        v2=(H[1]*(((ts*H[3])//100)+(((ts*((ts*H[4])//100))>>6)//100)+(1<<14)))>>10
        v3=v1*v2; v4=H[5]<<7; v4=(v4+((ts*H[6])//100))>>4
        v5=((v3>>14)*(v3>>14))>>10; v6=(v4*v5)>>1
        hum = min(max(((v3+v6)>>10)*1000>>12,0),100000)/1000.0
        return temp, hum

# ── Buzzer (built-in on GPIO 2) ──────────────────────────────────────────────
_buzzer = None
def buzz_on():
    global _buzzer
    if _buzzer is None:
        try: _buzzer = PWM(Pin(2), freq=4000, duty=512)
        except: pass

def buzz_off():
    global _buzzer
    if _buzzer is not None:
        try: _buzzer.deinit()
        except: pass
        _buzzer = None

# ── Bambu Lab printer discovery (SSDP) ────────────────────────────────────────
def find_bambu():
    """Send SSDP discovery, return printer IP or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try: s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except: pass
        req = (b'M-SEARCH * HTTP/1.1\r\n'
               b'HOST: 239.255.255.250:1900\r\n'
               b'MAN: "ssdp:discover"\r\n'
               b'ST: urn:bambulab-com:device:3dprinter:1\r\n'
               b'MX: 1\r\n\r\n')
        s.sendto(req, ('239.255.255.250', 1900))
        # Read up to 3 responses for ~2s
        deadline = time.ticks_add(time.ticks_ms(), 2000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                data, addr = s.recvfrom(1024)
                if b'bambu' in data.lower() or b'BambuLab' in data:
                    s.close()
                    return addr[0]
            except: break
        s.close()
    except: pass
    return None

def check_alive(ip, port=8883, timeout=1):
    try:
        s = socket.socket(); s.settimeout(timeout); s.connect((ip, port)); s.close()
        return True
    except: return False

def scan_bambu(my_ip, disp=None):
    """Scan local /24 for a device with MQTT (port 8883). Returns IP or None.
    Takes ~50s worst case. Feeds WDT each probe."""
    try:
        parts = my_ip.split('.')
        if len(parts) != 4: return None
        base = parts[0] + '.' + parts[1] + '.' + parts[2] + '.'
        my_last = int(parts[3])
    except: return None
    for i in range(1, 255):
        if i == my_last: continue
        _wdt.feed()
        # Show scan progress on display every 20 probes
        if disp is not None and i % 20 == 0:
            try:
                disp.rect(124, 70, 110, 10, BG)
                buf, w, h = render("Scan {}/254".format(i), DIM, BG, 1)
                disp.blit(buf, 124, 70, w, h)
            except: pass
        ip = base + str(i)
        s = None
        try:
            s = socket.socket()
            s.settimeout(0.2)
            s.connect((ip, 8883))
            s.close()
            return ip
        except:
            try:
                if s: s.close()
            except: pass
    return None

# ── Bambu MQTT (minimal MQTT 3.1.1 over TLS) ──────────────────────────────────
def _mqtt_str(b):
    return bytes([len(b)>>8, len(b)&0xFF]) + b

def _mqtt_len(n):
    out = b''
    while True:
        byte = n & 0x7F
        n >>= 7
        if n > 0: byte |= 0x80
        out += bytes([byte])
        if n == 0: break
    return out

def _mqtt_read_len(sock):
    n = 0; mul = 1
    for _ in range(4):
        b = sock.read(1)
        if not b: return -1
        b = b[0]
        n += (b & 0x7F) * mul
        if not (b & 0x80): return n
        mul *= 128
    return -1

def bambu_fetch(ip, serial, access_code, timeout=5):
    """One-shot MQTT pull: connect → subscribe → pushall → parse status. Returns dict or None."""
    import ujson
    gc.collect(); gc.collect()
    topic_rep = ("device/{}/report".format(serial)).encode()
    topic_req = ("device/{}/request".format(serial)).encode()
    try:
        s = socket.socket()
        s.settimeout(timeout)   # short timeout to keep compound waits bounded
        s.connect((ip, 8883))
        s = ssl.wrap_socket(s)

        # MQTT CONNECT
        var = b'\x00\x04MQTT\x04\xC2\x00\x3C'
        payload = _mqtt_str(b'm5stick') + _mqtt_str(b'bblp') + _mqtt_str(access_code.encode())
        body = var + payload
        s.write(b'\x10' + _mqtt_len(len(body)) + body)

        # CONNACK
        ctrl = s.read(1)
        if not ctrl or ctrl[0] != 0x20:
            s.close(); return None
        _mqtt_read_len(s)
        ack = s.read(2)
        if not ack or ack[1] != 0:
            s.close(); return None

        # SUBSCRIBE to report topic
        sub_pl = b'\x00\x01' + _mqtt_str(topic_rep) + b'\x00'
        s.write(b'\x82' + _mqtt_len(len(sub_pl)) + sub_pl)
        ctrl = s.read(1)
        if not ctrl or ctrl[0] != 0x90:
            s.close(); return None
        _mqtt_read_len(s)
        s.read(3)  # discard SUBACK rest

        # PUBLISH pushall to request topic (QoS 0, no packet ID)
        req_payload = b'{"pushing":{"sequence_id":"0","command":"pushall"}}'
        pub_pl = _mqtt_str(topic_req) + req_payload
        s.write(b'\x30' + _mqtt_len(len(pub_pl)) + pub_pl)

        # Read incoming PUBLISH messages — wait for one containing 'print'
        result = None
        # Cap total wait so compound timeouts can't exceed WDT window
        deadline = time.ticks_add(time.ticks_ms(), 6000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            ctrl = s.read(1)
            if not ctrl: break
            if (ctrl[0] & 0xF0) != 0x30:    # not PUBLISH, skip
                rem = _mqtt_read_len(s)
                if rem > 0: s.read(rem)
                continue
            qos = (ctrl[0] >> 1) & 0x03
            rem = _mqtt_read_len(s)
            if rem <= 0: break
            data = b''
            while len(data) < rem:
                chunk = s.read(rem - len(data))
                if not chunk: break
                data += chunk
            if len(data) < rem: break
            tl = (data[0] << 8) | data[1]
            payload_start = 2 + tl + (2 if qos > 0 else 0)   # skip packet ID for QoS>0
            payload = data[payload_start:]
            try:
                j = ujson.loads(payload)
                if 'print' in j:
                    result = j['print']
                    break
            except:
                pass
        try: s.close()
        except: pass
        gc.collect()
        return result
    except Exception:
        gc.collect()
        return None

# ── WiFi ──────────────────────────────────────────────────────────────────────
def wifi_connect():
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    if not sta.isconnected():
        sta.connect(WIFI_SSID, WIFI_PASS)
        for _ in range(30):
            if sta.isconnected(): break
            time.sleep(0.5)
    if sta.isconnected():
        try: sta.config(pm=network.WIFI_PS_NONE)
        except: pass
        try: sta.config(txpower=8)
        except: pass
        return sta.ifconfig()[0]
    return None

# ── Telegram (HTTPS) ──────────────────────────────────────────────────────────
def tg_request(path):
    gc.collect(); gc.collect()
    try:
        addr = socket.getaddrinfo(TG_HOST, 443)[0][-1]
        s = socket.socket(); s.settimeout(8); s.connect(addr)
        s = ssl.wrap_socket(s, server_hostname=TG_HOST)
        s.write(('GET /bot{}/{} HTTP/1.0\r\nHost: {}\r\n\r\n'.format(TG_TOKEN, path, TG_HOST)).encode())
        resp = b''
        while True:
            c = s.read(2048)
            if not c: break
            resp += c
        s.close(); del s; gc.collect()
        return resp.split(b'\r\n\r\n', 1)[-1]
    except: gc.collect(); return b''

def tg_send(text):
    safe = text.replace(' ', '%20').replace('\n', '%0A')
    tg_request('sendMessage?chat_id={}&text={}'.format(TG_CHAT_ID, safe))

def tg_poll(offset):
    body = tg_request('getUpdates?offset={}&timeout=0&limit=5'.format(offset))
    msgs = []
    try:
        for part in body.split(b'"update_id":')[1:]:
            uid = int(part.split(b',')[0])
            offset = uid + 1
            if b'"text":"' in part:
                msgs.append(part.split(b'"text":"')[1].split(b'"')[0].decode())
    except: pass
    return offset, msgs

# ── UI Layout (240×135 landscape) ────────────────────────────────────────────
# Top bar:    y=0-15  (15px)
# Left col:   x=0-117, y=18-110  (env data)
# Divider:    x=118-120
# Right col:  x=122-239, y=18-110 (printer data)
# Footer:     y=115-134 (button hints)

def draw_layout(d):
    d.fill(BG)
    # Top bar background
    d.rect(0, 0, 240, 16, PANEL_BG)
    # Vertical divider
    d.rect(118, 18, 2, 90, DIV)
    # Footer bar
    d.rect(0, 115, 240, 20, PANEL_BG)
    # Left panel labels
    draw_text(d, "TEMP",     6, 22, T_LBL, BG, 1)
    draw_text(d, "HUMIDITY", 6, 62, H_LBL, BG, 1)
    # Right panel labels
    draw_text(d, "BAMBU PRINTER", 124, 22, P_LBL, BG, 1)
    # Footer hints
    draw_text(d, "A: Menu", 6, 121, DIM, PANEL_BG, 1)
    draw_text(d, "B: Light", 130, 121, DIM, PANEL_BG, 1)

def draw_top(d, wifi_ok, ip, bat):
    # Clear top bar
    d.rect(0, 0, 240, 16, PANEL_BG)
    if wifi_ok:
        draw_text(d, "WiFi", 4, 4, P_OK, PANEL_BG, 1)
        if ip:
            draw_text(d, ip, 38, 4, DIM, PANEL_BG, 1)
    else:
        draw_text(d, "WiFi: OFF", 4, 4, P_ERR, PANEL_BG, 1)
    if bat is not None:
        if bat <= 10:   col = BAT_CRIT
        elif bat <= 25: col = BAT_LOW
        else:           col = BAT_OK
        draw_text(d, "{}%".format(bat), 218, 4, col, PANEL_BG, 1)

def draw_temp(d, temp):
    # Clear value area
    d.rect(6, 34, 110, 24, BG)
    if temp is None:
        draw_text(d, "--.-", 6, 36, DIM, BG, 2)
    else:
        col = HOT_RED if temp > BUZZ_TEMP else T_VAL
        draw_text(d, "{:.1f}".format(temp), 6, 36, col, BG, 2)
        draw_text(d, "C", 80, 42, col, BG, 1)

def draw_hum(d, hum):
    d.rect(6, 74, 110, 24, BG)
    if hum is None:
        draw_text(d, "--.-", 6, 76, DIM, BG, 2)
    else:
        draw_text(d, "{:.1f}".format(hum), 6, 76, H_VAL, BG, 2)
        draw_text(d, "%", 80, 82, H_VAL, BG, 1)

def draw_printer(d, ip, online, status=None):
    """status is the dict from bambu_fetch (or None)."""
    d.rect(124, 34, 114, 75, BG)
    if ip is None:
        draw_text(d, "Searching...", 124, 36, DIM, BG, 1)
        draw_text(d, "(SSDP scan)",   124, 50, DIM, BG, 1)
        return
    if not online:
        draw_text(d, "Offline", 124, 36, P_ERR, BG, 1)
        draw_text(d, ip, 124, 50, DIM, BG, 1)
        return
    if status is None:
        draw_text(d, "Online", 124, 36, P_OK, BG, 1)
        draw_text(d, ip, 124, 50, DIM, BG, 1)
        draw_text(d, "(no data)", 124, 70, DIM, BG, 1)
        return
    # Parse status fields
    state = status.get('gcode_state', '?')
    hotend = status.get('nozzle_temper', 0)
    bed = status.get('bed_temper', 0)
    progress = status.get('mc_percent', None)
    eta_min = status.get('mc_remaining_time', None)
    # State color
    if state == 'RUNNING':   sc = P_OK
    elif state == 'PAUSE':   sc = BAT_LOW
    elif state == 'FAILED':  sc = P_ERR
    else:                    sc = DIM
    draw_text(d, state[:11], 124, 36, sc, BG, 1)
    draw_text(d, "H:{:>3.0f} B:{:>2.0f}".format(hotend, bed), 124, 52, P_VAL, BG, 1)
    if progress is not None:
        draw_text(d, "{}%".format(progress), 124, 70, P_VAL, BG, 2)
        # Progress bar
        bar_w = int(110 * progress / 100)
        d.rect(124, 92, 110, 4, PANEL_BG)
        if bar_w > 0:
            d.rect(124, 92, bar_w, 4, P_OK)
    if eta_min:
        draw_text(d, "{}h{:02d}".format(eta_min//60, eta_min%60), 124, 100, DIM, BG, 1)

# ── Menu ──────────────────────────────────────────────────────────────────────
MENU_ITEMS = [
    "WiFi On/Off",
    "Find Printer",
    "Backlight On/Off",
    "Reset Device",
    "Exit Menu",
]

def draw_menu(d, idx):
    d.fill(BG)
    draw_text(d, "MENU", 100, 4, MENU_HI, BG, 2)
    d.rect(0, 24, 240, 1, DIV)
    for i, item in enumerate(MENU_ITEMS):
        y = 32 + i*16
        if i == idx:
            d.rect(4, y-2, 232, 14, PANEL_BG)
            draw_text(d, "> "+item, 8, y, MENU_HI, PANEL_BG, 1)
        else:
            draw_text(d, "  "+item, 8, y, WHITE, BG, 1)
    draw_text(d, "A:select B:next", 50, 121, DIM, BG, 1)

# ── Main ──────────────────────────────────────────────────────────────────────
disp = ST7789()
i2c  = I2C(0, scl=Pin(33), sda=Pin(32), freq=100000)

sensor = None
def try_init_sensor():
    global sensor
    try: sensor = BME680(i2c)
    except: sensor = None

try_init_sensor()

btn_a = Pin(37, Pin.IN)   # external pull-up on board; pins 37/39 are input-only
btn_b = Pin(39, Pin.IN)
prev_a = prev_b = 1
bl_on = True

# State
wifi_enabled = True       # WiFi on by default
view = "main"             # "main" or "menu"
menu_idx = 0
ip = None
printer_ip = None
printer_online = False
last_status = None        # last fetched Bambu status dict
boot_msg = None
buzzer_active = False
prev_t = prev_h = None
last_bat = -1
last_wifi_status = None
tg_offset_g = 0

# Initial layout
draw_layout(disp)
draw_top(disp, False, None, None)

# Try initial WiFi connect
ip = wifi_connect()
draw_top(disp, ip is not None, ip, battery_pct())
boot_msg = ("ENV+Bambu online\nIP: " + ip) if ip else None

# Timers
now0 = time.ticks_ms()
last_read = 0;      READ_MS    = 5000
last_tg   = 0;      TG_MS      = 60000
last_push = 0;      PUSH_MS    = 300000
last_disc = 0;      DISC_MS    = 60000     # printer discovery retry
last_topbar = 0;    TOPBAR_MS  = 10000     # refresh top bar (battery)
last_reconnect = 0; RECONNECT_MS = 30000

while True:
    _wdt.feed()
    now = time.ticks_ms()

    # ── Buttons ───────────────────────────────────────────────────────────────
    cur_a = btn_a.value()
    if prev_a == 1 and cur_a == 0:
        if view == "main":
            view = "menu"; menu_idx = 0
            draw_menu(disp, menu_idx)
        else:
            # select current menu item
            item = MENU_ITEMS[menu_idx]
            if item == "WiFi On/Off":
                wifi_enabled = not wifi_enabled
                if not wifi_enabled:
                    try:
                        _s = network.WLAN(network.STA_IF)
                        _s.disconnect(); _s.active(False)
                    except: pass
                    ip = None; printer_ip = None; boot_msg = None
                else:
                    last_reconnect = 0
            elif item == "Find Printer":
                printer_ip = None; last_disc = 0
            elif item == "Backlight On/Off":
                bl_on = not bl_on
                Pin(27, Pin.OUT, value=1 if bl_on else 0)
            elif item == "Reset Device":
                time.sleep_ms(200)
                machine.reset()
            # Always exit menu after selection
            view = "main"
            draw_layout(disp)
            draw_top(disp, ip is not None, ip, battery_pct())
            draw_temp(disp, prev_t); draw_hum(disp, prev_h)
            draw_printer(disp, printer_ip, printer_online, last_status)
    prev_a = cur_a

    cur_b = btn_b.value()
    if prev_b == 1 and cur_b == 0:
        if view == "menu":
            menu_idx = (menu_idx + 1) % len(MENU_ITEMS)
            draw_menu(disp, menu_idx)
        else:
            bl_on = not bl_on
            Pin(27, Pin.OUT, value=1 if bl_on else 0)
    prev_b = cur_b

    # Skip rest of loop while in menu (menu is static, only buttons matter)
    if view == "menu":
        time.sleep_ms(80)
        continue

    # ── WiFi reconnect ────────────────────────────────────────────────────────
    if wifi_enabled and not network.WLAN(network.STA_IF).isconnected():
        if time.ticks_diff(now, last_reconnect) >= RECONNECT_MS:
            ip = wifi_connect()
            last_reconnect = time.ticks_ms()
            if ip:
                boot_msg = "Online\nIP: " + ip
                last_tg = 0
                last_disc = 0   # retry printer discovery
            draw_top(disp, ip is not None, ip, battery_pct())

    # ── Sensor read ───────────────────────────────────────────────────────────
    if time.ticks_diff(now, last_read) >= READ_MS:
        if sensor is None:
            try_init_sensor()
        try:
            t, h = sensor.measure()
            if t != prev_t:
                draw_temp(disp, t); prev_t = t
            if h != prev_h:
                draw_hum(disp, h); prev_h = h
            # Buzzer alarm
            if t > BUZZ_TEMP and not buzzer_active:
                buzz_on(); buzzer_active = True
            elif t <= BUZZ_TEMP - 1 and buzzer_active:
                buzz_off(); buzzer_active = False
        except:
            sensor = None
            if prev_t is not None:
                draw_temp(disp, None); draw_hum(disp, None)
                prev_t = prev_h = None
        last_read = time.ticks_ms()

    # ── Top bar refresh (battery / wifi indicator) ────────────────────────────
    if time.ticks_diff(now, last_topbar) >= TOPBAR_MS:
        bat = battery_pct()
        if bat != last_bat or (ip is not None) != last_wifi_status:
            draw_top(disp, ip is not None, ip, bat)
            last_bat = bat
            last_wifi_status = ip is not None
        last_topbar = time.ticks_ms()

    # ── Printer discovery + status ────────────────────────────────────────────
    if wifi_enabled and ip and time.ticks_diff(now, last_disc) >= DISC_MS:
        if printer_ip is None:
            # 1. Hardcoded override
            if BAMBU_IP:
                printer_ip = BAMBU_IP
            else:
                # 2. SSDP (fast, ~2s)
                printer_ip = find_bambu()
                # 3. Subnet scan fallback (~50s, only if SSDP fails)
                if printer_ip is None:
                    draw_printer(disp, None, False, None)
                    draw_text(disp, "Scanning...", 124, 50, BAT_LOW, BG, 1)
                    printer_ip = scan_bambu(ip, disp)
                    if printer_ip is None:
                        # Show "not found" message before timer resets
                        draw_text(disp, "Not found",  124, 50, P_ERR, BG, 1)
        if printer_ip:
            # Fetch real status via MQTT
            stat = bambu_fetch(printer_ip, BAMBU_SERIAL, BAMBU_ACCESS, timeout=8)
            if stat:
                printer_online = True
                last_status = stat
                draw_printer(disp, printer_ip, True, stat)
            else:
                # MQTT failed — check basic alive
                printer_online = check_alive(printer_ip)
                last_status = None
                draw_printer(disp, printer_ip, printer_online, None)
                if not printer_online:
                    printer_ip = None
        else:
            last_status = None
            draw_printer(disp, None, False, None)
        last_disc = time.ticks_ms()

    # ── Auto-push every 5 min ─────────────────────────────────────────────────
    if ip and prev_t is not None and time.ticks_diff(now, last_push) >= PUSH_MS:
        msg = "Temp: {:.1f} C\nHum:  {:.1f} %".format(prev_t, prev_h)
        if printer_ip: msg += "\nPrinter: " + printer_ip
        tg_send(msg)
        last_push = time.ticks_ms()

    # ── Telegram poll ─────────────────────────────────────────────────────────
    if ip and time.ticks_diff(now, last_tg) >= TG_MS:
        if boot_msg:
            tg_send(boot_msg); boot_msg = None
            time.sleep_ms(3000)
        tg_offset_g, msgs = tg_poll(tg_offset_g)
        for text in msgs:
            cmd = text.strip().lower().split('@')[0]
            bat = battery_pct()
            if cmd in ('/temp','/temperature'):
                reply = "Temp: {:.1f} C".format(prev_t) if prev_t is not None else "No reading"
            elif cmd in ('/hum','/humidity'):
                reply = "Hum: {:.1f} %".format(prev_h) if prev_h is not None else "No reading"
            elif cmd in ('/bat','/battery'):
                reply = "Battery: {}%".format(bat) if bat is not None else "No battery info"
            elif cmd in ('/printer','/bambu'):
                if printer_ip:
                    reply = "Printer: " + printer_ip + " (" + ("online" if printer_online else "offline") + ")"
                    if last_status:
                        reply += "\nState: {}\nHotend: {:.0f}C\nBed: {:.0f}C\nProgress: {}%".format(
                            last_status.get('gcode_state','?'),
                            last_status.get('nozzle_temper',0),
                            last_status.get('bed_temper',0),
                            last_status.get('mc_percent',0))
                else:
                    reply = "Printer not found"
            elif cmd == '/help':
                reply = "/status /temp /hum /battery /printer /help"
            else:
                t_s = "{:.1f}".format(prev_t) if prev_t is not None else "--"
                h_s = "{:.1f}".format(prev_h) if prev_h is not None else "--"
                reply = "Temp: " + t_s + " C  Hum: " + h_s + " %"
                if printer_ip: reply += "\nPrinter: " + printer_ip
            tg_send(reply)
        last_tg = time.ticks_ms()

    time.sleep_ms(80)

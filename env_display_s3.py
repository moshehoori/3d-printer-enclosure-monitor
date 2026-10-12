from machine import SPI, Pin, I2C, WDT, PWM
import machine
import framebuf, time, struct, gc, network, socket, ssl

_wdt = WDT(timeout=120000)

# ── Config ────────────────────────────────────────────────────────────────────
from config import WIFI_SSID, WIFI_PASS, TG_TOKEN, TG_CHAT_ID, BAMBU_ACCESS, BAMBU_SERIAL  # secrets live in config.py (gitignored)
TG_HOST    = "api.telegram.org"
BUZZ_TEMP  = 40.0

BAMBU_IP     = None
LONG_PRESS_MS = 800

FAN_PIN      = 5
TACH_PIN     = 6
FAN_TEMP_MIN = 28.0   # °C — fan starts at this temp
FAN_TEMP_MAX = 38.0   # °C — fan at 100% here
FAN_DUTY_MIN = 25     # % minimum duty when running (fans stall below ~15-20%)
PAUSE_TEMP   = 44.0   # °C — auto-pause printer above this temperature

# ── Colors ────────────────────────────────────────────────────────────────────
def rgb(r,g,b): return ((r&0xF8)<<8)|((g&0xFC)<<3)|(b>>3)
def swap(c):    return ((c&0xFF)<<8)|(c>>8)

BG        = rgb(  8, 12, 24)
PANEL_BG  = rgb( 18, 24, 42)
DIV       = rgb( 50, 55, 80)
WHITE     = rgb(240,240,255)
DIM       = rgb(120,130,160)
ORANGE    = rgb(255,160, 40); AMBER  = rgb(255,210,100)
MINT      = rgb( 80,220,170); GREEN  = rgb( 80,230,140)
BLUE      = rgb(120,180,255); SKY    = rgb(180,210,255)
RED       = rgb(255, 80, 80); HOT    = rgb(255, 60, 60)
YELLOW    = rgb(255,210, 60); PURPLE = rgb(180,120,255)
BAT_OK    = rgb(100,220,140); BAT_LOW = rgb(255,180,60); BAT_CRIT = rgb(255,80,80)
MENU_HI   = rgb(255,200, 80)

# ── M5PM1 Power Management ────────────────────────────────────────────────────
_pm_i2c = None
_pm_ok = False

def m5pm1_init():
    # From M5GFX source: PM1_G2 drives L3B (LCD power).
    # gpio2 = bit 2. Registers: 0x16=func, 0x10=dir, 0x13=push-pull, 0x11=output, 0x09=sleep.
    global _pm_i2c, _pm_ok
    time.sleep_ms(200)
    for attempt in range(20):
        try:
            _pm_i2c = I2C(0, scl=Pin(48), sda=Pin(47), freq=100000)
            _pm_i2c.writeto_mem(0x6e, 0x09, bytes([0x00]))                           # disable I2C sleep mode
            v = _pm_i2c.readfrom_mem(0x6e, 0x06, 1)[0]
            _pm_i2c.writeto_mem(0x6e, 0x06, bytes([v | (1<<2) | (1<<3)]))            # enable Grove/peripheral power rail (sensor VCC)
            v = _pm_i2c.readfrom_mem(0x6e, 0x16, 1)[0]
            _pm_i2c.writeto_mem(0x6e, 0x16, bytes([v & ~(1<<2)]))                    # gpio2 = GPIO function
            v = _pm_i2c.readfrom_mem(0x6e, 0x10, 1)[0]
            _pm_i2c.writeto_mem(0x6e, 0x10, bytes([v | (1<<2)]))                     # gpio2 = output
            v = _pm_i2c.readfrom_mem(0x6e, 0x13, 1)[0]
            _pm_i2c.writeto_mem(0x6e, 0x13, bytes([v & ~(1<<2)]))                    # gpio2 = push-pull
            v = _pm_i2c.readfrom_mem(0x6e, 0x11, 1)[0]
            _pm_i2c.writeto_mem(0x6e, 0x11, bytes([v | (1<<2)]))                     # gpio2 HIGH = LCD on
            _pm_ok = True
            print("M5PM1 ok attempt", attempt)
            return
        except Exception as e:
            print("M5PM1 attempt", attempt, "err:", e)
            time.sleep_ms(200)
    print("M5PM1 FAILED")

def m5pm1_ensure():
    """Re-apply LCD power if gpio2 dropped; returns True if just re-enabled."""
    global _pm_i2c, _pm_ok
    try:
        if _pm_i2c is None:
            _pm_i2c = I2C(0, scl=Pin(48), sda=Pin(47), freq=100000)
        v = _pm_i2c.readfrom_mem(0x6e, 0x11, 1)[0]
        if not (v & (1<<2)):
            _pm_i2c.writeto_mem(0x6e, 0x11, bytes([v | (1<<2)]))
            print("M5PM1 re-enabled — redrawing")
            _pm_ok = True
            return True
    except: pass
    return False

def battery_pct(): return None  # M5PM1 battery reg not documented

# ── ST7789 Display ────────────────────────────────────────────────────────────
class ST7789:
    def __init__(self):
        m5pm1_init()
        time.sleep_ms(300)  # let LCD power rail stabilize before touching SPI
        self._spi = SPI(1, baudrate=40000000, polarity=0, phase=0,
                        sck=Pin(40), mosi=Pin(39))
        self._cs  = Pin(41, Pin.OUT, value=1)
        self._dc  = Pin(45, Pin.OUT, value=0)
        self._rst = Pin(21, Pin.OUT, value=1)
        Pin(38, Pin.OUT, value=1)
        self.w, self.h = 240, 135
        self._ox, self._oy = 40, 52
        self._init_controller()

    def _init_controller(self):
        self._rst.value(0); time.sleep_ms(50)
        self._rst.value(1); time.sleep_ms(150)
        for c, d in [(0x01,None),(0x11,None),(0x3A,[0x55]),
                     (0x36,[0x60]),(0x21,None),(0x29,None)]:
            self._cmd(c, d); time.sleep_ms(15)

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

# ── Icons (12x12 bitmaps, drawn pixel-by-pixel as 1-bit, blitted with color) ─
# Each icon: 12 bytes, MSB-first, row by row, 12 rows
ICON_TEMP = (   # thermometer
    0x07,0x00, 0x05,0x00, 0x05,0x00, 0x05,0x00,
    0x05,0x00, 0x05,0x00, 0x05,0x00, 0x05,0x00,
    0x0F,0x80, 0x1F,0xC0, 0x1F,0xC0, 0x0F,0x80)
ICON_DROP = (   # water droplet
    0x02,0x00, 0x02,0x00, 0x07,0x00, 0x07,0x00,
    0x0F,0x80, 0x0F,0x80, 0x1F,0xC0, 0x1F,0xC0,
    0x1F,0xC0, 0x0F,0x80, 0x0F,0x80, 0x07,0x00)
ICON_HOTEND = ( # nozzle (extruder)
    0x1F,0xC0, 0x10,0x40, 0x1F,0xC0, 0x10,0x40,
    0x1F,0xC0, 0x0F,0x80, 0x0F,0x80, 0x07,0x00,
    0x07,0x00, 0x02,0x00, 0x02,0x00, 0x02,0x00)
ICON_BED = (    # print bed
    0x00,0x00, 0x00,0x00, 0x00,0x00, 0x1F,0xC0,
    0x1F,0xC0, 0x10,0x40, 0x10,0x40, 0x10,0x40,
    0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0x00)
ICON_WIFI = (   # WiFi waves
    0x0F,0x80, 0x18,0xC0, 0x07,0x00, 0x18,0xC0,
    0x0F,0x80, 0x06,0x00, 0x06,0x00, 0x00,0x00,
    0x06,0x00, 0x06,0x00, 0x00,0x00, 0x00,0x00)
ICON_BULB = (   # light bulb
    0x07,0x00, 0x0F,0x80, 0x1F,0xC0, 0x1F,0xC0,
    0x1F,0xC0, 0x0F,0x80, 0x07,0x00, 0x07,0x00,
    0x07,0x00, 0x07,0x00, 0x02,0x00, 0x00,0x00)
ICON_PLAY = (   # play triangle
    0x18,0x00, 0x1C,0x00, 0x1F,0x00, 0x1F,0x80,
    0x1F,0xC0, 0x1F,0xC0, 0x1F,0x80, 0x1F,0x00,
    0x1C,0x00, 0x18,0x00, 0x00,0x00, 0x00,0x00)
ICON_PAUSE = (  # pause bars
    0x1B,0x80, 0x1B,0x80, 0x1B,0x80, 0x1B,0x80,
    0x1B,0x80, 0x1B,0x80, 0x1B,0x80, 0x1B,0x80,
    0x1B,0x80, 0x1B,0x80, 0x00,0x00, 0x00,0x00)
ICON_BAT = (    # battery
    0x00,0x00, 0x00,0x00, 0x1F,0x80, 0x10,0xC0,
    0x10,0xC0, 0x10,0xC0, 0x10,0xC0, 0x1F,0x80,
    0x00,0x00, 0x00,0x00, 0x00,0x00, 0x00,0x00)
ICON_FAN = (    # cooling fan (ring with diagonal spokes)
    0x1E,0x00, 0x21,0x00, 0x52,0x80, 0x8C,0x40,
    0xA1,0x40, 0x9E,0x40, 0xA1,0x40, 0x8C,0x40,
    0x52,0x80, 0x21,0x00, 0x1E,0x00, 0x00,0x00)

def draw_icon(d, icon, x, y, color, bg=None):
    """Draw a 12x12 icon as a single blit (fast). bg fills the unset pixels."""
    if bg is None: bg = BG
    chi = color >> 8; clo = color & 0xFF
    bhi = bg >> 8;    blo = bg & 0xFF
    buf = bytearray(12*12*2)
    p = 0
    for row in range(12):
        bits = (icon[row*2] << 8) | icon[row*2+1]
        for col in range(12):
            if bits & (1 << (15-col)):
                buf[p] = chi; buf[p+1] = clo
            else:
                buf[p] = bhi; buf[p+1] = blo
            p += 2
    d.blit(buf, x, y, 12, 12)

# ── Text rendering ────────────────────────────────────────────────────────────
def render(s, fg, bg, scale):
    n = len(s)
    if n == 0: return bytearray(0), 0, 0
    w1 = n * 8
    fgs = swap(fg); bgs = swap(bg)
    # Render all chars at scale=1 in one pass (fast C path)
    src = bytearray(w1 * 16)
    fb1 = framebuf.FrameBuffer(src, w1, 8, framebuf.RGB565)
    fb1.fill(bgs)
    for i, ch in enumerate(s):
        fb1.text(ch, i*8, 0, fgs)
    if scale == 1:
        return src, w1, 8
    # Upscale: expand pixels horizontally, duplicate rows vertically
    fg_cell = bytes([fgs & 0xFF, fgs >> 8]) * scale
    bg_cell = bytes([bgs & 0xFF, bgs >> 8]) * scale
    cs = scale * 2
    w = w1 * scale; h = 8 * scale; rd = w * 2
    buf = bytearray(rd * h)
    for row in range(8):
        exp = bytearray(rd)
        oe = 0; sb = row * w1 * 2
        for col in range(w1):
            os = sb + col * 2
            exp[oe:oe+cs] = fg_cell if (src[os] | (src[os+1] << 8)) == fgs else bg_cell
            oe += cs
        bd = row * scale * rd
        for r in range(scale):
            buf[bd:bd+rd] = exp; bd += rd
    return buf, w, h

def draw_text(d, s, x, y, fg, bg, scale=1):
    buf, w, h = render(s, fg, bg, scale)
    d.blit(buf, x, y, w, h)
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

# ── Bambu MQTT ────────────────────────────────────────────────────────────────
def _mqtt_str(b): return bytes([len(b)>>8, len(b)&0xFF]) + b
def _mqtt_len(n):
    out = b''
    while True:
        byte = n & 0x7F; n >>= 7
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

def _mqtt_connect(ip, access_code, client_id, timeout=5):
    s = socket.socket()
    s.settimeout(timeout); s.connect((ip, 8883))
    s = ssl.wrap_socket(s)
    var = b'\x00\x04MQTT\x04\xC2\x00\x3C'
    payload = _mqtt_str(client_id) + _mqtt_str(b'bblp') + _mqtt_str(access_code.encode())
    body = var + payload
    s.write(b'\x10' + _mqtt_len(len(body)) + body)
    ctrl = s.read(1)
    if not ctrl or ctrl[0] != 0x20: s.close(); return None
    _mqtt_read_len(s); ack = s.read(2)
    if not ack or ack[1] != 0: print("MQTT auth err", ack[1] if ack else -1); s.close(); return None
    return s

def bambu_fetch(ip, serial, access_code, timeout=5):
    import ujson
    gc.collect(); gc.collect()
    topic_rep = ("device/{}/report".format(serial)).encode()
    topic_req = ("device/{}/request".format(serial)).encode()
    try:
        s = _mqtt_connect(ip, access_code, b'm5stick', timeout)
        if s is None: return None
        sub_pl = b'\x00\x01' + _mqtt_str(topic_rep) + b'\x00'
        s.write(b'\x82' + _mqtt_len(len(sub_pl)) + sub_pl)
        ctrl = s.read(1)
        if not ctrl or ctrl[0] != 0x90: s.close(); return None
        _mqtt_read_len(s); s.read(3)
        req_payload = b'{"pushing":{"sequence_id":"0","command":"pushall"}}'
        pub_pl = _mqtt_str(topic_req) + req_payload
        s.write(b'\x30' + _mqtt_len(len(pub_pl)) + pub_pl)
        result = {}
        deadline = time.ticks_add(time.ticks_ms(), 6000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            ctrl = s.read(1)
            if not ctrl: break
            if (ctrl[0] & 0xF0) != 0x30:
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
            payload_start = 2 + tl + (2 if qos > 0 else 0)
            payload = data[payload_start:]
            try:
                j = ujson.loads(payload)
                if 'print' in j:
                    for k, v in j['print'].items():
                        result[k] = v
                    if ('gcode_state' in result and 'bed_temper' in result
                            and 'nozzle_temper' in result and 'mc_percent' in result):
                        break
            except: pass
        try: s.close()
        except: pass
        gc.collect()
        return result if result else None
    except:
        gc.collect()
        return None

def bambu_command(ip, serial, access_code, cmd_bytes, timeout=4):
    """Send a one-shot MQTT command (publish JSON, no wait)."""
    gc.collect()
    topic = ("device/{}/request".format(serial)).encode()
    try:
        s = _mqtt_connect(ip, access_code, b'm5cmd', timeout)
        if s is None: return False
        pub_pl = _mqtt_str(topic) + cmd_bytes
        s.write(b'\x30' + _mqtt_len(len(pub_pl)) + pub_pl)
        time.sleep_ms(300)
        s.close()
        gc.collect()
        return True
    except:
        gc.collect()
        return False

def bambu_pause(ip):
    return bambu_command(ip, BAMBU_SERIAL, BAMBU_ACCESS,
        b'{"print":{"sequence_id":"0","command":"pause"}}')

def bambu_resume(ip):
    return bambu_command(ip, BAMBU_SERIAL, BAMBU_ACCESS,
        b'{"print":{"sequence_id":"0","command":"resume"}}')

def bambu_light(ip, on):
    mode = b'on' if on else b'off'
    cmd = (b'{"system":{"sequence_id":"0","command":"ledctrl",'
           b'"led_node":"chamber_light","led_mode":"' + mode +
           b'","led_on_time":500,"led_off_time":500,"loop_times":0,"interval_time":0}}')
    return bambu_command(ip, BAMBU_SERIAL, BAMBU_ACCESS, cmd)

# ── IP cache ─────────────────────────────────────────────────────────────────
_IP_CACHE = "bambu_ip.txt"

def load_cached_ip():
    try:
        with open(_IP_CACHE) as f:
            v = f.read().strip()
            return v if v else None
    except: return None

def save_cached_ip(ip):
    try:
        with open(_IP_CACHE, 'w') as f: f.write(ip)
    except: pass

def clear_cached_ip():
    try:
        import uos; uos.remove(_IP_CACHE)
    except: pass

# ── Discovery ─────────────────────────────────────────────────────────────────
def find_bambu():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        try: s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except: pass
        req = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
               b'MAN: "ssdp:discover"\r\nST: urn:bambulab-com:device:3dprinter:1\r\nMX: 1\r\n\r\n')
        s.sendto(req, ('239.255.255.250', 1900))
        deadline = time.ticks_add(time.ticks_ms(), 2000)
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            try:
                data, addr = s.recvfrom(1024)
                if b'bambu' in data.lower() or b'BambuLab' in data:
                    s.close(); return addr[0]
            except: break
        s.close()
    except: pass
    return None

def check_alive(ip, port=8883, timeout=1):
    try:
        s = socket.socket(); s.settimeout(timeout); s.connect((ip, port)); s.close()
        return True
    except: return False

def scan_bambu(my_ip, progress_cb=None):
    try:
        parts = my_ip.split('.')
        if len(parts) != 4: return None
        base = parts[0]+'.'+parts[1]+'.'+parts[2]+'.'
        my_last = int(parts[3])
    except: return None
    for i in range(1, 255):
        if i == my_last: continue
        _wdt.feed()
        if progress_cb and i % 20 == 0:
            try: progress_cb(i)
            except: pass
        s = None
        try:
            s = socket.socket(); s.settimeout(0.2)
            s.connect((base+str(i), 8883))
            s.close()
            return base+str(i)
        except:
            try:
                if s: s.close()
            except: pass
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
        return sta.ifconfig()[0]
    return None

# ── Telegram ──────────────────────────────────────────────────────────────────
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

# ── UI: single-screen layout, icons + big fonts ──────────────────────────────
#   y=0-14:   top bar
#   y=16-49:  env section (2 rows: temp row + hum row, scale=2 each)
#   y=50-77:  printer state (scale=3, 24px tall)
#   y=78-99:  hotend/bed temps with icons
#   y=100-116:progress bar with % overlay
#   y=115-134:footer / ETA

def draw_top_bar(d, wifi_ok, ip, bat, light_on):
    d.rect(0, 0, 240, 14, PANEL_BG)
    # WiFi icon
    draw_icon(d, ICON_WIFI, 2, 1, GREEN if wifi_ok else RED, PANEL_BG)
    if wifi_ok and ip:
        draw_text(d, ip, 18, 3, DIM, PANEL_BG, 1)
    # Light status icon on right
    draw_icon(d, ICON_BULB, 180, 1, YELLOW if light_on else DIV, PANEL_BG)
    # Battery (hide when N/A — device is USB-powered)
    if bat is not None:
        col = BAT_CRIT if bat <= 10 else (BAT_LOW if bat <= 25 else BAT_OK)
        draw_text(d, "{}%".format(bat), 200, 3, col, PANEL_BG, 1)

def draw_env_line(d, temp, hum, fan_pct=0):
    d.rect(0, 16, 240, 34, BG)
    # Row 1: temperature (left) + fan % (right, scale 2)
    draw_icon(d, ICON_TEMP, 4, 18, ORANGE)
    if temp is None:
        draw_text(d, "--.-C", 20, 17, DIM, BG, 2)
    else:
        col = HOT if temp > BUZZ_TEMP else AMBER
        draw_text(d, "{:.1f}C".format(temp), 20, 17, col, BG, 2)
    draw_icon(d, ICON_FAN, 128, 18, MINT if fan_pct > 0 else DIM)
    fcol = (YELLOW if fan_mode == "manual" else SKY) if fan_pct > 0 else DIM
    ftxt = "{}%".format(fan_pct) if fan_pct > 0 else "off"
    draw_text(d, ftxt, 146, 17, fcol, BG, 2)
    # Row 2: humidity (left) + fan RPM (right, scale 2)
    draw_icon(d, ICON_DROP, 4, 35, MINT)
    if hum is None:
        draw_text(d, "--%", 20, 34, DIM, BG, 2)
    else:
        draw_text(d, "{:.1f}%".format(hum), 20, 34, MINT, BG, 2)
    rw = draw_text(d, "{:.1f}k".format(fan_rpm / 1000.0), 128, 34, (SKY if fan_pct > 0 else DIM), BG, 2)
    draw_text(d, "rpm", 130 + rw, 41, DIM, BG, 1)

def draw_state(d, state):
    d.rect(0, 50, 240, 28, BG)
    if state is None:
        draw_text(d, "OFFLINE", 6, 52, RED, BG, 3); return
    if state == 'RUNNING':   col = GREEN
    elif state == 'PAUSE':   col = YELLOW
    elif state == 'FAILED':  col = RED
    elif state == 'FINISH':  col = MINT
    elif state == 'ONLINE':  col = BLUE
    else:                    col = WHITE
    s = state[:10]
    w = len(s) * 24
    x = max(0, (240 - w) // 2)
    draw_text(d, s, x, 52, col, BG, 3)

def draw_printer_temps(d, hotend, bed):
    d.rect(0, 78, 240, 22, BG)
    draw_icon(d, ICON_HOTEND, 4, 83, RED)
    draw_text(d, "{:.0f}C".format(hotend), 22, 83, AMBER, BG, 2)
    draw_icon(d, ICON_BED, 124, 83, BLUE)
    draw_text(d, "{:.0f}C".format(bed), 142, 83, SKY, BG, 2)

def draw_progress(d, pct):
    d.rect(0, 100, 240, 17, BG)
    if pct is None:
        draw_text(d, "--", 8, 101, DIM, BG, 2)
        return
    pct_s = "{}%".format(pct)
    draw_text(d, pct_s, 4, 101, GREEN, BG, 2)
    bar_x = 72; bar_w = 162; bar_h = 14
    d.rect(bar_x, 101, bar_w, bar_h, PANEL_BG)
    fill_w = int(bar_w * max(0, min(100, pct)) / 100)
    if fill_w > 0:
        d.rect(bar_x, 101, fill_w, bar_h, GREEN)

def draw_footer(d, paused, light_on, eta_min=None):
    d.rect(0, 115, 240, 20, PANEL_BG)
    draw_icon(d, ICON_PLAY if paused else ICON_PAUSE, 4, 118, MENU_HI, PANEL_BG)
    draw_icon(d, ICON_BULB, 64, 118, YELLOW if light_on else DIM, PANEL_BG)
    if eta_min and eta_min > 0:
        eta_s = "{}h{:02d}".format(eta_min//60, eta_min%60)
        draw_text(d, eta_s, 100, 119, WHITE, PANEL_BG, 1)
    draw_text(d, "A:menu", 178, 119, DIM, PANEL_BG, 1)

# ── Menu ──────────────────────────────────────────────────────────────────────
MENU_ITEMS = ["Fan Mode", "Fan +10%", "Fan -10%", "WiFi On/Off", "Find Printer", "Backlight On/Off", "Reset Device", "Exit Menu"]

def menu_label(item):
    if item == "Fan Mode":
        return "Fan: MANUAL" if fan_mode == "manual" else "Fan: AUTO"
    if item == "Fan +10%":
        return "Fan +10% ({}%)".format(fan_manual_pct)
    if item == "Fan -10%":
        return "Fan -10% ({}%)".format(fan_manual_pct)
    return item

def draw_menu(d, idx):
    d.fill(BG)
    draw_text(d, "MENU", 88, 4, MENU_HI, BG, 2)
    d.rect(0, 24, 240, 1, DIV)
    for i, item in enumerate(MENU_ITEMS):
        item = menu_label(item)
        y = 28 + i*13
        if i == idx:
            d.rect(4, y-2, 232, 12, PANEL_BG)
            draw_text(d, "> "+item, 8, y, MENU_HI, PANEL_BG, 1)
        else:
            draw_text(d, "  "+item, 8, y, WHITE, BG, 1)

# ── Main ──────────────────────────────────────────────────────────────────────
disp = ST7789()

i2c = I2C(1, scl=Pin(10), sda=Pin(9), freq=100000)

sensor = None
def try_init_sensor():
    global sensor
    try:
        sensor = BME680(i2c)
        print("BME680 ok")
    except Exception as e:
        sensor = None
        print("BME680 fail:", e)
try_init_sensor()

btn_a = Pin(11, Pin.IN, Pin.PULL_UP)
btn_b = Pin(12, Pin.IN, Pin.PULL_UP)

# IRQ-based button detection: records press/release times in interrupt context
_irq_a_dn = 0; _irq_a_up = 0
_irq_b_dn = 0; _irq_b_up = 0

def _irq_a(p):
    global _irq_a_dn, _irq_a_up
    if p.value() == 0: _irq_a_dn = time.ticks_ms()
    else:              _irq_a_up = time.ticks_ms()
def _irq_b(p):
    global _irq_b_dn, _irq_b_up
    if p.value() == 0: _irq_b_dn = time.ticks_ms()
    else:              _irq_b_up = time.ticks_ms()

btn_a.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING, handler=_irq_a)
btn_b.irq(trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING, handler=_irq_b)

# ── Fan control ───────────────────────────────────────────────────────────────
fan_duty = 0          # current duty % (0-100)
fan_rpm  = 0          # measured RPM
fan_mode = "auto"     # "auto" = temperature curve, "manual" = fixed fan_manual_pct
fan_manual_pct = 50   # duty % used in manual mode
_overtemp_latched = False  # True once auto-pause sent; cleared when temp recovers
_tach_count = 0

def _tach_irq(p):
    global _tach_count
    _tach_count += 1

_fan_pwm  = PWM(Pin(FAN_PIN), freq=25000, duty=0)
_tach_pin = Pin(TACH_PIN, Pin.IN, Pin.PULL_UP)
_tach_pin.irq(trigger=Pin.IRQ_FALLING, handler=_tach_irq)

def set_fan(pct):
    global fan_duty
    fan_duty = max(0, min(100, pct))
    _fan_pwm.duty(int(fan_duty * 1023 // 100))

def fan_auto(temp):
    if temp is None or temp < FAN_TEMP_MIN:
        set_fan(0)
    elif temp >= FAN_TEMP_MAX:
        set_fan(100)
    else:
        ratio = (temp - FAN_TEMP_MIN) / (FAN_TEMP_MAX - FAN_TEMP_MIN)
        set_fan(int(FAN_DUTY_MIN + ratio * (100 - FAN_DUTY_MIN)))

bl_on = True
printer_light_on = False

wifi_enabled = True
view = "main"
menu_idx = 0
ip = None
printer_ip = None
printer_online = False
last_status = None
boot_msg = None
prev_t = prev_h = None
prev_ts = prev_hs = None
last_bat = -1
tg_offset_g = 0
need_redraw = True

# Initial layout
disp.fill(BG)
ip = wifi_connect()
boot_msg = ("StickS3 online\nIP: " + ip) if ip else None
# Restore cached printer IP (skips slow scan on reboot)
if ip:
    _cip = load_cached_ip()
    if _cip:
        printer_ip = _cip
        print("BAMBU: cached", printer_ip)

last_read = 0;      READ_MS    = 5000
last_tg   = 0;      TG_MS      = 60000
last_push = 0;      PUSH_MS    = 300000
last_disc = 0;      DISC_MS    = 60000   # full discovery scan interval
last_fetch = 0;     FETCH_MS   = 15000   # printer status fetch when IP known
last_reconnect = 0; RECONNECT_MS = 30000
last_tach = 0;      TACH_MS    = 2000    # measure RPM every 2s

def full_redraw():
    disp.fill(BG)
    draw_top_bar(disp, ip is not None, ip, battery_pct(), printer_light_on)
    draw_env_line(disp, prev_t, prev_h, fan_duty)
    if last_status:
        draw_state(disp, last_status.get('gcode_state'))
        draw_printer_temps(disp, last_status.get('nozzle_temper',0), last_status.get('bed_temper',0))
        draw_progress(disp, last_status.get('mc_percent'))
        paused = last_status.get('gcode_state') == 'PAUSE'
        draw_footer(disp, paused, printer_light_on, last_status.get('mc_remaining_time'))
    elif printer_ip is None:
        draw_state(disp, None)
        draw_text(disp, "Searching printer...", 30, 84, DIM, BG, 1)
        draw_footer(disp, False, printer_light_on)
    else:
        draw_state(disp, "ONLINE")
        draw_text(disp, printer_ip, 60, 84, DIM, BG, 1)
        draw_footer(disp, False, printer_light_on)

full_redraw()

def handle_short_a():
    """Short press A: pause/resume printer."""
    if not printer_ip or not last_status: return
    state = last_status.get('gcode_state')
    if state == 'RUNNING':
        bambu_pause(printer_ip)
    elif state == 'PAUSE':
        bambu_resume(printer_ip)

def handle_long_a():
    """Long press A: enter menu."""
    global view, menu_idx
    view = "menu"; menu_idx = 0
    draw_menu(disp, menu_idx)

def handle_short_b():
    """Short press B: toggle printer chamber light."""
    global printer_light_on
    if not printer_ip: return
    printer_light_on = not printer_light_on
    bambu_light(printer_ip, printer_light_on)
    draw_top_bar(disp, ip is not None, ip, battery_pct(), printer_light_on)
    if last_status:
        draw_footer(disp, last_status.get('gcode_state')=='PAUSE', printer_light_on, last_status.get('mc_remaining_time'))
    else:
        draw_footer(disp, False, printer_light_on)

def handle_long_b():
    """Long press B: toggle device backlight via M5PM1 gpio2."""
    global bl_on
    bl_on = not bl_on
    try:
        v = _pm_i2c.readfrom_mem(0x6e, 0x11, 1)[0]
        if bl_on: _pm_i2c.writeto_mem(0x6e, 0x11, bytes([v | (1<<2)]))
        else:     _pm_i2c.writeto_mem(0x6e, 0x11, bytes([v & ~(1<<2)]))
    except: pass

_last_pm_check = 0
_boot_reinit_done = False
_boot_time = time.ticks_ms()

while True:
    _wdt.feed()
    now = time.ticks_ms()

    # 5s after boot: unconditional display reinit — handles cold-boot LCD power race
    if not _boot_reinit_done and time.ticks_diff(now, _boot_time) >= 5000:
        m5pm1_ensure()
        disp._init_controller()
        full_redraw()
        _boot_reinit_done = True
        print("BOOT: display reinit done")

    # Re-apply M5PM1 display power every 10s in case it dropped
    if time.ticks_diff(now, _last_pm_check) >= 10000:
        if m5pm1_ensure():
            disp._init_controller()
            full_redraw()
        _last_pm_check = now

    # ── Button handling (IRQ-based: presses captured even during blocking calls) ─
    if _irq_a_up:
        dur = time.ticks_diff(_irq_a_up, _irq_a_dn)
        _irq_a_up = 0
        if dur >= 30:  # debounce
            if view == "main":
                if dur >= LONG_PRESS_MS: handle_long_a()
                else: handle_short_a()
            else:
                item = MENU_ITEMS[menu_idx]
                stay = False
                if item == "Fan Mode":
                    fan_mode = "manual" if fan_mode == "auto" else "auto"
                    if fan_mode == "manual": set_fan(fan_manual_pct)
                    stay = True
                elif item == "Fan +10%":
                    fan_mode = "manual"
                    fan_manual_pct = min(100, fan_manual_pct + 10)
                    set_fan(fan_manual_pct); stay = True
                elif item == "Fan -10%":
                    fan_mode = "manual"
                    fan_manual_pct = max(0, fan_manual_pct - 10)
                    set_fan(fan_manual_pct); stay = True
                elif item == "WiFi On/Off":
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
                    clear_cached_ip(); printer_ip = None; last_disc = 0; last_status = None
                elif item == "Backlight On/Off":
                    handle_long_b()
                elif item == "Reset Device":
                    time.sleep_ms(200); machine.reset()
                if stay:
                    draw_menu(disp, menu_idx)
                else:
                    view = "main"; full_redraw()

    if _irq_b_up:
        dur = time.ticks_diff(_irq_b_up, _irq_b_dn)
        _irq_b_up = 0
        if dur >= 30:
            if view == "menu":
                menu_idx = (menu_idx + 1) % len(MENU_ITEMS)
                draw_menu(disp, menu_idx)
            else:
                if dur >= LONG_PRESS_MS: handle_long_b()
                else: handle_short_b()

    if view == "menu":
        time.sleep_ms(60); continue

    # ── WiFi reconnect ────────────────────────────────────────────────────────
    if wifi_enabled and not network.WLAN(network.STA_IF).isconnected():
        if time.ticks_diff(now, last_reconnect) >= RECONNECT_MS:
            ip = wifi_connect()
            last_reconnect = time.ticks_ms()
            if ip:
                boot_msg = "Online\nIP: " + ip
                last_tg = 0; last_disc = 0
            draw_top_bar(disp, ip is not None, ip, battery_pct(), printer_light_on)

    # ── Sensor read ───────────────────────────────────────────────────────────
    if time.ticks_diff(now, last_read) >= READ_MS:
        if sensor is None: try_init_sensor()
        try:
            t, h = sensor.measure()
            prev_fan = fan_duty
            if fan_mode == "manual":
                set_fan(fan_manual_pct)
            else:
                fan_auto(t)
            # Auto-pause printer on over-temperature (latched until temp recovers)
            if t >= PAUSE_TEMP:
                if not _overtemp_latched and printer_ip:
                    print("OVERTEMP {:.1f}C -> pausing printer".format(t))
                    bambu_pause(printer_ip)
                    if ip: tg_send("OVERTEMP {:.1f} C - printer paused".format(t))
                    _overtemp_latched = True
            elif t < PAUSE_TEMP - 2:
                _overtemp_latched = False
            ts = "{:.1f}".format(t); hs = "{:.0f}".format(h)
            if ts != prev_ts or hs != prev_hs or fan_duty != prev_fan:
                prev_t = t; prev_h = h; prev_ts = ts; prev_hs = hs
                draw_env_line(disp, t, h, fan_duty)
        except:
            sensor = None
            if prev_ts is not None:
                draw_env_line(disp, None, None, fan_duty)
                prev_t = prev_h = prev_ts = prev_hs = None
        last_read = time.ticks_ms()

    # ── Tach: measure fan RPM every TACH_MS ───────────────────────────────────
    if time.ticks_diff(now, last_tach) >= TACH_MS:
        elapsed = time.ticks_diff(now, last_tach)
        count = _tach_count; _tach_count = 0
        new_rpm = int(count * 30000 // elapsed)  # 2 pulses/rev * 60000ms / elapsed_ms
        last_tach = now
        if view == "main" and new_rpm != fan_rpm:
            fan_rpm = new_rpm
            draw_env_line(disp, prev_t, prev_h, fan_duty)
        else:
            fan_rpm = new_rpm

    # ── Printer discovery + status ────────────────────────────────────────────
    # Two rates: slow scan (DISC_MS) when no IP, fast fetch (FETCH_MS) when IP known
    _do_disc  = wifi_enabled and ip and printer_ip is None and time.ticks_diff(now, last_disc) >= DISC_MS
    _do_fetch = wifi_enabled and ip and printer_ip is not None and time.ticks_diff(now, last_fetch) >= FETCH_MS
    if _do_disc:
        if BAMBU_IP:
            printer_ip = BAMBU_IP
        else:
            print("BAMBU: SSDP")
            printer_ip = find_bambu()
            if printer_ip is None:
                print("BAMBU: subnet scan")
                def _cb(i):
                    disp.rect(0, 96, 240, 20, BG)
                    draw_text(disp, "Scan {}/254".format(i), 60, 100, BAT_LOW, BG, 1)
                printer_ip = scan_bambu(ip, _cb)
                if printer_ip is None:
                    print("BAMBU: not found")
        last_disc = time.ticks_ms()
        if printer_ip:
            save_cached_ip(printer_ip)
            last_fetch = 0  # trigger immediate fetch
    if _do_fetch or (printer_ip and last_fetch == 0):
        print("BAMBU: fetch from", printer_ip)
        stat = bambu_fetch(printer_ip, BAMBU_SERIAL, BAMBU_ACCESS, timeout=5)
        if stat:
            printer_online = True; last_status = stat
            print("BAMBU:", stat.get('gcode_state'), stat.get('mc_percent'), "%")
            draw_state(disp, stat.get('gcode_state'))
            draw_printer_temps(disp, stat.get('nozzle_temper',0), stat.get('bed_temper',0))
            draw_progress(disp, stat.get('mc_percent'))
            paused = stat.get('gcode_state') == 'PAUSE'
            draw_footer(disp, paused, printer_light_on, stat.get('mc_remaining_time'))
            if 'lights_report' in stat:
                for lr in stat['lights_report']:
                    if lr.get('node') == 'chamber_light':
                        printer_light_on = (lr.get('mode') == 'on')
                        draw_top_bar(disp, ip is not None, ip, battery_pct(), printer_light_on)
                        break
        else:
            printer_online = check_alive(printer_ip)
            if not printer_online:
                clear_cached_ip()
                printer_ip = None; last_status = None; last_disc = 0
                draw_state(disp, None)
        last_fetch = time.ticks_ms()

    # ── Auto-push ─────────────────────────────────────────────────────────────
    if ip and prev_t is not None and time.ticks_diff(now, last_push) >= PUSH_MS:
        msg = "Temp: {:.1f} C\nHum:  {:.1f} %".format(prev_t, prev_h)
        if last_status:
            msg += "\nPrinter: {}".format(last_status.get('gcode_state','?'))
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
            if cmd in ('/temp','/temperature'):
                reply = "Temp: {:.1f} C".format(prev_t) if prev_t is not None else "No reading"
            elif cmd in ('/hum','/humidity'):
                reply = "Hum: {:.1f} %".format(prev_h) if prev_h is not None else "No reading"
            elif cmd in ('/printer','/bambu'):
                if last_status:
                    reply = "State: {}\nHotend: {:.0f}C\nBed: {:.0f}C\nProgress: {}%".format(
                        last_status.get('gcode_state','?'),
                        last_status.get('nozzle_temper',0),
                        last_status.get('bed_temper',0),
                        last_status.get('mc_percent',0))
                else:
                    reply = "Printer not found" if not printer_ip else "Printer: " + printer_ip
            elif cmd == '/pause':
                ok = bambu_pause(printer_ip) if printer_ip else False
                reply = "Pause sent" if ok else "Pause failed"
            elif cmd == '/resume':
                ok = bambu_resume(printer_ip) if printer_ip else False
                reply = "Resume sent" if ok else "Resume failed"
            elif cmd == '/lighton':
                ok = bambu_light(printer_ip, True) if printer_ip else False
                reply = "Light ON" if ok else "Failed"
            elif cmd == '/lightoff':
                ok = bambu_light(printer_ip, False) if printer_ip else False
                reply = "Light OFF" if ok else "Failed"
            elif cmd == '/help':
                reply = "/status /temp /hum /printer /pause /resume /lighton /lightoff"
            else:
                t_s = "{:.1f}".format(prev_t) if prev_t is not None else "--"
                h_s = "{:.1f}".format(prev_h) if prev_h is not None else "--"
                reply = "Temp: " + t_s + " C  Hum: " + h_s + " %"
                if last_status:
                    reply += "\n{} {}%".format(last_status.get('gcode_state','?'), last_status.get('mc_percent',0))
            tg_send(reply)
        last_tg = time.ticks_ms()

    time.sleep_ms(60)

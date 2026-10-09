#!/usr/bin/env python3
# =====================================================================
#  Simulador EspelhoHex
#
#  Faz o computador se comportar como uma parede de módulos EspelhoHex:
#   - responde ao ArtPoll (a mesa enxerga um nó por módulo)
#   - recebe ArtDmx e extrai os canais de cada módulo como o firmware
#   - aceita ArtAddress (nome, universo, localizar) e RDM sobre Art-Net
#     (endereço DMX, modo de canais, nome, identificar)
#   - abre o EspelhoHex CAD no navegador, com os espelhos 3D seguindo a mesa
#
#  Uso:  python simulador.py            (abre http://localhost:8080)
#        python simulador.py --modulos 7 --porta-http 8081
#        pythonw simulador.py --app       (janela própria; fecha junto com ela)
#
#  A configuração (formato da parede e endereços) fica salva em
#  espelhohex-config.json: no Windows em %LOCALAPPDATA%\EspelhoHex,
#  nos outros sistemas na mesma pasta do simulador.
#
#  Só usa a biblioteca padrão do Python 3.8 ou mais novo.
# =====================================================================
import argparse
import hashlib
import json
import os
import re
import runpy
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARTNET_PORT = 6454
TILT_MAX = 25.0
PISTON_MAX = 40.0
APP_VERSION = "3.3"
VERSION = "EspelhoHex sim " + APP_VERSION
PAGE = "espelho-hex-cad.html"
HERE = os.path.dirname(os.path.abspath(__file__))
# pasta onde o app foi instalado (o Python e a biblioteca 3D ficam lá);
# quando roda uma versão atualizada, HERE é a pasta da atualização
INSTALL = os.environ.get("ESPELHOHEX_INSTALL") or HERE


def data_dir():
    # configuração, log e perfil da janela ficam na pasta do usuário
    # (o app pode estar instalado em "Arquivos de Programas", sem permissão de escrita)
    d = os.environ.get("ESPELHOHEX_DATA")
    if not d and sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        d = os.path.join(os.environ["LOCALAPPDATA"], "EspelhoHex")
    d = d or HERE
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = HERE
    return d


DATA = data_dir()
CONFIG = os.path.join(DATA, "espelhohex-config.json")
UPDATES = os.path.join(DATA, "app")          # versões baixadas pela atualização online
settings = {"update_url": "", "auto_update": True}
upd = {"checking": False, "available": None, "notes": "", "error": "", "last_check": 0, "applying": False, "done": ""}
VENDOR = {   # bibliotecas da página: usa a cópia local (instalador) quando existir
    "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js": "three.min.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js": "OrbitControls.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/exporters/STLExporter.js": "STLExporter.js",
    "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/exporters/OBJExporter.js": "OBJExporter.js",
    "https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js": "jszip.min.js",
}
MAX_RADIUS = 6

lock = threading.Condition()
modules = []
cells = []         # posição de cada módulo na colmeia [q, r], na ordem de numeração
dirty = False
clients = {"n": 0, "seen": False, "zero_since": 0.0}
rev = 0            # muda a cada novo valor de DMX
cfg_rev = 0        # muda a cada mudança de configuração
stats = {"packets": 0, "fps": 0.0, "last_from": "", "universes": [], "bind_ok": False, "bind_error": ""}


# ---------------------------------------------------------------------
#  Módulo virtual (mesmo estado do firmware)
# ---------------------------------------------------------------------
class Module:
    def __init__(self, i):
        self.i = i
        self.uid = bytes([0x7F, 0xF0, 0x00, 0x00, 0x00, i + 1])
        self.mac = bytes([0x02, 0x45, 0x48, 0x00, 0x00, i + 1])
        self.universe = 0
        self.mode = 4
        self.address = 1 + 4 * i
        self.name = "M%02d" % (i + 1)
        self.identify = False
        self.vals = [0.0, 0.0, 0.0, 0.0]   # inclinação X (°), Y (°), avanço (mm), suavização 0..1
        self.frames = 0
        self.last = 0.0

    def footprint(self):
        return 7 if self.mode == 7 else 4

    def cfg(self):
        return {"i": self.i, "name": self.name, "universe": self.universe, "address": self.address,
                "mode": self.mode, "identify": self.identify,
                "uid": "%02X%02X:%02X%02X%02X%02X" % tuple(self.uid)}


def cell_key(c):
    # numeração por fileira: de trás para a frente, da esquerda para a direita (igual à página)
    q, r = c
    return (-r, q + r / 2.0)


def preset_cells(n):
    rings = 2 if n >= 19 else 1 if n >= 7 else 0
    out = [[q, r] for q in range(-rings, rings + 1) for r in range(-rings, rings + 1) if abs(-q - r) <= rings]
    return sorted(out, key=cell_key)


def set_layout(n=None, new_cells=None):
    global cells
    if new_cells is not None:
        seen, clean = set(), []
        for c in new_cells:
            try:
                q, r = int(c[0]), int(c[1])
            except (TypeError, ValueError, IndexError):
                continue
            if max(abs(q), abs(r), abs(q + r)) <= MAX_RADIUS and (q, r) not in seen:
                seen.add((q, r))
                clean.append([q, r])
        new_cells = sorted(clean, key=cell_key)[:127] or [[0, 0]]
    else:
        new_cells = preset_cells(max(1, int(n or 19)))
    with lock:
        cells = new_cells
        while len(modules) < len(cells):
            modules.append(Module(len(modules)))
        del modules[len(cells):]
        changed()


def auto_address(mode=None):
    global cfg_rev
    with lock:
        for m in modules:
            if mode in (4, 7):
                m.mode = mode
            per = 512 // m.footprint()
            m.universe = m.i // per
            m.address = 1 + m.footprint() * (m.i % per)
        changed()


def factory_reset():
    global cfg_rev
    with lock:
        for m in modules:
            m.universe, m.address, m.mode, m.name, m.identify = 0, 1, 4, "EspelhoHex", False
        changed()


def changed():
    """Chamar com o lock: avisa a página e marca para salvar."""
    global cfg_rev, dirty
    cfg_rev += 1
    dirty = True
    lock.notify_all()


def save_config():
    with lock:
        data = {"version": VERSION, "cells": cells, "settings": settings,
                "modules": [{"name": m.name, "universe": m.universe, "address": m.address, "mode": m.mode} for m in modules]}
    tmp = CONFIG + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG)
    except OSError as e:
        print("Não consegui salvar a configuração:", e)


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    s = data.get("settings") or {}
    settings["update_url"] = str(s.get("update_url") or settings["update_url"])[:500]
    settings["auto_update"] = bool(s.get("auto_update", True))
    set_layout(new_cells=data.get("cells") or [[0, 0]])
    with lock:
        for m, c in zip(modules, data.get("modules", [])):
            m.name = str(c.get("name", m.name))[:17]
            m.mode = 7 if c.get("mode") == 7 else 4
            m.universe = max(0, min(32767, int(c.get("universe", 0))))
            m.address = max(1, min(513 - m.footprint(), int(c.get("address", 1))))
        changed()
    return True


def saver_thread():
    global dirty
    while True:
        time.sleep(1)
        if dirty:
            dirty = False
            save_config()


# ---------------------------------------------------------------------
#  DMX → valores do módulo (igual a handleDmx do firmware)
# ---------------------------------------------------------------------
def dmx8(v):
    return (v - 127.5) / 127.5


def dmx16(hi, lo):
    return (((hi << 8) | lo) - 32767.5) / 32767.5


def handle_dmx(uni, data):
    global rev
    now = time.time()
    with lock:
        hit = False
        for m in modules:
            if m.universe != uni:
                continue
            a = m.address - 1
            if a + m.footprint() > len(data):
                continue
            if m.mode == 7:
                x, y, z, s = dmx16(data[a], data[a + 1]), dmx16(data[a + 2], data[a + 3]), dmx16(data[a + 4], data[a + 5]), data[a + 6] / 255
            else:
                x, y, z, s = dmx8(data[a]), dmx8(data[a + 1]), dmx8(data[a + 2]), data[a + 3] / 255
            m.vals = [x * TILT_MAX, y * TILT_MAX, z * PISTON_MAX, s]
            m.frames += 1
            m.last = now
            hit = True
        if hit:
            rev += 1
            lock.notify_all()


# ---------------------------------------------------------------------
#  RDM (mesma lógica de rdm.h)
# ---------------------------------------------------------------------
GET, SET, DISCOVERY = 0x20, 0x30, 0x10
PID_SUPPORTED, PID_DEVICE_INFO = 0x0050, 0x0060
PID_MODEL, PID_MANUF, PID_LABEL, PID_SWVER = 0x0080, 0x0081, 0x0082, 0x00C0
PID_PERS, PID_PERS_DESC, PID_START, PID_IDENT = 0x00E0, 0x00E1, 0x00F0, 0x1000
NR_UNKNOWN_PID, NR_FORMAT, NR_UNSUP, NR_RANGE, NR_SUBDEV = 0x0000, 0x0001, 0x0005, 0x0006, 0x0009


def rdm_checksum(b):
    return (0xCC + sum(b)) & 0xFFFF


def rdm_is_broadcast(dst):
    return dst == b"\xff" * 6 or (dst[:2] == b"\x7f\xf0" and dst[2:] == b"\xff" * 4)


def rdm_handle(m, req, bcast):
    """Devolve (resposta ou None, mudou_config)."""
    if len(req) < 25 or req[0] != 0x01 or req[1] < 24:
        return None, False
    body = req[1] - 1
    if body + 2 > len(req) or rdm_checksum(req[:body]) != struct.unpack(">H", req[body:body + 2])[0]:
        return None, False
    cc, pid, pdl = req[19], struct.unpack(">H", req[20:22])[0], req[22]
    sub = struct.unpack(">H", req[17:19])[0]
    if 23 + pdl != body or cc == DISCOVERY or cc not in (GET, SET):
        return None, False
    pd = req[23:23 + pdl]
    data, nack, ch = b"", None, False
    fp = lambda mode: 7 if mode == 7 else 4
    if sub not in (0, 0xFFFF) or (cc == GET and sub == 0xFFFF):
        nack = NR_SUBDEV
    elif cc == GET:
        if pdl and pid != PID_PERS_DESC:
            nack = NR_FORMAT
        elif pid == PID_SUPPORTED:
            data = struct.pack(">5H", PID_MODEL, PID_MANUF, PID_LABEL, PID_PERS, PID_PERS_DESC)
        elif pid == PID_DEVICE_INFO:
            data = struct.pack(">HHHIHBBHHB", 0x0100, 1, 0x7FFF, 2, fp(m.mode), 2 if m.mode == 7 else 1, 2, m.address, 0, 0)
        elif pid == PID_MODEL:
            data = b"EspelhoHex modulo cinetico"
        elif pid == PID_MANUF:
            data = b"DIY EspelhoHex"
        elif pid == PID_SWVER:
            data = VERSION.encode()
        elif pid == PID_LABEL:
            data = m.name.encode("latin-1", "replace")[:32]
        elif pid == PID_START:
            data = struct.pack(">H", m.address)
        elif pid == PID_IDENT:
            data = bytes([1 if m.identify else 0])
        elif pid == PID_PERS:
            data = bytes([2 if m.mode == 7 else 1, 2])
        elif pid == PID_PERS_DESC:
            if pdl != 1 or pd[0] not in (1, 2):
                nack = NR_RANGE
            else:
                d = b"7 canais 16 bits" if pd[0] == 2 else b"4 canais 8 bits"
                data = bytes([pd[0]]) + struct.pack(">H", 7 if pd[0] == 2 else 4) + d
        else:
            nack = NR_UNKNOWN_PID
    else:
        if pid == PID_START:
            if pdl != 2:
                nack = NR_FORMAT
            else:
                a = struct.unpack(">H", pd)[0]
                if a < 1 or a + fp(m.mode) - 1 > 512:
                    nack = NR_RANGE
                elif a != m.address:
                    m.address, ch = a, True
        elif pid == PID_IDENT:
            if pdl != 1 or pd[0] > 1:
                nack = NR_FORMAT
            else:
                m.identify, ch = pd[0] == 1, True
        elif pid == PID_LABEL:
            if pdl > 32:
                nack = NR_FORMAT
            else:
                m.name, ch = pd.decode("latin-1")[:17], True
        elif pid == PID_PERS:
            if pdl != 1:
                nack = NR_FORMAT
            elif pd[0] not in (1, 2):
                nack = NR_RANGE
            else:
                mode = 7 if pd[0] == 2 else 4
                if m.address + fp(mode) - 1 > 512:
                    nack = NR_RANGE
                elif mode != m.mode:
                    m.mode, ch = mode, True
        elif pid in (PID_SUPPORTED, PID_DEVICE_INFO, PID_MODEL, PID_MANUF, PID_SWVER, PID_PERS_DESC):
            nack = NR_UNSUP
        else:
            nack = NR_UNKNOWN_PID
    if bcast:
        return None, ch
    if nack is not None:
        data = struct.pack(">H", nack)
    out = bytearray([0x01, 24 + len(data)]) + req[8:14] + m.uid
    out += bytes([req[14], 0x02 if nack is not None else 0x00, 0, req[17], req[18], cc + 1])
    out += struct.pack(">H", pid) + bytes([len(data)]) + data
    out += struct.pack(">H", rdm_checksum(out))
    return bytes(out), ch


# ---------------------------------------------------------------------
#  Art-Net
# ---------------------------------------------------------------------
sock = None


def local_ip_towards(ip):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((ip if ip and not ip.startswith("127.") else "8.8.8.8", ARTNET_PORT))
        r = s.getsockname()[0]
        s.close()
        return r
    except OSError:
        return "127.0.0.1"


def header(op):
    return b"Art-Net\x00" + struct.pack("<H", op)


def poll_reply(m, ip):
    r = bytearray(239)
    r[0:10] = header(0x2100)
    r[10:14] = socket.inet_aton(ip)
    r[14], r[15] = 0x36, 0x19
    r[16], r[17] = 2, 1
    r[18] = (m.universe >> 8) & 0x7F
    r[19] = (m.universe >> 4) & 0x0F
    r[20], r[21] = 0xFF, 0xFF
    r[23] = (0x40 if m.identify else 0xC0) | 0x20 | 0x02
    name = m.name.encode("latin-1", "replace")[:17]
    r[26:26 + len(name)] = name
    ln = ("EspelhoHex SIMULADO - DMX %d, %d canais" % (m.address, m.mode)).encode()[:63]
    r[44:44 + len(ln)] = ln
    rep = ("#0001 [%04d] OK" % (m.frames % 10000)).encode()
    r[108:108 + len(rep)] = rep
    r[172], r[173] = 0, 1
    r[174] = 0x80
    r[182] = 0x80 if time.time() - m.last < 3 else 0x00
    r[190] = m.universe & 0x0F
    r[200] = 0x00
    r[201:207] = m.mac
    r[207:211] = socket.inet_aton(ip)
    r[211] = m.i + 1                 # BindIndex: um nó por módulo no mesmo IP
    r[212] = 0x0D
    return bytes(r)


def send(data, addr):
    try:
        sock.sendto(data, addr)
    except OSError:
        pass


def reply_poll(addr):
    ip = local_ip_towards(addr[0])
    with lock:
        packets = [poll_reply(m, ip) for m in modules]
    for p in packets:
        send(p, (addr[0], ARTNET_PORT))


def tod_data(net, address):
    with lock:
        uids = [m.uid for m in modules if (m.universe >> 8) & 0x7F == net and m.universe & 0xFF == address]
    out = []
    for blk in range(0, max(1, len(uids)), 200):
        part = uids[blk:blk + 200]
        r = bytearray(28)
        r[0:10] = header(0x8100)
        r[10], r[11] = 0, 14
        r[12], r[13] = 0x01, 1
        r[20], r[21], r[22], r[23] = 1, net, 0x00, address
        r[24:26] = struct.pack(">H", len(uids))
        r[26], r[27] = blk // 200, len(part)
        out.append(bytes(r) + b"".join(part))
    return out


def handle_packet(p, addr):
    if len(p) < 12 or p[:8] != b"Art-Net\x00":
        return
    op = struct.unpack("<H", p[8:10])[0]
    if op == 0x5000 and len(p) >= 18:                       # ArtDmx
        uni = p[14] | ((p[15] & 0x7F) << 8)
        n = min((p[16] << 8) | p[17], len(p) - 18)
        stats["packets"] += 1
        stats["last_from"] = addr[0]
        if uni not in stats["universes"]:
            stats["universes"] = sorted(stats["universes"] + [uni])[:16]
        handle_dmx(uni, p[18:18 + n])
    elif op == 0x2000:                                      # ArtPoll
        reply_poll(addr)
    elif op == 0x6000 and len(p) >= 107:                    # ArtAddress
        bind = max(1, p[13])
        with lock:
            if bind > len(modules):
                return
            m = modules[bind - 1]
            net, sub, uni = (m.universe >> 8) & 0x7F, (m.universe >> 4) & 0x0F, m.universe & 0x0F

            def field(v, mask, cur):
                if v & 0x80:
                    return v & mask
                return 0 if v == 0 else cur
            net, sub, uni = field(p[12], 0x7F, net), field(p[104], 0x0F, sub), field(p[100], 0x0F, uni)
            m.universe = (net << 8) | (sub << 4) | uni
            if p[14]:
                m.name = p[14:32].split(b"\x00")[0].decode("latin-1")[:17]
            if p[106] in (0x02, 0x03):
                m.identify = False
            elif p[106] == 0x04:
                m.identify = True
            changed()
        reply_poll(addr)
    elif op in (0x8000, 0x8200) and len(p) >= 24:           # ArtTodRequest / ArtTodControl
        net = p[21]
        addrs = list(p[24:24 + p[23]]) if op == 0x8000 else [p[23]]
        for a in addrs:
            for pkt in tod_data(net, a):
                send(pkt, ("255.255.255.255", ARTNET_PORT))
                send(pkt, (addr[0], ARTNET_PORT))
    elif op == 0x8300 and len(p) >= 24 + 25 and p[22] == 0:  # ArtRdm
        net, address, req = p[21], p[23], p[24:]
        dst = bytes(req[2:8])
        bcast = rdm_is_broadcast(dst)
        with lock:
            targets = [m for m in modules if (m.universe >> 8) & 0x7F == net and m.universe & 0xFF == address
                       and (bcast or m.uid == dst)]
            replies, ch = [], False
            for m in targets:
                resp, c = rdm_handle(m, req, bcast)
                ch = ch or c
                if resp:
                    replies.append(resp)
            if ch:
                changed()
        for resp in replies:
            r = bytearray(24)
            r[0:10] = header(0x8300)
            r[10], r[11], r[12] = 0, 14, 0x01
            r[21], r[22], r[23] = net, 0x00, address
            send(bytes(r) + resp, (addr[0], ARTNET_PORT))


def artnet_thread():
    global sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("0.0.0.0", ARTNET_PORT))
        stats["bind_ok"] = True
    except OSError as e:
        stats["bind_error"] = str(e)
        print("ERRO: não consegui abrir a porta Art-Net 6454 (%s)." % e)
        print("Feche outros programas de Art-Net neste computador (QLC+, Resolume...) e rode de novo.")
        return
    while True:
        try:
            p, addr = sock.recvfrom(2048)
            handle_packet(p, addr)
        except OSError:
            time.sleep(0.05)


def fps_thread():
    last = 0
    while True:
        time.sleep(1)
        n = stats["packets"]
        stats["fps"] = n - last
        last = n


# ---------------------------------------------------------------------
#  Servidor da página (HTTP + Server-Sent Events)
# ---------------------------------------------------------------------
def snapshot_live():
    now = time.time()
    return {"rev": rev, "cfg_rev": cfg_rev,
            "m": [[round(v, 3) for v in m.vals] + [1 if m.identify else 0, 1 if now - m.last < 1.5 else 0] for m in modules],
            "fps": stats["fps"], "from": stats["last_from"], "unis": stats["universes"]}


_ips_cache = {"t": 0, "v": []}


def my_ips():
    if time.time() - _ips_cache["t"] > 10:
        ips = {local_ip_towards("")}
        try:
            ips |= {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
        except OSError:
            pass
        _ips_cache["v"] = sorted(i for i in ips if not i.startswith("127."))
        _ips_cache["t"] = time.time()
    return _ips_cache["v"]


def vendor_path(name):
    for base in (HERE, INSTALL):
        p = os.path.join(base, "vendor", name)
        if os.path.isfile(p):
            return p
    return None


def snapshot_cfg():
    ips = my_ips()
    return {"modules": [m.cfg() for m in modules], "cells": cells, "ips": ips, "bind_ok": stats["bind_ok"],
            "bind_error": stats["bind_error"], "version": VERSION, "app_version": APP_VERSION,
            "update": {"url": settings["update_url"], "auto": settings["auto_update"], "checking": upd["checking"],
                       "available": upd["available"], "notes": upd["notes"], "error": upd["error"],
                       "last_check": upd["last_check"], "applying": upd["applying"], "done": upd["done"]}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            path = os.path.join(HERE, PAGE)
            if not os.path.isfile(path):
                path = os.path.join(INSTALL, PAGE)
            try:
                with open(path, "rb") as f:
                    b = f.read()
            except OSError:
                self.send_error(404, "Coloque %s na mesma pasta do simulador" % PAGE)
                return
            for url, name in VENDOR.items():
                if vendor_path(name):
                    b = b.replace(url.encode(), ("/vendor/" + name).encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith("/vendor/") and self.path[8:] in VENDOR.values():
            try:
                with open(vendor_path(self.path[8:]) or "", "rb") as f:
                    b = f.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/sim/state":
            with lock:
                self._json(snapshot_cfg())
        elif self.path == "/sim/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen, seen_cfg, last_beat = -1, -1, 0
            with lock:
                clients["n"] += 1
                clients["seen"] = True
            try:
                while True:
                    with lock:
                        lock.wait(0.05)
                        cfg = snapshot_cfg() if cfg_rev != seen_cfg else None
                        if cfg is not None:
                            seen_cfg = cfg_rev
                        live = snapshot_live() if rev != seen or time.time() - last_beat > 0.5 else None
                    if cfg is not None:
                        self.wfile.write(("event: cfg\ndata: %s\n\n" % json.dumps(cfg)).encode())
                    if live:
                        seen, last_beat = live["rev"], time.time()
                        self.wfile.write(("data: %s\n\n" % json.dumps(live)).encode())
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
            finally:
                with lock:
                    clients["n"] -= 1
                    if clients["n"] == 0:
                        clients["zero_since"] = time.time()
        else:
            self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._json({"erro": "JSON inválido"}, 400)
            return
        if self.path == "/sim/layout":
            if isinstance(body.get("cells"), list):
                set_layout(new_cells=body["cells"])
            else:
                set_layout(body.get("n", 19))
        elif self.path == "/sim/auto":
            auto_address(body.get("mode"))
        elif self.path == "/sim/update/settings":
            with lock:
                if "url" in body:
                    settings["update_url"] = str(body["url"]).strip()[:500]
                    upd["available"], upd["error"] = None, ""
                if "auto" in body:
                    settings["auto_update"] = bool(body["auto"])
                changed()
            if "url" in body and settings["update_url"]:
                threading.Thread(target=check_update, daemon=True).start()
        elif self.path == "/sim/update/check":
            threading.Thread(target=check_update, daemon=True).start()
        elif self.path == "/sim/update/apply":
            threading.Thread(target=apply_update, daemon=True).start()
        elif self.path == "/sim/factory":
            factory_reset()
        elif self.path == "/sim/module":
            with lock:
                i = int(body.get("i", -1))
                if not 0 <= i < len(modules):
                    self._json({"erro": "módulo inexistente"}, 400)
                    return
                m = modules[i]
                if "mode" in body and int(body["mode"]) in (4, 7):
                    m.mode = int(body["mode"])
                if "address" in body:
                    m.address = max(1, min(513 - m.footprint(), int(body["address"])))
                if "universe" in body:
                    m.universe = max(0, min(32767, int(body["universe"])))
                if "name" in body:
                    m.name = str(body["name"])[:17]
                if "identify" in body:
                    m.identify = bool(body["identify"])
                changed()
        else:
            self.send_error(404)
            return
        with lock:
            self._json(snapshot_cfg())


# ---------------------------------------------------------------------
#  Atualização online
#
#  O endereço de atualização aponta para um "atualizacao.json":
#   {"versao": "2.4", "notas": "...", "arquivos": {"simulador.py": {"url": "app/simulador.py",
#    "sha256": "..."}, "espelho-hex-cad.html": {...}}}
#  ("url" pode ser relativa ao próprio atualizacao.json).
#  Os arquivos novos vão para %LOCALAPPDATA%\EspelhoHex\app (não precisa
#  de administrador) e o app passa a rodar a versão mais nova de lá.
# ---------------------------------------------------------------------
def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def script_version(path):
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def fetch(url, limit=20 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": "EspelhoHex/" + APP_VERSION, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("arquivo grande demais")
    return data


def default_channel():
    # canal.txt (ao lado do simulador) traz o endereço de atualização de fábrica
    for base in (HERE, INSTALL):
        try:
            with open(os.path.join(base, "canal.txt"), encoding="utf-8") as f:
                url = f.read().strip()
            if url.startswith("http"):
                return url
        except OSError:
            pass
    return ""


def get_manifest():
    url = settings["update_url"]
    if not url:
        raise ValueError("nenhum endereço de atualização configurado")
    sep = "&" if "?" in url else "?"
    man = json.loads(fetch(url + sep + "t=%d" % time.time()).decode("utf-8"))
    if not isinstance(man.get("arquivos"), dict) or "simulador.py" not in man["arquivos"]:
        raise ValueError("atualizacao.json sem a lista de arquivos")
    return man


def check_update():
    with lock:
        if upd["checking"] or upd["applying"]:
            return
        upd["checking"], upd["error"] = True, ""
        changed()
    try:
        man = get_manifest()
        newer = vtuple(man.get("versao")) > vtuple(APP_VERSION)
        with lock:
            upd["available"] = str(man.get("versao")) if newer else None
            upd["notes"] = str(man.get("notas", ""))[:2000] if newer else ""
    except Exception as e:                  # sem internet, endereço errado, etc.
        msg = str(e)
        if isinstance(e, urllib.error.HTTPError):
            msg = "o endereço respondeu com erro %d (confira o link)" % e.code
        elif isinstance(e, (urllib.error.URLError, OSError)):
            msg = "sem conexão com o endereço de atualização (sem internet?)"
        elif isinstance(e, ValueError) and "JSON" in type(e).__name__ + msg:
            msg = "o endereço não é um atualizacao.json válido"
        with lock:
            upd["error"] = msg[:300]
    with lock:
        upd["checking"], upd["last_check"] = False, time.time()
        changed()
    if upd["available"] and settings["auto_update"]:
        apply_update()


def apply_update():
    with lock:
        if upd["applying"]:
            return
        upd["applying"], upd["error"] = True, ""
        changed()
    try:
        man = get_manifest()
        if vtuple(man.get("versao")) <= vtuple(APP_VERSION):
            raise ValueError("já está na versão mais nova")
        stage = UPDATES + ".novo"
        if os.path.isdir(stage):
            for f in os.listdir(stage):
                os.remove(os.path.join(stage, f))
        os.makedirs(stage, exist_ok=True)
        for name, info in man["arquivos"].items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
                raise ValueError("nome de arquivo inválido: " + name)
            data = fetch(urllib.request.urljoin(settings["update_url"], info["url"]))
            if hashlib.sha256(data).hexdigest() != str(info.get("sha256", "")).lower():
                raise ValueError("arquivo corrompido no download: " + name)
            with open(os.path.join(stage, name), "wb") as f:
                f.write(data)
        if script_version(os.path.join(stage, "simulador.py")) != str(man.get("versao")):
            raise ValueError("a versão do simulador baixado não confere com o atualizacao.json")
        # troca a pasta de uma vez
        old = UPDATES + ".velho"
        if os.path.isdir(old):
            for f in os.listdir(old):
                os.remove(os.path.join(old, f))
            os.rmdir(old)
        if os.path.isdir(UPDATES):
            os.rename(UPDATES, old)
        os.rename(stage, UPDATES)
        with lock:
            upd["done"] = str(man.get("versao"))
            changed()
        save_config()
        time.sleep(1.2)                     # a página mostra "atualizado, reiniciando"
        restart()
    except Exception as e:
        with lock:
            upd["error"] = "não foi possível atualizar: %s" % str(e)[:300]
            upd["applying"] = False
            changed()


ARGS = {}


def restart():
    # reabre pelo lançador instalado, que escolhe a versão mais nova; a janela continua aberta
    launcher = os.path.join(INSTALL, "simulador.py")
    args = [sys.executable, launcher, "--sem-navegador", "--reiniciando", "--porta-http", str(ARGS.get("porta", 8080))]
    if ARGS.get("app"):
        args.append("--app")
    env = dict(os.environ)
    env.pop("ESPELHOHEX_UPDATED", None)
    env.pop("ESPELHOHEX_INSTALL", None)
    flags = 0x08000000 if sys.platform == "win32" else 0
    subprocess.Popen(args, cwd=INSTALL, env=env, creationflags=flags)
    os._exit(0)


def update_thread():
    time.sleep(5)
    while True:
        if settings["update_url"] and not upd["applying"]:
            check_update()
        time.sleep(6 * 3600)


def run_newest_version():
    """Se já foi baixada uma versão mais nova, roda ela no lugar desta."""
    if os.environ.get("ESPELHOHEX_UPDATED"):
        return
    newer = os.path.join(UPDATES, "simulador.py")
    v = script_version(newer)
    if v and vtuple(v) > vtuple(APP_VERSION):
        os.environ["ESPELHOHEX_UPDATED"] = "1"
        os.environ["ESPELHOHEX_INSTALL"] = HERE
        sys.argv[0] = newer
        try:
            runpy.run_path(newer, run_name="__main__")
        except Exception as e:
            # a versão nova não abriu: deixa ela de lado e volta para a instalada
            print("A versão atualizada falhou (%s); voltando para a versão instalada." % e)
            try:
                os.replace(UPDATES, UPDATES + ".falhou")
            except OSError:
                pass
            os.environ.pop("ESPELHOHEX_UPDATED", None)
            os.environ.pop("ESPELHOHEX_INSTALL", None)
            sys.argv[0] = os.path.abspath(__file__)
            return
        sys.exit(0)


def find_browser():
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
    return None


def open_window(url, app):
    exe = find_browser() if app else None
    if exe:
        profile = os.path.join(DATA, "janela")
        flags = 0x08000000 if sys.platform == "win32" else 0   # sem console
        subprocess.Popen([exe, "--app=" + url, "--user-data-dir=" + profile, "--window-size=1440,900",
                          "--no-first-run", "--no-default-browser-check"], creationflags=flags)
    else:
        webbrowser.open(url)


def watchdog_thread():
    # modo app: encerra quando a janela fica fechada por alguns segundos
    while True:
        time.sleep(1)
        with lock:
            idle = clients["seen"] and clients["n"] == 0 and time.time() - clients["zero_since"] > 8
        if idle:
            if dirty:
                save_config()
            os._exit(0)


def main():
    if not settings["update_url"]:
        settings["update_url"] = default_channel()
    ap = argparse.ArgumentParser(description="Simulador de módulos EspelhoHex (Art-Net)")
    ap.add_argument("--modulos", type=int, default=None, help="quantidade de módulos (1, 7, 19...)")
    ap.add_argument("--porta-http", type=int, default=8080)
    ap.add_argument("--sem-navegador", action="store_true")
    ap.add_argument("--app", action="store_true", help="abre numa janela própria e fecha junto com ela")
    ap.add_argument("--reiniciando", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    ARGS.update(porta=a.porta_http, app=a.app)
    if a.app:
        # sem console (pythonw): mensagens e erros vão para um arquivo
        try:
            log = open(os.path.join(DATA, "simulador.log"), "w", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = log
        except OSError:
            pass
    url = "http://localhost:%d/" % a.porta_http
    srv = None
    for _ in range(40 if a.reiniciando else 1):     # após atualizar, espera a versão antiga liberar a porta
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", a.porta_http), Handler)
            break
        except OSError:
            time.sleep(0.25)
    if srv is None:
        # já está aberto: só mostra a janela
        print("O simulador já está rodando em", url)
        if not a.reiniciando:
            open_window(url, a.app)
        return
    srv.daemon_threads = True
    if a.modulos is not None or not load_config():
        set_layout(a.modulos or 19)
    threading.Thread(target=artnet_thread, daemon=True).start()
    threading.Thread(target=fps_thread, daemon=True).start()
    threading.Thread(target=saver_thread, daemon=True).start()
    if a.app:
        if a.reiniciando:
            clients["seen"], clients["zero_since"] = True, time.time() + 20   # dá tempo da janela reconectar
        threading.Thread(target=watchdog_thread, daemon=True).start()
    threading.Thread(target=update_thread, daemon=True).start()
    print("Simulador EspelhoHex rodando com %d módulos." % len(modules))
    print("IPs deste computador:", ", ".join(my_ips()) or "-")
    print("Configure a mesa para enviar Art-Net para um desses IPs (ou broadcast).")
    print("Página 3D:", url)
    print("Ctrl+C para sair.")
    if not a.sem_navegador:
        threading.Timer(0.6, lambda: open_window(url, a.app)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        save_config()
        print("\nEncerrado.")


if __name__ == "__main__":
    run_newest_version()
    main()

#!/usr/bin/env python3
"""server.py 的端到端测试。

真起两个服务：一个"目标"（模拟没有 CORS 头的内网接口），一个是被测的 server.py。
只用标准库，不需要 pytest：直接 `python tests/test_server.py`。
"""

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv                                     # noqa: E402

TARGET_PORT, CONSOLE_PORT = 8093, 8092
BASE = 'http://127.0.0.1:%d' % TARGET_PORT
CONSOLE = 'http://127.0.0.1:%d' % CONSOLE_PORT

passed = failed = 0


def check(name, cond, extra=''):
    global passed, failed
    if cond:
        passed += 1
        print('  PASS  ' + name)
    else:
        failed += 1
        print('  FAIL  ' + name + ('  → ' + str(extra) if extra else ''))


# ── 目标服务：故意不发任何 CORS 头 ──
BIG = json.dumps({
    'type': 'FeatureCollection',
    'features': [{
        'type': 'Feature',
        'properties': {'name': '要素%d' % i, 'note': '中文内容用于验证多字节字符' * 3},
        'geometry': {'type': 'Point', 'coordinates': [116 + i * 1e-4, 39 + i * 1e-4]},
    } for i in range(4775)],
}, ensure_ascii=False).encode('utf-8')


class Target(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, body, ctype='application/json', extra=None):
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('X-Custom-Header', 'hello-from-target')
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for i in range(4):
                c = ('data: {"i": %d}\n\n' % i).encode()
                try:
                    self.wfile.write(b'%x\r\n%s\r\n' % (len(c), c))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                time.sleep(0.35)
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
            return
        if self.path == '/withcors':          # 模拟"接口自己配过 CORS"
            self._send(b'{"ok":true}', extra={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': '*',
                'Access-Control-Allow-Headers': '*',
                'Access-Control-Allow-Credentials': 'true',
                'Vary': 'Origin'})
            return
        if self.path == '/big':
            self.send_response(200)
            self.send_header('Content-Type', 'application/geo+json')
            self.send_header('Content-Length', str(len(BIG)))
            self.end_headers()
            self.wfile.write(BIG)
            return
        self._send(json.dumps({'ok': True, 'who': 'target-no-cors'}).encode())

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        got = self.rfile.read(n)
        self._send(json.dumps({'got': got.decode('utf-8'),
                               'ct': self.headers.get('Content-Type')}).encode())

    def do_HEAD(self):
        self._send(b'')


target = ThreadingHTTPServer(('127.0.0.1', TARGET_PORT), Target)
console = ThreadingHTTPServer(('127.0.0.1', CONSOLE_PORT), srv.Handler)
threading.Thread(target=target.serve_forever, daemon=True).start()
threading.Thread(target=console.serve_forever, daemon=True).start()
time.sleep(0.3)


def fetch(url, method='GET', headers=None, body=None):
    """返回原始 headers 对象（不是 dict）—— dict 会吃掉重复头，
    而"是否出现重复头"正是要抓的东西。"""
    req = urlrequest.Request(url, data=body, headers=headers or {}, method=method)
    try:
        r = urlrequest.urlopen(req, timeout=20)
        return r.status, r.headers, r.read()
    except urlerror.HTTPError as e:
        return e.code, e.headers, e.read()


def fwd(target_url, **kw):
    return fetch(CONSOLE + '/fetch?target=' + urlparse.quote(target_url, safe=''), **kw)


def save(name, data, **kw):
    url = CONSOLE + '/save' + (('?name=' + urlparse.quote(name, safe='')) if name else '')
    return fetch(url, method='POST', body=data, **kw)


try:
    print('\n[1] 页面与健康探测')
    st, h, b = fetch(CONSOLE + '/')
    check('GET / 返回 HTML', st == 200 and 'text/html' in (h.get('Content-Type') or ''),
          h.get('Content-Type'))
    check('返回的确实是这个工具', 'HTTP Console'.encode('utf-8') in b)
    check('带 no-store（改完刷新即生效）',
          'no-store' in (h.get('Cache-Control') or ''), h.get('Cache-Control'))
    st, h, b = fetch(CONSOLE + '/health')
    check('/health 标明身份', st == 200 and json.loads(b)['service'] == 'http-console', b[:60])
    st, h, b = fetch(CONSOLE + '/../server.py')
    check('不做目录遍历（只提供 index.html）', st == 404, st)
    st, h, b = fetch(CONSOLE + '/nope')
    check('未知路径 404 并说明能提供什么',
          st == 404 and '只提供'.encode('utf-8') in b, b[:50])

    print('\n[2] 转发：目标没有 CORS 头也能取到')
    st, h, b = fetch(BASE + '/', headers={'Origin': 'null'})
    check('（基准）直连目标：没有任何 access-control-* 头',
          not any(k.lower().startswith('access-control') for k in h))
    st, h, b = fwd(BASE + '/')
    check('经 /fetch 取回 200', st == 200, st)
    check('body 逐字节相同', b == b'{"ok": true, "who": "target-no-cors"}', b)
    check('目标自定义响应头一字未改地到了',
          h.get('X-Custom-Header') == 'hello-from-target', h.get('X-Custom-Header'))
    check('本服务不发任何 CORS 头（同源，不需要）',
          not any(k.lower().startswith('access-control') for k in h))

    print('\n[3] 同源模式的关键好处：上游响应头一字不改')
    st, h, b = fwd(BASE + '/withcors')
    check('上游自己的 Allow-Origin 原样透传', h.get_all('Access-Control-Allow-Origin') == ['*'],
          h.get_all('Access-Control-Allow-Origin'))
    check('上游的 Allow-Credentials 也原样来了',
          h.get('Access-Control-Allow-Credentials') == 'true')
    check('Vary 不会被弄重复', (h.get_all('Vary') or []) == ['Origin'], h.get_all('Vary'))

    print('\n[4] POST / body / 自定义请求头')
    payload = b'{"message": "from 39.9,116.4 to 39.95,116.5"}'
    st, h, b = fwd(BASE + '/', method='POST',
                   headers={'Content-Type': 'application/json',
                            'Authorization': 'Bearer T-123'}, body=payload)
    echoed = json.loads(b)
    check('POST body 逐字节送达', echoed['got'] == payload.decode())
    check('Content-Type 送达', echoed['ct'] == 'application/json')

    print('\n[5] 流式：必须边到边推，不能攒成一坨')
    r = urlrequest.urlopen(CONSOLE + '/fetch?target=' +
                           urlparse.quote(BASE + '/stream', safe=''), timeout=20)
    t0 = time.time()
    arrivals = []
    while True:
        c = r.read(1)
        if not c:
            break
        arrivals.append(time.time() - t0)
    span = arrivals[-1] - arrivals[0] if arrivals else 0
    check('流式内容收全', len(arrivals) > 0 and r.status == 200, r.status)
    check('确实是流式（跨度 ≈1s，而非 0）', span > 0.6, 'span=%.2fs' % span)
    check('首字节很早就到（没有"先攒 1 秒"）', arrivals and arrivals[0] < 0.3,
          'first=%.2fs' % (arrivals[0] if arrivals else -1))

    print('\n[6] 大载荷逐字节')
    st, h, b = fwd(BASE + '/big')
    check('1.5MB 级 GeoJSON 长度一致', len(b) == len(BIG), '%d vs %d' % (len(b), len(BIG)))
    check('SHA256 一致', hashlib.sha256(b).hexdigest() == hashlib.sha256(BIG).hexdigest())
    check('Content-Type 透传', h.get('Content-Type') == 'application/geo+json')

    print('\n[7] 安全边界')
    st, h, b = fwd(BASE + '/', headers={'Origin': 'https://evil.example.com'})
    check('陌生 Origin 被拒 403', st == 403, st)
    check('拒绝时说明原因', '拒绝'.encode('utf-8') in b, b[:50])
    st, h, b = fetch(CONSOLE + '/fetch?target=' + urlparse.quote(BASE + '/', safe=''))
    check('无 Origin 放行（同源 GET 浏览器就不发 Origin）', st == 200, st)
    st, h, b = fetch(CONSOLE + '/fetch?target=' + urlparse.quote(BASE + '/', safe=''),
                     method='OPTIONS')
    check('不答 OPTIONS 预检（陌生网站因此发不出复杂请求）', st != 200, st)

    print('\n[8] 出错路径')
    st, h, b = fwd('http://127.0.0.1:9/nope')
    check('连不上目标 → 502', st == 502, st)
    check('正文写明是本服务的判断，不是目标返回的',
          b.startswith('[本地服务]'.encode('utf-8')), b[:60])
    check('带 X-Console-Error 标记', h.get('X-Console-Error') == '1')
    st, h, b = fetch(CONSOLE + '/fetch')
    check('缺 target → 400', st == 400, st)
    st, h, b = fetch(CONSOLE + '/fetch?target=ftp%3A%2F%2Fx%2F')
    check('非 http/https 的 target → 400', st == 400, st)

    print('\n[9] 页面中途挂断：不能崩，也不能刷堆栈')
    captured = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, captured
    try:
        s = socket.create_connection(('127.0.0.1', CONSOLE_PORT), timeout=5)
        s.sendall(b'GET /fetch?target=' +
                  urlparse.quote(BASE + '/stream', safe='').encode() + b' HTTP/1.1\r\n'
                  b'Host: x\r\nOrigin: null\r\n\r\n')
        s.recv(160)
        s.close()
        time.sleep(0.8)
    finally:
        sys.stderr = real_stderr
    st, h, b = fwd(BASE + '/')
    check('挂断后仍能正常服务后续请求', st == 200, st)
    check('没有刷出 traceback（ConnectionAbortedError 已被吃掉）',
          'Traceback' not in captured.getvalue(), captured.getvalue()[:200])

    print('\n[10] 落盘 /save')
    shutil.rmtree(srv.SAVE_DIR, ignore_errors=True)

    raw = bytes([0x7B, 0x22, 0xFF, 0xFE, 0x22, 0x7D])       # 含非法 UTF-8 字节
    st, h, b = save('roads.geojson', raw)
    info = json.loads(b)
    check('/save 返回 200', st == 200, (st, b[:80]))
    check('返回实际落盘的绝对路径', os.path.isabs(info['path']), info.get('path'))
    check('确实写在 saved/ 目录里',
          os.path.dirname(os.path.abspath(info['path'])) == os.path.abspath(srv.SAVE_DIR),
          info['path'])
    with open(info['path'], 'rb') as f:
        on_disk = f.read()
    check('磁盘内容与发过去的逐字节相同', on_disk == raw, list(on_disk))
    check('非法 UTF-8 字节原样保留（没经过编码）',
          on_disk[2] == 0xFF and on_disk[3] == 0xFE, list(on_disk))

    print('\n[11] 不覆盖同名文件')
    st, h, b = save('roads.geojson', b'SECOND')
    check('重名另存为带序号的名字', json.loads(b)['name'] == 'roads-2.geojson',
          json.loads(b)['name'])
    with open(info['path'], 'rb') as f:
        check('第一次那个文件没被动', f.read() == raw)

    print('\n[12] 路径穿越：只当文件名处理，越不出 saved/')
    for evil in ['../../evil.txt', '..\\..\\evil.txt', 'C:/Windows/Temp/evil.txt',
                 '/etc/passwd', '....//....//evil.txt', '.']:
        st, h, b = save(evil, b'X')
        inside = True
        if st == 200:
            p = json.loads(b)['path']
            inside = os.path.dirname(os.path.abspath(p)) == os.path.abspath(srv.SAVE_DIR)
        check('恶意名 %-26s → 仍落在 saved/ 内' % evil, st == 200 and inside,
              b[:70] if st != 200 else '')

    print('\n[13] /save 的安全与错误路径')
    st, h, b = save('x.bin', b'X', headers={'Origin': 'https://evil.example.com'})
    check('陌生 Origin 写入被拒 403', st == 403, st)
    st, h, b = fetch(CONSOLE + '/save?name=x.bin')
    check('GET /save → 405（只接受 POST）', st == 405, st)
    st, h, b = save(None, b'X')
    check('没给 name → 回退成 response 开头的名字',
          st == 200 and json.loads(b)['name'].startswith('response'), b[:90])

    shutil.rmtree(srv.SAVE_DIR, ignore_errors=True)

finally:
    console.shutdown()
    target.shutdown()
    console.server_close()
    target.server_close()

print('\n' + '=' * 42)
print('%d passed, %d failed' % (passed, failed))
sys.exit(1 if failed else 0)

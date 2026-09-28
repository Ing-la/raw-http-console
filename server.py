#!/usr/bin/env python3
"""http-console 的本地服务 —— 既发页面，又替页面发请求。

为什么需要它
------------
浏览器有同源策略：页面 JS 读不到「没回 Access-Control-Allow-Origin」的跨源响应。
内网接口大多没配这个头，于是浏览器直接拒收——而 curl / Thunder 这类非浏览器程序不受限。

关键不是"有个服务在跑"，而是**页面把请求发给了谁**：

    双击 index.html          → 页面源是 file://(null)，直接打目标 → 跨源 → 靠对方有 CORS
    由本服务托管             → 页面源就是本服务，请求打 /fetch      → 同源 → 根本不涉及 CORS

所以本服务干两件事：

    GET  /           把 index.html 发出去（让页面和自己同源）
    ANY  /fetch?target=<url>   转发到目标，流式回传

转发仍然是核心：浏览器自己够不着那个内网接口，得有个浏览器之外的东西替它跑这一趟。
只是因为变成了同源，**响应头可以一字不改地透传**（上游自己的 CORS 头也照原样显示）。

用法
----
    python server.py            # 监听 127.0.0.1:8099
    python server.py 9000       # 换端口

然后浏览器打开它打印出来的那个地址。**双击 index.html 依然可用**——那种情况下
页面直连目标，适合打本来就有 CORS 的接口。

安全边界
--------
- 只监听 127.0.0.1，网卡不外露；只提供 index.html，不做目录遍历
- 只接受本机来源的请求（Origin 为空 / null / 127.0.0.1 / localhost），
  否则任意网站都能盲发请求借你这个服务打内网
- 同源模式下不发任何 CORS 头：陌生网站既读不到响应，非简单请求连预检都过不去

已知限制
--------
Cookie 无法经此转发：`Cookie` 属于浏览器禁止 JS 设置的头，页面根本带不上。
需要 cookie 鉴权的接口，本服务帮不了。
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

DEFAULT_PORT = 8099
UPSTREAM_TIMEOUT = 600          # 秒；够长以支持 SSE 长连接
CHUNK = 8192
ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, 'index.html')
SAVE_DIR = os.path.join(ROOT, 'saved')      # 页面导出的响应落到这里


def safe_name(raw):
    """只取文件名本体，剥掉任何路径成分与危险字符。

    页面传的是 roads.geojson 这样的名字；万一传了 ..\\..\\x 之类，
    这里会把它压成一个纯文件名，再配合下面的目录归属校验兜底。
    """
    name = str(raw or '').replace('\\', '/')
    name = name.split('/')[-1]              # 丢掉任何目录成分
    name = name.replace('\x00', '').strip()
    name = name.lstrip('.')                 # 不产生隐藏文件，也挡掉 "." / ".."
    name = re.sub(r'[^\w .\-()\[\]]+', '_', name)   # \w 含中文
    name = name.strip('. ')
    return name[:120] or 'response.bin'


def unique_path(folder, name):
    """不覆盖同名文件：重名自动加序号。"""
    base, ext = os.path.splitext(name)
    cand = os.path.join(folder, name)
    i = 2
    while os.path.exists(cand):
        cand = os.path.join(folder, '%s-%d%s' % (base, i, ext))
        i += 1
    return cand

# 逐跳头：不该在转发中传递（RFC 7230 §6.1）。Content-Length 也丢掉——
# 我们按"读到 EOF 就结束"重新分帧，长度由我们自己决定。
HOP_BY_HOP = {
    'host', 'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length',
    'accept-encoding',          # 主动要求 identity，避免拿回压缩体
}

# 上游响应里只丢这些：分帧相关的头由我们重新决定。
# 其余（包括上游自己的 Access-Control-*、Vary）**全部原样透传** ——
# 同源模式下浏览器不检查 CORS，所以没必要动刀，能更忠实地还原服务端原样。
SKIP_RESPONSE = {'transfer-encoding', 'connection', 'keep-alive', 'content-length'}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：不必声明 Content-Length，响应以连接关闭为结束标志
    # —— 这正是流式回传（SSE）所需要的
    protocol_version = 'HTTP/1.0'
    server_version = 'http-console'
    sys_version = ''

    # ---- 工具 ----

    def log_message(self, fmt, *args):
        pass                     # 自己打，格式更可控

    def _note(self, *parts):
        print('  ' + '  '.join(str(p) for p in parts), flush=True)

    def _origin_ok(self):
        """只放行本机页面。
        注意同源 GET 浏览器**不发** Origin，所以"缺失"必须放行。"""
        o = self.headers.get('Origin')
        if o is None or o == '' or o == 'null':
            return True
        o = o.lower()
        return (o.startswith('http://127.0.0.1') or o.startswith('http://localhost')
                or o.startswith('http://[::1]'))

    def _plain(self, code, message, marker=None):
        body = (message + '\n').encode('utf-8')
        self.send_response_only(code, 'Error')
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        if marker:
            self.send_header('X-Console-Error', '1')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _send_file(self):
        if not os.path.exists(INDEX):
            self._plain(500, '找不到 index.html（它应和 server.py 放在同一目录）')
            return
        with open(INDEX, 'rb') as f:
            body = f.read()
        self.send_response_only(200, 'OK')
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')   # 改了页面刷新就生效
        self.end_headers()
        self.wfile.write(body)
        self._note('GET /', '(index.html %d B)' % len(body))

    def _send_health(self):
        body = json.dumps({'service': 'http-console', 'canFetch': True}).encode('utf-8')
        self.send_response_only(200, 'OK')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    # ---- 落盘 ----

    def _save(self):
        """把页面 POST 上来的原始字节落到 SAVE_DIR。

        只接受**文件名**，不接受路径；写之前再确认最终路径确实在 SAVE_DIR 之内。
        这样路径穿越无从谈起，也不需要用户配置任何目录。
        """
        if not self._origin_ok():
            self._plain(403, '[本地服务] 拒绝非本机页面的写入请求（Origin: %s）'
                        % self.headers.get('Origin'), marker=1)
            return

        query = urlparse.urlparse(self.path).query
        raw_name = (urlparse.parse_qs(query).get('name') or [''])[0]
        length = int(self.headers.get('Content-Length') or 0)
        data = self.rfile.read(length) if length else b''

        try:
            os.makedirs(SAVE_DIR, exist_ok=True)
            path = unique_path(SAVE_DIR, safe_name(raw_name))
            # 双保险：即便 safe_name 哪天有洞，也不会写到这里之外
            if os.path.dirname(os.path.abspath(path)) != os.path.abspath(SAVE_DIR):
                self._plain(400, '[本地服务] 文件名不合法', marker=1)
                return
            with open(path, 'wb') as f:
                f.write(data)
        except OSError as e:
            self._plain(500, '[本地服务] 写文件失败：%s' % e, marker=1)
            return

        self._note('SAVE', os.path.basename(path), '(%d B)' % len(data))
        body = json.dumps({'path': path, 'name': os.path.basename(path),
                           'bytes': len(data)}, ensure_ascii=False).encode('utf-8')
        self.send_response_only(200, 'OK')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    # ---- 转发 ----

    def _forward(self):
        if not self._origin_ok():
            self._plain(403, '[本地服务] 拒绝非本机页面的请求（Origin: %s）'
                        % self.headers.get('Origin'), marker=1)
            return

        query = urlparse.urlparse(self.path).query
        target = (urlparse.parse_qs(query).get('target') or [''])[0]
        if not target:
            self._plain(400, '[本地服务] 缺少 target 参数，不知道要转发到哪儿', marker=1)
            return
        if not target.lower().startswith(('http://', 'https://')):
            self._plain(400, '[本地服务] target 必须是完整的 http/https 地址', marker=1)
            return

        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None

        fwd = {}
        for key, value in self.headers.items():
            if key.lower() not in HOP_BY_HOP:
                fwd[key] = value
        fwd['Accept-Encoding'] = 'identity'

        self._note(self.command, target, '(%d B)' % len(body) if body else '')
        req = urlrequest.Request(target, data=body, headers=fwd, method=self.command)
        try:
            up = urlrequest.urlopen(req, timeout=UPSTREAM_TIMEOUT)
        except urlerror.HTTPError as e:
            up = e                                  # 4xx/5xx 也是正常响应，照原样回传
        except Exception as e:
            self._plain(502, '[本地服务] 连不上目标 %s —— %s: %s'
                        % (target, type(e).__name__, e), marker=1)
            return

        try:
            self.send_response_only(up.status, up.reason)
            for key, value in up.headers.items():
                if key.lower() not in SKIP_RESPONSE:
                    self.send_header(key, value)
            self.end_headers()

            if self.command == 'HEAD':
                return
            # 必须用 read1 而不是 read(n)：对 chunked 响应，read(n) 会一直
            # 累积到 n 字节或 EOF 才返回 —— 那就把流式攒成一坨了
            # （SSE 场景下表现为"卡满全程、最后一次性吐出"）。
            # read1 最多做一次底层读，有数据就立刻交出来。
            while True:
                chunk = up.read1(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()                  # 关键：立刻推给浏览器，不攒
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 页面那边取消了，或者浏览器因故挂断了——都正常，不必打堆栈。
            # Windows 上这种中止抛的是 ConnectionAbortedError（WinError 10053），
            # 光捕 BrokenPipeError 会漏掉、于是刷出一大段吓人的 traceback。
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    # ---- 路由 ----

    def _route(self):
        path = urlparse.urlparse(self.path).path
        if path == '/fetch':
            self._forward()
        elif path == '/save':
            if self.command != 'POST':
                self._plain(405, '[本地服务] /save 只接受 POST')
            else:
                self._save()
        elif path == '/health':
            self._send_health()
        elif path == '/':
            self._send_file()
        elif path == '/favicon.ico':
            self._plain(404, 'not found')
        else:
            self._plain(404, '本地服务只提供 / （页面）、/health、/fetch?target=、POST /save?name=')

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    do_POST = do_PUT = do_PATCH = do_DELETE = _route


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print('端口必须是数字，例如：python server.py 8099')
            return 1

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print('http-console 本地服务已启动')
    print('  打开页面：  http://127.0.0.1:%d' % port)
    print('  接口转发：  /fetch?target=<目标地址>')
    print('  页面导出：  落到 %s' % SAVE_DIR)
    print('只监听本机，且只接受本机页面的请求。按 Ctrl+C 停止。')
    if not os.path.exists(INDEX):
        print('  ⚠ 同目录下没找到 index.html，页面会打不开')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

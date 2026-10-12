// http-console 前端行为测试
//
// 跑法：npm install && npm test
//
// 覆盖的核心承诺：
//   原始字节逐块追加、传输状态区分「服务端关闭」与「本地取消」、
//   导出字节精确、整理视图不动原始字节、同源/直连两种模式、诊断信息可定位

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const HTML = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS  ' + name); }
  else { fail++; console.log('  FAIL  ' + name + (extra ? '  → ' + extra : '')); }
}

// ── 假 fetch：可控的 chunk 序列，支持挂起 / 中途挂断 / /health / /save ──
function makeResponse(chunks, meta) {
  meta = meta || {};
  const headers = new Headers(meta.headers || { 'content-type': 'text/event-stream' });
  return {
    status: meta.status || 200,
    statusText: meta.statusText || 'OK',
    headers,
    body: {
      getReader() {
        let i = 0;
        return {
          read() {
            return new Promise((resolve, reject) => {
              if (meta.signal && meta.signal.aborted) {
                return reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
              }
              if (meta.hangAfter !== undefined && i >= meta.hangAfter) return;  // 永不 resolve
              if (i < chunks.length) {
                const next = chunks[i++];
                const v = (next instanceof Uint8Array) ? next : new TextEncoder().encode(next);
                if (meta.signal) {
                  const onAbort = () => {
                    meta.signal.removeEventListener('abort', onAbort);
                    reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
                  };
                  meta.signal.addEventListener('abort', onAbort);
                }
                return resolve({ done: false, value: v });
              }
              return resolve({ done: true, value: undefined });
            });
          },
          cancel() { return Promise.resolve(); },
        };
      },
    },
  };
}

function boot(opts) {
  opts = opts || {};
  const dom = new JSDOM(HTML, {
    url: 'http://localhost/', runScripts: 'dangerously',
    virtualConsole: new VirtualConsole(),
    beforeParse(window) {
      window.Headers = Headers;
      window.TextDecoder = TextDecoder;
      window.TextEncoder = TextEncoder;
      window.AbortController = AbortController;
      window.Blob = Blob;
      window.fetch = async (url, o) => {
        const u = String(url);
        // 启动探测：opts.served=true 假装被本机服务托管，否则退回直连
        if (u.indexOf('/health') >= 0) {
          return new Response('{"service":"http-console","canFetch":true}',
            { status: opts.served ? 200 : 404 });
        }
        if (u.indexOf('/save') >= 0) {
          window.__lastSave = { url: u, method: o.method, body: o.body };
          if (opts.saveFails) return new Response('[本地服务] 写文件失败：磁盘满了', { status: 500 });
          return new Response(JSON.stringify({
            path: 'E:\\tools\\http-console\\saved\\' +
                  decodeURIComponent(u.split('name=')[1] || 'x'),
            bytes: 7,
          }), { status: 200, headers: { 'content-type': 'application/json' } });
        }
        window.__lastReq = { url: u, method: o.method, headers: o.headers, body: o.body };
        if (opts.pendingForever) {
          return new Promise((resolve, reject) => {
            o.signal.addEventListener('abort', () =>
              reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
          });
        }
        if (opts.fetchThrows) throw new TypeError('Failed to fetch');
        return makeResponse(opts.chunks || [], {
          headers: opts.headers, signal: o.signal, status: opts.status,
          statusText: opts.statusText, hangAfter: opts.hangAfter,
        });
      };
    },
  });
  return dom;
}

const tick = (ms) => new Promise(r => setTimeout(r, ms || 30));

(async () => {
  // ═══ 1. 原始字节与传输状态 ═══
  console.log('\n[1] 原始字节逐块追加 / 传输状态');
  {
    const chunks = ['data: {"type": "think", "text": "你好"}\n\n', 'data: {"type": "done"}\n\n'];
    const dom = boot({ chunks });
    const w = dom.window, d = w.document;
    await tick(50);
    check('空闲时耗时显示 —（不是假的已运行时长）',
      d.getElementById('mElapsed').textContent.includes('—'));
    check('空闲时字节显示 —', d.getElementById('mBytes').textContent === '—');

    d.getElementById('url').value = 'http://10.0.19.60:3001/api/chat/stream';
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(120);

    check('响应体逐字节原样拼接',
      d.getElementById('rawBody').textContent === chunks.join(''),
      JSON.stringify(d.getElementById('rawBody').textContent.slice(0, 40)));
    check('不解析（JSON 原样保留）', d.getElementById('rawBody').textContent.includes('"type"'));
    check('状态 = 服务端已关闭', d.getElementById('state').textContent === '服务端已关闭',
      d.getElementById('state').textContent);
    check('日志记录了「服务端关闭了流」',
      d.getElementById('logEl').textContent.includes('服务端关闭了流'));
    check('字节数正确', d.getElementById('mBytes').textContent.includes(
      String(chunks.reduce((a, c) => a + new TextEncoder().encode(c).length, 0))));
    check('网络块数在日志里', d.getElementById('logEl').textContent.includes('2 网络块'));
    dom.window.close();
  }

  // ═══ 2. 本地取消 ≠ 服务端关闭 ═══
  console.log('\n[2] 本地取消要和服务端关闭分开说');
  {
    const dom = boot({ pendingForever: true });
    const w = dom.window, d = w.document;
    d.getElementById('url').value = 'http://x/y';
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    d.getElementById('cancelBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(80);
    check('状态 = 已取消', d.getElementById('state').textContent === '已取消',
      d.getElementById('state').textContent);
    check('日志明确区分「不是服务端关闭」',
      d.getElementById('logEl').textContent.includes('不是服务端关闭'));
    check('取消后按钮复位', !d.getElementById('sendBtn').disabled);
    dom.window.close();
  }

  // ═══ 3. 请求构造 ═══
  console.log('\n[3] 请求构造：Content-Type 固定行 / 禁止头告警');
  {
    const dom = boot({ chunks: ['{}'], headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(50);
    check('Content-Type 是固定行（不在可删的行列表里）',
      !!d.querySelector('.hdrfixed') && d.querySelector('.hdrfixed button') === null);
    check('默认值是 application/json', d.getElementById('ctValue').value === 'application/json');
    check('用户头列表默认为空', d.querySelectorAll('#hdrList .hdrrow').length === 0);

    d.getElementById('url').value = 'http://x/y';
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(50);
    check('默认发出去的是 application/json',
      w.__lastReq.headers.get('content-type') === 'application/json');

    // 加一个浏览器禁止设置的头
    d.getElementById('addHdr').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    const row = d.querySelector('#hdrList .hdrrow');
    const k = row.querySelector('.hk');
    k.value = 'Connection';
    k.dispatchEvent(new w.Event('input', { bubbles: true }));
    check('禁止设置的头被标红', row.classList.contains('bad'));
    check('并给出告警', !d.getElementById('hdrWarn').hidden &&
      d.getElementById('hdrWarn').textContent.includes('Connection'));

    // 清空固定行 = 明确不发送该头
    d.querySelectorAll('#hdrList .hdrrow').forEach(r => r.remove());
    d.getElementById('ctValue').value = '';
    d.getElementById('ctValue').dispatchEvent(new w.Event('input', { bubbles: true }));
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(50);
    check('清空后不发送该头',
      w.__lastReq.headers.get('content-type') === null,
      String(w.__lastReq.headers.get('content-type')));
    check('并在日志里提示浏览器会补 text/plain',
      d.getElementById('logEl').textContent.includes('text/plain;charset=UTF-8'));
    dom.window.close();
  }

  // ═══ 4. URL 校验：把「地址打错」和「网络问题」分开 ═══
  console.log('\n[4] URL 校验');
  {
    const dom = boot({ chunks: ['x'] });
    const w = dom.window, d = w.document;
    let fetched = 0;
    const of = w.fetch;
    w.fetch = async (...a) => { fetched++; return of(...a); };
    await tick(50);

    d.getElementById('url').value = 'http://10.0.19.60:3001 /api/health';   // 端口后多一个空格
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(40);
    let log = d.getElementById('logEl').textContent;
    check('中间有空格 → 本地就拦下，不发请求', fetched === 0, 'fetched=' + fetched);
    check('指出是空白字符并带插入符定位', log.includes('空格') && log.includes('↑'), log);
    check('说明跟网络连通性无关', log.includes('无关'));
    check('不甩误导性的失败原因清单', !log.includes('目标服务未开启 CORS'));
    check('输入框标红', d.getElementById('url').classList.contains('invalid'));

    d.getElementById('url').value = '10.0.19.60:3001/api/health';           // 缺 http://
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(40);
    log = d.getElementById('logEl').textContent;
    check('缺 http:// 被拦并给出正确写法', log.includes('http://'), log.slice(0, 120));

    d.getElementById('url').value = '  http://x/y  ';                       // 首尾空白
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(50);
    check('首尾空白被忽略且有说明',
      d.getElementById('logEl').textContent.includes('首尾'));
    check('首尾空白不算错误（请求照发）', fetched > 0);
    dom.window.close();
  }

  // ═══ 5. 请求体 JSON 语法：只报告，不阻断 ═══
  console.log('\n[5] 请求体 JSON 语法检查');
  {
    const mk = (bodyText) => {
      const dom = boot({ chunks: ['{}'], headers: { 'content-type': 'application/json' } });
      const w = dom.window, d = w.document;
      d.getElementById('url').value = 'http://x/y';
      d.getElementById('body').value = bodyText;
      d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
      return { dom, w, d };
    };
    {
      const { dom, d } = mk('{\n  "a": "从 39.9 到 39.95",\n}');            // 尾随逗号
      await tick(60);
      const log = d.getElementById('logEl').textContent;
      check('尾随逗号被本地识别', log.includes('不是合法 JSON'), log.slice(0, 100));
      check('给出字符位置与插符', /第 \d+ 个字符/.test(log) && log.includes('⟪'), log);
      check('仍原样发出（响应区有服务端回答）', d.getElementById('rawBody').textContent === '{}');
      dom.window.close();
    }
    {
      const { dom, d } = mk('{\n  // 注释\n  "a": 1\n}');
      await tick(60);
      check('注释被识别为「不支持注释」而不是「少了逗号」',
        d.getElementById('logEl').textContent.includes('不支持注释'));
      dom.window.close();
    }
    {
      const { dom, d } = mk('{"a": 1, "b": [1, 2]}');
      await tick(60);
      check('合法 JSON 不误报',
        !d.getElementById('logEl').textContent.includes('不是合法 JSON'));
      dom.window.close();
    }
    {
      const dom = boot({ chunks: ['ok'] });
      const w = dom.window, d = w.document;
      d.getElementById('url').value = 'http://x/y';
      d.getElementById('body').value = 'not json at all {{{';
      d.getElementById('ctValue').value = 'text/plain';
      d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
      await tick(60);
      check('非 JSON 的 Content-Type 不做 JSON 检查',
        !d.getElementById('logEl').textContent.includes('不是合法 JSON'));
      dom.window.close();
    }
  }

  // ═══ 6. 两种运行模式 ═══
  console.log('\n[6] 运行模式：同源托管 vs 直连');
  const TARGET = 'http://10.0.19.60:3001/api/geo/roads';
  {
    const dom = boot({ served: true, chunks: ['{"ok":1}'],
                       headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(60);
    check('探测到本机服务 → 徽标显示同源托管',
      d.getElementById('modeChip').textContent === '同源托管',
      d.getElementById('modeChip').textContent);
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    check('请求打给同源的 /fetch', w.__lastReq.url.indexOf('/fetch?target=') === 0,
      w.__lastReq.url);
    check('目标被正确编码进 target 参数',
      w.__lastReq.url.indexOf('target=' + encodeURIComponent(TARGET)) > 0);
    check('日志显示的仍是真实目标',
      d.getElementById('logEl').textContent.includes('→ POST ' + TARGET));
    check('日志说明这一趟是同源托管转发',
      d.getElementById('logEl').textContent.includes('同源托管'));
    check('响应头面板说明浏览器未做裁剪',
      d.getElementById('respHeaders').textContent.includes('一字未改'));
    dom.window.close();
  }
  {
    const dom = boot({ served: false, chunks: ['{"ok":1}'],
                       headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(60);
    check('探测不到 → 徽标显示直连', d.getElementById('modeChip').textContent === '直连');
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    check('直连模式下直接打目标', w.__lastReq.url === TARGET, w.__lastReq.url);
    check('日志里不出现同源托管',
      !d.getElementById('logEl').textContent.includes('同源托管'));
    check('响应头面板提醒跨源只暴露少数几个头',
      d.getElementById('respHeaders').textContent.includes('跨源'));
    dom.window.close();
  }
  {
    const dom = boot({ served: false, fetchThrows: true });
    const w = dom.window, d = w.document;
    await tick(60);
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    const log = d.getElementById('logEl').textContent;
    check('直连失败 → 提示 CORS 是常见原因', log.includes('CORS'), log.slice(0, 160));
    check('并指出可用 server.py 绕开', log.includes('python server.py'));
    dom.window.close();
  }
  {
    const dom = boot({ served: true, fetchThrows: true });
    const w = dom.window, d = w.document;
    await tick(60);
    w.fetch = async () => { throw new TypeError('Failed to fetch'); };
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    const log = d.getElementById('logEl').textContent;
    check('同源失败 → 提示确认本机服务还活着', log.includes('本机服务还活着'));
    check('给出启动命令', log.includes('python server.py'));
    dom.window.close();
  }

  // ═══ 7. 导出：两种模式、字节精确 ═══
  console.log('\n[7] 导出');
  {
    // 同源：交给本机服务落盘
    const dom = boot({ served: true, chunks: ['{"ok":1}'],
                       headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(60);
    check('同源模式按钮文案说明会落盘',
      d.getElementById('exportRespBtn').textContent === '保存到 saved/',
      d.getElementById('exportRespBtn').textContent);
    let downloads = 0;
    w.URL.createObjectURL = () => { downloads++; return 'blob:fake'; };
    w.URL.revokeObjectURL = () => {};
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    d.getElementById('exportRespBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(50);
    check('同源模式不发下载，改为 POST 给 /save',
      downloads === 0 && w.__lastSave && w.__lastSave.method === 'POST');
    check('文件名取自真实目标', w.__lastSave.url.indexOf('name=roads.json') > 0,
      w.__lastSave.url);
    check('交给服务的是原始字节',
      (await w.__lastSave.body.text()) === '{"ok":1}');
    check('日志给出落盘路径', d.getElementById('logEl').textContent.includes('已保存到'));
    dom.window.close();
  }
  {
    const dom = boot({ served: true, saveFails: true, chunks: ['{"a":1}'],
                       headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(60);
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    d.getElementById('exportRespBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(50);
    const log = d.getElementById('logEl').textContent;
    check('落盘失败会报出来，并带上服务端给的原因',
      log.includes('保存失败') && log.includes('磁盘满了'), log.slice(-80));
    dom.window.close();
  }
  {
    // 直连：只能走浏览器下载
    const dom = boot({ served: false, chunks: ['{"a":1}'],
                       headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(60);
    check('直连模式按钮文案为下载',
      d.getElementById('exportRespBtn').textContent === '下载',
      d.getElementById('exportRespBtn').textContent);
    let downloads = 0;
    w.URL.createObjectURL = () => { downloads++; return 'blob:fake'; };
    w.URL.revokeObjectURL = () => {};
    d.getElementById('url').value = TARGET;
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(60);
    d.getElementById('exportRespBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(40);
    check('直连模式走浏览器下载', downloads === 1, 'downloads=' + downloads);
    check('直连模式不会去碰 /save', w.__lastSave === undefined);
    dom.window.close();
  }

  // ═══ 8. 整理视图：只是看的一层，原始字节不动 ═══
  console.log('\n[8] 整理视图');
  {
    const SSE = [
      'data: {"type": "think", "text": "短帧"}\n\n',
      'data: {"type": "done", "result": {"a": [1, 2, 3], "b": {"c": 1}, "d": 4}}\n\n',
    ];
    const dom = boot({ served: false, chunks: SSE,
                       headers: { 'content-type': 'text/event-stream' } });
    const w = dom.window, d = w.document;
    await tick(50);
    check('无用内容时整理按钮是熄灭的', d.getElementById('fmtBtn').disabled === true);

    d.getElementById('url').value = 'http://x/sse';
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(80);
    check('收完后整理按钮亮起', d.getElementById('fmtBtn').disabled === false);
    check('默认是原始视图',
      d.getElementById('rawFmt').hidden === true &&
      d.getElementById('rawWrap').hidden === false);

    const rawBefore = d.getElementById('rawBody').textContent;
    d.getElementById('fmtBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(40);
    check('切到整理视图',
      d.getElementById('rawFmt').hidden === false &&
      d.getElementById('rawWrap').hidden === true);
    check('顶部有「非原始字节」警示条',
      d.getElementById('fmtBanner').textContent.includes('非原始'),
      d.getElementById('fmtBanner').textContent.slice(0, 30));
    check('警示条只在整理视图内',
      d.getElementById('fmtBanner').closest('#rawFmt') !== null);
    check('复制按钮改名，避免误以为复制的是整理后内容',
      d.getElementById('copyBtn').textContent === '复制原始');
    const frames = d.querySelectorAll('#rawFmt .fframe');
    check('每个 data 帧成为一个条目', frames.length === 2, 'frames=' + frames.length);
    check('短帧默认折叠', frames[0].querySelector('.fbody').hidden === true);
    check('长帧默认展开（缩进过）',
      frames[1].querySelector('.fbody').hidden === false &&
      frames[1].querySelector('.fbody').textContent.includes('\n  "type": "done"'),
      JSON.stringify(frames[1].querySelector('.fbody').textContent.slice(0, 50)));
    click(w, frames[0].querySelector('.fhead'));
    await tick(20);
    check('点折叠行可展开', frames[0].querySelector('.fbody').hidden === false);
    check('整理全程原始视图文本未变',
      d.getElementById('rawBody').textContent === rawBefore);

    d.getElementById('fmtBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(30);
    check('可切回原始视图',
      d.getElementById('rawFmt').hidden === true &&
      d.getElementById('copyBtn').textContent === '复制');
    dom.window.close();
  }

  // ═══ 9. 常用请求：保存 / 载入 / 去重 / 导入导出 ═══
  console.log('\n[9] 常用请求');
  {
    const dom = boot({ chunks: ['x'], headers: { 'content-type': 'application/json' } });
    const w = dom.window, d = w.document;
    await tick(50);
    check('默认列表为空，不预置任何条目',
      d.getElementById('savedList').textContent.includes('还没有保存的请求'));

    d.getElementById('url').value = TARGET;
    d.getElementById('body').value = '{"message":"甲"}';
    d.getElementById('saveCurrent').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(30);
    check('保存后列表出现 1 条', d.querySelectorAll('#savedList .menu-row').length === 1);
    const stored = JSON.parse(w.localStorage.getItem('http-console.saved')).saved[0];
    check('存的是整份请求（含 body 与 Content-Type）',
      stored.body === '{"message":"甲"}' && stored.ct === 'application/json');
    check('条目显示 方法 + 主机/路径',
      d.querySelector('#savedList .menu-row').textContent.includes('10.0.19.60:3001/api/geo/roads'));

    d.getElementById('url').value = 'http://changed/other';
    d.getElementById('body').value = 'x';
    click(w, d.querySelector('#savedList .menu-load'));
    await tick(30);
    check('载入后地址与 body 一并还原',
      d.getElementById('url').value === TARGET &&
      d.getElementById('body').value === '{"message":"甲"}');

    // 同地址不同 body = 两条（去重键含 body）
    d.getElementById('url').value = TARGET;
    d.getElementById('body').value = '{"message":"乙"}';
    d.getElementById('saveCurrent').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(30);
    check('同地址不同请求体存成两条（可区分）',
      d.querySelectorAll('#savedList .menu-row').length === 2);

    // 完全相同的再存一次 → 只更新不新增
    d.getElementById('saveCurrent').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(30);
    check('完全相同的重复保存只更新不新增',
      d.querySelectorAll('#savedList .menu-row').length === 2);
    dom.window.close();
  }

  // ═══ 10. 清空与响应头开关 ═══
  console.log('\n[10] 清空 / 响应头开关');
  {
    const dom = boot({ chunks: ['data: {"a":1}\n\n'],
                       headers: { 'content-type': 'text/event-stream' } });
    const w = dom.window, d = w.document;
    await tick(50);
    d.getElementById('url').value = 'http://x/sse';
    d.getElementById('sendBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(70);
    check('响应头条数显示在开关上',
      d.getElementById('respHdrCount').textContent === '1',
      d.getElementById('respHdrCount').textContent);
    check('响应头默认收起', d.getElementById('respHeaders').hidden === true);
    d.getElementById('hdrToggle').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(20);
    check('点开关展开并高亮',
      d.getElementById('respHeaders').hidden === false &&
      d.getElementById('hdrToggle').classList.contains('chip-on'));

    d.getElementById('clearBtn').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
    await tick(30);
    check('清空后响应区归零',
      d.getElementById('rawBody').textContent === '' &&
      d.getElementById('mBytes').textContent === '—' &&
      d.getElementById('state').textContent === '空闲');
    check('清空后整理按钮回到熄灭', d.getElementById('fmtBtn').disabled === true);
    dom.window.close();
  }

  console.log('\n' + '='.repeat(42));
  console.log(pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('harness error:', e); process.exit(2); });

function click(w, el) {
  el.dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebScreenShare v2 —— 极简 Web 实时桌面共享 + 双向文本消息（被控端）

不装 VNC：被控机运行本程序（或打包好的单文件 exe），
控制机用浏览器打开 http://<被控机IP>:<端口> 即可实时看到被控机桌面，
并且双方可以互发文本消息（保持换行/缩进等原始格式）。

v2 新增:
    - 双向文本消息：浏览器(主控机) <-> 桌面小窗(被控机)
    - 被控机桌面弹出一个置顶小窗，可收发消息，可选自动复制收到的文本
    - 格式保持：消息按原文显示（换行、空格、缩进不丢失，等宽字体展示）
    - --no-ui  无界面模式（不弹消息小窗）

依赖:
    pip install mss pillow
    （可选 DXGI 后端: pip install dxcam numpy）

打包成免 Python 的单文件 exe:
    pip install pyinstaller
    pyinstaller --onefile screen-share.py
    dist/screen-share.exe 直接拷到被控机运行即可（绿色、不写注册表）

常用参数:
    --host 0.0.0.0     监听地址（默认 0.0.0.0）
    --port 8080        端口
    --fps 15           采集帧率
    --quality 70       JPEG 质量 1-95
    --scale 0.75       画面缩放（降低带宽）
    --monitor 0        显示器序号，0=全部屏幕，1=主屏
    --backend mss      采集后端：mss(GDI) / dxcam(DXGI)。抓到黑屏就换另一个
    --token 123456     访问口令，访问地址后加 ?token=123456
    --no-ui            不弹出被控端消息小窗

示例（两机用网线直连，拿到 169.254.x.x 之后）:
    被控机: screen-share.exe --port 8080 --fps 20 --token abc123
    控制机: 浏览器打开 http://169.254.x.x:8080/?token=abc123
"""
import argparse
import hashlib
import io
import json
import os
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

try:
    from mss import MSS as _MSS          # mss >= 9
except Exception:
    try:
        from mss import mss as _MSS      # 旧版本兜底
    except Exception:
        _MSS = None
try:
    from PIL import Image
except Exception:
    Image = None

if _MSS is None or Image is None:
    sys.exit("缺少依赖，请先执行:  pip install mss pillow")

BOUNDARY = "FRAMEBOUNDARY"
MAX_MSG_LEN = 200000         # 单条消息最大字符数（粘贴大段代码/日志够用）
RENDER_CHUNK = 4000          # 日志区每帧最多插入的字符数，避免大消息一次性卡住 UI
LONGPOLL_SECONDS = 25        # /poll 长轮询挂起时长
XFER_BLOCK = 1 << 20         # 上传/下载流式读写的块大小（1MB，内存恒定）
DEFAULT_MAX_FILE_MB = 8192   # 单文件上限，默认 8GB

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>桌面共享</title>
<style>
  :root{--bg:#f6f7f9;--card:#fff;--bd:#e3e6ea;--tx:#1f2328;--sub:#6b7280;--ok:#1a7f37;--bad:#c0392b}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--tx);
       font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
         padding:10px 16px;background:var(--card);border-bottom:1px solid var(--bd)}
  h1{font-size:15px;font-weight:600;margin:0}
  .meta{color:var(--sub);font-size:13px}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block;margin-right:6px}
  .dot.bad{background:var(--bad)}
  .spacer{flex:1}
  button{padding:5px 12px;border:1px solid var(--bd);background:#fff;border-radius:6px;
         cursor:pointer;font-size:13px;color:var(--tx)}
  button:hover{background:#f0f2f5}
  main{padding:12px;display:flex;justify-content:center}
  #stage{background:#000;border-radius:8px;overflow:hidden;line-height:0;
         box-shadow:0 1px 3px rgba(0,0,0,.08)}
  img{display:block;max-width:100%;cursor:zoom-in}
  img.fit{width:auto;max-height:calc(100vh - 130px)}
  img.orig{max-width:none;max-height:none}
  footer{padding:0 16px 16px;color:var(--sub);font-size:12px}
  /* ---- 消息面板 ---- */
  #chat{position:fixed;right:12px;bottom:12px;width:340px;max-height:70vh;
        display:flex;flex-direction:column;background:var(--card);
        border:1px solid var(--bd);border-radius:10px;
        box-shadow:0 4px 16px rgba(0,0,0,.12);overflow:hidden;z-index:50}
  #chat.hidden{display:none}
  #chat.min{max-height:none}
  #chat.min #msgs,#chat.min .ft{display:none}
  #chat .hd{padding:7px 12px;border-bottom:1px solid var(--bd);
            font-weight:600;font-size:13px;background:#fafbfc;
            display:flex;align-items:center;gap:8px;cursor:default}
  #chat.min .hd{border-bottom:none}
  #chat .hd .ttl{flex:1}
  #btnMin{border:none;background:none;font-size:15px;line-height:1;
          padding:2px 6px;color:var(--sub);cursor:pointer;border-radius:4px}
  #btnMin:hover{background:#eceef1;color:var(--tx)}
  #msgs{flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:8px}
  .m{max-width:92%}
  .m .who{font-size:11px;color:var(--sub);margin-bottom:2px}
  .m pre{margin:0;padding:7px 10px;border-radius:8px;white-space:pre-wrap;
         word-break:break-word;font:12.5px/1.55 Consolas,"Courier New",monospace}
  .m.remote pre{background:#eef4ff;border:1px solid #d7e3fa}
  .m.me pre{background:#e8f6ec;border:1px solid #cdebd6;margin-left:auto}
  .m.me{align-self:flex-end;text-align:right}
  .m.me .who{text-align:right}
  #chat .ft{display:flex;gap:6px;padding:8px;border-top:1px solid var(--bd)}
  #msgBox{flex:1;height:56px;resize:none;border:1px solid var(--bd);border-radius:6px;
          padding:6px 8px;font:12.5px/1.5 Consolas,"Courier New",monospace;outline:none}
  #msgBox:focus{border-color:#7aa7e8}
  #btnSend{white-space:nowrap}
  .badge{display:none;background:var(--bad);color:#fff;border-radius:8px;
         padding:0 6px;font-size:11px;margin-left:4px}
  /* ---- 文件传输 ---- */
  #chat .xfer{display:none;border-bottom:1px solid var(--bd);padding:8px;background:#fcfcfd}
  #chat.xfer-open .xfer{display:block}
  #chat.min .xfer{display:none}
  .drop{border:1px dashed #c7ccd4;border-radius:8px;padding:12px 10px;text-align:center;
        color:var(--sub);font-size:12px;cursor:pointer;background:#fff;user-select:none}
  .drop.hot{border-color:#7aa7e8;background:#f2f7ff;color:#2b5d9e}
  .xr{margin-top:8px;font-size:12px}
  .xr .xn{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .xr .xb{height:5px;background:#e9ecf0;border-radius:3px;overflow:hidden;margin:3px 0}
  .xr .xb i{display:block;height:100%;width:0;background:#3b82f6;transition:width .25s}
  .xr .xs{color:var(--sub);font-size:11px;display:flex;gap:8px;align-items:center}
  .xr .xs .sp{flex:1}
  .xr button{padding:1px 6px;font-size:11px}
  .dl{border:1px solid #d7e3fa;background:#eef4ff;border-radius:8px;padding:8px 10px;font-size:12.5px}
  .dl .nm{font-weight:500;word-break:break-all;margin-bottom:3px}
  .dl .xs{color:var(--sub);font-size:11px}
  .dl a{color:#2563eb;text-decoration:none;font-weight:500}
  .dl a:hover{text-decoration:underline}
</style>
</head>
<body>
<header>
  <h1>桌面共享</h1>
  <span class="meta"><span id="dot" class="dot"></span><span id="status">连接中…</span></span>
  <span class="meta" id="meta"></span>
  <span class="spacer"></span>
  <button id="btnChat">💬 消息<span class="badge" id="badge"></span></button>
  <button id="btnFiles">📁 文件</button>
  <button id="btnFit">适应窗口 / 原始大小</button>
  <button id="btnFs">全屏</button>
</header>
<main><div id="stage"><img id="view" class="fit" alt="桌面画面"></div></main>
<footer>画面来自被控机实时采集，只读不可操作。右侧消息面板可与被控机互发文本（保留原始格式）。关闭页面即断开。</footer>

<div id="chat">
  <div class="hd">
    <span class="ttl">与被控机的消息</span>
    <button id="btnMin" title="最小化 / 展开">—</button>
  </div>
  <div id="xfer" class="xfer">
    <div class="drop" id="drop">拖文件到这里，或点击选择（可多选）</div>
    <input type="file" id="fileIn" multiple hidden>
    <div id="tlist"></div>
  </div>
  <div id="msgs"></div>
  <div class="ft">
    <textarea id="msgBox" placeholder="输入内容，Ctrl+Enter 发送"></textarea>
    <button id="btnSend">发送</button>
  </div>
</div>

<script>
  const view = document.getElementById('view');
  const dot = document.getElementById('dot');
  const status = document.getElementById('status');
  const src = '__SRC__';
  let retry = 0;

  function tokenQS(){
    const t = new URLSearchParams(location.search).get('token');
    return t ? 'token=' + encodeURIComponent(t) : '';
  }

  function info(){
    const q = new URLSearchParams(location.search);
    const fps = q.get('fps') || '__FPS__';
    const q2 = q.get('quality') || '__QUALITY__';
    document.getElementById('meta').textContent = '帧率上限 ' + fps + ' fps · 质量 ' + q2;
  }
  function load(){
    status.textContent = '连接中…';
    view.src = src + '&_t=' + Date.now();
  }
  view.onload = () => {
    retry = 0; dot.classList.remove('bad');
    status.textContent = '已连接 · ' + new Date().toLocaleTimeString();
  };
  view.onerror = () => {
    dot.classList.add('bad');
    retry++;
    status.textContent = '连接断开，' + Math.min(retry, 5) + ' 秒后重连…';
    setTimeout(load, Math.min(retry, 5) * 1000);
  };

  let fit = true;
  document.getElementById('btnFit').onclick = () => {
    fit = !fit;
    view.className = fit ? 'fit' : 'orig';
  };
  document.getElementById('btnFs').onclick = () => {
    const el = document.getElementById('stage');
    if (document.fullscreenElement) document.exitFullscreen();
    else if (el.requestFullscreen) el.requestFullscreen();
  };

  /* ================= 消息 ================= */
  const chat = document.getElementById('chat');
  const msgs = document.getElementById('msgs');
  const msgBox = document.getElementById('msgBox');
  const badge = document.getElementById('badge');
  let unread = 0;

  document.getElementById('btnChat').onclick = () => {
    chat.classList.toggle('hidden');
    unread = 0; badge.style.display = 'none';
  };

  /* 最小化 / 展开 */
  const btnMin = document.getElementById('btnMin');
  function setMin(m){
    chat.classList.toggle('min', m);
    btnMin.textContent = m ? '□' : '—';
    btnMin.title = m ? '展开' : '最小化';
    if (!m){ unread = 0; badge.style.display = 'none'; }
  }
  btnMin.onclick = (e) => {
    e.stopPropagation();
    setMin(!chat.classList.contains('min'));
  };
  /* 最小化时点标题栏直接展开 */
  chat.querySelector('.hd').addEventListener('click', (e) => {
    if (e.target === btnMin) return;
    if (chat.classList.contains('min')) setMin(false);
  });

  const seenIds = new Set();   // 已渲染的消息 id，防止本地插入 + 轮询回显造成重复

  function addMsg(m){
    if (m.id != null){
      if (seenIds.has(m.id)) return;   // 已经显示过，跳过
      seenIds.add(m.id);
    }
    const div = document.createElement('div');
    div.className = 'm ' + (m.sender === '主控机' ? 'me' : 'remote');
    const who = document.createElement('div');
    who.className = 'who';
    const t = new Date(m.ts * 1000);
    who.textContent = m.sender + ' · ' +
      t.toLocaleTimeString('zh-CN', {hour12:false});

    /* 被控机共享的文件：渲染成下载卡，走浏览器原生下载器（自带断点续传） */
    if (m.kind === 'file' && m.file){
      const card = document.createElement('div');
      card.className = 'dl';
      const nm = document.createElement('div');
      nm.className = 'nm'; nm.textContent = m.file.name;
      const xs = document.createElement('div');
      xs.className = 'xs';
      xs.textContent = fmtSize(m.file.size) + ' · ';
      const a = document.createElement('a');
      a.href = '/download/' + encodeURIComponent(m.file.sid) + '?' + tokenQS();
      a.download = m.file.name;
      a.textContent = '下载';
      xs.appendChild(a);
      card.appendChild(nm); card.appendChild(xs);
      div.className = 'm remote';
      div.appendChild(who); div.appendChild(card);
      msgs.appendChild(div);
      msgs.scrollTop = msgs.scrollHeight;
      return;
    }

    const pre = document.createElement('pre');
    pre.textContent = m.text;          // textContent: 完整保留换行/缩进，天然防注入
    div.appendChild(who); div.appendChild(pre);
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
    if (div.classList.contains('remote') &&
        (chat.classList.contains('hidden') || chat.classList.contains('min'))){
      unread++; badge.textContent = unread; badge.style.display = 'inline-block';
    }
    // 收到被控机消息时，若文本较短可一键复制
    pre.ondblclick = () => navigator.clipboard.writeText(m.text);
  }

  async function sendMessage(){
    const text = msgBox.value;
    if (!text) return;
    msgBox.value = '';
    try{
      const r = await fetch('/send?' + tokenQS(), {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text:text})
      });
      const j = await r.json();
      // 用服务端返回的消息（带 id）回显，轮询再拿到同一条时按 id 去重
      if (j && j.message) addMsg(j.message);
      else addMsg({sender:'主控机', text:text, ts:Date.now()/1000});
    }catch(e){ status.textContent = '发送失败: ' + e; }
  }
  document.getElementById('btnSend').onclick = sendMessage;
  msgBox.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)){
      e.preventDefault(); sendMessage();
    }
  });

  let lastId = 0;
  async function poll(){
    try{
      const r = await fetch('/poll?after=' + lastId + '&wait=1&' + tokenQS());
      if (r.ok){
        const j = await r.json();
        for (const m of (j.messages || [])) addMsg(m);
        if (j.messages && j.messages.length) lastId = j.next;
      }
    }catch(e){ /* 网络抖动静默重试 */ }
    setTimeout(poll, 300);
  }

  /* ================= 文件传输 ================= */
  const dropZone = document.getElementById('drop');
  const fileIn = document.getElementById('fileIn');
  const tlist = document.getElementById('tlist');
  const CH_MIN = 4 << 20, CH_MAX = 32 << 20;   // 分片 4MB 起，按 RTT/带宽自适应到 32MB

  document.getElementById('btnFiles').onclick = () => {
    chat.classList.remove('hidden');
    if (chat.classList.contains('min')) setMin(false);
    chat.classList.toggle('xfer-open');
  };

  function fmtSize(n){
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1048576) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1073741824) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
  }
  function fmtEta(s){
    if (!isFinite(s) || s <= 0) return '--:--';
    const m = Math.floor(s / 60), ss = Math.round(s % 60);
    return (m < 10 ? '0' : '') + m + ':' + (ss < 10 ? '0' : '') + ss;
  }
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  /* 首片指纹：http 局域网下 crypto.subtle 不可用（非安全上下文），用 FNV-1a 足够做续传匹配 */
  function fnvHead(u8, tail){
    let h = 0x811c9dc5;
    for (let i = 0; i < u8.length; i++){
      h ^= u8[i];
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ('0000000' + h.toString(16)).slice(-8) + '-' + tail;
  }
  async function headHash(file){
    const n = Math.min(file.size, 1 << 20);
    const u8 = new Uint8Array(await file.slice(0, n).arrayBuffer());
    return fnvHead(u8, file.size);
  }

  dropZone.onclick = () => fileIn.click();
  fileIn.onchange = () => { uploadAll(fileIn.files); fileIn.value = ''; };
  ['dragenter', 'dragover'].forEach(ev => dropZone.addEventListener(ev, e => {
    e.preventDefault(); dropZone.classList.add('hot');
  }));
  ['dragleave', 'drop'].forEach(ev => dropZone.addEventListener(ev, e => {
    e.preventDefault(); dropZone.classList.remove('hot');
  }));
  dropZone.addEventListener('drop', e => {
    if (e.dataTransfer && e.dataTransfer.files.length) uploadAll(e.dataTransfer.files);
  });

  function makeRow(name){
    const el = document.createElement('div');
    el.className = 'xr';
    const nm = document.createElement('div');
    nm.className = 'xn'; nm.textContent = name; nm.title = name;
    const bar = document.createElement('div');
    bar.className = 'xb';
    const fill = document.createElement('i');
    bar.appendChild(fill);
    const xs = document.createElement('div');
    xs.className = 'xs';
    const txt = document.createElement('span');
    txt.textContent = '准备中…';
    const sp = document.createElement('span');
    sp.className = 'sp';
    const btn = document.createElement('button');
    btn.textContent = '暂停';
    xs.appendChild(txt); xs.appendChild(sp); xs.appendChild(btn);
    el.appendChild(nm); el.appendChild(bar); el.appendChild(xs);
    tlist.appendChild(el);
    return {el: el, fill: fill, txt: txt, btn: btn};
  }

  async function uploadOne(file){
    const ui = makeRow(file.name);
    const st = {paused: false, abort: false};
    const samples = [];
    const qs = tokenQS();
    const t0All = Date.now();
    let tid = null, offset = 0, chunk = CH_MIN;

    ui.btn.onclick = () => {
      st.paused = !st.paused;
      ui.btn.textContent = st.paused ? '继续' : '暂停';
    };
    const speed = () => {
      const now = Date.now();
      while (samples.length && now - samples[0].t > 3000) samples.shift();
      if (!samples.length) return 0;
      const dt = Math.max(0.2, (now - samples[0].t) / 1000);
      let b = 0;
      for (const s of samples) b += s.b;
      return b / dt;
    };
    const paint = () => {
      const pct = file.size ? Math.min(100, offset / file.size * 100) : 0;
      ui.fill.style.width = pct.toFixed(1) + '%';
      const sp = speed();
      let line = fmtSize(offset) + ' / ' + fmtSize(file.size);
      if (st.paused) line += ' · 已暂停';
      else if (sp > 0) line += ' · ' + fmtSize(sp) + '/s · 剩 ' + fmtEta((file.size - offset) / sp);
      ui.txt.textContent = line;
    };
    const timer = setInterval(paint, 400);

    try {
      const hash = await headHash(file);
      const ri = await fetch('/upload/init?' + qs, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: file.name, size: file.size, hash: hash})
      });
      if (!ri.ok){
        const j = await ri.json().catch(() => ({}));
        throw new Error(j.error || ('init ' + ri.status));
      }
      const ji = await ri.json();
      tid = ji.tid;
      offset = ji.offset || 0;
      if (offset > 0 && offset < file.size) ui.txt.textContent = '断点续传，从 ' + fmtSize(offset) + ' 继续';
      paint();

      while (offset < file.size){
        if (st.abort) throw new Error('已取消');
        if (st.paused){ await sleep(250); continue; }

        const end = Math.min(file.size, offset + chunk);
        const blob = file.slice(offset, end);
        const tChunk = performance.now();
        let resp = null, lastErr = null;

        for (let attempt = 0; attempt < 5; attempt++){
          try {
            resp = await fetch('/upload/chunk?' + qs + '&tid=' + encodeURIComponent(tid) + '&off=' + offset, {
              method: 'POST',
              headers: {'Content-Type': 'application/octet-stream'},
              body: blob
            });
            if (resp.status === 409){            // 服务端 offset 和我们不一致，按它给的重新定位
              const j = await resp.json();
              offset = Number(j.offset) || 0;
              resp = null;
              break;
            }
            if (!resp.ok) throw new Error('chunk ' + resp.status);
            lastErr = null;
            break;
          } catch (e) {
            resp = null; lastErr = e;
            await sleep(400 * (attempt + 1));
          }
        }
        if (lastErr) throw lastErr;
        if (!resp) { chunk = CH_MIN; continue; }   // 409 重定位后重来，分片回落到最小

        const j = await resp.json();
        const got = Math.max(0, (j.offset || 0) - offset);
        offset = j.offset || offset;
        samples.push({t: Date.now(), b: got});

        const dt = (performance.now() - tChunk) / 1000;
        if (dt < 1.0 && chunk < CH_MAX) chunk = Math.min(CH_MAX, chunk * 2);
        else if (dt > 4.0 && chunk > CH_MIN) chunk = Math.max(CH_MIN, Math.floor(chunk / 2));

        paint();
        if (j.done) break;
      }

      const secs = Math.max(0.2, (Date.now() - t0All) / 1000);
      ui.fill.style.width = '100%';
      ui.txt.textContent = '已存到被控机 · ' + fmtSize(file.size) + ' · 平均 ' + fmtSize(file.size / secs) + '/s';
      ui.btn.remove();
      clearInterval(timer);
    } catch (e) {
      clearInterval(timer);
      paint();
      ui.txt.textContent = '失败：' + (e.message || e) + (tid ? '（重选同一文件可续传）' : '');
      ui.btn.textContent = '重试';
      ui.btn.onclick = () => { ui.el.remove(); uploadOne(file); };
    }
  }

  function uploadAll(list){
    const arr = Array.from(list || []);
    if (!arr.length) return;
    (async () => {
      for (const f of arr) await uploadOne(f);      // 串行：并行会抢推流带宽
    })();
  }

  info(); load(); poll();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 消息总线

class MessageBus:
    """线程安全的双向消息存储（浏览器 <-> 被控端小窗 共用）"""

    def __init__(self, keep=1000):
        self.lock = threading.Lock()
        self.msgs = []            # [{"id","sender","text","ts"}]
        self.next_id = 1
        self.keep = keep

    def push(self, sender, text, kind=None, file=None):
        with self.lock:
            m = {"id": self.next_id, "sender": sender,
                 "text": text, "ts": time.time()}
            if kind:
                m["kind"] = kind
            if file:
                m["file"] = file
            self.next_id += 1
            self.msgs.append(m)
            if len(self.msgs) > self.keep:
                self.msgs = self.msgs[-self.keep:]
        return m

    def after(self, idx):
        """返回 id > idx 的消息列表"""
        with self.lock:
            return [m for m in self.msgs if m["id"] > idx]


# ---------------------------------------------------------------- 屏幕采集

class Capture:
    """屏幕采集 + JPEG 编码，带锁，线程安全"""

    def __init__(self, backend="mss", monitor=0, scale=1.0, quality=70):
        self.lock = threading.Lock()
        self.scale = scale
        self.quality = quality
        self.backend = backend
        self.monitor = monitor
        self._sct = None
        self._cam = None

        if backend == "dxcam":
            try:
                import dxcam  # noqa
                self._cam = dxcam.create(output_color="RGB")
                if self._cam is None:
                    raise RuntimeError("dxcam 初始化失败")
            except Exception as e:
                print(f"[!] dxcam 后端不可用({e})，自动回退 mss", flush=True)
                self.backend = "mss"

        if self.backend == "mss":
            self._sct = _MSS()
            mons = self._sct.monitors
            idx = monitor if 0 <= monitor < len(mons) else 0
            self._mon = mons[idx]
        else:
            self._mon = None

    @property
    def size(self):
        return (self._mon["width"], self._mon["height"]) if self._mon else (0, 0)

    def _raw_image(self):
        if self.backend == "dxcam":
            frame = self._cam.grab()
            if frame is None:
                return None
            return Image.fromarray(frame)
        img = self._sct.grab(self._mon)
        raw = getattr(img, "bgra", None)
        if raw is not None:
            return Image.frombytes("RGB", img.size, raw, "raw", "BGRX")
        return Image.frombytes("RGB", img.size, img.rgb)

    def jpeg(self, quality=None, scale=None):
        q = int(quality if quality is not None else self.quality)
        s = float(scale if scale is not None else self.scale)
        with self.lock:
            im = self._raw_image()
            if im is None:
                return None
            if s and abs(s - 1.0) > 1e-3:
                w, h = im.size
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=False)
            return buf.getvalue()


# ---------------------------------------------------------------- 文件传输

def safe_filename(name):
    """只留纯文件名，去掉路径分隔符和 Windows 非法字符"""
    name = os.path.basename(str(name).replace("\\", "/")) or "unnamed.bin"
    out = []
    for ch in name:
        out.append("_" if (ch in '<>:"/\\|?*' or ord(ch) < 32) else ch)
    name = "".join(out).strip(" .")
    return (name or "unnamed.bin")[:180]


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def human_eta(sec):
    try:
        sec = int(max(0, sec))
    except Exception:
        return "--:--"
    return "%02d:%02d" % (sec // 60, sec % 60)


class Transfers:
    """上传任务（分片落盘 / 断点续传）与下载共享（Range 续传）

    磁盘布局（都在 recv_dir 下）:
        <tid>.part        正在接收的数据，按 offset 顺序追加
        <tid>.part.json   {"name","size","created"}，用于重启后恢复
        <文件名>            收完后 rename 成最终文件（重名自动加 (1)）
    """

    WINDOW = 3.0        # 测速滑动窗口（秒）

    def __init__(self, root_dir, max_bytes, bus, allow_upload=True):
        self.root = os.path.abspath(root_dir)
        self.max_bytes = int(max_bytes)
        self.bus = bus
        self.allow_upload = bool(allow_upload)
        self.lock = threading.RLock()
        self.tasks = {}       # tid -> task
        self.shared = {}      # sid -> {sid,path,name,size,sent,ts}
        os.makedirs(self.root, exist_ok=True)
        self._recover()

    # ---- 路径
    def _part(self, tid):
        return os.path.join(self.root, tid + ".part")

    def _meta(self, tid):
        return os.path.join(self.root, tid + ".part.json")

    def _unique(self, path):
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 1
        while os.path.exists("%s (%d)%s" % (base, i, ext)):
            i += 1
        return "%s (%d)%s" % (base, i, ext)

    # ---- 启动时恢复未完成任务
    def _recover(self):
        try:
            names = os.listdir(self.root)
        except Exception:
            return
        for fn in names:
            if not fn.endswith(".part.json"):
                continue
            tid = fn[:-len(".part.json")]
            try:
                with open(os.path.join(self.root, fn), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                size = int(meta.get("size") or 0)
            except Exception:
                continue
            part = self._part(tid)
            recv = os.path.getsize(part) if os.path.exists(part) else 0
            if size <= 0 or recv >= size:
                continue
            self.tasks[tid] = {
                "id": tid, "name": meta.get("name") or "unnamed.bin",
                "size": size, "recv": recv, "status": "paused",
                "created": meta.get("created") or time.time(),
                "updated": time.time(), "samples": deque(maxlen=128),
                "wlock": threading.Lock(), "final": None,
                "path": part,
            }
            print("[file] 发现未完成上传: %s  %s/%s"
                  % (self.tasks[tid]["name"], human_bytes(recv), human_bytes(size)),
                  flush=True)

    # ---- 上传
    def init_task(self, name, size, fhash):
        name = safe_filename(name)
        size = int(size)
        if size <= 0:
            raise ValueError("文件为空")
        if size > self.max_bytes:
            raise ValueError("超过单文件上限 %s" % human_bytes(self.max_bytes))
        tid = hashlib.sha1(("%s|%d|%s" % (name, size, fhash)).encode("utf-8")).hexdigest()[:16]
        with self.lock:
            t = self.tasks.get(tid)
            part = self._part(tid)
            if t is None:
                open(part, "ab").close()
                t = {
                    "id": tid, "name": name, "size": size, "recv": 0,
                    "status": "active", "created": time.time(),
                    "updated": time.time(), "samples": deque(maxlen=128),
                    "wlock": threading.Lock(), "final": None, "path": part,
                }
                self.tasks[tid] = t
                try:
                    with open(self._meta(tid), "w", encoding="utf-8") as f:
                        json.dump({"name": name, "size": size,
                                   "created": t["created"]}, f)
                except Exception as e:
                    print("[!] 写元数据失败: %r" % (e,), flush=True)
            recv = os.path.getsize(part) if os.path.exists(part) else 0
            t["recv"] = recv
            t["status"] = "active"
            t["updated"] = time.time()
            t["samples"].clear()
        return {"tid": tid, "offset": recv, "name": name, "size": size,
                "resumed": recv > 0}

    def task(self, tid):
        with self.lock:
            return self.tasks.get(tid)

    def part_size(self, tid):
        p = self._part(tid)
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    def bump(self, tid, n):
        """写入 n 字节后更新进度与测速样本（调用方需持有 task['wlock']）"""
        with self.lock:
            t = self.tasks.get(tid)
            if t is None:
                return 0
            t["recv"] = self.part_size(tid)
            t["updated"] = time.time()
            if n:
                t["samples"].append((time.time(), int(n)))
            return t["recv"]

    def finalize_if_complete(self, tid):
        """收满就 rename 成最终文件（调用方需持有 task['wlock']）"""
        with self.lock:
            t = self.tasks.get(tid)
            if t is None:
                return None
            if t["recv"] < t["size"]:
                return None
            final = self._unique(os.path.join(self.root, t["name"]))
            try:
                os.replace(t["path"], final)
            except Exception as e:
                print("[!] 重命名失败: %r" % (e,), flush=True)
                return None
            try:
                os.remove(self._meta(tid))
            except OSError:
                pass
            t["status"] = "done"
            t["final"] = final
            t["updated"] = time.time()
        print("[file] 接收完成: %s  %s  -> %s"
              % (t["name"], human_bytes(t["size"]), final), flush=True)
        if self.bus is not None:
            self.bus.push("被控机", "[file] 已收到 %s（%s）\n已存到：%s"
                          % (t["name"], human_bytes(t["size"]), final))
        return {"path": final, "name": t["name"], "size": t["size"]}

    def abort(self, tid):
        with self.lock:
            t = self.tasks.pop(tid, None)
        for p in (self._part(tid), self._meta(tid)):
            try:
                os.remove(p)
            except OSError:
                pass
        return t is not None

    # ---- 下载（被控机往外发）
    def share(self, path):
        path = os.path.abspath(path)
        st = os.stat(path)
        sid = hashlib.sha1(("%s|%d|%d" % (path, st.st_size, st.st_mtime)).encode("utf-8")).hexdigest()[:12]
        info = {"sid": sid, "path": path, "name": os.path.basename(path),
                "size": st.st_size, "sent": 0, "ts": 0}
        with self.lock:
            self.shared[sid] = info
        print("[file] 共享给主控机: %s (%s)" % (info["name"], human_bytes(info["size"])), flush=True)
        return dict(info)

    def download_info(self, sid):
        with self.lock:
            d = self.shared.get(sid)
            return dict(d) if d else None

    def list_shared(self):
        with self.lock:
            return [{"sid": d["sid"], "name": d["name"], "size": d["size"]}
                    for d in self.shared.values()]

    def mark_download(self, sid, n):
        with self.lock:
            d = self.shared.get(sid)
            if d is not None:
                d["sent"] = d.get("sent", 0) + n
                d["ts"] = time.time()

    # ---- 状态汇总（给小窗和接口用）
    def _speed(self, t):
        now = time.time()
        samples = [s for s in t["samples"] if now - s[0] <= self.WINDOW]
        if not samples:
            return 0.0
        span = max(0.2, now - samples[0][0])
        return sum(s[1] for s in samples) / span

    def snapshot(self):
        now = time.time()
        out = []
        with self.lock:
            for t in self.tasks.values():
                if t["status"] == "done":
                    continue
                out.append({"dir": "in", "name": t["name"], "done": t["recv"],
                            "size": t["size"], "speed": self._speed(t),
                            "status": t["status"]})
            for d in self.shared.values():
                if d.get("ts") and now - d["ts"] < 3:
                    out.append({"dir": "out", "name": d["name"],
                                "done": d.get("sent", 0), "size": d["size"],
                                "speed": 0.0, "status": "active"})
        return out

    def busy(self):
        """是否有传输在跑（用于自动降帧）"""
        now = time.time()
        with self.lock:
            for t in self.tasks.values():
                if t["status"] == "active" and now - t["updated"] < 3:
                    return True
            for d in self.shared.values():
                if d.get("ts") and now - d["ts"] < 2:
                    return True
        return False


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "WebScreenShare/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    def handle_one_request(self):
        # 客户端直接断开时 stdlib 会抛异常并打堆栈，这里安静收掉
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    def _q(self):
        return parse_qs(urlparse(self.path).query)

    def _authorized(self, q):
        token = self.server.token
        if not token:
            return True
        if q.get("token", [None])[0] == token:
            return True
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and auth[7:].strip() == token:
            return True
        self.send_error(401, "Unauthorized")
        return False

    def _num(self, q, key, default, cast, lo=None, hi=None):
        try:
            v = cast(q.get(key, [default])[0])
        except Exception:
            return default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = self._q()
        if not self._authorized(q):
            return
        if u.path in ("/", "/index.html"):
            self._page(q)
        elif u.path == "/stream":
            self._stream(q)
        elif u.path in ("/snapshot.jpg", "/snapshot"):
            self._snapshot(q)
        elif u.path == "/poll":
            self._poll(q)
        elif u.path == "/health":
            self._json({"ok": True})
        elif u.path == "/dl/list":
            self._json({"files": self.server.transfers.list_shared()})
        elif u.path.startswith("/download/"):
            self._download(q, u.path[len("/download/"):])
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        q = self._q()
        if not self._authorized(q):
            return
        if u.path == "/send":
            self._send_text()
        elif u.path == "/upload/init":
            self._upload_init()
        elif u.path == "/upload/chunk":
            self._upload_chunk(q)
        elif u.path == "/upload/abort":
            tid = q.get("tid", [""])[0]
            self._json({"ok": self.server.transfers.abort(tid)})
        else:
            self.send_error(404)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        return json.loads(raw.decode("utf-8"))

    def _send_text(self):
        try:
            text = str(self._body_json().get("text", ""))
        except Exception:
            self.send_error(400, "bad request body")
            return
        text = text[:MAX_MSG_LEN]
        msg = None
        if text:
            msg = self.server.bus.push("主控机", text)
            print("[msg] 主控机 -> %d 字符" % len(text), flush=True)
        self._json({"ok": True, "message": msg})

    # ---- 上传：init / chunk ----
    def _upload_init(self):
        tr = self.server.transfers
        if not tr.allow_upload:
            self._json({"error": "服务端已关闭上传（--no-upload）"}, 403)
            return
        try:
            d = self._body_json()
            r = tr.init_task(d.get("name", ""), d.get("size", 0), d.get("hash", ""))
        except ValueError as e:
            self._json({"error": str(e)}, 413)
            return
        except Exception as e:
            self._json({"error": "参数错误: %r" % (e,)}, 400)
            return
        print("[file] 上传开始: %s  %s%s"
              % (r["name"], human_bytes(r["size"]),
                 "（续传，已有 %s）" % human_bytes(r["offset"]) if r["offset"] else ""),
              flush=True)
        self._json(r)

    def _drain(self, n):
        """把请求体读干净，否则 keep-alive 连接会错位"""
        remain = n
        while remain > 0:
            buf = self.rfile.read(min(remain, XFER_BLOCK))
            if not buf:
                break
            remain -= len(buf)

    def _upload_chunk(self, q):
        tid = q.get("tid", [""])[0]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            off = int(q.get("off", ["0"])[0])
        except Exception:
            off = -1
        tr = self.server.transfers
        task = tr.task(tid)
        if task is None:
            self._drain(n)
            self._json({"error": "unknown tid"}, 404)
            return
        with task["wlock"]:
            cur = tr.part_size(tid)
            if off != cur:
                # 客户端和服务端进度不一致：读干净再让它按正确 offset 重发
                self._drain(n)
                self._json({"ok": False, "offset": cur}, 409)
                return
            got = 0
            f = open(task["path"], "r+b")
            try:
                f.seek(off)
                remain = n
                while remain > 0:
                    buf = self.rfile.read(min(remain, XFER_BLOCK))
                    if not buf:
                        break
                    f.write(buf)
                    remain -= len(buf)
                    got += len(buf)
                f.flush()
            finally:
                f.close()
            recv = tr.bump(tid, got)
            done = tr.finalize_if_complete(tid)
        self._json({"ok": True, "offset": recv, "done": bool(done)})

    # ---- 下载：支持 Range，浏览器原生下载器可续传 ----
    def _download(self, q, sid):
        info = self.server.transfers.download_info(unquote(sid).strip("/"))
        if not info:
            # 注意：send_error 的 message 走 latin-1，中文会直接抛异常，这里用 JSON 正文
            self._json({"error": "文件已失效，请让被控机重新共享"}, 404)
            return
        path, name, size = info["path"], info["name"], info["size"]
        start, end, code = 0, max(0, size - 1), 200
        rng = (self.headers.get("Range") or "").strip()
        if rng.lower().startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "":
                    start = max(0, size - int(b))
                    end = size - 1
                else:
                    start = int(a)
                    end = int(b) if b else size - 1
            except Exception:
                start, end = 0, size - 1
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            code = 206
        length = end - start + 1

        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
        self.send_header("Content-Disposition",
                         "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                         % (ascii_name, quote(name)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        print("[file] 下发: %s  %s%s"
              % (name, human_bytes(length), "（Range）" if code == 206 else ""), flush=True)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0 and not self.server.stopped:
                    buf = f.read(min(remain, XFER_BLOCK))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remain -= len(buf)
                    self.server.transfers.mark_download(info["sid"], len(buf))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _page(self, q):
        base = "/stream?" + urlparse(self.path).query
        fps = self._num(q, "fps", self.server.fps, int, 1, 60)
        quality = self._num(q, "quality", self.server.quality, int, 1, 95)
        page = (HTML.replace("__SRC__", base)
                    .replace("__FPS__", str(fps))
                    .replace("__QUALITY__", str(quality)))
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _snapshot(self, q):
        jpg = self.server.capture.jpeg(
            quality=self._num(q, "quality", self.server.quality, int, 1, 95),
            scale=self._num(q, "scale", self.server.scale, float, 0.1, 2.0),
        )
        if not jpg:
            self.send_error(503, "capture failed")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(jpg)

    def _poll(self, q):
        """长轮询：wait=1 时最多挂起 LONGPOLL_SECONDS 秒等新消息"""
        after = self._num(q, "after", 0, int, 0)
        wait = q.get("wait", ["0"])[0] == "1"
        deadline = time.time() + LONGPOLL_SECONDS if wait else 0
        msgs = self.server.bus.after(after)
        while not msgs and wait and not self.server.stopped \
                and time.time() < deadline:
            time.sleep(0.25)
            msgs = self.server.bus.after(after)
        self._json({"messages": msgs, "next": msgs[-1]["id"] if msgs else after})

    def _stream(self, q):
        fps = self._num(q, "fps", self.server.fps, int, 1, 60)
        quality = self._num(q, "quality", self.server.quality, int, 1, 95)
        scale = self._num(q, "scale", self.server.scale, float, 0.1, 2.0)

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=%s" % BOUNDARY)
        self.end_headers()
        print("[+] 客户端接入: %s  (fps=%s quality=%s scale=%s)"
              % (self.client_address[0], fps, quality, scale), flush=True)
        try:
            while not self.server.stopped:
                # 传输期间自动降帧：把带宽和 CPU 让给文件，默认 4fps
                fps_eff = fps
                if self.server.autothrottle and self.server.transfers.busy():
                    fps_eff = max(1, min(fps, self.server.throttle_fps))
                interval = 1.0 / fps_eff
                t0 = time.time()
                jpg = self.server.capture.jpeg(quality=quality, scale=scale)
                if not jpg:
                    time.sleep(interval)
                    continue
                self.wfile.write(("--%s\r\n" % BOUNDARY).encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(jpg)).encode())
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                dt = interval - (time.time() - t0)
                if dt > 0:
                    time.sleep(dt)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            sys.stderr.write("[!] 推流异常: %r\n" % (e,))
        finally:
            print("[-] 客户端断开: %s" % self.client_address[0], flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, capture, fps, quality, scale, token, bus,
                 transfers, autothrottle=True, throttle_fps=4):
        super().__init__(addr, Handler)
        self.capture = capture
        self.fps = fps
        self.quality = quality
        self.scale = scale
        self.token = token
        self.bus = bus
        self.transfers = transfers
        self.autothrottle = autothrottle
        self.throttle_fps = throttle_fps
        self.stopped = False

    def share_file(self, path):
        return self.transfers.share(path)

    def transfer_snapshot(self):
        return self.transfers.snapshot()


# ---------------------------------------------------------------- 被控端消息小窗

def run_overlay(bus, port, srv=None):
    """被控机桌面上的置顶小窗：收发消息 + 文件传输进度 + 主动共享文件"""
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as e:
        print("[!] tkinter 不可用(%s)，被控端将不弹消息小窗" % e, flush=True)
        # 阻塞等待退出信号，让服务继续跑
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    root = tk.Tk()
    root.title("桌面共享 · 消息 (端口 %d)" % port)
    root.attributes("-topmost", True)
    root.geometry("470x580+40+40")
    root.minsize(400, 460)

    MONO = ("Consolas", 10)

    top = tk.Frame(root)
    top.pack(fill="x", padx=8, pady=(8, 2))
    auto_copy = tk.BooleanVar(value=True)
    tk.Checkbutton(top, text="自动复制收到的文本到剪贴板",
                   variable=auto_copy).pack(side="left")

    # ---- 文件传输进度 + 主动共享 ----
    xf = tk.Frame(root)
    xf.pack(fill="x", padx=8, pady=(2, 2))
    xf_lbl = tk.Label(xf, text="暂无传输", anchor="w",
                      font=("Consolas", 9), fg="#555")
    xf_lbl.pack(fill="x")
    pb = ttk.Progressbar(xf, mode="determinate", maximum=100)
    pb.pack(fill="x", pady=(2, 0))

    txt = tk.Text(root, height=12, width=52, font=MONO, wrap="word",
                  state="disabled", bg="#fafafa")
    txt.pack(fill="both", expand=True, padx=8, pady=4)

    bottom = tk.Frame(root)
    bottom.pack(fill="x", padx=8, pady=(0, 8))
    # 多行输入框：单行 Entry 粘贴上万字符极慢（实测 17k 要 0.6s），Text 只要毫秒级
    entry = tk.Text(bottom, height=3, font=MONO, wrap="word",
                    undo=True, maxundo=50)
    # 注意 pack 顺序：按钮先占右侧，否则 expand 的输入框会把按钮挤成 1x1
    # （send 在下方定义，这里用 lambda 延迟绑定）
    tk.Button(bottom, text="发送", command=lambda: send()).pack(side="right", padx=(6, 0))
    entry.pack(side="left", fill="both", expand=True)

    # ---- 分块渲染队列：大消息按 RENDER_CHUNK 字符/帧写入，UI 不再卡住 ----
    queue = []          # 待渲染任务 {"m":..., "chunks":[...], "i":0}
    pumping = [False]

    def pump():
        if not queue:
            pumping[0] = False
            return
        job = queue[0]
        if job["i"] < len(job["chunks"]):
            txt.configure(state="normal")
            txt.insert("end", job["chunks"][job["i"]])
            txt.see("end")
            txt.configure(state="disabled")
            job["i"] += 1
        else:
            txt.configure(state="normal")
            txt.insert("end", "\n\n")
            txt.see("end")
            txt.configure(state="disabled")
            m = job["m"]
            queue.pop(0)
            if auto_copy.get() and m["sender"] == "主控机":
                root.clipboard_clear()
                root.clipboard_append(m["text"])
            # 长跑保护：日志超过 4000 行裁掉最早 1000 行，防止越用越慢
            try:
                if int(txt.index("end-1c").split(".")[0]) > 4000:
                    txt.configure(state="normal")
                    txt.delete("1.0", "1000.0")
                    txt.configure(state="disabled")
            except Exception:
                pass
        if not queue:
            pumping[0] = False
            return
        root.after(8, pump)

    def add_line(m):
        ts = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
        head = "[%s] %s:\n" % (ts, m["sender"])
        body = m["text"]
        pieces = [body[i:i + RENDER_CHUNK]
                  for i in range(0, len(body), RENDER_CHUNK)] or [""]
        # 头部和第一块一起塞，视觉上立刻出现
        queue.append({"m": m, "chunks": [head + pieces[0]] + pieces[1:], "i": 0})
        if not pumping[0]:
            pumping[0] = True
            root.after(1, pump)

    def send(_event=None):
        text = entry.get("1.0", "end-1c")
        if not text.strip():
            return
        entry.delete("1.0", "end")
        bus.push("被控机", text[:MAX_MSG_LEN])
        return "break"

    entry.bind("<Return>", send)                     # 回车发送
    entry.bind("<Shift-Return>", lambda e: None)      # Shift+回车换行
    entry.bind("<Control-Return>", send)

    last_id = [0]

    # ---- 主动把本机文件共享给主控机（浏览器原生下载，支持 Range 续传）----
    def pick_file():
        if srv is None:
            return
        from tkinter import filedialog
        path = filedialog.askopenfilename(title="选择要发给主控机的文件")
        if not path:
            return
        try:
            info = srv.share_file(path)
        except Exception as e:
            xf_lbl.config(text="共享失败：%r" % (e,))
            return
        bus.push("被控机",
                 "已共享文件：%s（%s）\n点下面的「下载」即可，支持断点续传"
                 % (info["name"], human_bytes(info["size"])),
                 kind="file",
                 file={"sid": info["sid"], "name": info["name"], "size": info["size"]})

    tk.Button(top, text="发送文件…", command=pick_file).pack(side="right")

    def xfer_tick():
        snap = []
        if srv is not None:
            try:
                snap = srv.transfer_snapshot()
            except Exception:
                snap = []
        if snap:
            t = snap[0]
            pct = (t["done"] / t["size"] * 100.0) if t["size"] else 0
            pb.configure(value=max(0.0, min(100.0, pct)))
            arrow = "↓ 接收" if t["dir"] == "in" else "↑ 发送"
            line = "%s %s  %s / %s" % (arrow, t["name"],
                                      human_bytes(t["done"]), human_bytes(t["size"]))
            sp = t.get("speed") or 0
            if sp > 0:
                line += "  %s/s  剩 %s" % (human_bytes(sp),
                                          human_eta((t["size"] - t["done"]) / sp))
            elif t["dir"] == "out":
                line += "  （主控机下载中）"
            if len(line) > 62:
                line = line[:60] + "…"
            xf_lbl.config(text=line)
        else:
            pb.configure(value=0)
            xf_lbl.config(text="暂无传输")
        root.after(500, xfer_tick)

    xfer_tick()

    def poll_loop():
        for m in bus.after(last_id[0]):
            last_id[0] = m["id"]
            add_line(m)
        root.after(300, poll_loop)

    poll_loop()

    def on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    print("[+] 被控端消息小窗已启动（发消息后回车发送）", flush=True)
    root.mainloop()


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


def main():
    ap = argparse.ArgumentParser(description="Web 实时桌面共享 + 双向消息（被控端）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    ap.add_argument("--port", type=int, default=8080, help="端口，默认 8080")
    ap.add_argument("--fps", type=int, default=15, help="帧率，默认 15")
    ap.add_argument("--quality", type=int, default=70, help="JPEG 质量 1-95，默认 70")
    ap.add_argument("--scale", type=float, default=0.75, help="画面缩放，默认 0.75")
    ap.add_argument("--monitor", type=int, default=0, help="显示器序号，0=全部，1=主屏")
    ap.add_argument("--backend", choices=["mss", "dxcam"], default="mss",
                    help="采集后端，抓到黑屏换另一个试试")
    ap.add_argument("--token", default="", help="访问口令，访问时 URL 加 ?token=xxx")
    ap.add_argument("--recv-dir", default="",
                    help="接收文件的存放目录，默认 ~/Downloads/WebScreenShare")
    ap.add_argument("--max-file-size", type=int, default=DEFAULT_MAX_FILE_MB,
                    help="单文件上限 MB，默认 %d" % DEFAULT_MAX_FILE_MB)
    ap.add_argument("--no-upload", action="store_true",
                    help="关闭浏览器上传（只允许被控机往外发文件）")
    ap.add_argument("--no-autothrottle", action="store_true",
                    help="传输时不自动降帧（默认降到 --throttle-fps）")
    ap.add_argument("--throttle-fps", type=int, default=4,
                    help="传输期间自动降到的帧率，默认 4")
    ap.add_argument("--no-ui", action="store_true",
                    help="不弹出被控端消息小窗（消息仍可通过 /poll 收）")
    args = ap.parse_args()

    bus = MessageBus()
    cap = Capture(backend=args.backend, monitor=args.monitor,
                  scale=args.scale, quality=args.quality)
    recv_dir = args.recv_dir or os.path.join(os.path.expanduser("~"),
                                             "Downloads", "WebScreenShare")
    transfers = Transfers(recv_dir, args.max_file_size * 1024 * 1024, bus,
                          allow_upload=not args.no_upload)
    w, h = cap.size
    print("=" * 56)
    print(" WebScreenShare v2 被控端已启动")
    print(" 采集后端: %s   分辨率: %sx%s   缩放: %s" % (cap.backend, w, h, args.scale))
    print(" 帧率: %s fps   质量: %s   消息小窗: %s"
          % (args.fps, args.quality, "关" if args.no_ui else "开"))
    print(" 文件接收: %s  单文件上限 %s  上传 %s"
          % (transfers.root, human_bytes(transfers.max_bytes),
             "关" if args.no_upload else "开"))
    print(" 传输时自动降帧: %s"
          % ("关" if args.no_autothrottle else ("开（降到 %d fps）" % args.throttle_fps)))
    print("-" * 56)
    for ip in local_ips():
        print(" 控制机浏览器打开:  http://%s:%d/%s"
              % (ip, args.port, ("?token=%s" % args.token) if args.token else ""))
    if not local_ips():
        print(" 未取到 IP，用 ipconfig 查本机地址后访问 http://<IP>:%d/" % args.port)
    print(" 防火墙放行: netsh advfirewall firewall add rule "
          'name="WebScreenShare" dir=in action=allow protocol=TCP '
          "localport=%d" % args.port)
    print(" 停止: Ctrl+C 或关闭消息小窗")
    print("=" * 56, flush=True)

    srv = Server((args.host, args.port), cap, args.fps,
                 args.quality, args.scale, args.token, bus, transfers,
                 autothrottle=not args.no_autothrottle,
                 throttle_fps=args.throttle_fps)

    if args.no_ui:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n正在退出…")
        finally:
            srv.stopped = True
            srv.shutdown()
            srv.server_close()
    else:
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            run_overlay(bus, args.port, srv)   # 关闭小窗 = 退出
        except KeyboardInterrupt:
            pass
        finally:
            srv.stopped = True
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    main()

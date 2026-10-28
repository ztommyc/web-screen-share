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
import io
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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
MAX_MSG_LEN = 20000          # 单条消息最大字符数
LONGPOLL_SECONDS = 25        # /poll 长轮询挂起时长

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
</style>
</head>
<body>
<header>
  <h1>桌面共享</h1>
  <span class="meta"><span id="dot" class="dot"></span><span id="status">连接中…</span></span>
  <span class="meta" id="meta"></span>
  <span class="spacer"></span>
  <button id="btnChat">💬 消息<span class="badge" id="badge"></span></button>
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

  function addMsg(m){
    const div = document.createElement('div');
    div.className = 'm ' + (m.sender === '主控机' ? 'me' : 'remote');
    const who = document.createElement('div');
    who.className = 'who';
    const t = new Date(m.ts * 1000);
    who.textContent = m.sender + ' · ' +
      t.toLocaleTimeString('zh-CN', {hour12:false});
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
      await fetch('/send?' + tokenQS(), {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text:text})
      });
      addMsg({sender:'主控机', text:text, ts:Date.now()/1000});
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

    def push(self, sender, text):
        with self.lock:
            m = {"id": self.next_id, "sender": sender,
                 "text": text, "ts": time.time()}
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
        if token and q.get("token", [None])[0] != token:
            self.send_error(401, "Unauthorized")
            return False
        return True

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
        else:
            self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        q = self._q()
        if not self._authorized(q):
            return
        if u.path != "/send":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            data = json.loads(raw.decode("utf-8"))
            text = str(data.get("text", ""))
        except Exception:
            self.send_error(400, "bad request body")
            return
        text = text[:MAX_MSG_LEN]
        if text:
            self.server.bus.push("主控机", text)
            print("[msg] 主控机 -> %d 字符" % len(text), flush=True)
        self._json({"ok": True})

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
        interval = 1.0 / fps

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

    def __init__(self, addr, capture, fps, quality, scale, token, bus):
        super().__init__(addr, Handler)
        self.capture = capture
        self.fps = fps
        self.quality = quality
        self.scale = scale
        self.token = token
        self.bus = bus
        self.stopped = False


# ---------------------------------------------------------------- 被控端消息小窗

def run_overlay(bus, port):
    """被控机桌面上的置顶小窗：收发消息 + 可选自动复制收到的文本"""
    try:
        import tkinter as tk
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
    root.geometry("+40+40")
    root.minsize(360, 300)

    MONO = ("Consolas", 10)

    top = tk.Frame(root)
    top.pack(fill="x", padx=8, pady=(8, 2))
    auto_copy = tk.BooleanVar(value=True)
    tk.Checkbutton(top, text="自动复制收到的文本到剪贴板",
                   variable=auto_copy).pack(side="left")

    txt = tk.Text(root, height=12, width=52, font=MONO, wrap="word",
                  state="disabled", bg="#fafafa")
    txt.pack(fill="both", expand=True, padx=8, pady=4)

    bottom = tk.Frame(root)
    bottom.pack(fill="x", padx=8, pady=(0, 8))
    entry = tk.Entry(bottom, font=MONO)
    entry.pack(side="left", fill="x", expand=True)

    def add_line(m):
        ts = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
        txt.configure(state="normal")
        txt.insert("end", "[%s] %s:\n%s\n\n" % (ts, m["sender"], m["text"]))
        txt.see("end")
        txt.configure(state="disabled")
        if auto_copy.get() and m["sender"] == "主控机":
            root.clipboard_clear()
            root.clipboard_append(m["text"])

    def send(_event=None):
        text = entry.get()
        if not text:
            return
        entry.delete(0, "end")
        bus.push("被控机", text[:MAX_MSG_LEN])

    tk.Button(bottom, text="发送", command=send).pack(side="left", padx=(6, 0))
    entry.bind("<Return>", send)

    last_id = [0]

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
    ap.add_argument("--no-ui", action="store_true",
                    help="不弹出被控端消息小窗（消息仍可通过 /poll 收）")
    args = ap.parse_args()

    bus = MessageBus()
    cap = Capture(backend=args.backend, monitor=args.monitor,
                  scale=args.scale, quality=args.quality)
    w, h = cap.size
    print("=" * 56)
    print(" WebScreenShare v2 被控端已启动")
    print(" 采集后端: %s   分辨率: %sx%s   缩放: %s" % (cap.backend, w, h, args.scale))
    print(" 帧率: %s fps   质量: %s   消息小窗: %s"
          % (args.fps, args.quality, "关" if args.no_ui else "开"))
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
                 args.quality, args.scale, args.token, bus)

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
            run_overlay(bus, args.port)   # 关闭小窗 = 退出
        except KeyboardInterrupt:
            pass
        finally:
            srv.stopped = True
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    main()

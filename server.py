#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黑白市集 · 本地商品管理 Server
Python3 标准库实现, 无任何第三方依赖。

用法:
    python3 server.py            启动后打开 http://127.0.0.1:8000/admin
    bash run.sh                 一键启动(推荐)

功能:
    /admin            管理界面(增删改商品 / 上传图片), 修改后即时覆盖 index.html
    /api/products     读取商品;  POST 保存并重写 index.html
    /api/upload       上传图片到 images/
    /                 本地预览首页(等同于即将发布的 index.html)

部署说明:
    本工具只做本地编辑。改完把整个目录推送到 GitHub Pages 即可，
    需要发布的纯静态文件就是: index.html / style.css / images/ / products.json。
    本地编辑 Server 本身不需要、也不应被部署。
"""
import os
import re
import json
import math
import html
import shutil
import threading
import base64
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "products.json")
SITE_FILE = os.path.join(HERE, "site.json")
IMAGES_DIR = os.path.join(HERE, "images")
INDEX_FILE = os.path.join(HERE, "index.html")

PORT = 8000
EMOJI_FALLBACK = "◼"

# ---- 网站自定义(改这里即可; 保存后会自动覆盖 index.html) ----
SITE = {
    "title": "小玲阿姨 · 黑白市集",          # 浏览器标签页标题
    "logo": "小玲阿姨",                        # 左上角(不加"·市集"字样, 见 logo_dot)
    "logo_dot": "·",                           # logo 里的分隔符
    "logo_suffix": "市集",                     # logo 末尾
    # 以下两段本次按要求删除 → 留空即不显示。想恢复就填文字。
    "tagline": "",                             # 顶部副标题(留空不显示)
    "footer_main": "",                         # 页脚主文案(留空不显示)
    "footer_sub": "",                          # 页脚副文案(留空不显示)
    # CSS 变量自定义(明暗主题配色)
    "colors": {
        "light": {"ink": "#000000", "paper": "#ffffff", "gray": "#666666", "light": "#efefef", "line": "#c8c8c8"},
        "dark":  {"ink": "#ffffff", "paper": "#101010", "gray": "#9a9a9a", "light": "#1c1c1c", "line": "#3a3a3a"},
    },
    # 暗色模式: auto=跟随系统; light=默认浅色; dark=默认深色
    "dark_default": "auto",
}
DEFAULT_PRODUCTS = [
    {
        "name": "复古机械键盘",
        "price": "¥ 299",
        "cat": "桌面 / 数码",
        "desc": "87 键茶轴，铝合金外壳，黑白撞色键帽。9 成新，功能完好，含原装数据线。",
        "img": "",
        "buy": "https://www.facebook.com/marketplace/",
    },
    {
        "name": "羊毛针织开衫",
        "price": "¥ 159",
        "cat": "服饰 / 毛衣",
        "desc": "米灰混色羊毛混纺，M 码宽松版型，秋冬厚实保暖。无起球无瑕疵。",
        "img": "",
        "buy": "https://www.facebook.com/marketplace/",
    },
    {
        "name": "手冲咖啡套装",
        "price": "¥ 199",
        "cat": "居家 / 咖啡",
        "desc": "玻璃分享壶 + 树脂滤杯 + 20 张滤纸 + 电子秤。日常自用，导热均匀。",
        "img": "",
        "buy": "https://www.facebook.com/marketplace/",
    },
    {
        "name": "极简帆布托特包",
        "price": "¥ 89",
        "cat": "配饰 / 包包",
        "desc": "12 安厚帆布，黑底白字标语，内袋两个，可放 13 寸笔记本。全新未使用。",
        "img": "",
        "buy": "https://www.facebook.com/marketplace/",
    },
    {
        "name": "盆栽小绿植",
        "price": "¥ 45",
        "cat": "居家 / 桌面",
        "desc": "白瓷小花盆配多肉，养了半年状态很好，皮实好养，附送营养土。仅限自取。",
        "img": "",
        "buy": "https://www.facebook.com/marketplace/",
    },
    {
        "name": "皮质工装短靴",
        "price": "¥ 249",
        "cat": "服饰 / 鞋靴",
        "desc": "头层牛皮磨砂黑，41 码，厚底防滑。穿过一季，皮质完好。",
        "img": "",
        "buy": "https://www.facebook.com/marketplace/",
    },
]


# ------------------------- 数据读写 -------------------------
def load_products():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return list(DEFAULT_PRODUCTS)


def save_products(products):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def _deep_merge(base, extra):
    """把 extra 递归并进 base 的副本, 返回新 dict。"""
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_site():
    """读取 site.json, 覆盖到默认 SITE 之上。"""
    if os.path.exists(SITE_FILE):
        try:
            with open(SITE_FILE, "r", encoding="utf-8") as f:
                extra = json.load(f)
                if isinstance(extra, dict):
                    return _deep_merge(SITE, extra)
        except Exception:
            pass
    return SITE


def save_site(site):
    with open(SITE_FILE, "w", encoding="utf-8") as f:
        json.dump(site, f, ensure_ascii=False, indent=2)


def esc(s):
    """转义 HTML 且保留换行, 防止 XSS。"""
    return html.escape(s or "")


def esc_js(s):
    """转义一个 JS 字符串字面量(双引号包裹场景)。"""
    s = s or ""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


def esc_css_var(v):
    """清洗 CSS 变量值, 防止注入闭合/换行搞坏样式块。"""
    v = v or ""
    return re.sub(r"[{};\n\r]", "", v).strip()



def thumb_html(p):
    """返回缩略图 HTML。有图出 img，没图显示黑块占位。"""
    im = (p.get("img") or "").lstrip("./")
    if im and os.path.exists(os.path.join(IMAGES_DIR, os.path.basename(im))):
        return '<img src="%s" alt="%s" loading="lazy">' % (
            esc("/" + im), esc(p.get("name", "")))
    return '<div class="ph">◼</div>'


def verify_images(products):
    """把 img 字段规范成 images/ 下的相对路径, 并裁掉不符的。"""
    for p in products:
        im = p.get("img") or ""
        im = im.replace("\\", "/")
        name = os.path.basename(im)
        if name and os.path.exists(os.path.join(IMAGES_DIR, name)):
            p["img"] = "images/" + name
        else:
            p["img"] = ""
    return products


# ------------------------- 静态页渲染 -------------------------
@staticmethod
def _asset_escape(s):
    return json.dumps(s, ensure_ascii=False)


def render_index(products):
    cards = []
    for i, p in enumerate(products):
        cards.append(
            """            <button class="card" type="button" aria-label="查看 %s 简介" data-idx="%d">
              <div class="card-thumb">%s</div>
              <div class="card-name">%s</div>
              <div class="card-price">%s</div>
              <div class="card-cat">%s</div>
            </button>"""
            % (
                esc(p.get("name", "")),
                i,
                thumb_html(p),
                esc(p.get("name", "")),
                esc(p.get("price", "")),
                esc(p.get("cat", "")),
            )
        )
    if cards:
        cards_html = "\n".join(cards)
        empty_html = ""
    else:
        cards_html = ""
        empty_html = (
            '<section class="empty-state">'
            '<div class="empty-mark">［空］</div>'
            '<h2>暂时没有在售商品</h2>'
            '<p>本店当前暂无上架商品，敬请期待。'
            '<br>（商品上架前，本页显示空状态提示）</p>'
            '<p class="empty-hint">有新货想上架？店主可在管理后台添加。</p>'
            '</section>'
        )

    products_json = _asset_escape(products)

    # ---- 注入网站自定义 ----
    s = load_site()
    # 文案为空则不输出对应区块
    tag_html = ('<p class="tagline">%s</p>' % esc(s.get("tagline"))) if s.get("tagline") else ""
    foot_html = ("<p>%s</p>" % esc(s.get("footer_main"))) if s.get("footer_main") else ""
    foot_sub_html = ('<p class="footer-sub">%s</p>' % esc(s.get("footer_sub"))) if s.get("footer_sub") else ""

    c = s.get("colors", {})
    light, dark = c.get("light", {}), c.get("dark", {})
    # 构造明/暗两套 CSS 变量内联块(覆盖 style.css 中的 :root); 值经清洗防注入
    css = ":root{" + "".join("--%s:%s;" % (esc_css_var(k), esc_css_var(v)) for k, v in light.items()) + "}"
    css += "[data-theme=dark]{"
    css += "".join("--%s:%s;" % (esc_css_var(k), esc_css_var(v)) for k, v in dark.items()) + "}"
    # 主题默认: auto → 表示跟随系统。
    _def = "system" if s.get("dark_default", "auto") == "auto" else s.get("dark_default", "system")
    theme_js = (
        "var _t=localStorage.getItem('bw-theme')||'%s';"
        "document.documentElement.setAttribute('data-theme',"
        " _t==='system'? (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light') : _t);"
        "window.toggleTheme=function(){"
        " var e=document.documentElement.getAttribute('data-theme');"
        " var n=(e==='dark')?'light':'dark';"
        " document.documentElement.setAttribute('data-theme',n);"
        " localStorage.setItem('bw-theme',n);"
        "};"
    ) % esc_js(_def)

    index = INDEX_TEMPLATE.replace("/*__TITLE__*/", esc(s.get("title", "")))
    index = index.replace("/*__LOGO__*/", esc(s.get("logo", "")))
    index = index.replace("/*__LOGO_DOT__*/", esc(s.get("logo_dot", "")))
    index = index.replace("/*__LOGO_SUFFIX__*/", esc(s.get("logo_suffix", "")))
    index = index.replace("/*__TAGLINE__*/", tag_html)
    index = index.replace("/*__FOOTER_MAIN__*/", foot_html)
    index = index.replace("/*__FOOTER_SUB__*/", foot_sub_html)
    index = index.replace("/*__COLOR_CSS__*/", css)
    index = index.replace("/*__THEME_JS__*/", theme_js)
    index = index.replace("/*__CARDS__*/", cards_html)
    index = index.replace("/*__EMPTY__*/", empty_html)
    index = index.replace("/*__PRODUCTS_JSON__*/", products_json)
    return index


# ------------------------- HTTP 服务 -------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path in ("/", "/index.html"):
            self._send(200, render_index(load_products()))
        elif path == "/admin":
            self._send(200, self.admin_page())
        elif path == "/products.json":
            self._send(200, json.dumps(load_products(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/api/products":
            self._json(200, load_products())
        elif path == "/api/settings":
            self._json(200, load_site())
        elif path.startswith("/images/"):
            self._serve_image(path)
        else:
            # 预留 style.css / script.js 旧文件兼容
            fpath = os.path.join(HERE, path.lstrip("/"))
            if os.path.isfile(fpath):
                ctype, _ = mimetypes.guess_type(fpath)
                with open(fpath, "rb") as f:
                    self._send(200, f.read(), ctype or "application/octet-stream")
            else:
                self._send(404, "<h1>404 Not Found</h1>")

    def _serve_image(self, path):
        name = os.path.basename(path)
        img_path = os.path.join(IMAGES_DIR, name)
        if os.path.isfile(img_path):
            ctype, _ = mimetypes.guess_type(name)
            with open(img_path, "rb") as f:
                self._send(200, f.read(), ctype or "application/octet-stream")
        else:
            self._send(404, "<h1>image not found</h1>")

    # ---- API: 保存 ---- 
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/products":
            self._handle_save()
        elif path == "/api/settings":
            self._handle_settings_save()
        elif path == "/api/upload":
            self._handle_upload()
        else:
            self._send(404, "<h1>404</h1>")

    def _handle_settings_save(self):
        try:
            body = self._read_body().decode("utf-8")
            incoming = json.loads(body)
            if not isinstance(incoming, dict):
                raise ValueError("body must be an object")
            merged = _deep_merge(SITE, incoming)
            save_site(merged)
            # 同时覆盖 index.html 让设置生效
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                f.write(render_index(load_products()))
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_save(self):
        try:
            body = self._read_body().decode("utf-8")
            products = json.loads(body)
            if not isinstance(products, list):
                raise ValueError("body must be a list")
            products = verify_images(products)
            save_products(products)
            # 重写 index.html
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                f.write(render_index(products))
            self._json(200, {"ok": True, "count": len(products)})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_upload(self):
        """接收一个 JSON 对象 {filename, data(base64)} 存到 images/。"""
        try:
            body = self._read_body().decode("utf-8")
            payload = json.loads(body)
            raw = payload.get("data", "")
            if "," in raw and raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            data = base64.b64decode(raw)
            uploads = payload.get("filename", "image.png")
            name = os.path.basename(uploads) or "image.png"
            # 安全命名
            name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
            if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                name += ".png"
            os.makedirs(IMAGES_DIR, exist_ok=True)
            final = name
            i = 1
            while os.path.exists(os.path.join(IMAGES_DIR, final)):
                stem, ext = os.path.splitext(name)
                final = "%s_%d%s" % (stem, i, ext)
                i += 1
            final_path = os.path.join(IMAGES_DIR, final)
            with open(final_path, "wb") as f:
                f.write(data)
            self._json(200, {"ok": True, "img": "images/" + final})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def admin_page(self):
        products = load_products()
        page = ADMIN_TEMPLATE.replace("/*__PRODUCTS_JSON__*/", json.dumps(products, ensure_ascii=False))
        page = page.replace("/*__SITE_JSON__*/", json.dumps(load_site(), ensure_ascii=False))
        return page


# ------------------------- 模板字符串 -------------------------
INDEX_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>/*__TITLE__*/</title>
<link rel="stylesheet" href="style.css">
<style>
  /*__COLOR_CSS__*/
</style>
</head>
<body>
<script>
  /*__THEME_JS__*/
</script>

<header class="site-header">
  <div class="container header-inner">
    <a href="/" class="logo">/*__LOGO__*/<span class="logo-dot">/*__LOGO_DOT__*/</span>/*__LOGO_SUFFIX__*/</a>
    <button type="button" class="theme-toggle" onclick="window.toggleTheme()" aria-label="切换明暗" title="切换明暗模式">◐</button>
  </div>
  /*__TAGLINE__*/
</header>

<main class="container">
  <section class="hero">
    <h1>THINGS I SELL</h1>
    <p class="hero-sub">以下均为演示商品。点任一方格查看简介，喜欢就点「立即购买」，会跳转到 Facebook Marketplace。</p>
    <p class="hero-note">PURE STATIC · NO BACKEND</p>
  </section>

  <section id="grid" class="grid">
/*__CARDS__*/
  </section>
/*__EMPTY__*/
</main>

<footer class="site-footer">
  /*__FOOTER_MAIN__*/
  /*__FOOTER_SUB__*/
</footer>

<div id="modal" class="modal hidden" role="dialog" aria-modal="true" aria-labelledby="modal-title">
  <div class="modal-backdrop" data-close></div>
  <article class="modal-card">
    <button class="modal-close" data-close aria-label="关闭">&times;</button>
    <div class="modal-thumb" data-thumb></div>
    <h2 id="modal-title" data-title></h2>
    <p class="modal-price" data-price></p>
    <p class="modal-desc" data-desc></p>
    <a class="btn btn-buy" data-buy href="#" target="_blank" rel="noopener noreferrer">立即购买 · 前往 Marketplace →</a>
  </article>
</div>

<script>
const PRODUCTS = /*__PRODUCTS_JSON__*/;
// 卡片已由服务端渲染；JS 只负责弹窗。
(function () {
  const grid = document.getElementById('grid');
  const modal = document.getElementById('modal');
  const mThumb = modal.querySelector('[data-thumb]');
  const mTitle = modal.querySelector('[data-title]');
  const mPrice = modal.querySelector('[data-price]');
  const mDesc = modal.querySelector('[data-desc]');
  const mBuy = modal.querySelector('[data-buy]');

  function open(idx) {
    const p = PRODUCTS[idx] || {};
    mThumb.innerHTML = p.img
      ? '<img src="' + p.img + '" alt="" class="thumb-img">'
      : '<span>◼</span>';
    mTitle.textContent = p.name || '';
    mPrice.textContent = p.price || '';
    mDesc.textContent = p.desc || '暂无简介。';
    mBuy.href = p.buy || 'https://www.facebook.com/marketplace/';
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
  }
  window.closeModal = function () {
    modal.classList.add('hidden');
    document.body.style.overflow = '';
  };

  grid.addEventListener('click', function (e) {
    const card = e.target.closest('.card');
    if (card) open(parseInt(card.dataset.idx, 10));
  });
  modal.addEventListener('click', function (e) {
    if (e.target.hasAttribute('data-close')) window.closeModal();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') window.closeModal();
  });
})();
</script>
</body>
</html>
'''

ADMIN_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>黑白市集 · 管理后台</title>
<link rel="stylesheet" href="style.css">
<style>
/* —— 管理页专用版式 —— */
body.admin-body { padding: 24px; max-width: 1080px; margin: 0 auto; }
.toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; padding: 16px 0; border-bottom: 1px solid var(--ink); }
.toolbar h1 { font-size: 22px; font-weight: 900; letter-spacing: 2px; }
.toolbar .spacer { flex: 1; }
.muted { color: var(--gray); font-size: 12px; }
table { width: 100%; border-collapse: collapse; margin-top: 20px; }
th, td { border: 1px solid var(--ink); padding: 10px; text-align: left; font-size: 14px; vertical-align: middle; }
th { background: var(--ink); color: var(--paper); letter-spacing: 1px; }
.rowimg { width: 56px; height: 56px; object-fit: cover; border: 1px solid var(--ink); }
.btn { padding: 8px 12px; font-size: 13px; font-weight: 700; border: 1px solid var(--ink); background: var(--paper); cursor: pointer; }
.btn:hover { background: var(--ink); color: var(--paper); }
.btn-danger:hover { background: #000; }
.small { font-size: 12px; color: var(--gray); }
a.small { color: var(--ink); text-decoration: underline; }
#status { font-weight: 700; padding: 6px 10px; }
#status.ok { border: 1px solid var(--ink); }
#status.err { border: 1px solid #000; background: #000; color: #fff; }

/* 弹窗表单 */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: flex; align-items: center; justify-content: center; z-index: 50; padding: 16px; }
.overlay.hidden { display: none; }
form.panel { background: var(--paper); border: 1px solid var(--ink); max-width: 520px; width: 100%; padding: 24px; }
form.panel h2 { margin-bottom: 16px; }
label { display: block; font-weight: 700; margin: 12px 0 4px; font-size: 13px; }
input[type=text], input[type=url], textarea { width: 100%; border: 1px solid var(--ink); padding: 8px; font-size: 14px; font-family: inherit; }
textarea { resize: vertical; min-height: 80px; }
.form-row { display: flex; gap: 10px; }
#thumbPreview { max-width: 120px; max-height: 120px; border: 1px solid var(--ink); object-fit: contain; margin-top: 8px; display: none; }
.form-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; }
</style>
</head>
<body class="admin-body">

<div class="toolbar">
  <h1>黑白市集 · 管理后台</h1>
  <span class="spacer"></span>
  <span id="status" class="muted">就绪</span>
  <a class="btn" href="/" target="_blank">预览首页 →</a>
  <button class="btn" onclick="openSettings()">⚙ 网站设置</button>
  <button class="btn" onclick="addProduct()">＋ 新增商品</button>
</div>

<p class="muted">改动后自动重写 <code>index.html</code>。图片上传到 <code>images/</code> 文件夹。</p>

<table>
  <thead>
    <tr><th>图片</th><th>名称</th><th>价格</th><th>分类</th><th>简介</th><th>购买链接</th><th>操作</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>

<!-- 表单弹窗 -->
<div id="overlay" class="overlay hidden">
  <form id="form" class="panel" onsubmit="return save(event)">
    <h2 id="formTitle">编辑商品</h2>
    <input type="hidden" id="f_idx">
    <label>名称</label>
    <input type="text" id="f_name" required>
    <label>价格 <span class="muted">(例：¥ 299)</span></label>
    <input type="text" id="f_price">
    <label>分类</label>
    <input type="text" id="f_cat">
    <label>简介</label>
    <textarea id="f_desc"></textarea>
    <label>购买链接 (Facebook Marketplace 页)</label>
    <input type="url" id="f_buy" placeholder="https://www.facebook.com/marketplace/...">
    <label>商品图片</label>
    <div class="form-row">
      <input type="file" id="f_file" accept="image/*">
    </div>
    <img id="thumbPreview" alt="图片预览">
    <div class="form-actions">
      <button type="button" class="btn" onclick="hideForm()">取消</button>
      <button type="submit" class="btn">保存</button>
    </div>
  </form>
</div>

<!-- 网站设置弹窗 -->
<div id="settingsOverlay" class="overlay hidden">
  <form id="settingsForm" class="panel" onsubmit="return saveSettings(event)">
    <h2>网站设置</h2>
    <label>站点标题（浏览器标签）</label>
    <input type="text" id="s_title">
    <label>Logo 文字</label>
    <div class="form-row">
      <input type="text" id="s_logo" placeholder="左">
      <input type="text" id="s_logo_dot" placeholder="中(可留空)">
      <input type="text" id="s_logo_suffix" placeholder="右">
    </div>
    <label>顶部副标题 Tagline（留空则不显示）</label>
    <input type="text" id="s_tagline">
    <label>页脚主文案（留空则不显示）</label>
    <input type="text" id="s_footer_main">
    <label>页脚副文案（留空则不显示）</label>
    <input type="text" id="s_footer_sub">
    <label>默认配色主题</label>
    <div class="form-row">
      <select id="s_dark_default" style="padding:8px">
        <option value="auto">跟随系统 (auto)</option>
        <option value="light">浅色</option>
        <option value="dark">深色</option>
      </select>
    </div>
    <fieldset style="border:1px solid var(--ink);padding:10px;margin-top:12px">
      <legend class="muted">浅色模式配色</legend>
      <div class="form-row" style="flex-wrap:wrap">
        <label style="flex:1;min-width:120px">背景<input type="color" id="c_light_paper" style="width:100%"></label>
        <label style="flex:1;min-width:120px">文字<input type="color" id="c_light_ink" style="width:100%"></label>
        <label style="flex:1;min-width:120px">次要文字<input type="color" id="c_light_gray" style="width:100%"></label>
      </div>
    </fieldset>
    <fieldset style="border:1px solid var(--ink);padding:10px;margin-top:12px">
      <legend class="muted">深色模式配色</legend>
      <div class="form-row" style="flex-wrap:wrap">
        <label style="flex:1;min-width:120px">背景<input type="color" id="c_dark_paper" style="width:100%"></label>
        <label style="flex:1;min-width:120px">文字<input type="color" id="c_dark_ink" style="width:100%"></label>
        <label style="flex:1;min-width:120px">次要文字<input type="color" id="c_dark_gray" style="width:100%"></label>
      </div>
    </fieldset>
    <div class="form-actions">
      <button type="button" class="btn" onclick="hideSettings()">取消</button>
      <button type="submit" class="btn">保存设置</button>
    </div>
  </form>
</div>

<script>
/*__PRODUCTS_JSON__*/
/*__SITE_JSON__*/
let PRODUCTS = /*__PRODUCTS_JSON__*/;
let pendingImage = null;   // base64
let pendingFilename = null;

const rows = document.getElementById('rows');
const status = document.getElementById('status');

function renderRows() {
  rows.innerHTML = PRODUCTS.map((p, i) =>
    '<tr>' +
    '<td><img class="rowimg" src="' + (p.img || 'favicon.ico') + '" alt=""></td>' +
    '<td><b>' + p.name + '</b></td>' +
    '<td>' + p.price + '</td>' +
    '<td>' + p.cat + '</td>' +
    '<td class="small">' + (p.desc ? p.desc.substring(0, 30) : '') + '</td>' +
    '<td><a class="small" href="' + p.buy + '" target="_blank">打开</a></td>' +
    '<td>' +
      '<button class="btn" onclick="edit(' + i + ')">编辑</button> ' +
      '<button class="btn" onclick="move(' + i + ',-1)">↑</button> ' +
      '<button class="btn" onclick="move(' + i + ',1)">↓</button> ' +
      '<button class="btn btn-danger" onclick="del(' + i + ')">删</button>' +
    '</td>' +
    '</tr>'
  ).join('');
}

function setStatus(msg, ok) {
  status.textContent = msg;
  status.className = ok ? 'ok' : 'err';
}

/* 表单 */
function addProduct() {
  resetForm();
  document.getElementById('formTitle').textContent = '新增商品';
  showForm();
}
function edit(i) {
  const p = PRODUCTS[i];
  document.getElementById('f_idx').value = i;
  document.getElementById('f_name').value = p.name || '';
  document.getElementById('f_price').value = p.price || '';
  document.getElementById('f_cat').value = p.cat || '';
  document.getElementById('f_desc').value = p.desc || '';
  document.getElementById('f_buy').value = p.buy || '';
  pendingImage = null; pendingFilename = null;
  const prev = document.getElementById('thumbPreview');
  prev.style.display = p.img ? 'block' : 'none';
  prev.src = p.img || '';
  document.getElementById('formTitle').textContent = '编辑商品';
  showForm();
}
function resetForm() {
  document.getElementById('f_idx').value = '';
  document.getElementById('f_name').value = '';
  document.getElementById('f_price').value = '';
  document.getElementById('f_cat').value = '';
  document.getElementById('f_desc').value = '';
  document.getElementById('f_buy').value = 'https://www.facebook.com/marketplace/';
  document.getElementById('f_file').value = '';
  pendingImage = null; pendingFilename = null;
  document.getElementById('thumbPreview').style.display = 'none';
}
function showForm() { document.getElementById('overlay').classList.remove('hidden'); }
function hideForm() { document.getElementById('overlay').classList.add('hidden'); }

document.getElementById('f_file').addEventListener('change', function (e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function () {
    pendingImage = reader.result;        // data URL
    const dv = document.getElementById('thumbPreview');
    dv.src = pendingImage;
    dv.style.display = 'block';
  };
  reader.readAsDataURL(file);
});

/* 保存：先上传图（若有），再保存商品列表 */
async function save(ev) {
  ev.preventDefault();
  const idx = document.getElementById('f_idx').value;
  const item = idx === ''
    ? {}
    : Object.assign({}, PRODUCTS[parseInt(idx, 10)]);

  item.name = document.getElementById('f_name').value.trim();
  item.price = document.getElementById('f_price').value.trim();
  item.cat = document.getElementById('f_cat').value.trim();
  item.desc = document.getElementById('f_desc').value.trim();
  item.buy = document.getElementById('f_buy').value.trim() || 'https://www.facebook.com/marketplace/';

  try {
    if (pendingImage) {
      setStatus('上传图片…', true);
      const resp = await fetch('/api/upload', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: pendingFilename || 'image.jpg', data: pendingImage })
      });
      const j = await resp.json();
      if (!j.ok) throw new Error(j.error || '上传失败');
      item.img = j.img;
      pendingImage = null;
    }

    if (idx === '') {
      PRODUCTS.push(item);
    } else {
      PRODUCTS[parseInt(idx, 10)] = item;
    }

    setStatus('保存中…', true);
    const resp = await fetch('/api/products', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(PRODUCTS)
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || '保存失败');
    setStatus('已保存 · ' + j.count + ' 件商品', true);
    renderRows();
    hideForm();
  } catch (e) {
    setStatus('出错：' + e.message, false);
  }
}

function del(i) {
  if (!confirm('确定删除「' + PRODUCTS[i].name + '」？')) return;
  PRODUCTS.splice(i, 1);
  saveList();
}
function move(i, d) {
  const j = i + d;
  if (j < 0 || j >= PRODUCTS.length) return;
  const tmp = PRODUCTS[i];
  PRODUCTS[i] = PRODUCTS[j];
  PRODUCTS[j] = tmp;
  saveList();
}
async function saveList() {
  setStatus('保存中…', true);
  try {
    const resp = await fetch('/api/products', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(PRODUCTS)
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || '保存失败');
    setStatus('已保存 · ' + j.count + ' 件商品', true);
    renderRows();
  } catch (e) {
    setStatus('出错：' + e.message, false);
  }
}

/* ============ 网站设置 ============ */
const SITE_DEFAULT = /*__SITE_JSON__*/;

function openSettings() {
  const s = (typeof SITE !== 'undefined' && SITE) ? SITE : SITE_DEFAULT;
  document.getElementById('s_title').value = s.title || '';
  document.getElementById('s_logo').value = s.logo || '';
  document.getElementById('s_logo_dot').value = s.logo_dot || '';
  document.getElementById('s_logo_suffix').value = s.logo_suffix || '';
  document.getElementById('s_tagline').value = s.tagline || '';
  document.getElementById('s_footer_main').value = s.footer_main || '';
  document.getElementById('s_footer_sub').value = s.footer_sub || '';
  document.getElementById('s_dark_default').value = s.dark_default || 'auto';
  const l = (s.colors || {}).light || {}, d = (s.colors || {}).dark || {};
  document.getElementById('c_light_paper').value = l.paper || '#ffffff';
  document.getElementById('c_light_ink').value = l.ink || '#000000';
  document.getElementById('c_light_gray').value = l.gray || '#666666';
  document.getElementById('c_dark_paper').value = d.paper || '#101010';
  document.getElementById('c_dark_ink').value = d.ink || '#ffffff';
  document.getElementById('c_dark_gray').value = d.gray || '#9a9a9a';
  document.getElementById('settingsOverlay').classList.remove('hidden');
}
function hideSettings() {
  document.getElementById('settingsOverlay').classList.add('hidden');
}

async function saveSettings(ev) {
  ev.preventDefault();
  const payload = {
    title: document.getElementById('s_title').value.trim(),
    logo: document.getElementById('s_logo').value.trim(),
    logo_dot: document.getElementById('s_logo_dot').value.trim(),
    logo_suffix: document.getElementById('s_logo_suffix').value.trim(),
    tagline: document.getElementById('s_tagline').value.trim(),
    footer_main: document.getElementById('s_footer_main').value.trim(),
    footer_sub: document.getElementById('s_footer_sub').value.trim(),
    dark_default: document.getElementById('s_dark_default').value,
    colors: {
      light: {
        paper: document.getElementById('c_light_paper').value,
        ink: document.getElementById('c_light_ink').value,
        gray: document.getElementById('c_light_gray').value,
      },
      dark: {
        paper: document.getElementById('c_dark_paper').value,
        ink: document.getElementById('c_dark_ink').value,
        gray: document.getElementById('c_dark_gray').value,
      }
    }
  };
  setStatus('保存设置…', true);
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || '保存失败');
    setStatus('网站设置已保存，首页已更新', true);
    hideSettings();
  } catch (e) {
    setStatus('出错：' + e.message, false);
  }
}

renderRows();
</script>
</body>
</html>
'''


def open_browser():
    try:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open("http://127.0.0.1:%d/admin" % PORT)).start()
    except Exception:
        pass


def main():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    if not os.path.exists(DATA_FILE):
        save_products(list(DEFAULT_PRODUCTS))
    # 初始生成 index.html
    if not os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            f.write(render_index(load_products()))

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("=" * 46)
    print("  黑白市集 · 管理后台 已启动")
    print("  管理后台: http://127.0.0.1:%d/admin" % PORT)
    print("  首页预览: http://127.0.0.1:%d/" % PORT)
    print("  按 Ctrl+C 停止")
    print("=" * 46)
    open_browser()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黑白极简 · 商品管理 Server
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
import time
import html
import threading
import base64
import mimetypes
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "products.json")
SITE_FILE = os.path.join(HERE, "site.json")
IMAGES_DIR = os.path.join(HERE, "images")
INDEX_FILE = os.path.join(HERE, "index.html")

PORT = int(os.environ.get("BWMARKET_PORT", "8000"))
EMOJI_FALLBACK = "◼"

# ---- 站点配置(保存后覆盖 index.html) ----
# 首页面向访客(英文), 后台面向本地管理(中文)。
SITE = {
    "title": "My Store · Black & White Store",   # 站点标题
    "logo": "My Store",                          # 左上角店名
    "logo_dot": "·",                             # 分隔符
    "logo_suffix": "Store",                      # 店名后缀
    "tagline": "",                               # 顶部副标题 (空则不显示)
    "footer_main": "",                           # 页脚主文案 (空则不显示)
    "footer_sub": "",                            # 页脚副文案 (空则不显示)
    # 首页 hero 文案
    "hero_title": "Things I Sell",               # 大标题
    "hero_sub": "Click a card to view details. Press BUY to jump over to Facebook Marketplace.",
    "hero_note": "JUMPS DIRECTLY TO FACEBOOK MARKETPLACE",
    # 购买按钮全局默认文案; 单个商品可单独覆盖
    "buy_default": "BUY NOW · GO TO FACEBOOK MARKETPLACE →",
    # 配色 (CSS 变量)
    "colors": {
        "light": {"ink": "#000000", "paper": "#ffffff", "gray": "#666666", "light": "#efefef", "line": "#c8c8c8"},
        "dark":  {"ink": "#ffffff", "paper": "#101010", "gray": "#9a9a9a", "light": "#1c1c1c", "line": "#3a3a3a"},
    },
    "dark_default": "auto",                      # auto=跟随系统; light/dark 固定
}

# 无内置商品: 首次启动保持空列表
DEFAULT_PRODUCTS = []


# ---- 自动 Git 发布 ----
# 保存后自动 git add/commit/push, 让 GitHub Pages 实时更新(零后端/数据库)。
# remote/branch 按你仓库设置改; commit_msg_prefix 用于区分改动来源。
GIT = {
    "enabled": True,          # 关闭则保存后只写本地文件, 不提交
    "push": True,             # True=commit 后还会 push; False=只 commit
    "commit_prefix": "chore(shop): ",   # 提交信息前缀
    "branch": "main",         # 当前工作分支
    "remote_url": "",         # 远程仓库地址(可空, 由 setup/设置写入)
}
# 有子目录限制时用 (如只提交本站目录), 留空则整个仓库。
GIT_SUBPATH = ""

# 首次使用标记: 完成 setup 后写入
SETUP_FLAG_FILE = os.path.join(HERE, "setup.json")


def load_git():
    """读取站点配置里的 git 块, 覆盖到默认 GIT 之上。"""
    try:
        extra = load_site().get("git") or {}
    except Exception:
        extra = {}
    merged = _deep_merge(GIT, extra)
    return merged


def setup_done():
    return os.path.exists(SETUP_FLAG_FILE)


def mark_setup_done():
    with open(SETUP_FLAG_FILE, "w", encoding="utf-8") as f:
        json.dump({"done": True, "ts": int(time.time())}, f)


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


# ------------------------- 自动 Git 发布 -------------------------
def run_git(args, timeout=30):
    """执行 git 命令, 返回 (ok, output)"""
    try:
        p = subprocess.run(
            ["git"] + args,
            cwd=HERE, capture_output=True, text=True, timeout=timeout,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode == 0, out.strip()
    except Exception as e:
        return False, str(e)


def git_has_changes():
    ok, _ = run_git(["status", "--porcelain"])
    return ok


def git_commit_push(message):
    """自动 add / commit / (push)。返回 (ok, 说明)。"""
    g = load_git()
    if not g.get("enabled"):
        return False, "git 自动提交已关闭 (enabled=false)"

    # 1) add
    if GIT_SUBPATH:
        ok, out = run_git(["add", "-A", "--", GIT_SUBPATH])
    else:
        ok, out = run_git(["add", "-A"])
    if not ok:
        return False, "git add 失败: " + out

    # 2) 无改动则跳过
    ok, changed = run_git(["status", "--porcelain"])
    if not ok:
        return False, "git status 失败: " + changed
    if not changed.strip():
        return False, "没有任何改动, 已跳过提交"

    # 3) commit
    prefix = g.get("commit_prefix", "")
    msg = prefix + (message or "update")
    ok, out = run_git(["commit", "-m", msg])
    if not ok:
        # 常见: 没有配置 identity
        if "user.name" in out or "user.email" in out:
            return False, "git 未配置 user.name/user.email, 全局先 `git config --global user.name ...`"
        return False, "git commit 失败: " + out
    commit_hash = out.strip().splitlines()[-1] if out.strip() else ""

    # 4) push (可选)
    if g.get("push", True):
        branch = g.get("branch", "main")
        # 若配置了 remote_url 且尚未与 origin 关联, 先 set-url
        remote_url = g.get("remote_url", "")
        if remote_url:
            ok, out = run_git(["remote", "get-url", "origin"])
            if not ok or (ok and out.strip() != remote_url.strip()):
                run_git(["remote", "set-url", "origin", remote_url.strip()])
        ok, out = run_git(["push", "origin", branch])
        if not ok:
            return False, "commit 成功但 push 失败: " + out
        return True, "已 commit + push: " + message
    return True, "已 commit (未 push): " + message


# 后台异步发布: 记录最近一次结果, 供前端轮询展示
_last_push = {"running": False, "ts": 0, "ok": None, "msg": ""}
_push_lock = threading.Lock()


def start_async_push(message):
    """在后台线程执行 git_commit_push, 立即返回状态."""
    global _last_push
    with _push_lock:
        if _last_push.get("running"):
            return False, "已有发布任务进行中, 请稍候"
        _last_push = {"running": True, "ts": int(time.time()), "ok": None, "msg": "发布中…"}
    def _run():
        try:
            ok, msg = git_commit_push(message)
        except Exception as e:
            ok, msg = False, "发布出错: " + str(e)
        with _push_lock:
            _last_push["running"] = False
            _last_push["ok"] = ok
            _last_push["msg"] = msg
    threading.Thread(target=_run, daemon=True).start()
    return True, "已开始发布(后台执行), 稍后刷新即可看到结果"


def push_status():
    """返回后台发布任务状态."""
    with _push_lock:
        return dict(_last_push)


def git_status():
    """返回 git 环境信息, 供 setup/设置页展示。"""
    g = load_git()
    is_repo = os.path.isdir(os.path.join(HERE, ".git"))
    ok, remote = run_git(["remote", "get-url", "origin"])
    remote_url = remote if ok else g.get("remote_url", "")
    ok2, branch = run_git(["branch", "--show-current"])
    return {
        "is_repo": is_repo,
        "remote_url": remote_url.strip(),
        "branch": branch.strip() if ok2 else g.get("branch", "main"),
        "auto_enabled": g.get("enabled", True),
        "auto_push": g.get("push", True),
        "commit_prefix": g.get("commit_prefix", ""),
    }


def _host_from_url(url):
    """从 https://host/... 或 git@host:... 里取出 host。"""
    url = (url or "").strip()
    if url.startswith("git@"):
        try:
            return url.split("@", 1)[1].split(":", 1)[0]
        except Exception:
            return "github.com"
    try:
        return urllib.parse.urlparse(url).hostname or "github.com"
    except Exception:
        return "github.com"


def git_store_credentials(user, token, remote_url):
    """把 HTTPS 凭据存入系统凭据管理器 / 本地凭据存储. 不写明文入库."""
    if not user or not token:
        return False, "用户名或 Token 为空"
    host = _host_from_url(remote_url)
    # 若本机没有任何 credential helper, 用 store (写入 .git-credentials, 已 gitignore)
    if not _has_credential_helper():
        run_git(["config", "--local", "credential.helper", "store"])
    payload = "protocol=https\nhost=%s\nusername=%s\npassword=%s\n\n" % (
        host, user.replace("\n", ""), token.replace("\n", ""))
    try:
        p = subprocess.run(
            ["git", "credential", "approve"],
            cwd=HERE, input=payload.encode("utf-8"),
            capture_output=True, timeout=20)
        if p.returncode != 0:
            return False, (p.stderr or "").strip() or "credential helper 失败"
        return True, "已保存凭据 (%s)，后续推送自动使用" % host
    except Exception as e:
        return False, str(e)


def _has_credential_helper():
    ok, out = run_git(["config", "--get-regexp", "credential.helper"])
    return ok and out.strip() != ""


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
            """            <button class="card" type="button" aria-label="View %s details" data-idx="%d">
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
            '<div class="empty-mark">EMPTY</div>'
            '<h2>Nothing is on sale yet</h2>'
            '<p>No items available at the moment. Please check back later.</p>'
            '<p class="empty-hint">New arrivals coming soon.</p>'
            '</section>'
        )

    products_json = _asset_escape(products)

    # ---- 注入网站自定义 ----
    s = load_site()
    # 文案为空则不输出对应区块
    tag_html = ('<p class="tagline">%s</p>' % esc(s.get("tagline"))) if s.get("tagline") else ""
    foot_html = ("<p>%s</p>" % esc(s.get("footer_main"))) if s.get("footer_main") else ""
    foot_sub_html = ('<p class="footer-sub">%s</p>' % esc(s.get("footer_sub"))) if s.get("footer_sub") else ""
    hero_title_html = esc(s.get("hero_title") or "Things I Sell")
    hero_sub_html = esc(s.get("hero_sub") or "")
    hero_note_html = esc(s.get("hero_note") or "")

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
    index = index.replace("/*__HERO_TITLE__*/", hero_title_html)
    index = index.replace("/*__HERO_SUB__*/", hero_sub_html)
    index = index.replace("/*__HERO_NOTE__*/", hero_note_html)
    index = index.replace("/*__COLOR_CSS__*/", css)
    index = index.replace("/*__THEME_JS__*/", theme_js)
    index = index.replace("/*__BUY_DEFAULT__*/", esc_js(s.get("buy_default") or "BUY NOW · GO TO FACEBOOK MARKETPLACE →"))
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
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端提前断开(如浏览器关闭/超时), 不报吓人的错
            self.close_connection = True
        except Exception:
            pass

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
        elif path == "/setup":
            self._send(200, self.setup_page())
        elif path == "/admin":
            # 首次未完成 setup → 引导到 setup
            if not setup_done():
                self._send(200, self.setup_page())
            else:
                self._send(200, self.admin_page())
        elif path == "/products.json":
            self._send(200, json.dumps(load_products(), ensure_ascii=False), "application/json; charset=utf-8")
        elif path == "/api/products":
            self._json(200, load_products())
        elif path == "/api/settings":
            self._json(200, load_site())
        elif path == "/api/git/status":
            st = git_status()
            st["push"] = push_status()
            self._json(200, st)
        elif path.startswith("/images/"):
            self._serve_image(path)
        else:
            # 静态文件直读 (style.css / 图片等)
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
        elif path == "/api/git/setup":
            self._handle_git_setup()
        elif path == "/api/git/auth":
            self._handle_git_auth()
        elif path == "/api/git/push":
            self._handle_git_push()
        elif path == "/api/git/set-url":
            self._handle_git_seturl()
        elif path == "/api/setup/complete":
            self._handle_setup_complete()
        elif path == "/api/upload":
            self._handle_upload()
        else:
            self._send(404, "<h1>404</h1>")

    # ---- Git 发布 & Setup ----
    def _handle_git_push(self):
        """触发后台异步发布, 立即返回, 不阻塞浏览器(避免超时/断连)。"""
        ok, msg = start_async_push("manual publish")
        self._json(200 if ok else 400, {"ok": ok, "msg": msg})

    def _handle_git_auth(self):
        """保存 HTTPS 凭据到系统凭据管理器 (不写明文入库)。"""
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            user = (body.get("user") or "").strip()
            token = (body.get("pass") or "").strip()
            remote_url = (body.get("remote_url") or "").strip()
            if not user or not token:
                self._json(400, {"ok": False, "error": "缺少用户名或 Token"})
                return
            ok, msg = git_store_credentials(user, token, remote_url)
            self._json(200 if ok else 400, {"ok": ok, "msg": msg, "error": msg if not ok else ""})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_git_setup(self):
        """执行 setup 里的 git 任务: (可选存凭据) 关联 remote。"""
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            remote_url = (body.get("remote_url") or "").strip()
            branch = (body.get("branch") or "main").strip()
            if not remote_url:
                self._json(400, {"ok": False, "error": "缺少远程仓库地址"})
                return
            # 0) 若提供了 user/pass 且是 HTTPS, 先存凭据
            user = (body.get("user") or "").strip()
            token = (body.get("pass") or "").strip()
            cred_note = ""
            if user and token and remote_url.startswith("https://"):
                okc, msgc = git_store_credentials(user, token, remote_url)
                cred_note = ("凭据: " + msgc + "; ") if okc else "凭据未保存: " + msgc + "; "
            # 1) 关联 remote
            ok, _ = run_git(["remote", "add", "origin", remote_url])
            if not ok:
                # origin 已存在则改地址
                ok, out = run_git(["remote", "set-url", "origin", remote_url])
                if not ok:
                    self._json(400, {"ok": False, "error": "设置 remote 失败: " + out})
                    return
            self._json(200, {"ok": True, "remote_url": remote_url, "branch": branch, "cred": cred_note})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_git_seturl(self):
        """设置页保存 git 配置: 更新 site.json 里的 git 块 + 关联 remote。"""
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            site = load_site()
            git = dict(site.get("git") or {})
            for k in ("enabled", "push", "commit_prefix", "branch", "remote_url"):
                if k in body:
                    git[k] = body[k]
            site["git"] = git
            save_site(site)
            # 若给了 remote_url, 同步关联本地 remote
            if git.get("remote_url"):
                ok, out = run_git(["remote", "get-url", "origin"])
                if not ok or (ok and out.strip() != git["remote_url"].strip()):
                    run_git(["remote", "add", "origin", git["remote_url"].strip()])
                    run_git(["remote", "set-url", "origin", git["remote_url"].strip()])
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_setup_complete(self):
        """完成首次 setup 标记。"""
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            mark_setup_done()
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def setup_page(self):
        return SETUP_TEMPLATE

    def _handle_settings_save(self):
        try:
            body = self._read_body().decode("utf-8")
            incoming = json.loads(body)
            if not isinstance(incoming, dict):
                raise ValueError("body must be an object")
            merged = _deep_merge(SITE, incoming)
            save_site(merged)
            # 若设置里给了 git remote, 先同步本地 remote
            git_merge = merged.get("git") or {}
            if git_merge.get("remote_url"):
                ru = str(git_merge["remote_url"]).strip()
                ok, out = run_git(["remote", "get-url", "origin"])
                if not ok or (ok and out.strip() != ru):
                    run_git(["remote", "add", "origin", ru])
                    run_git(["remote", "set-url", "origin", ru])
            # 同时覆盖 index.html 让设置生效
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                f.write(render_index(load_products()))
            _ok, _msg = start_async_push("site settings update")
            self._json(200, {"ok": True, "git": _ok, "git_msg": _msg})
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
            _ok, _msg = start_async_push("products update")
            self._json(200, {"ok": True, "count": len(products), "git": _ok, "git_msg": _msg})
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

# 首次设置向导(本地 server 专用, 不参与 GitHub Pages 部署)
SETUP_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>首次设置 · 内容发布系统</title>
<link rel="stylesheet" href="style.css">
<style>
  .setup-wrap { max-width: 560px; margin: 40px auto; padding: 0 20px; }
  .setup-card { border: 1px solid var(--ink); padding: 28px; }
  .setup-card h1 { font-size: 24px; font-weight: 900; letter-spacing: 1px; margin-bottom: 6px; }
  .setup-desc { color: var(--gray); font-size: 13px; margin-bottom: 20px; }
  .setup-card label { display: block; font-weight: 700; margin: 16px 0 6px; font-size: 13px; }
  .setup-card input[type=text], .setup-card input[type=url] { width: 100%; border: 1px solid var(--ink); padding: 10px; font-size: 14px; box-sizing: border-box; }
  .setup-card .hint { font-size: 12px; color: var(--gray); margin-top: 4px; }
  .setup-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 22px; flex-wrap: wrap; }
  .log { margin-top: 14px; border: 1px dashed var(--ink); padding: 10px; font-size: 12px; color: var(--gray); min-height: 20px; white-space: pre-wrap; }
  .step { display: inline-block; font-size: 11px; letter-spacing: 2px; padding: 2px 8px; background: var(--ink); color: var(--paper); margin-bottom: 10px; }
  .skip { color: var(--gray); font-size: 12px; }
</style>
</head>
<body>
<div class="setup-wrap">
  <div class="setup-card">
    <div class="step">首次设置</div>
    <h1>开始使用</h1>
    <p class="setup-desc">在本地编辑内容，每次保存自动发布到你自己的 GitHub Pages 站点。仓库需你先在 GitHub 上创建好。</p>

    <label>步骤 1 · GitHub 远程仓库地址</label>
    <input type="text" id="remote_url" placeholder="https://github.com/用户名/仓库名.git  或  git@github.com:用户名/仓库名.git">
    <p class="hint">先去 GitHub 新建一个空仓库，把它的 HTTPS 或 SSH 地址粘贴到这里。</p>

    <!-- 认证方式(根据地址自动切换) -->
    <div id="authBlock" style="display:none;margin-top:16px;border:1px dashed var(--ink);padding:14px">
      <div class="step" style="margin-bottom:8px">远程仓库认证</div>
      <label id="authModeLabel">HTTPS 需要用户名 + Token 才能推送</label>
      <input type="text" id="auth_user" placeholder="GitHub 用户名">
      <input type="password" id="auth_token" placeholder="Personal Access Token (PAT)" style="margin-top:10px">
      <p class="hint">GitHub 已不支持密码推送，请用 Personal Access Token（Settings → Developer settings → Personal access tokens，勾选 <b>repo</b> 权限）。凭据会安全存入系统凭据管理器，下次自动记住。</p>
      <div class="setup-actions">
        <button class="btn" id="btnAuth">验证并保存凭据</button>
      </div>
    </div>

    <div class="setup-actions">
      <button class="btn" id="btnConnect">连接仓库</button>
      <button class="btn" id="btnPush" disabled>推送发布内容</button>
    </div>
    <div class="log" id="log">等待连接…</div>

    <div class="setup-actions" style="margin-top:16px">
      <a class="skip" href="/admin">跳过，直接进后台</a>
      <button class="btn" id="btnFinish">完成并进入后台</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
function log(msg){ $('log').textContent = msg; }
const btnC=$('btnConnect'), btnP=$('btnPush'), btnF=$('btnFinish');

// 根据地址自动判断认证方式
$('remote_url').addEventListener('input', () => {
  const url = $('remote_url').value.trim();
  const auth = $('authBlock');
  if (url.startsWith('https://')) {
    auth.style.display = 'block';
    $('authModeLabel').textContent = 'HTTPS 需要用户名 + Token 才能推送';
  } else if (url.startsWith('git@') || url.startsWith('ssh')) {
    auth.style.display = 'block';
    $('authModeLabel').textContent = 'SSH 方式：使用本机 SSH key，无需在浏览器填用户名/密码';
    // SSH 不需要 token 输入, 但保留提示
  } else if (url) {
    auth.style.display = 'block';
    $('authModeLabel').textContent = '未知协议，请确认地址是否为 https:// 或 git@ssh';
  } else {
    auth.style.display = 'none';
  }
});

// 验证并保存凭据(存入系统凭据管理器)
$('btnAuth').onclick = async () => {
  const url = $('remote_url').value.trim();
  const user = $('auth_user').value.trim();
  const token = $('auth_token').value.trim();
  if(!url || !user || !token){ log('请填写 用户名 + Token（SSH 方式则无需填写，直接连接即可）'); return; }
  log('正在保存凭据到系统…');
  try{
    const r = await fetch('/api/git/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({remote_url:url,user:user,pass:token})});
    const j = await r.json();
    if(!j.ok){ log('凭据保存失败: '+(j.error||'')); return; }
    log('✓ 凭据已保存（SSH 方式可跳过此步）。现在点「连接仓库」。');
  }catch(e){ log('保存出错: '+e.message); }
};

btnC.onclick = async () => {
  const url = $('remote_url').value.trim();
  if(!url){ log('请先粘贴远程仓库地址'); return; }
  log('正在关联远程仓库…');
  try{
    const r = await fetch('/api/git/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({remote_url:url,branch:'main',user:$('auth_user').value.trim(),pass:$('auth_token').value.trim()})});
    const j = await r.json();
    if(!j.ok){ log('失败: '+(j.error||'')); return; }
    log('已关联远程仓库: '+j.remote_url);
    btnP.disabled=false;
  }catch(e){ log('连接出错: '+e.message); }
};

btnP.onclick = async () => {
  log('正在提交并推送…');
  try{
    const r = await fetch('/api/git/push',{method:'POST'});
    const j = await r.json();
    log(j.ok?('✓ '+j.msg):('推送失败: '+j.msg));
  }catch(e){ log('推送出错: '+e.message); }
};

btnF.onclick = async () => {
  // 完成 setup(站点信息之后可在后台设置里改)
  try{
    await fetch('/api/setup/complete',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  }catch(e){}
  location.href='/admin';
};
</script>
</body>
</html>'''

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
    <button type="button" class="theme-toggle" onclick="window.toggleTheme()" aria-label="Toggle dark mode" title="Toggle dark / light">◐</button>
  </div>
  /*__TAGLINE__*/
</header>

<main class="container">
  <section class="hero">
    <h1>/*__HERO_TITLE__*/</h1>
    <p class="hero-sub">/*__HERO_SUB__*/</p>
    <p class="hero-note">/*__HERO_NOTE__*/</p>
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
    <a class="btn btn-buy" data-buy href="#" target="_blank" rel="noopener noreferrer">BUY NOW · GO TO FACEBOOK MARKETPLACE →</a>
  </article>
</div>

<script>
const PRODUCTS = /*__PRODUCTS_JSON__*/;
const BUY_DEFAULT = "/*__BUY_DEFAULT__*/";
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
    mDesc.textContent = p.desc || 'No description yet.';
    mBuy.href = p.buy || 'https://www.facebook.com/marketplace/';
    mBuy.textContent = p.buy_text || BUY_DEFAULT;
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
<title>商品管理后台</title>
<link rel="stylesheet" href="style.css">
<style>
/* —— 管理页专用版式 —— */
body.admin-body { padding: 20px; max-width: 1200px; margin: 0 auto; overflow-x: hidden; }
.toolbar {
  display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
  padding: 16px 0; border-bottom: 1px solid var(--ink); box-sizing: border-box;
}
.toolbar-title {
  display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; min-width: 0;
}
.toolbar-title h1 { font-size: 20px; font-weight: 900; letter-spacing: 1px; white-space: normal; }
.toolbar-actions {
  display: flex; flex-wrap: wrap; gap: 10px; margin-left: auto; min-width: 0;
}
.toolbar .btn { white-space: nowrap; }
.toolbar .btn {
  flex: 0 0 auto;
}
.muted { color: var(--gray); font-size: 12px; }
.table-wrap { overflow-x: auto; width: 100%; }
.table-wrap table { min-width: 760px; }
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
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: flex; align-items: center; justify-content: center; z-index: 50; padding: 16px; box-sizing: border-box; overflow-y: auto; }
.overlay.hidden { display: none; }
form.panel { background: var(--paper); border: 1px solid var(--ink); max-width: 520px; width: 100%; padding: 24px; box-sizing: border-box; overflow-y: auto; max-height: calc(100vh - 32px); }
form.panel h2 { margin-bottom: 16px; }
form.panel fieldset { min-width: 0; border: 1px solid var(--ink); padding: 10px; margin-top: 12px; }
form.panel legend { padding: 0 6px; }
form.panel .form-row > * { min-width: 0; }
label { display: block; font-weight: 700; margin: 12px 0 4px; font-size: 13px; }
input[type=text], input[type=url], textarea { width: 100%; border: 1px solid var(--ink); padding: 8px; font-size: 14px; font-family: inherit; box-sizing: border-box; }
input[type=color] { width: 100%; height: 36px; border: 1px solid var(--ink); padding: 0; box-sizing: border-box; cursor: pointer; }
textarea { resize: vertical; min-height: 80px; }
.form-row { display: flex; gap: 10px; flex-wrap: wrap; }
.color-item { flex: 1 1 130px; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.color-item span { font-size: 12px; font-weight: 700; }
#thumbPreview { max-width: 120px; max-height: 120px; border: 1px solid var(--ink); object-fit: contain; margin-top: 8px; display: none; }
.form-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; flex-wrap: wrap; }
</style>
</head>
<body class="admin-body">

<div class="toolbar">
  <div class="toolbar-title">
    <h1 data-i18n="title">商品管理后台</h1>
    <span id="status" class="muted">就绪</span>
  </div>
  <div class="toolbar-actions">
    <button class="btn" id="langToggle" onclick="toggleLang()" title="语言 / Language">EN</button>
    <a class="btn" href="/" target="_blank" data-i18n="preview">预览首页 →</a>
    <button class="btn" onclick="openSettings()" data-i18n="settings">⚙ 网站设置</button>
    <button class="btn" onclick="addProduct()" data-i18n="addItem">＋ 新增商品</button>
  </div>
</div>

<p class="muted" data-i18n="tip">改动后自动重写 <code>index.html</code>。图片上传到 <code>images/</code> 文件夹。</p>

<div class="table-wrap">
<table>
  <thead>
    <tr><th data-i18n="thImg">图片</th><th data-i18n="thName">名称</th><th data-i18n="thPrice">价格</th><th data-i18n="thCat">分类</th><th data-i18n="thDesc">简介</th><th data-i18n="thLink">购买链接</th><th data-i18n="thOp">操作</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
</div>

<!-- 表单弹窗 -->
<div id="overlay" class="overlay hidden">
  <form id="form" class="panel" onsubmit="return save(event)">
    <h2 id="formTitle" data-i18n="editItem">编辑商品</h2>
    <input type="hidden" id="f_idx">
    <label data-i18n="lblName">名称</label>
    <input type="text" id="f_name" required>
    <label data-i18n="lblPrice">价格 <span class="muted">(例：¥ 299)</span></label>
    <input type="text" id="f_price">
    <label data-i18n="lblCat">分类</label>
    <input type="text" id="f_cat">
    <label data-i18n="lblDesc">简介</label>
    <textarea id="f_desc"></textarea>
    <label data-i18n="lblBuyLink">购买链接 (Facebook Marketplace 页)</label>
    <input type="url" id="f_buy" placeholder="https://www.facebook.com/marketplace/...">
    <label data-i18n="lblBuyText">购买按钮文案（留空用全局默认）</label>
    <input type="text" id="f_buy_text" placeholder="留空则用网站设置的全局默认">
    <label data-i18n="lblImg">商品图片</label>
    <div class="form-row">
      <input type="file" id="f_file" accept="image/*">
    </div>
    <img id="thumbPreview" alt="图片预览">
    <div class="form-actions">
      <button type="button" class="btn" onclick="hideForm()" data-i18n="btnCancel">取消</button>
      <button type="submit" class="btn" data-i18n="btnSave">保存</button>
    </div>
  </form>
</div>

<!-- 网站设置弹窗 -->
<div id="settingsOverlay" class="overlay hidden">
  <form id="settingsForm" class="panel" onsubmit="return saveSettings(event)">
    <h2 data-i18n="settingsTitle">网站设置</h2>
    <label data-i18n="lblSiteTitle">站点标题（浏览器标签）</label>
    <input type="text" id="s_title">
    <label data-i18n="lblLogo">Logo 文字</label>
    <div class="form-row">
      <input type="text" id="s_logo" placeholder="左">
      <input type="text" id="s_logo_dot" placeholder="中(可留空)">
      <input type="text" id="s_logo_suffix" placeholder="右">
    </div>
    <label data-i18n="lblTagline">顶部副标题 Tagline（留空则不显示）</label>
    <input type="text" id="s_tagline">
    <label data-i18n="lblHeroTitle">首页大标题</label>
    <input type="text" id="s_hero_title">
    <label data-i18n="lblHeroSub">首页副标题说明</label>
    <input type="text" id="s_hero_sub">
    <label data-i18n="lblHeroNote">首页小徽标</label>
    <input type="text" id="s_hero_note">
    <label data-i18n="lblBuyDefault">购买按钮全局默认文案（没单独设的商品用这个）</label>
    <input type="text" id="s_buy_default" placeholder="如: BUY NOW · GO TO FB MARKETPLACE">
    <label data-i18n="lblFooterMain">页脚主文案（留空则不显示）</label>
    <input type="text" id="s_footer_main">
    <label data-i18n="lblFooterSub">页脚副文案（留空则不显示）</label>
    <input type="text" id="s_footer_sub">
    <label data-i18n="lblTheme">默认配色主题</label>
    <div class="form-row">
      <select id="s_dark_default" style="padding:8px">
        <option value="auto" data-i18n="optAuto">跟随系统 (auto)</option>
        <option value="light" data-i18n="optLight">浅色</option>
        <option value="dark" data-i18n="optDark">深色</option>
      </select>
    </div>
    <fieldset>
      <legend class="muted" data-i18n="legendLight">浅色模式配色</legend>
      <div class="form-row">
        <label class="color-item"><span data-i18n="spBg">背景</span><input type="color" id="c_light_paper"></label>
        <label class="color-item"><span data-i18n="spText">文字</span><input type="color" id="c_light_ink"></label>
        <label class="color-item"><span data-i18n="spSub">次要文字</span><input type="color" id="c_light_gray"></label>
      </div>
    </fieldset>
    <fieldset>
      <legend class="muted" data-i18n="legendDark">深色模式配色</legend>
      <div class="form-row">
        <label class="color-item"><span data-i18n="spBg">背景</span><input type="color" id="c_dark_paper"></label>
        <label class="color-item"><span data-i18n="spText">文字</span><input type="color" id="c_dark_ink"></label>
        <label class="color-item"><span data-i18n="spSub">次要文字</span><input type="color" id="c_dark_gray"></label>
      </div>
    </fieldset>
    <fieldset>
      <legend class="muted" data-i18n="legendGit">Git 自动发布</legend>
      <label data-i18n="lblGitRemote">远程仓库地址 (GitHub)</label>
      <input type="url" id="s_git_remote" placeholder="https://github.com/用户名/仓库名.git">
      <div class="form-row">
        <label style="flex:1"><span class="muted" style="font-size:11px" data-i18n="spBranch">分支</span><input type="text" id="s_git_branch" value="main"></label>
        <label style="flex:1"><span class="muted" style="font-size:11px" data-i18n="spPrefix">提交前缀</span><input type="text" id="s_git_prefix" value="chore(shop): "></label>
      </div>
      <div class="form-row" style="align-items:center;margin-top:8px">
        <label style="display:flex;align-items:center;gap:6px;font-weight:600;margin:0"><input type="checkbox" id="s_git_enabled" checked> <span data-i18n="cbAutoCommit">保存后自动提交</span></label>
        <label style="display:flex;align-items:center;gap:6px;font-weight:600;margin:0"><input type="checkbox" id="s_git_push" checked> <span data-i18n="cbAutoPush">自动 push</span></label>
        <button type="button" class="btn" onclick="publishNow()" style="margin-left:auto" data-i18n="btnPublishNow">立即发布</button>
      </div>
      <p class="muted" id="gitStatusHint" style="margin-top:8px">— 远程仓库未配置 —</p>
    </fieldset>
    <div class="form-actions">
      <button type="button" class="btn" onclick="hideSettings()" data-i18n="btnCancel">取消</button>
      <button type="submit" class="btn" data-i18n="btnSaveSettings">保存设置</button>
    </div>
  </form>
</div>

<script>
let PRODUCTS = /*__PRODUCTS_JSON__*/;
let SITE_DEFAULT = /*__SITE_JSON__*/;
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
    '<td><a class="small" href="' + p.buy + '" target="_blank">' + (I18N[LANG].openLink || '打开') + '</a></td>' +
    '<td>' +
      '<button class="btn" onclick="edit(' + i + ')">' + I18N[LANG].rowEdit + '</button> ' +
      '<button class="btn" onclick="move(' + i + ',-1)">↑</button> ' +
      '<button class="btn" onclick="move(' + i + ',1)">↓</button> ' +
      '<button class="btn btn-danger" onclick="del(' + i + ')">' + I18N[LANG].rowDel + '</button>' +
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
  document.getElementById('formTitle').textContent = (I18N[LANG].addProductTitle || '新增商品');
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
  document.getElementById('f_buy_text').value = p.buy_text || '';
  pendingImage = null; pendingFilename = null;
  const prev = document.getElementById('thumbPreview');
  prev.style.display = p.img ? 'block' : 'none';
  prev.src = p.img || '';
  document.getElementById('formTitle').textContent = (I18N[LANG].editItem || '编辑商品');
  showForm();
}
function resetForm() {
  document.getElementById('f_idx').value = '';
  document.getElementById('f_name').value = '';
  document.getElementById('f_price').value = '';
  document.getElementById('f_cat').value = '';
  document.getElementById('f_desc').value = '';
  document.getElementById('f_buy').value = 'https://www.facebook.com/marketplace/';
  document.getElementById('f_buy_text').value = '';
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
  item.buy_text = document.getElementById('f_buy_text').value.trim();

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

    setStatus(I18N[LANG].saving, true);
    const resp = await fetch('/api/products', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(PRODUCTS)
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || '保存失败');
    setStatus(I18N[LANG].statusSaved.replace('{n}', j.count) + (j.git ? I18N[LANG].gitPublished : I18N[LANG].notPushed + (j.git_msg||'')), true);
    renderRows();
    hideForm();
  } catch (e) {
    setStatus((I18N[LANG].err||'出错：') + e.message, false);
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
  setStatus(I18N[LANG].saving, true);
  try {
    const resp = await fetch('/api/products', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(PRODUCTS)
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || '保存失败');
    setStatus(I18N[LANG].statusSaved.replace('{n}', j.count) + (j.git ? I18N[LANG].gitPublished : I18N[LANG].notPushed + (j.git_msg||'')), true);
    renderRows();
  } catch (e) {
    setStatus((I18N[LANG].err||'出错：') + e.message, false);
  }
}

/* ============ 网站设置 ============ */

function openSettings() {
  const s = SITE_DEFAULT;
  document.getElementById('s_title').value = s.title || '';
  document.getElementById('s_logo').value = s.logo || '';
  document.getElementById('s_logo_dot').value = s.logo_dot || '';
  document.getElementById('s_logo_suffix').value = s.logo_suffix || '';
  document.getElementById('s_tagline').value = s.tagline || '';
  document.getElementById('s_hero_title').value = s.hero_title || '';
  document.getElementById('s_hero_sub').value = s.hero_sub || '';
  document.getElementById('s_hero_note').value = s.hero_note || '';
  document.getElementById('s_buy_default').value = s.buy_default || '';
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
  // git 设置
  const g = (s.git || {});
  document.getElementById('s_git_remote').value = g.remote_url || '';
  document.getElementById('s_git_branch').value = g.branch || 'main';
  document.getElementById('s_git_prefix').value = g.commit_prefix || 'chore(shop): ';
  document.getElementById('s_git_enabled').checked = (g.enabled !== false);
  document.getElementById('s_git_push').checked = (g.push !== false);
  document.getElementById('settingsOverlay').classList.remove('hidden');
  loadGitStatus();
}
function hideSettings() {
  document.getElementById('settingsOverlay').classList.add('hidden');
}

async function loadGitStatus() {
  try {
    const r = await fetch('/api/git/status');
    const j = await r.json();
    const el = document.getElementById('gitStatusHint');
    if (j && j.is_repo) {
      el.textContent = '本地仓库: ✓  远程: ' + (j.remote_url || '未设置') + '  分支: ' + j.branch;
    } else {
      el.textContent = '未初始化 Git 仓库，请在 setup 或设置里配置远程地址。';
    }
  } catch (e) {
    document.getElementById('gitStatusHint').textContent = '读取 git 状态失败';
  }
}

async function publishNow() {
  const btn = event.target;
  btn.textContent = (LANG==='zh') ? '发布中…' : 'Publishing…';
  try {
    const r = await fetch('/api/git/push', { method: 'POST' });
    const j = await r.json();
    if (j.ok) {
      setStatus((LANG==='zh'?'已开始发布，请稍候…':'Publishing started…'), true);
    } else {
      setStatus((LANG==='zh'?'发布启动失败: ':'Could not start: ') + (j.msg||''), false);
    }
  } catch (e) {
    setStatus((LANG==='zh'?'发布出错: ':'Publish error: ') + e.message, false);
  }
  btn.textContent = (LANG==='zh') ? '立即发布' : 'Publish now';
  // 轮询后台任务结果
  (async () => {
    for (let i=0;i<30;i++) {
      await new Promise(res => setTimeout(res, 800));
      try {
        const r2 = await fetch('/api/git/status');
        const s = await r2.json();
        const p = (s && s.push) || {};
        if (!p.running) {
          setStatus((p.ok ? ((LANG==='zh'?'发布完成: ':'Published: ')+p.msg) : ((LANG==='zh'?'发布失败: ':'Publish failed: ')+p.msg)), !!p.ok);
          break;
        }
      } catch(e) { break; }
    }
    loadGitStatus();
  })();
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
    hero_title: document.getElementById('s_hero_title').value.trim(),
    hero_sub: document.getElementById('s_hero_sub').value.trim(),
    hero_note: document.getElementById('s_hero_note').value.trim(),
    buy_default: document.getElementById('s_buy_default').value.trim(),
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
    },
    git: {
      remote_url: document.getElementById('s_git_remote').value.trim(),
      branch: document.getElementById('s_git_branch').value.trim() || 'main',
      commit_prefix: document.getElementById('s_git_prefix').value.trim(),
      enabled: document.getElementById('s_git_enabled').checked,
      push: document.getElementById('s_git_push').checked,
    }
  };
  setStatus(I18N[LANG].saving, true);
  try {
    const resp = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || '保存失败');
    setStatus((LANG==='zh'?'网站设置已保存 · ':'Settings saved · ') + (j.git ? I18N[LANG].gitPublished : I18N[LANG].notPushed + (j.git_msg||'')), true);
    hideSettings();
  } catch (e) {
    setStatus((I18N[LANG].err||'出错：') + e.message, false);
  }
}

/* ============ 中英文切换 ============ */
const I18N = {
  zh: {
    title:'商品管理后台', preview:'预览首页 →', settings:'⚙ 网站设置', addItem:'＋ 新增商品',
    tip:'改动后自动重写 index.html。图片上传到 images/ 文件夹。',
    thImg:'图片', thName:'名称', thPrice:'价格', thCat:'分类', thDesc:'简介', thLink:'购买链接', thOp:'操作',
    editItem:'编辑商品', lblName:'名称', lblPrice:'价格 (例：¥ 299)', lblCat:'分类', lblDesc:'简介',
    lblBuyLink:'购买链接 (Facebook Marketplace 页)', lblBuyText:'购买按钮文案（留空用全局默认）', lblImg:'商品图片',
    btnCancel:'取消', btnSave:'保存',
    settingsTitle:'网站设置', lblSiteTitle:'站点标题（浏览器标签）', lblLogo:'Logo 文字',
    lblTagline:'顶部副标题 Tagline（留空则不显示）', lblHeroTitle:'首页大标题', lblHeroSub:'首页副标题说明', lblHeroNote:'首页小徽标',
    lblBuyDefault:'购买按钮全局默认文案', lblFooterMain:'页脚主文案（留空则不显示）', lblFooterSub:'页脚副文案（留空则不显示）',
    lblTheme:'默认配色主题', optAuto:'跟随系统 (auto)', optLight:'浅色', optDark:'深色',
    legendLight:'浅色模式配色', legendDark:'深色模式配色', spBg:'背景', spText:'文字', spSub:'次要文字',
    legendGit:'Git 自动发布', lblGitRemote:'远程仓库地址 (GitHub)', spBranch:'分支', spPrefix:'提交前缀',
    cbAutoCommit:'保存后自动提交', cbAutoPush:'自动 push', btnPublishNow:'立即发布', btnSaveSettings:'保存设置',
    rowEdit:'编辑', rowDel:'删', btnAdd:'＋ 新增商品', openLink:'打开',
    ready:'就绪', statusSaved:'已保存 {n} 件商品 · ', gitPublished:'自动发布中', notPushed:'未提交:', saving:'保存设置…',
    err:'出错：', addProductTitle:'新增商品'
  },
  en: {
    title:'Item Admin', preview:'Preview →', settings:'⚙ Settings', addItem:'＋ Add Item',
    tip:'Every change rewrites index.html. Uploaded images go into images/.',
    thImg:'Image', thName:'Name', thPrice:'Price', thCat:'Category', thDesc:'Description', thLink:'Buy Link', thOp:'Actions',
    editItem:'Edit Item', lblName:'Name', lblPrice:'Price (e.g. ¥ 299)', lblCat:'Category', lblDesc:'Description',
    lblBuyLink:'Buy Link (Facebook Marketplace)', lblBuyText:'Buy button text (blank = global default)', lblImg:'Image',
    btnCancel:'Cancel', btnSave:'Save',
    settingsTitle:'Settings', lblSiteTitle:'Site title (browser tab)', lblLogo:'Logo text',
    lblTagline:'Tagline (blank = hidden)', lblHeroTitle:'Homepage headline', lblHeroSub:'Homepage subtitle', lblHeroNote:'Homepage badge',
    lblBuyDefault:'Default buy-button text', lblFooterMain:'Footer main text (blank = hidden)', lblFooterSub:'Footer sub text (blank = hidden)',
    lblTheme:'Default theme', optAuto:'Follow system (auto)', optLight:'Light', optDark:'Dark',
    legendLight:'Light palette', legendDark:'Dark palette', spBg:'Background', spText:'Text', spSub:'Muted text',
    legendGit:'Git auto-publish', lblGitRemote:'Remote repository (GitHub)', spBranch:'Branch', spPrefix:'Commit prefix',
    cbAutoCommit:'Auto commit on save', cbAutoPush:'Auto push', btnPublishNow:'Publish now', btnSaveSettings:'Save settings',
    rowEdit:'Edit', rowDel:'Del', btnAdd:'＋ Add Item', openLink:'Open',
    ready:'Ready', statusSaved:'Saved {n} items · ', gitPublished:'auto-publishing', notPushed:'not pushed:', saving:'Saving…',
    err:'Error: ', addProductTitle:'Add Item'
  }
};
let LANG = localStorage.getItem('bw_admin_lang') || 'zh';

function applyLang() {
  const d = I18N[LANG] || I18N.zh;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.getAttribute('data-i18n');
    if (d[k] != null) el.textContent = d[k];
  });
  document.getElementById('langToggle').textContent = (LANG === 'zh') ? 'EN' : '中文';
  document.getElementById('langToggle').title = (LANG === 'zh') ? 'Switch to English' : '切换为中文';
  renderRows();
}
function toggleLang() {
  LANG = (LANG === 'zh') ? 'en' : 'zh';
  localStorage.setItem('bw_admin_lang', LANG);
  applyLang();
}

applyLang();
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
    print("  商品管理后台 已启动")
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

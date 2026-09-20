#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黑白极简 · 商品管理 Server
Python3 标准库实现; 仅 Facebook 完整图片轮播抓取这一项功能可选依赖 playwright
(见 requirements.txt), 未安装时该功能静默降级(退回只抓一张封面图), 其余功能不受影响。

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
import hmac
import time
import html
import threading
import hashlib
import base64
import mimetypes
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "products.json")
SITE_FILE = os.path.join(HERE, "site.json")
STATS_FILE = os.path.join(HERE, "stats.json")
IMAGES_DIR = os.path.join(HERE, "images")
INDEX_FILE = os.path.join(HERE, "index.html")
FB_KEY_FILE = os.path.join(HERE, "fb.cookie.key")
FB_ENC_PREFIX = "FB-ENC:v1:"
FB_PROFILE_DIR = os.path.join(HERE, ".fb_browser_profile")

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
    # 货币符号(价格前缀): 全局默认, 单个商品可单独覆盖; 价格输入数字后自动加此符号
    "currency": "$",
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


def _product_identity(p):
    """商品没有 id 字段, 用来判断"这条是不是新增的"的稳定标识: 有 Facebook 链接就用
    商品 ID, 否则退化用 (名称, 购买链接)。"""
    item_id = _fb_item_id((p or {}).get("buy"))
    if item_id:
        return "fb:" + item_id
    return "nb:" + (p or {}).get("name", "") + "|" + (p or {}).get("buy", "")


def count_new_products(old_products, new_products):
    """对比保存前后的商品列表, 数出真正新增的条数(编辑/删除/排序都不算)。"""
    old_ids = {_product_identity(p) for p in old_products}
    return sum(1 for p in new_products if _product_identity(p) not in old_ids)


def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("total_uploads", 0)
                    data.setdefault("recent_uploads", [])
                    return data
        except Exception:
            pass
    return {"total_uploads": 0, "recent_uploads": []}


def save_stats(stats):
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f)


_stats_lock = threading.Lock()


def record_new_uploads(n):
    """记 n 条新增商品: 累计总数 +n, 并记下时间戳供"过去12小时"统计用(旧的定期清掉,
    文件不会无限变大)。"""
    if n <= 0:
        return
    with _stats_lock:
        stats = load_stats()
        now = time.time()
        stats["total_uploads"] = stats.get("total_uploads", 0) + n
        recent = stats.get("recent_uploads", [])
        recent.extend([now] * n)
        cutoff = now - 24 * 3600  # 留 24 小时缓冲, 比"过去12小时"查询窗口宽松一点
        stats["recent_uploads"] = [t for t in recent if t >= cutoff]
        save_stats(stats)


def stats_summary():
    with _stats_lock:
        stats = load_stats()
    now = time.time()
    last12h = sum(1 for t in stats.get("recent_uploads", []) if t >= now - 12 * 3600)
    total = stats.get("total_uploads", 0)
    earnings = (total // 10) * 0.5
    return {"total": total, "last12h": last12h, "earnings": round(earnings, 2),
            "until_next_payout": (10 - total % 10) % 10}


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


def _fb_key():
    """读取/生成本机 FB Cookie 加密密钥: 随机 32 字节, 0600 权限, git-ignored 不上传。"""
    try:
        with open(FB_KEY_FILE, "rb") as f:
            return f.read()
    except OSError:
        pass
    key = os.urandom(32)
    fd = None
    try:
        fd = os.open(FB_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        fd = None
    except OSError:
        pass
    finally:
        if fd is not None:
            os.close(fd)
    try:
        with open(FB_KEY_FILE, "rb") as f:
            return f.read()
    except OSError:
        return None


def _xor_stream(key, iv, size):
    """CTR 式流密钥: keystream = SHA256(key || iv || ctr) 逐块拼接。"""
    out = b""
    ctr = 0
    while len(out) < size:
        h = hashlib.new("sha256")
        h.update(key)
        h.update(iv)
        h.update(str(ctr).encode("ascii"))
        out += h.digest()
        ctr += 1
    return out[:size]


def _fb_encrypt(plain):
    """加密 FB Cookie: FB-ENC:v1: + urlsafe_base64(iv + hmactag + ciphertext)。"""
    key = _fb_key()
    if not key:
        return None
    data = plain.encode("utf-8")
    iv = os.urandom(16)
    stream = _xor_stream(key, iv, len(data))
    cipher = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(key, iv + cipher, hashlib.sha256).digest()
    return FB_ENC_PREFIX + base64.urlsafe_b64encode(iv + tag + cipher).decode("ascii")


def _fb_decrypt(value):
    """解密 FB Cookie: 密钥缺失/格式非法/HMAC 校验失败一律返回 None(不抛异常)。"""
    key = _fb_key()
    if not key or not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not value.startswith(FB_ENC_PREFIX):
        return None
    try:
        blob = base64.urlsafe_b64decode(value[len(FB_ENC_PREFIX):])
    except Exception:
        return None
    if len(blob) < 16 + 32 + 1:
        return None
    iv, tag, cipher = blob[:16], blob[16:48], blob[48:]
    expect = hmac.new(key, iv + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, tag):
        return None
    stream = _xor_stream(key, iv, len(cipher))
    raw = bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8", "replace")
    return raw


def site_public():
    """下发给浏览器的站点配置: 抹掉明文/密文 Cookie, 改给一个“是否已设置”的布尔值。"""
    site = dict(load_site())
    site.pop("fb_cookie", None)
    site["fb_cookie_set"] = bool(fb_cookie().strip())
    return site


def _normalize_cookie(raw):
    """把粘贴的 FB Cookie 归一化成单行 'name=value; name=value' 请求头格式。

    兼容常见三种贴法:
      1) 浏览器请求头里的整行 Cookie (name=value; name=value; ...)
      2) DevTools 折行显示的长 Cookie 头 (每行一段, 自动用 ; 拼回)
      3) DevTools Application 面板的 Cookie 表格 (每行 tab 分隔: 名字\t值\t域名\t路径\t...)
    无法识别或无需转换时原样返回。"""
    raw = (raw or "").strip()
    if not raw:
        return raw
    while len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()  # 去掉用户误粘的成对引号
    rows = [r.strip() for r in raw.splitlines() if r.strip()]
    # Application 表格格式: 有行含制表符 → 按「名字=值」取前两列拼成 Cookie 头
    if any("\t" in r for r in rows):
        _skip = {"name", "value", "domain", "path", "expires", "size",
                 "httponly", "secure", "samesite", "priority", "partitionkey"}
        parts = []
        for r in rows:
            cols = r.split("\t")
            if len(cols) < 2:
                continue
            name = cols[0].strip()
            val = cols[1].strip()
            if (name and val and name.lower() not in _skip
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name)):
                parts.append(name + "=" + val)
        if parts:
            return "; ".join(parts)
    # 请求头格式: 逐行去除残尾分号后, 用 ; 拼回(兼续航行折行的情况)
    return "; ".join(rrst for rrst in (r.rstrip(";").strip() for r in rows) if rrst)


def fb_cookie():
    """返回可用的 FB 登录 Cookie(解密后明文, 仅内存中短暂存在)。

    以 FB-ENC:v1: 开头视为密文自动解密; 若是历史明文则就地加密回写 site.json
    (兼容迁移)。密钥在本地 fb.cookie.key, 不提交, 不落明文。"""
    raw = load_site().get("fb_cookie") or ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith(FB_ENC_PREFIX):
        return _normalize_cookie(_fb_decrypt(raw) or "")
    # 历史明文 → 迁移为密文
    enc = _fb_encrypt(raw)
    if enc and enc != raw:
        site = load_site()
        site["fb_cookie"] = enc
        save_site(site)
    return _normalize_cookie(raw)


def _merge_products(base, ours, theirs):
    """按商品标识做三方合并 products.json(远端另有人/定时任务也在改它时用)。
    某条只有一边改过就取改过的那边; 两边都改过取本地(ours), 但若本地没动过图片而远端
    动了(fb-image-check 刷新过期链接), 图片取远端; 一边删除且另一边没改 → 删除;
    只在远端新增的追加到末尾。顺序以本地为准。"""
    def by_id(lst):
        return {_product_identity(p): p for p in lst if isinstance(p, dict)}
    b, o, t = by_id(base), by_id(ours), by_id(theirs)
    out = []
    for p in ours:
        k = _product_identity(p)
        tp, bp = t.get(k), b.get(k)
        if tp is None:
            # 远端没有: 本地新增(不在 base)保留; base 里有而远端删了 → 本地没改才跟着删
            if bp is None or p != bp:
                out.append(p)
            continue
        if bp is None or p == bp:
            out.append(tp)
        elif tp == bp:
            out.append(p)
        else:
            m = dict(p)
            if product_imgs(p) == product_imgs(bp) and product_imgs(tp) != product_imgs(bp):
                m["imgs"] = tp.get("imgs")
                m["img"] = tp.get("img")
            out.append(m)
    for p in theirs:
        k = _product_identity(p)
        if k not in o and k not in b:
            out.append(p)
    return out


def _git_show_json(ref):
    ok, out = run_git(["show", ref])
    try:
        v = json.loads(out) if ok else []
    except Exception:
        v = []
    return v if isinstance(v, list) else []


def _merge_remote(remote, branch):
    """把远端分支合进本地: 先走 git 自动合并; products.json / index.html 冲突时按商品
    三方合并 + 重新渲染 index.html; 其它文件冲突则中止合并并报错(不丢数据)。返回 (ok, 说明)。"""
    ok, out = run_git(["merge", "--no-edit", "%s/%s" % (remote, branch)], timeout=60)
    if ok:
        return True, "已合并远端更新"
    ok, files = run_git(["diff", "--name-only", "--diff-filter=U"])
    conflicted = set(files.split()) if ok else set()
    if not conflicted or not conflicted <= {"products.json", "index.html"}:
        run_git(["merge", "--abort"])
        return False, "与远端合并冲突, 需要手动处理: " + out[-300:]
    if "products.json" in conflicted:
        merged = _merge_products(
            _git_show_json(":1:products.json"), _git_show_json(":2:products.json"),
            _git_show_json(":3:products.json"))
    else:
        merged = load_products()
    save_products(merged)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        f.write(render_index(merged))
    run_git(["add", "products.json", "index.html"])
    ok, out = run_git(["commit", "--no-edit"])
    if not ok:
        run_git(["merge", "--abort"])
        return False, "合并提交失败: " + out
    return True, "已合并远端更新(自动解决 products.json 冲突)"


def _push_with_sync(branch, tries=3):
    """push; 被拒(远端有别人先推了)就 fetch + 合并 + 重试。返回 (ok, 说明)。"""
    out = ""
    for _ in range(tries):
        ok, out = run_git(["push", "origin", branch], timeout=60)
        if ok:
            return True, ""
        if not any(k in out for k in ("rejected", "fetch first", "non-fast-forward")):
            return False, out
        ok, msg = run_git(["fetch", "origin", branch], timeout=60)
        if not ok:
            return False, "fetch 失败: " + msg
        ok, msg = _merge_remote("origin", branch)
        if not ok:
            return False, msg
    return False, out


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


def _fallback_identity():
    """提交缺少 user.name/user.email 时的兜底身份：优先取仓库最近一次提交的作者, 全新空仓库用通用身份。"""
    ok, out = run_git(["log", "-1", "--format=%an%n%ae"])
    if ok and out.strip():
        lines = out.strip().splitlines()
        if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
            return lines[0].strip(), lines[1].strip()
    return "Auto Publisher", "auto@example.com"


def git_commit_push(message, manual=False):
    """自动 add / commit / (push)。返回 (ok, 说明)。

    manual=True 表示「立即发布」按钮触发的强制发布: 不受 enabled 开关限制,
    始终 commit + push。
    """
    g = load_git()
    if not g.get("enabled") and not manual:
        return False, "git 自动发布未开启 (设置→Git 自动发布)"

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
        # 没有新改动, 但之前可能有 push 失败遗留的未推送提交: 有就补推, 没有才跳过
        ok, ahead = run_git(["rev-list", "--count", "origin/%s..HEAD" % g.get("branch", "main")])
        if not (g.get("push", True) or manual) or not ok or ahead.strip() in ("", "0"):
            return False, "没有任何改动, 已跳过提交"
        ok, out = _push_with_sync(g.get("branch", "main"))
        if not ok:
            return False, "push 失败: " + out
        return True, "已补推之前未推送的提交: " + message

    # 3) commit
    prefix = g.get("commit_prefix", "")
    msg = prefix + (message or "update")
    ok, out = run_git(["commit", "-m", msg])
    if not ok and ("user.name" in out or "user.email" in out):
        # 常见: 没有配置 identity → 用最近一次提交作者兜底重试 (仅本次提交生效, 不写 git config)
        name, email = _fallback_identity()
        ok, out = run_git(["-c", "user.name=" + name, "-c", "user.email=" + email, "commit", "-m", msg])
    if not ok:
        if "user.name" in out or "user.email" in out:
            return False, "git 未配置 user.name/user.email, 全局先 `git config --global user.name ...`"
        return False, "git commit 失败: " + out
    commit_hash = out.strip().splitlines()[-1] if out.strip() else ""

    # 4) push (可选); 手动发布强制 push
    if g.get("push", True) or manual:
        branch = g.get("branch", "main")
        # 若配置了 remote_url 且尚未与 origin 关联, 先 set-url
        remote_url = g.get("remote_url", "")
        if remote_url:
            ok, out = run_git(["remote", "get-url", "origin"])
            if not ok or (ok and out.strip() != remote_url.strip()):
                run_git(["remote", "set-url", "origin", remote_url.strip()])
        ok, out = _push_with_sync(branch)
        if not ok:
            return False, "commit 成功但 push 失败: " + out
        return True, "已 commit + push: " + message
    return True, "已 commit (未 push): " + message


# 后台异步发布: 记录最近一次结果, 供前端轮询展示
_last_push = {"running": False, "ts": 0, "ok": None, "msg": ""}
_push_lock = threading.Lock()


def start_async_push(message, manual=False):
    """在后台线程执行 git_commit_push, 立即返回状态."""
    global _last_push
    with _push_lock:
        if _last_push.get("running"):
            return False, "已有发布任务进行中, 请稍候"
        _last_push = {"running": True, "ts": int(time.time()), "ok": None, "msg": "发布中…"}
    def _run():
        try:
            ok, msg = git_commit_push(message, manual=manual)
        except Exception as e:
            ok, msg = False, "发布出错: " + str(e)
        with _push_lock:
            _last_push["running"] = False
            _last_push["ok"] = ok
            _last_push["msg"] = msg
    threading.Thread(target=_run, daemon=True).start()
    return True, "已开始发布(后台执行), 稍后刷新即可看到结果"


def start_save_publish(message):
    """保存商品/设置后触发发布。自动发布关闭时直接返回未发布, 不误报已自动发布。

    与「立即发布」按钮(start_async_push, manual=True)区分: 手动发布不受开关限制。
    """
    g = load_git()
    if not g.get("enabled"):
        return False, "git 自动发布未开启 (设置→Git 自动发布)"
    if not g.get("push", True):
        return True, "已开始自动提交(未勾选自动 push, 仅本地 commit, 不推送)"
    return start_async_push(message)


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



def is_external_img(s):
    """判断是否为外链图片 URL(http/https)。"""
    return bool(re.match(r"^(https?:)?//", s or ""))


def product_imgs(p):
    """返回商品的图片列表(规范化, 兼容新旧数据: 优先 imgs, 回退 img)。

    外链图片(http/https URL)原样保留; 本地图按完整值去重。
    """
    imgs = p.get("imgs") or []
    if isinstance(imgs, str):
        imgs = [imgs]
    if not isinstance(imgs, list):
        imgs = []
    imgs = [im for im in imgs if isinstance(im, str)]
    if not imgs and p.get("img"):
        imgs = [p["img"]]
    seen, out = set(), []
    for im in imgs:
        key = im.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(im)
    return out


def thumb_html(p):
    """返回缩略图 HTML。有图出 img，没图显示黑块占位。"""
    imgs = product_imgs(p)
    im = (imgs[0] if imgs else "").strip()
    if is_external_img(im):
        return '<img src="%s" alt="%s" loading="lazy">' % (
            esc(im), esc(p.get("name", "")))
    im = im.lstrip("./")
    if im and os.path.exists(os.path.join(IMAGES_DIR, os.path.basename(im))):
        return '<img src="%s" alt="%s" loading="lazy">' % (
            esc(im), esc(p.get("name", "")))
    return '<div class="ph">◼</div>'


_FB_ITEM_RE = re.compile(r"marketplace/item/(\d+)")


def _fb_item_id(url):
    m = _FB_ITEM_RE.search(url or "")
    return m.group(1) if m else None


def _fb_item_id_counts(products):
    counts = {}
    for p in products:
        item_id = _fb_item_id((p or {}).get("buy"))
        if item_id:
            counts[item_id] = counts.get(item_id, 0) + 1
    return counts


def find_new_duplicate_fb_listing(old_products, new_products):
    """只拦截"这次保存新引入的"撞车 Facebook 商品(某个 ID 在新列表里出现的次数比
    保存前更多)。保存前就已经存在的旧重复不算数——不然旧数据没清理干净之前,
    以后所有保存(哪怕跟那条旧重复毫不相干)都会被一直卡死。撞车就返回商品名, 否则 None。"""
    old_counts = _fb_item_id_counts(old_products)
    new_counts = _fb_item_id_counts(new_products)
    for item_id, cnt in new_counts.items():
        if cnt > 1 and cnt > old_counts.get(item_id, 0):
            for p in new_products:
                if _fb_item_id((p or {}).get("buy")) == item_id:
                    return p.get("name") or item_id
    return None


def find_existing_product_by_fb_item(item_id, exclude_idx=None):
    """已保存的商品列表里有没有同一个 Facebook 商品 ID; exclude_idx 用来排除"正在编辑
    的这一条自己"(编辑时重新导入同一个链接刷新图片, 不该被判定为撞了自己)。"""
    if not item_id:
        return None
    for i, p in enumerate(load_products()):
        if exclude_idx is not None and i == exclude_idx:
            continue
        if _fb_item_id((p or {}).get("buy")) == item_id:
            return p.get("name") or item_id
    return None


def verify_images(products):
    """把图片字段规范成 imgs 列表; 外链 URL 原样保留, 本地图规范化成
    images/ 下相对路径, 并裁掉不存在的。同时剪掉已废弃的 category 字段。"""
    for p in products:
        p.pop("cat", None)
        imgs = product_imgs(p)
        clean = []
        for im in imgs:
            im = (im or "").strip().replace("\\", "/")
            if is_external_img(im):
                if im not in clean:
                    clean.append(im)
                continue
            name = os.path.basename(im)
            target = "images/" + name
            if name and target not in clean and os.path.exists(os.path.join(IMAGES_DIR, name)):
                clean.append(target)
        p["imgs"] = clean
        p["img"] = clean[0] if clean else ""
    return products


# ------------------------- Marketplace 链接提取 -------------------------
# 从 Facebook Marketplace 商品页提取 名称/价格/简介/图片链接(只提链接, 不下载图片)。
_CUR_SYM_MAP = {
    "US$": "$", "A$": "A$", "CA$": "CA$", "HK$": "HK$", "NT$": "NT$", "S$": "S$",
    "USD": "$", "CAD": "CA$", "AUD": "A$", "SGD": "S$", "HKD": "HK$", "NZD": "NZ$",
    "TWD": "NT$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "MYR": "RM",
    "THB": "฿", "INR": "₹", "KRW": "₩", "VND": "₫", "PHP": "₱", "PKR": "₨",
    "BDT": "৳", "LKR": "රු", "CHF": "CHF", "SEK": "kr", "NOK": "kr", "DKK": "kr",
    "PLN": "zł", "CZK": "Kč", "ZAR": "R", "IDR": "Rp", "MXN": "MX$", "BRL": "R$",
    "RUB": "₽", "TRY": "₺",
}
_PRICE_PAT = re.compile(
    r"(" + "|".join(re.escape(k) for k in _CUR_SYM_MAP) + r"|[$€£¥฿₹₩₫])"
    r"\s*([0-9][0-9,\.]*)",
    re.I,
)


# 浏览器 UA 列表: FB 反爬会拒绝现代版浏览器头, 短/老版本反而放行, 依次尝试
_UA_LIST = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/49.0.2623.112 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Mozilla/5.0",
    "curl/8.0",
)


def _http_get(url, timeout=20, cookie=None):
    """用浏览器 UA 抓取网页(自动跟随重定向)。成功返回 (html, final_url, "")；
    失败返回 (None, None, 失败原因)。cookie 传入时优先用它, 否则用设置里保存的。"""
    errors = []
    saved_cookie = fb_cookie()
    for ua in _UA_LIST:
        try:
            headers = {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9",
            }
            ck = saved_cookie if cookie is None else cookie
            if ck:
                headers["Cookie"] = ck
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ctype = resp.headers.get("Content-Type", "")
                enc = "utf-8"
                m = re.search(r"charset=([\w-]+)", ctype, re.I)
                if m:
                    enc = m.group(1)
                data = resp.read()
                try:
                    return data.decode(enc, "replace"), resp.geturl(), ""
                except (LookupError, UnicodeDecodeError):
                    return data.decode("utf-8", "replace"), resp.geturl(), ""
        except urllib.error.HTTPError as e:
            errors.append("HTTP %s" % (e.code or 0))
        except Exception as e:
            errors.append("%s: %s" % (type(e).__name__, str(e)[:80]))
    return None, None, "; ".join(errors) or "network error"


def _meta_tags(html_txt):
    """抽取页面所有 <meta property/name = content> 成 dict。"""
    out = {}
    for m in re.finditer(r"<meta[^>]*>", html_txt or "", re.I):
        tag = m.group(0)
        p = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        c = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I | re.S)
        if p and c:
            key = p.group(1).strip().lower()
            if key and key not in out:
                out[key] = html.unescape(c.group(1))
    return out


def _find_price(text):
    """从文本里找第一处价格, 返回 (数字串, 货币符号) 或 (None, None)。"""
    m = _PRICE_PAT.search(text or "")
    if not m:
        return None, None
    tok = m.group(1)
    sym = _CUR_SYM_MAP.get(tok) or _CUR_SYM_MAP.get(tok.upper()) or tok
    num = re.sub(r"\.00$", "", m.group(2).replace(",", ""))
    return num, sym


def _sans_prices(text):
    """剥掉文本里的价格片段。"""
    return _PRICE_PAT.sub(" ", text or "")


def _clean_name(text):
    """把 og:title / <title> 清洗成商品名称。

    FB 页面标题常见两种带前缀形式: "Marketplace - 商品名" 或
    "Facebook Marketplace - 商品名", 这里一并剥掉。"""
    t = (text or "").strip()
    # 后缀: "商品名 | Facebook" 之类
    t = re.sub(r"\s*[-|·–]\s*(Facebook|Marketplace).*$", "", t, flags=re.I)
    # 前缀: "Marketplace - 商品名" / "Facebook Marketplace - 商品名" / "Facebook - 商品名"
    t = re.sub(r"^\s*(?:Facebook\s+)?Marketplace\s*[-|·–:]\s*", "", t, flags=re.I)
    t = re.sub(r"^\s*(?:Marketplace|Facebook)\s*[-|·–:]\s*", "", t, flags=re.I)
    t = _sans_prices(t)
    t = re.sub(r"^\s*[-|·–:\s]+", "", t)
    t = re.sub(r"[-|·–\s]+$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _clean_desc(text):
    """清洗简介: 去掉登录墙/失效提示/面包屑噪音(如 "Marketplace - 标题 > > > ...")。"""
    t = html.unescape(text or "").strip()
    if not t:
        return ""
    low = t.lower()
    if "log in to facebook" in low or "login" in low or "登录" in t:
        return ""
    if "this content isn't available" in low or "isn’t available" in low:
        return ""
    if "page not found" in low:
        return ""
    # 面包屑/标题重复: "Marketplace - xx"、"xx > > > > xx" 之类纯噪音
    if re.match(r"marketplace\s*[-|·:]", low):
        return ""
    if "> >" in low or re.search(r">\s*>\s*>", low) or low.count(">") > 4:
        return ""
    return t[:2000]


def _jsonld(html_txt):
    """解析页面里的 application/ld+json 块, 返回 dict 列表。"""
    out = []
    for m in re.finditer(
            r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
            html_txt or "", re.I | re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
        elif isinstance(data, list):
            out.extend(x for x in data if isinstance(x, dict))
    return out


def _unescape_json_url(u):
    """还原 JSON 里的 \\/ 和 \\uXXXX 转义。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})",
                  lambda m: chr(int(m.group(1), 16)), u.replace("\\/", "/"))


def _dedup_photo_urls(urls, limit=40):
    """同一张照片的不同尺寸变体按「照片ID」(URL 里 <id>_<seq>_ 那段)去重, 保留先出现的。"""
    out = []
    seen_ids = set()
    for u in urls:
        u = (u or "").strip()
        if not u or "/v/" not in u or "\\" in u:
            continue
        if re.search(r"/cp[0-9]+", u):  # 头像角标缩略图, 跳过
            continue
        if len(out) >= limit:
            break
        mid = re.search(r"/(\d+)_\d+_[a-z0-9_]*\.(?:jpg|jpeg|png|webp)", u, re.I)
        key = mid.group(1) if mid else u
        if key in seen_ids:
            continue
        seen_ids.add(key)
        out.append(u)
    return out


def _extract_img_urls(html_txt):
    """提取页面里的 FB CDN 图片 URL, 保留完整签名查询串(?stp=...&oh=...&oe=...,
    截断会变成打不开的 403 死链)。同一张照片的不同尺寸变体按照片ID去重。"""
    txt = re.sub(r"\\u([0-9a-fA-F]{4})",
                 lambda m: chr(int(m.group(1), 16)), html_txt or "")
    txt = txt.replace("\\/", "/")
    txt = html.unescape(txt)  # 还原 &amp; 实体, 得到真实带 & 的查询参数
    return _dedup_photo_urls(
        re.findall(r"https://scontent[^\"'<> )]+\.(?:jpg|jpeg|png|webp)[^\"'<> )]*", txt, re.I))


def _playwright_sync():
    """惰性导入 playwright.sync_api; 未安装则返回 None(功能优雅降级)。"""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        return None


_fb_login_status = {"running": False, "ok": None, "msg": ""}
_fb_login_lock = threading.Lock()


def start_browser_login():
    """后台线程打开登录窗口, 立即返回状态, 供 /api/fb/login-status 轮询进度。"""
    with _fb_login_lock:
        if _fb_login_status.get("running"):
            return False, "已有登录窗口打开中, 请先完成或关闭它"
        _fb_login_status.update(running=True, ok=None, msg="浏览器窗口已打开, 请在窗口里登录后关闭它")
    def _run():
        ok, msg = browser_login_facebook()
        with _fb_login_lock:
            _fb_login_status.update(running=False, ok=ok, msg=msg)
    threading.Thread(target=_run, daemon=True).start()
    return True, "已打开登录窗口"


def browser_login_facebook():
    """打开一个可见的、带持久化会话的浏览器窗口停在 Facebook 登录页, 供用户手动登录。
    登录态(Cookie)会自动写入 FB_PROFILE_DIR, 供之后的无头抓取复用。
    该函数会阻塞直到用户关闭浏览器窗口, 所以调用方应在后台线程里跑。"""
    sync_playwright = _playwright_sync()
    if not sync_playwright:
        return False, "未安装 playwright, 无法打开浏览器"
    os.makedirs(FB_PROFILE_DIR, exist_ok=True)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(FB_PROFILE_DIR, headless=False)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto("https://www.facebook.com/login", timeout=30000)
                while ctx.pages:
                    time.sleep(1)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
        return True, "浏览器已关闭, 登录态已保存"
    except Exception as e:
        return False, str(e)


_CAROUSEL_JS = """
els => {
  // 商品自己的轮播图每张都用同一个 alt 文案(标题); 页面下方"你可能也喜欢"的推荐
  // 商品卡片各有各的 alt(各自的商品名)。按 alt 分组, 取图片最多的那组, 就是轮播图,
  // 不会混进推荐区的图。
  const groups = {};
  for (const el of els) {
    const alt = (el.alt || '').trim();
    if (!alt) continue;
    (groups[alt] = groups[alt] || []).push(el.src);
  }
  let best = [];
  for (const list of Object.values(groups)) {
    if (list.length > best.length) best = list;
  }
  return best;
}
"""


def browser_extract_listing(url, timeout_ms=20000):
    """用已登录的持久化浏览器实际渲染商品页, 一次性读取 HTML 静态抓取拿不到的两项:
    完整图片轮播 + 价格(登录后页面是 JS 壳, 这两项都要等 JS 跑完才会出现在 DOM/文本里)。
    未安装 playwright 或未登录过时, 静默返回全空(上层原样保留 HTTP 抓取的结果)。"""
    sync_playwright = _playwright_sync()
    if not sync_playwright or not os.path.isdir(FB_PROFILE_DIR):
        return {"imgs": [], "price": None, "sym": None}
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(FB_PROFILE_DIR, headless=True)
            try:
                page = ctx.new_page()
                # domcontentloaded 比 networkidle 快得多: FB 页面有持续的后台请求
                # (埋点/长轮询), networkidle 经常要等到超时才返回; 读 img.src/文本不需要
                # 图真的下载完, 只要 DOM 渲染出来就行。
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_selector("img[src*='scontent']", timeout=8000)
                except Exception:
                    pass  # 没等到也继续按现状读一次, 好过直接判定失败
                srcs = page.eval_on_selector_all("img[src*='scontent']", _CAROUSEL_JS)
                body_text = page.inner_text("body")
            finally:
                ctx.close()
        price, sym = _find_price(body_text)  # 页面正文里第一处价格 = 商品自己的价格
        return {"imgs": _dedup_photo_urls(srcs), "price": price, "sym": sym}
    except Exception:
        return {"imgs": [], "price": None, "sym": None}


def _decent_desc(t, skip_text=""):
    """简介候选是否靠谱: 拒绝面包屑「>」串/标题重复/纯符号行。"""
    low = (t or "").lower().strip()
    if not low:
        return False
    if "> >" in low or re.search(r">\s*>\s*>", low) or low.count(">") > 4:
        return False
    sym = sum(1 for ch in low if not ch.isalnum() and ch not in " \t,.'\u2019\"!?-()/@#%&+*=\u3001\u3002\uff0c\u300a\u300b")
    if sym and sym > len(low) * 0.35:
        return False
    skip = (skip_text or "").lower().strip()
    if skip and (low == skip or low.startswith(skip[:24])):
        return False
    if re.match(r"marketplace\s*[-|·:]", low):
        return False
    return True


def _first_text(html_txt, skip_text=""):
    """兜底抓简介: 挑页面第一段「像样」的文字(跳过面包屑/标题重复)。"""
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", html_txt or "", re.I | re.S):
        t = re.sub(r"<[^>]+>", " ", m.group(1))
        t = html.unescape(re.sub(r"\s+", " ", t)).strip()
        if len(t) >= 8 and _decent_desc(t, skip_text):
            return _clean_desc(t)
    t = re.sub(r"<script.*?</script>??", " ", html_txt or "", flags=re.I | re.S)
    t = re.sub(r"<style.*?</style>??", " ", t, flags=re.I | re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(re.sub(r"\s+", " ", t)).strip()
    return _clean_desc(t)[:500]


# FB 价格常见的内嵌 JSON key(用于提取 + 诊断), 按可能命中率大致排序
_PRICE_KEY_NAMES = (
    "formatted_price", "amountWithFormat", "amountWithCurrencyFormat",
    "amountWithCurrency", "marketplace_listing_price_amount",
    "listing_price_amount", "sale_price_amount", "price_amount",
    "marketplacePrice",
)


def _json_price(html_txt):
    """从页面内嵌 JSON 里找价格(登录态下 FB 常把价格放在这些 key 里)。"""
    pats = (
        r'"formatted_price"\s*:\s*"([^"]+)"',
        r'"amountWithFormat"\s*:\s*"([^"]+)"',
        r'"amountWithCurrencyFormat"\s*:\s*"([^"]+)"',
        r'"amountWithCurrency"\s*:\s*"([^"]+)"',
        r'"marketplace_listing_price_amount"\s*:\s*"?([0-9][0-9,.]*)"?',
        r'"listing_price_amount"\s*:\s*([0-9][0-9,.]*)',
        r'"sale_price_amount"\s*:\s*([0-9][0-9,.]*)',
        r'"price_amount"\s*:\s*([0-9][0-9,.]*)',
        r'"marketplacePrice"\s*:\s*([0-9][0-9,.]*)',
    )
    for p in pats:
        m = re.search(p, html_txt or "")
        if not m:
            continue
        v = m.group(1)
        num, sym = _find_price(v)
        if num:
            return num, sym
        v2 = re.sub(r"[^0-9.,]", "", v)
        if re.fullmatch(r"[0-9][0-9,.]*", v2):
            return v2, ""
    return None, None


def _diag_info(html_txt, meta, imgs):
    """返回抓取诊断数据, 用于定位“价格/图片没提取到”的根因。"""
    page = html_txt or ""
    return {
        "price_keys": [k for k in _PRICE_KEY_NAMES if k in page],
        "minPrice_null": '"minPrice":null' in page,
        "has_og_image": bool(meta.get("og:image")),
        "spa_shell": bool(meta.get("og:title") or meta.get("og:image")) is False
                     and ("fb_dtsg" in page or '"LSD"' in page),
        "scontent": page.count("scontent"),
        "imgs_extracted": len(imgs),
    }


def _parse_item_page(html_txt, final):
    """解析单个页面: 成功返回 {name,price,sym,desc,imgs}; 撞登录墙返回 None。"""
    meta = _meta_tags(html_txt)
    tm = re.search(r"<title[^>]*>(.*?)</title>", html_txt or "", re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", tm.group(1))).strip() if tm else ""
    title = meta.get("og:title") or title
    if not title:
        return None
    low_title = title.lower()
    if re.search(r"log\s*in|login|登录", low_title):
        return None
    if final and "/login" in urllib.parse.urlparse(final).path:
        return None

    name = _clean_name(title)
    price, sym = _find_price(title)
    desc = _clean_desc(meta.get("og:description") or meta.get("description"))
    imgs = _extract_img_urls(html_txt)
    if not imgs and meta.get("og:image"):
        imgs.append(meta["og:image"])

    for jd in _jsonld(html_txt):
        jtype = str(jd.get("@type") or "")
        if not name and jd.get("name"):
            name = str(jd["name"])[:200]
        if not price and jd.get("offers"):
            off = jd["offers"]
            if isinstance(off, dict):
                off = [off]
            if isinstance(off, list) and off and isinstance(off[0], dict) and off[0].get("price") is not None:
                price = str(off[0]["price"])
                sym = ""
        if not desc and jd.get("description"):
            desc = _clean_desc(str(jd["description"]))
        imgv = jd.get("image")
        if imgv:
            if isinstance(imgv, str) and imgv.startswith("http"):
                imgs.append(_unescape_json_url(imgv))
            elif isinstance(imgv, list):
                for iv in imgv:
                    u = iv.get("url") if isinstance(iv, dict) else iv
                    if isinstance(u, str) and u.startswith("http"):
                        imgs.append(_unescape_json_url(u))

    if not price:
        jp_num, jp_sym = _json_price(html_txt)
        if jp_num:
            price, sym = jp_num, jp_sym

    if not desc:
        desc = _first_text(html_txt, skip_text=name)
    seen, imgs2 = set(), []
    for u in imgs:
        if u and u not in seen:
            seen.add(u)
            imgs2.append(u)
    return {"name": name, "price": price, "sym": sym, "desc": desc,
            "imgs": imgs2[:30], "diag": _diag_info(html_txt, meta, imgs2)}


def scrape_marketplace(url, cookie=None):
    """从 Facebook Marketplace 商品链接提取 名称/价格/简介/图片 URL。只提取链接, 不下载。

    策略: FB 桌面商品页登录后是纯 JS 壳(SRP 不含价格/图片数据), 反而是
    匿名 SEO 版的名字/简介/封面图最干净——所以一律先匿名抓一份做基础,
    再在有 Cookie 时补抓一次登录态(万一登录态能拿到更多图/价),
    最后按「照片ID去重 + 字段择优」合并。
    匿名/Cookie 抓的静态 HTML 里最多只有一张封面图、价格也常常拿不到(这两项
    是登录后客户端 JS 拉的, 纯文本抓取拿不到), 所以额外用一个真实登录过的
    浏览器(browser_extract_listing)把页面渲染出来读 DOM/正文, 图片拿到就整体
    替换, 价格拿到就覆盖(拿不到则保留上面 HTTP 抓取的结果)。"""
    m = _FB_ITEM_RE.search(url or "")
    if not m:
        return {"ok": False, "error": "不是有效的 Facebook Marketplace 商品链接，请使用形如 …/marketplace/item/123456789/ 的地址"}
    item_id = m.group(1)
    buy = "https://www.facebook.com/marketplace/item/%s/" % item_id
    result = {"ok": True, "name": "", "price": "", "sym": "", "desc": "", "imgs": [], "buy": buy}

    got_login = False
    last_err = ""
    www = "https://www.facebook.com/marketplace/item/%s/" % item_id
    # 匿名(必须显式传 "" 才能真正不带 Cookie → FB 才会给 SEO 干净页)
    seq = [(www, "")]
    saved_ck = cookie or fb_cookie() or None
    if saved_ck:
        seq.append((www, saved_ck))
    seq.append(("https://mbasic.facebook.com/marketplace/item/%s/" % item_id, ""))

    parses = []
    for turl, ck in seq:
        html_txt, final, err = _http_get(turl, cookie=ck)
        if not html_txt:
            last_err = err or last_err
            continue
        parsed = _parse_item_page(html_txt, final)
        if parsed is None:
            got_login = True
            continue
        parses.append(parsed)
        if not result.get("diag"):
            result["diag"] = parsed.get("diag") or {}

    if not parses:
        if last_err:
            return {"ok": False, "error": "无法访问链接（网络或代理异常）：" + last_err}
        if got_login:
            return {"ok": False, "error": "Facebook 要求登录，此商品无法自动提取。请手动填写，或换用可公开访问的链接。"}
        return {"ok": False, "error": "未能从链接提取到商品信息，链接可能已失效或页面结构已变更。"}

    # 图片: 按「照片ID」去重合并(同名不同尺寸只保留一张)
    seen_ids = set()
    for p in parses:
        for u in (p.get("imgs") or []):
            mid = re.search(r"/(\d+)_\d+_", u)
            key = mid.group(1) if mid else u
            if key not in seen_ids:
                seen_ids.add(key)
                result["imgs"].append(u)
    result["imgs"] = result["imgs"][:30]

    # 其余字段按首个非空(匿名 SEO 在前, 名字/简介已清洗, 无 "Marketplace - " 前缀)
    for k in ("name", "price", "sym", "desc"):
        for p in parses:
            if not result.get(k) and p.get(k):
                result[k] = p[k]
                break

    # 图片 + 价格: 若有登录过的浏览器, 用它渲染出真实页面读取(这两项匿名 HTML 抓不到,
    # 图片会整体替换掉只有一张封面图的结果; 价格拿到才覆盖, 拿不到就保留上面 HTTP 抓的)。
    browser_data = browser_extract_listing(buy)
    if browser_data.get("imgs"):
        result["imgs"] = browser_data["imgs"]
    if browser_data.get("price"):
        result["price"] = browser_data["price"]
        result["sym"] = browser_data.get("sym") or result["sym"]
    return result


# ------------------------- 静态页渲染 -------------------------
@staticmethod
def _asset_escape(s):
    return json.dumps(s, ensure_ascii=False)


def price_display(p, cur="$"):
    """价格展示: 纯数字价格自动在前面加货币符号(商品 sym 覆盖全局 currency),
    非纯数字(旧数据/特殊文案)原样显示; 空价格不显示。"""
    raw = (p.get("price") or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[0-9.,]+", raw):
        sym = (p.get("sym") or "").strip() or cur
        return sym + raw
    return raw


def render_index(products):
    _cur = load_site().get("currency") or "$"
    cards = []
    for i, p in enumerate(products):
        n = len(product_imgs(p))
        badge = '<span class="thumb-count">%d</span>' % n if n > 1 else ""
        price_html = esc(price_display(p, _cur))
        cards.append(
            """            <button class="card" type="button" aria-label="View %s details" data-idx="%d">
              <div class="card-thumb">%s%s</div>
              <div class="card-name">%s</div>
              <div class="card-price">%s</div>
            </button>"""
            % (
                esc(p.get("name", "")),
                i,
                thumb_html(p),
                badge,
                esc(p.get("name", "")),
                price_html,
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
    # 首页 hero: 标题/副标题/小徽标全部可留空, 全空则整块不显示
    hero_title_html = esc(s.get("hero_title") or "")
    hero_sub_html = esc(s.get("hero_sub") or "")
    hero_note_html = esc(s.get("hero_note") or "")
    if hero_title_html or hero_sub_html or hero_note_html:
        hero_html = ('    <section class="hero">\n'
                     '      <h1>%s</h1>\n'
                     '      <p class="hero-sub">%s</p>\n'
                     '      <p class="hero-note">%s</p>\n'
                     '    </section>') % (hero_title_html, hero_sub_html, hero_note_html)
    else:
        hero_html = ""
    # 全局货币符号: 价格输入数字后自动加在数字前面, 商品可单独覆盖
    currency = _cur

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
    index = index.replace("/*__HERO__*/", hero_html)
    index = index.replace("/*__COLOR_CSS__*/", css)
    index = index.replace("/*__THEME_JS__*/", theme_js)
    index = index.replace("/*__BUY_DEFAULT__*/", esc_js(s.get("buy_default") or "BUY NOW · GO TO FACEBOOK MARKETPLACE →"))
    index = index.replace("/*__CURRENCY__*/", esc_js(currency))
    index = index.replace("/*__CARDS__*/", cards_html)
    index = index.replace("/*__EMPTY__*/", empty_html)
    index = index.replace("/*__PRODUCTS_JSON__*/", products_json)
    return index


# ------------------------- HTTP 服务 -------------------------
# ------------------------- 预览时同步远端图片 -------------------------
_sync_lock = threading.Lock()
_sync_last = 0.0
SYNC_INTERVAL = 60  # 秒: 预览页刷新时最多每分钟拉取一次远端


def sync_images_from_remote(force=False):
    """打开预览时, 从 origin/main 的 products.json 同步图片 URL(check_fb_images 会在
    远端刷新过期的 Facebook 图片链接)。只按商品标识匹配, 只改 imgs/img, 且只覆盖
    本地全为外链图片的商品(不动本地上传图); 只写本地文件, 不 commit / push。
    返回更新的商品数。"""
    global _sync_last
    if not _sync_lock.acquire(blocking=False):
        return 0
    try:
        now = time.time()
        if not force and now - _sync_last < SYNC_INTERVAL:
            return 0
        _sync_last = now
        branch = load_git().get("branch") or "main"
        remote = "origin"
        ok, _ = run_git(["fetch", remote, branch], timeout=20)
        if not ok:
            return 0
        ok, out = run_git(["show", "%s/%s:%s" % (remote, branch, os.path.basename(DATA_FILE))])
        if not ok:
            return 0
        remote_products = json.loads(out)
        if not isinstance(remote_products, list):
            return 0
        remote_by_id = {_product_identity(p): p for p in remote_products if isinstance(p, dict)}
        products = load_products()
        changed = 0
        for p in products:
            r = remote_by_id.get(_product_identity(p))
            if not r:
                continue
            local_imgs, remote_imgs = product_imgs(p), product_imgs(r)
            if not remote_imgs or local_imgs == remote_imgs:
                continue
            if not all(is_external_img(im) for im in local_imgs):
                continue
            p["imgs"] = list(remote_imgs)
            p["img"] = remote_imgs[0]
            changed += 1
        if changed:
            save_products(products)
        return changed
    except Exception:
        return 0
    finally:
        _sync_lock.release()


DEFAULT_SELLER_URL = "https://www.facebook.com/marketplace/profile/100031022612345/"
TODO_RESCAN_SECONDS = 600  # 打开 /todo 时, 上次扫描超过这么久就自动重扫

_SELLER_ITEMS_JS = """
els => els.map(a => {
  const img = a.querySelector('img');
  const txt = (a.innerText || '').split('\\n').map(t => t.trim()).filter(Boolean);
  return { href: a.href, title: (img && img.alt) || txt[txt.length - 1] || '' };
})
"""

# 卖家主页在 Facebook 里是浮在 Marketplace 首页上的弹窗(role=dialog); 弹窗背后的首页
# 推荐流是别的卖家的商品, 必须只读弹窗里的链接。弹窗内部有自己的滚动容器, 且列表是
# 虚拟滚动(滚出视野的商品会被移出 DOM), 所以要边滚边收集, 不能滚到底再一次性读。
_SELLER_DIALOG_JS = """
() => {
  const d = [...document.querySelectorAll('[role=dialog]')]
    .find(d => d.querySelector("a[href*='/marketplace/item/']"));
  if (!d) return null;
  let sc = null;
  for (const e of d.querySelectorAll('*')) {
    if (e.scrollHeight > e.clientHeight + 20 && ['auto', 'scroll'].includes(getComputedStyle(e).overflowY)) sc = e;
  }
  return sc ? { top: sc.scrollTop, h: sc.scrollHeight, view: sc.clientHeight } : { top: 0, h: 0, view: 0 };
}
"""

_SELLER_SCROLL_JS = """
() => {
  const d = [...document.querySelectorAll('[role=dialog]')]
    .find(d => d.querySelector("a[href*='/marketplace/item/']"));
  if (!d) return;
  let sc = null;
  for (const e of d.querySelectorAll('*')) {
    if (e.scrollHeight > e.clientHeight + 20 && ['auto', 'scroll'].includes(getComputedStyle(e).overflowY)) sc = e;
  }
  if (sc) sc.scrollTop = sc.scrollTop + sc.clientHeight * 0.7;
}
"""

_SELLER_LINKS_JS = """
sel => {
  const d = [...document.querySelectorAll('[role=dialog]')]
    .find(d => d.querySelector("a[href*='/marketplace/item/']"));
  if (!d) return [];
  return [...d.querySelectorAll("a[href*='/marketplace/item/']")].map(a => {
    const img = a.querySelector('img');
    const txt = (a.innerText || '').split('\\n').map(t => t.trim()).filter(Boolean);
    return { href: a.href, title: (img && img.alt) || txt[txt.length - 1] || '' };
  });
}
"""


def browser_extract_seller_items(url, max_scrolls=150, timeout_ms=30000):
    """用已登录的持久化浏览器打开卖家主页(弹窗), 边滚动边收集弹窗内的商品链接, 滚到底为止,
    返回 ([{id, url, title}], 错误信息)。未安装 playwright / 未登录时返回 (None, 原因)。"""
    sync_playwright = _playwright_sync()
    if not sync_playwright:
        return None, "未安装 playwright"
    if not os.path.isdir(FB_PROFILE_DIR) or not os.listdir(FB_PROFILE_DIR):
        return None, "未登录 Facebook, 请先在后台设置里点「登录 Facebook」"
    found = {}
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                FB_PROFILE_DIR, headless=True, viewport={"width": 1400, "height": 1000})
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_selector("[role=dialog] a[href*='/marketplace/item/']", timeout=15000)
                except Exception:
                    pass
                stuck = 0
                for _ in range(max_scrolls):
                    for r in page.evaluate(_SELLER_LINKS_JS, None) or []:
                        item_id = _fb_item_id(r.get("href"))
                        if item_id and item_id not in found:
                            found[item_id] = {
                                "id": item_id,
                                "url": "https://www.facebook.com/marketplace/item/%s/" % item_id,
                                "title": (r.get("title") or "").strip(),
                            }
                    pos = page.evaluate(_SELLER_DIALOG_JS)
                    if not pos:
                        break
                    at_end = pos["top"] + pos["view"] >= pos["h"] - 5
                    page.evaluate(_SELLER_SCROLL_JS)
                    page.wait_for_timeout(1200)
                    # 到底后再多等几轮, 给懒加载留时间; 连续没长高才算真的到底
                    new = page.evaluate(_SELLER_DIALOG_JS) or pos
                    stuck = stuck + 1 if (at_end and new["h"] <= pos["h"]) else 0
                    if stuck >= 3:
                        break
            finally:
                ctx.close()
    except Exception as e:
        if not found:
            return None, str(e)
    if not found:
        return None, "没有读到任何商品(可能未登录或页面结构变化)"
    return list(found.values()), None


_todo_state = {"running": False, "scanned_at": 0, "error": None, "seller": []}
_todo_lock = threading.Lock()


def start_todo_scan():
    """后台线程扫描卖家主页, 立即返回; 已有扫描在跑则不重复启动。"""
    with _todo_lock:
        if _todo_state["running"]:
            return False
        _todo_state["running"] = True
    def _run():
        url = load_site().get("seller_url") or DEFAULT_SELLER_URL
        items, err = browser_extract_seller_items(url)
        with _todo_lock:
            if items is not None:
                _todo_state["seller"] = items
            _todo_state.update(running=False, error=err, scanned_at=int(time.time()))
    threading.Thread(target=_run, daemon=True).start()
    return True


_todo_uploads = {}  # 商品ID -> {"state": "running"|"error", "msg": str}
_todo_save_lock = threading.Lock()  # 上传逐个串行: 抓取后读-改-写 products.json 不能交错


def _upload_todo_item(item_id):
    """抓取单个商品并按后台「新增商品」同样的流程保存(置顶 → verify_images → 写
    products.json + index.html → 记录新增 → 自动发布)。成功后商品进了本地文件,
    清单里下一次轮询自然消失; 失败则记下原因供页面显示并可重试。"""
    try:
        with _todo_save_lock:
            url = "https://www.facebook.com/marketplace/item/%s/" % item_id
            data = scrape_marketplace(url)
            if not data.get("ok"):
                raise ValueError(data.get("error") or "提取失败")
            if not data.get("name"):
                raise ValueError("没有提取到商品名称")
            if not data.get("imgs"):
                raise ValueError("没有提取到图片")
            old_products = load_products()
            if find_existing_product_by_fb_item(item_id):
                return  # 期间已被手动添加
            item = {
                "name": data["name"], "price": data.get("price") or "", "sym": data.get("sym") or "",
                "desc": data.get("desc") or "", "buy": data["buy"], "buy_text": "",
                "imgs": data["imgs"], "img": data["imgs"][0],
            }
            products = verify_images([item] + old_products)
            save_products(products)
            record_new_uploads(count_new_products(old_products, products))
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                f.write(render_index(products))
            start_save_publish("products update")
        with _todo_lock:
            _todo_uploads.pop(item_id, None)
    except Exception as e:
        with _todo_lock:
            _todo_uploads[item_id] = {"state": "error", "msg": str(e)}


def start_todo_upload(item_id):
    """后台线程上传一个商品; 该商品正在上传则不重复启动。"""
    if not re.fullmatch(r"\d+", item_id or ""):
        return False
    with _todo_lock:
        if _todo_uploads.get(item_id, {}).get("state") == "running":
            return False
        _todo_uploads[item_id] = {"state": "running", "msg": ""}
    threading.Thread(target=_upload_todo_item, args=(item_id,), daemon=True).start()
    return True


TODO_AUTO_DELAY = 20  # 秒: 自动添加时两个商品之间的间隔(避免 Facebook 限流 / 刷屏式提交)
_todo_auto = {"on": False, "current": None}  # 仅内存: 重启服务后回到关闭, 不会悄悄接着往线上发
_todo_auto_stop = None


def _todo_auto_loop(stop):
    """自动添加: 永远取清单最上面的商品上传, 传完再取下一个; 清单空了就定期重扫等新商品。
    上传失败的商品本轮跳过(页面上显示原因, 可手动重试), 不会卡在同一个商品上死循环。"""
    while not stop.is_set():
        with _todo_lock:
            stale = time.time() - _todo_state["scanned_at"] > TODO_RESCAN_SECONDS
        if stale:
            start_todo_scan()
        snap = todo_snapshot()
        todo = [it for it in snap["items"]
                if snap["uploads"].get(it["id"], {}).get("state") != "error"]
        if not todo:
            stop.wait(10)
            continue
        item_id = todo[0]["id"]
        with _todo_lock:
            _todo_auto["current"] = item_id
            _todo_uploads[item_id] = {"state": "running", "msg": ""}
        _upload_todo_item(item_id)
        with _todo_lock:
            _todo_auto["current"] = None
        stop.wait(TODO_AUTO_DELAY)


def set_todo_auto(on):
    """开/关自动添加(幂等)。"""
    global _todo_auto_stop
    with _todo_lock:
        if on and not _todo_auto["on"]:
            _todo_auto["on"] = True
            _todo_auto_stop = threading.Event()
            threading.Thread(target=_todo_auto_loop, args=(_todo_auto_stop,), daemon=True).start()
        elif not on and _todo_auto["on"]:
            _todo_auto["on"] = False
            _todo_auto_stop.set()


def todo_snapshot():
    """卖家商品 减去 本地 products.json 里已有的(按 FB 商品 ID), 每次请求实时计算,
    所以商品一保存进本地文件, 下一次轮询就会从清单里消失。"""
    have = {_fb_item_id(p.get("buy")) for p in load_products() if isinstance(p, dict)}
    with _todo_lock:
        st = dict(_todo_state)
    st["items"] = [it for it in st.pop("seller") if it["id"] not in have]
    st["total_seller"] = len(_todo_state["seller"])
    with _todo_lock:
        st["uploads"] = {k: dict(v) for k, v in _todo_uploads.items()}
        st["auto"] = dict(_todo_auto)
    return st


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
            sync_images_from_remote()
            self._send(200, render_index(load_products()))
        elif path == "/todo":
            with _todo_lock:
                stale = time.time() - _todo_state["scanned_at"] > TODO_RESCAN_SECONDS
            if stale:
                start_todo_scan()
            self._send(200, TODO_TEMPLATE)
        elif path == "/api/todo":
            self._json(200, dict(todo_snapshot(), ok=True))
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
            self._json(200, site_public())
        elif path == "/api/git/status":
            st = git_status()
            st["push"] = push_status()
            self._json(200, st)
        elif path == "/api/fb/login-status":
            with _fb_login_lock:
                st = dict(_fb_login_status)
            st["logged_in"] = os.path.isdir(FB_PROFILE_DIR) and bool(os.listdir(FB_PROFILE_DIR))
            st["available"] = _playwright_sync() is not None
            self._json(200, st)
        elif path == "/api/stats":
            self._json(200, stats_summary())
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
        elif path == "/api/scrape/marketplace":
            self._handle_scrape_marketplace()
        elif path == "/api/fb/login":
            self._handle_fb_login()
        elif path == "/api/todo/scan":
            self._json(200, {"ok": True, "started": start_todo_scan()})
        elif path == "/api/todo/auto":
            try:
                body = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception:
                body = {}
            set_todo_auto(bool(body.get("on")))
            self._json(200, {"ok": True, "on": _todo_auto["on"]})
        elif path == "/api/todo/upload":
            try:
                body = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception:
                body = {}
            self._json(200, {"ok": True, "started": start_todo_upload(str(body.get("id") or ""))})
        else:
            self._send(404, "<h1>404</h1>")

    # ---- Git 发布 & Setup ----
    def _handle_git_push(self):
        """触发后台异步发布, 立即返回, 不阻塞浏览器(避免超时/断连)。

        手动「立即发布」始终强制 commit + push, 不受自动发布开关影响。
        """
        ok, msg = start_async_push("manual publish", manual=True)
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
            # 2) 把远程配置写入 site.json, 让设置页能显示并启用自动发布
            site = load_site()
            git = dict(site.get("git") or {})
            git["remote_url"] = remote_url
            git["branch"] = branch
            git.setdefault("enabled", True)
            git.setdefault("push", True)
            site["git"] = git
            save_site(site)
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
            # fb_cookie: 入站明文一律加密存储; 空串显式清空; 缺省不动(保已有)
            raw_cookie = incoming.get("fb_cookie")
            if isinstance(raw_cookie, str):
                rc = _normalize_cookie(raw_cookie)
                if rc and not rc.startswith(FB_ENC_PREFIX):
                    incoming["fb_cookie"] = _fb_encrypt(rc) or rc
                elif not rc:
                    incoming["fb_cookie"] = ""
            elif raw_cookie is not None:
                raise ValueError("fb_cookie must be a string")
            merged = _deep_merge(SITE, incoming)
            # 若本次保存未带 git 字段, 保留已有 git 配置, 防止远程配置被清空
            if "git" not in incoming:
                existing_git = load_site().get("git")
                if existing_git:
                    merged["git"] = existing_git
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
            _ok, _msg = start_save_publish("site settings update")
            self._json(200, {"ok": True, "git": _ok, "git_msg": _msg})
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_save(self):
        try:
            body = self._read_body().decode("utf-8")
            products = json.loads(body)
            if not isinstance(products, list):
                raise ValueError("body must be a list")
            old_products = load_products()
            dup = find_new_duplicate_fb_listing(old_products, products)
            if dup:
                self._json(409, {"ok": False, "code": "dup_listing", "name": dup,
                                 "error": "This Facebook Marketplace listing has already been added: \"%s\"" % dup})
                return
            products = verify_images(products)
            save_products(products)
            record_new_uploads(count_new_products(old_products, products))
            # 重写 index.html
            with open(INDEX_FILE, "w", encoding="utf-8") as f:
                f.write(render_index(products))
            _ok, _msg = start_save_publish("products update")
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

    def _handle_scrape_marketplace(self):
        """从 Marketplace 商品链接自动提取 名称/价格/简介/图片链接(不下载)。"""
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
            url = (body.get("url") or "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "缺少链接地址"})
                return
            idx_raw = body.get("idx")
            try:
                exclude_idx = int(idx_raw) if idx_raw not in (None, "") else None
            except (TypeError, ValueError):
                exclude_idx = None
            dup_name = find_existing_product_by_fb_item(_fb_item_id(url), exclude_idx=exclude_idx)
            if dup_name:
                self._json(400, {"ok": False, "error": "这个 Facebook Marketplace 商品已经导入过了: \"%s\"" % dup_name})
                return
            # cookie: 设置了才用; 空串等价于未设置。未登录时也会尽力提取名称/简介/封面图。
            cookie = _normalize_cookie((body.get("cookie") or "").strip()) or None
            data = scrape_marketplace(url, cookie=cookie)
            self._json(200 if data.get("ok") else 400, data)
        except Exception as e:
            self._json(400, {"ok": False, "error": str(e)})

    def _handle_fb_login(self):
        """打开一个真实浏览器窗口登录 Facebook, 登录态之后供抓完整图片轮播用。"""
        if _playwright_sync() is None:
            self._json(400, {"ok": False, "error": "未安装 playwright, 请先安装(见 requirements.txt)"})
            return
        ok, msg = start_browser_login()
        self._json(200 if ok else 409, {"ok": ok, "msg": msg})

    def admin_page(self):
        products = load_products()
        page = ADMIN_TEMPLATE.replace("/*__PRODUCTS_JSON__*/", json.dumps(products, ensure_ascii=False))
        page = page.replace("/*__SITE_JSON__*/", json.dumps(site_public(), ensure_ascii=False))
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
<link rel="stylesheet" href="style.css?v=2">
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
  /*__HERO__*/

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
    <div class="modal-gallery">
      <div class="modal-thumb" data-thumb></div>
      <button type="button" class="gal-arrow gal-prev hidden" data-prev aria-label="上一张">‹</button>
      <button type="button" class="gal-arrow gal-next hidden" data-next aria-label="下一张">›</button>
      <div class="gal-dots" data-dots></div>
    </div>
    <h2 id="modal-title" data-title></h2>
    <p class="modal-price" data-price></p>
    <p class="modal-desc" data-desc></p>
    <a class="btn btn-buy" data-buy href="#" target="_blank" rel="noopener noreferrer">BUY NOW · GO TO FACEBOOK MARKETPLACE →</a>
  </article>
</div>

<script>
const PRODUCTS = /*__PRODUCTS_JSON__*/;
const BUY_DEFAULT = "/*__BUY_DEFAULT__*/";
const CURRENCY = "/*__CURRENCY__*/";
// 卡片已由服务端渲染；JS 只负责弹窗。
(function () {
  const grid = document.getElementById('grid');
  const modal = document.getElementById('modal');
  const mThumb = modal.querySelector('[data-thumb]');
  const galPrev = modal.querySelector('[data-prev]');
  const galNext = modal.querySelector('[data-next]');
  const galDots = modal.querySelector('[data-dots]');
  const mTitle = modal.querySelector('[data-title]');
  const mPrice = modal.querySelector('[data-price]');
  const mDesc = modal.querySelector('[data-desc]');
  const mBuy = modal.querySelector('[data-buy]');

  // —— 多图轮播 ——
  let gImgs = [];
  let gCur = 0;

  function renderGallery() {
    const hasMany = gImgs.length > 1;
    if (!gImgs.length) {
      mThumb.innerHTML = '<span>◼</span>';
    } else {
      mThumb.innerHTML = '<img src="' + gImgs[gCur] + '" alt="" class="thumb-img">';
    }
    galPrev.classList.toggle('hidden', !hasMany);
    galNext.classList.toggle('hidden', !hasMany);
    galDots.innerHTML = gImgs.map(function (_, i) {
      return '<button type="button" class="gal-dot' + (i === gCur ? ' on' : '') + '" data-dot="' + i + '" aria-label="image ' + (i + 1) + '"></button>';
    }).join('');
    galDots.style.display = hasMany ? '' : 'none';
  }

  function goGallery(i) {
    if (!gImgs.length) return;
    gCur = (i + gImgs.length) % gImgs.length;
    renderGallery();
  }

  function open(idx) {
    const p = PRODUCTS[idx] || {};
    gImgs = Array.isArray(p.imgs) ? p.imgs.slice() : (p.img ? [p.img] : []);
    gCur = 0;
    renderGallery();
    mTitle.textContent = p.name || '';
    mPrice.textContent = (function (pr) {
      pr = (pr || '').trim();
      if (!pr) return '';
      return /^[0-9.,]+$/.test(pr) ? ((p.sym || CURRENCY || '$') + pr) : pr;
    })(p.price);
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
    else if (e.target.closest('[data-prev]')) goGallery(gCur - 1);
    else if (e.target.closest('[data-next]')) goGallery(gCur + 1);
    else {
      const dot = e.target.closest('[data-dot]');
      if (dot) goGallery(parseInt(dot.getAttribute('data-dot'), 10));
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') window.closeModal();
    else if (modal.classList.contains('hidden')) return;
    else if (e.key === 'ArrowLeft') goGallery(gCur - 1);
    else if (e.key === 'ArrowRight') goGallery(gCur + 1);
  });
})();
</script>
</body>
</html>
'''

TODO_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>待添加商品</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 760px; margin: 0 auto; padding: 20px; color: #111; background: #fff; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; border-bottom: 1px solid #111; padding-bottom: 12px; }
h1 { font-size: 20px; margin: 0; }
.meta { color: #666; font-size: 13px; flex: 1; }
button { font: inherit; border: 1px solid #111; background: #fff; padding: 4px 12px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
ul { list-style: none; padding: 0; margin: 0; }
li { display: flex; gap: 12px; align-items: center; padding: 12px 4px; border-bottom: 1px solid #ddd; }
li:hover { background: #f4f4f4; }
li button { padding: 2px 10px; white-space: nowrap; }
li .st { font-size: 12px; color: #666; }
li .st.bad { color: #b00020; }
li .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
li .id { color: #888; font-size: 12px; font-variant-numeric: tabular-nums; }
li .badge { font-size: 12px; background: #111; color: #fff; padding: 1px 8px; }
.msg { padding: 24px 4px; color: #666; }
.err { color: #b00020; }
@media (prefers-color-scheme: dark) {
  body { background: #111; color: #eee; } header { border-color: #eee; }
  button { background: #111; color: #eee; border-color: #eee; }
  li { border-color: #333; } li:hover { background: #1c1c1c; } li .badge { background: #eee; color: #111; }
}
</style>
</head>
<body>
<header>
  <h1 data-i18n="title">待添加商品</h1>
  <span class="meta" id="meta"></span>
  <button id="autoBtn" onclick="toggleAuto()"></button>
  <button id="scanBtn" onclick="rescan()" data-i18n="rescan">重新扫描</button>
  <button onclick="toggleLang()" data-i18n="langBtn">EN</button>
</header>
<div id="err" class="msg err" style="display:none"></div>
<ul id="list"></ul>
<div id="empty" class="msg" style="display:none" data-i18n="empty">全部都已添加 🎉</div>
<script>
const I18N = {
  zh: { title: '待添加商品', rescan: '重新扫描', langBtn: 'EN', empty: '全部都已添加 🎉',
        scanning: '扫描中…', count: '还有 {n} 个未添加(卖家共 {t} 个)', last: '上次扫描 ',
        copied: '已复制', copy: '复制链接', upload: '上传', uploading: '上传中…', retry: '重试', pageTitle: '待添加商品', autoOff: '自动添加：关', autoOn: '自动添加：开(点击停止)',
        autoConfirm: '开启后会从最上面开始逐个抓取并直接发布到线上商店，一直进行到你关闭为止。确定开启？' },
  en: { title: 'To add', rescan: 'Rescan', langBtn: '中文', empty: 'All caught up 🎉',
        scanning: 'Scanning…', count: '{n} missing (seller has {t})', last: 'Last scan ',
        copied: 'Copied', copy: 'Copy link', upload: 'Upload', uploading: 'Uploading…', retry: 'Retry', pageTitle: 'To add', autoOff: 'Auto-add: off', autoOn: 'Auto-add: ON (click to stop)',
        autoConfirm: 'This uploads the top item, then the next, and so on, publishing each straight to the live store until you turn it off. Turn on?' }
};
let LANG = localStorage.getItem('bw_admin_lang') || 'zh';
let DATA = null;
const COPIED = new Set();
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function applyLang() {
  document.title = I18N[LANG].pageTitle;
  document.querySelectorAll('[data-i18n]').forEach(e => { e.textContent = I18N[LANG][e.dataset.i18n]; });
  render();
}
function toggleLang() { LANG = LANG === 'zh' ? 'en' : 'zh'; localStorage.setItem('bw_admin_lang', LANG); applyLang(); }
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta); ta.select();
  document.execCommand('copy'); ta.remove();
  return Promise.resolve();
}
async function toggleAuto() {
  const on = !(DATA && DATA.auto && DATA.auto.on);
  if (on && !confirm(I18N[LANG].autoConfirm)) return;
  try { await fetch('/api/todo/auto', { method: 'POST', body: JSON.stringify({ on: on }) }); } catch (e) {}
  poll();
}
async function upload(id) {
  try { await fetch('/api/todo/upload', { method: 'POST', body: JSON.stringify({ id: id }) }); } catch (e) {}
  poll();
}
function copyItem(id) {
  const it = (DATA.items || []).find(x => x.id === id);
  if (!it) return;
  copyText(it.url).then(() => { COPIED.add(id); render(); });
}
function render() {
  if (!DATA) return;
  const L = I18N[LANG];
  const items = DATA.items || [];
  document.getElementById('scanBtn').disabled = DATA.running;
  const autoOn = !!(DATA.auto && DATA.auto.on);
  const ab = document.getElementById('autoBtn');
  ab.textContent = autoOn ? L.autoOn : L.autoOff;
  ab.style.background = autoOn ? '#111' : '';
  ab.style.color = autoOn ? '#fff' : '';
  let meta = DATA.running ? L.scanning : '';
  if (!DATA.running && DATA.scanned_at) {
    meta = L.count.replace('{n}', items.length).replace('{t}', DATA.total_seller) +
      ' · ' + L.last + new Date(DATA.scanned_at * 1000).toLocaleTimeString();
  }
  document.getElementById('meta').textContent = meta;
  const err = document.getElementById('err');
  err.style.display = DATA.error ? '' : 'none';
  err.textContent = DATA.error || '';
  const ups = DATA.uploads || {};
  document.getElementById('list').innerHTML = items.map(it => {
    const u = ups[it.id] || {};
    const busy = u.state === 'running';
    return '<li>' +
      '<span class="t">' + esc(it.title || it.url) + '</span>' +
      (COPIED.has(it.id) ? '<span class="badge">' + esc(L.copied) + '</span>' : '') +
      (busy ? '<span class="st">' + esc(L.uploading) + '</span>' : '') +
      (u.state === 'error' ? '<span class="st bad" title="' + esc(u.msg) + '">' + esc(u.msg) + '</span>' : '') +
      '<span class="id">' + esc(it.id) + '</span>' +
      '<button onclick="copyItem(\\'' + esc(it.id) + '\\')">' + esc(L.copy) + '</button>' +
      '<button ' + (busy ? 'disabled ' : '') + 'onclick="upload(\\'' + esc(it.id) + '\\')">' +
        esc(u.state === 'error' ? L.retry : L.upload) + '</button></li>';
  }).join('');
  document.getElementById('empty').style.display =
    (!items.length && DATA.scanned_at && !DATA.running && !DATA.error) ? '' : 'none';
}
async function poll() {
  try {
    const j = await (await fetch('/api/todo')).json();
    if (j.ok) { DATA = j; render(); }
  } catch (e) {}
}
async function rescan() {
  try { await fetch('/api/todo/scan', { method: 'POST', body: '{}' }); } catch (e) {}
  poll();
}
applyLang();
poll();
setInterval(poll, 3000);
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
body.admin-body { padding: 20px; width: auto; overflow-x: hidden; }
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
.row-ops { display: flex; gap: 6px; white-space: nowrap; align-items: center; }
.row-ops .btn { padding: 6px 9px; }
table { width: 100%; border-collapse: collapse; margin-top: 20px; }
th, td { border: 1px solid var(--ink); padding: 10px; text-align: left; font-size: 14px; vertical-align: middle; }
th { background: var(--ink); color: var(--paper); letter-spacing: 1px; }
.rowimg { width: 56px; height: 56px; object-fit: cover; border: 1px solid var(--ink); }
.rowimg-cell { position: relative; display: inline-block; }
.rowimg-count { position: absolute; right: -6px; top: -6px; background: var(--ink); color: var(--paper); font-size: 10px; font-weight: 800; padding: 1px 5px; }
.img-list { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 10px; }
.img-item { position: relative; border: 1px solid var(--ink); padding: 4px; }
.img-frame { position: relative; }
.img-frame img { width: 96px; height: 96px; object-fit: contain; display: block; background: var(--paper); }
.img-cover { position: absolute; left: 4px; bottom: 4px; background: var(--ink); color: var(--paper); font-size: 10px; font-weight: 800; letter-spacing: 1px; padding: 1px 5px; }
.img-ops { display: flex; gap: 4px; margin-top: 4px; }
.img-ops .btn { flex: 1; padding: 4px 2px; font-size: 12px; }
.img-ops .btn[disabled] { opacity: .35; cursor: not-allowed; }
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
textarea { resize: vertical; min-height: 80px; max-height: 50vh; }
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
    <span class="muted" id="totalCount" style="font-weight:700"></span>
    <span class="muted" id="uploadStats" style="font-weight:700"></span>
    <span id="status" class="muted">就绪</span>
  </div>
  <div class="toolbar-actions">
    <input type="search" id="searchBox" data-i18n-ph="searchPlaceholder" placeholder="搜索商品…" oninput="renderRows()" style="min-width:220px;padding:7px 10px;border:1px solid #ccc;border-radius:6px">
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
    <tr><th data-i18n="thImg">图片</th><th data-i18n="thName">名称</th><th data-i18n="thPrice">价格</th><th data-i18n="thDesc">简介</th><th data-i18n="thLink">购买链接</th><th data-i18n="thOp">操作</th></tr>
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
    <label data-i18n="lblPrice">价格（直接输入数字，自动加符号）</label>
    <input type="text" id="f_price" inputmode="decimal" placeholder="$" oninput="onPriceInput()">
    <label data-i18n="lblSym">货币符号（留空用全局默认）</label>
    <input type="text" id="f_sym" placeholder="$ / ¥ / NT$ …" oninput="onSymInput()" style="max-width:140px">
    <label data-i18n="lblDesc">简介</label>
    <textarea id="f_desc"></textarea>
    <label data-i18n="lblBuyLink">购买链接 (Facebook Marketplace 页)</label>
    <input type="url" id="f_buy" placeholder="https://www.facebook.com/marketplace/...">
    <div class="form-row" style="margin-top:6px">
      <button type="button" class="btn" onclick="scrapeLink()" data-i18n="btnScrape">◆ 从 Marketplace 链接导入</button>
      <span class="muted" style="align-self:center" data-i18n="lblScrapeHint">自动提取 名称/价格/简介/图片链接（不下载）</span>
    </div>
    <label data-i18n="lblBuyText">购买按钮文案（留空用全局默认）</label>
    <input type="text" id="f_buy_text" placeholder="留空则用网站设置的全局默认">
    <label data-i18n="lblImg">商品图片</label>
    <p class="muted" data-i18n="lblImgHint">可上传多张，第一张为列表封面。点 “+ 添加” 继续选择。</p>
    <div id="imgList" class="img-list"></div>
    <div class="form-row" style="margin-top:4px">
      <button type="button" class="btn" data-i18n="addImgBtn" onclick="document.getElementById('f_file').click()">＋ 添加图片</button>
      <button type="button" class="btn" data-i18n="btnLinkImg" onclick="addLinkImg()">＋ 外链图片</button>
    </div>
    <input type="file" id="f_file" accept="image/*" multiple style="display:none">
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
    <label data-i18n="lblCurrency">货币符号（价格前缀，默认 $）</label>
    <input type="text" id="s_currency" placeholder="$" style="max-width:140px">
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
      <div class="form-row" style="margin-top:10px">
        <label style="flex:1"><span class="muted" style="font-size:11px" data-i18n="lblGitUser">GitHub 用户名</span><input type="text" id="s_git_user" autocomplete="username" placeholder="your-github-name"></label>
        <label style="flex:1"><span class="muted" style="font-size:11px" data-i18n="lblGitToken">GitHub Token (PAT)</span><input type="password" id="s_git_token" autocomplete="current-password" placeholder="ghp_xxx / github_pat_xxx" title="Personal Access Token"></label>
      </div>
      <div class="form-row" style="align-items:center;margin-top:6px">
        <button type="button" class="btn" onclick="saveGitAuth()" data-i18n="btnSaveGitAuth">保存 GitHub 凭据</button>
        <span class="muted" id="gitCredHint">‐</span>
      </div>
      <div class="form-row" style="align-items:center;margin-top:8px">
        <label style="display:flex;align-items:center;gap:6px;font-weight:600;margin:0"><input type="checkbox" id="s_git_enabled" checked> <span data-i18n="cbAutoPublish">保存后自动发布（自动 commit + push）</span></label>
        <button type="button" class="btn" onclick="publishNow()" style="margin-left:auto" data-i18n="btnPublishNow">立即发布</button>
      </div>
      <p class="muted" id="gitStatusHint" style="margin-top:8px">— 远程仓库未配置 —</p>
    </fieldset>
    <fieldset>
      <legend class="muted" data-i18n="legendFbLogin">Facebook 登录（抓取完整图片轮播 + 价格）</legend>
      <p class="muted" data-i18n="lblFbLoginHint">默认只能抓到一张封面图、价格也常抓不到；登录一次后，导入商品时会用登录态渲染出完整的图片轮播和价格。</p>
      <div class="form-row" style="align-items:center">
        <button type="button" class="btn" onclick="fbBrowserLogin()" data-i18n="btnFbLogin">登录 Facebook</button>
        <span class="muted" id="fbLoginHint">‐</span>
      </div>
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
// 商品图片列表: formImgs=已保存/已上传的图片路径; pendingImgs=待上传的新图 {filename, data}
let formImgs = [];
let pendingImgs = [];

const rows = document.getElementById('rows');
const status = document.getElementById('status');

function imgsOf(p) {
  const imgs = Array.isArray(p && p.imgs) ? p.imgs : (p && p.img ? [p.img] : []);
  return imgs.filter(Boolean);
}
function matches(p, q) {
  return (p.name || '').toLowerCase().indexOf(q) >= 0 ||
         (p.desc || '').toLowerCase().indexOf(q) >= 0 ||
         (p.buy || '').toLowerCase().indexOf(q) >= 0;
}

function renderRows() {
  const q = document.getElementById('searchBox').value.trim().toLowerCase();
  const L = I18N[LANG] || I18N.zh;
  const idx = PRODUCTS.map((p, i) => ({ p, i })).filter(x => !q || matches(x.p, q));
  document.getElementById('totalCount').textContent = L.countItems.replace('{n}', PRODUCTS.length) +
    (q && PRODUCTS.length ? L.matchItems.replace('{n}', idx.length) : '');
  rows.innerHTML = idx.map(({ p, i }) => {
    const imgs = imgsOf(p);
    const cell = imgs.length
      ? '<img class="rowimg" src="' + imgs[0] + '" alt="">' + (imgs.length > 1 ? '<span class="rowimg-count">' + imgs.length + '</span>' : '')
      : '<span class="small">—</span>';
    return '<tr>' +
    '<td><div class="rowimg-cell">' + cell + '</div></td>' +
    '<td><b>' + p.name + '</b></td>' +
    '<td>' + dispPrice(p) + '</td>' +
    '<td class="small">' + (p.desc ? p.desc.substring(0, 30) : '') + '</td>' +
    '<td><a class="small" href="' + p.buy + '" target="_blank">' + (I18N[LANG].openLink || '打开') + '</a></td>' +
    '<td>' +
      '<div class="row-ops">' +
      '<button class="btn" onclick="edit(' + i + ')">' + I18N[LANG].rowEdit + '</button>' +
      '<button class="btn" onclick="move(' + i + ',-1)" title="↑">↑</button>' +
      '<button class="btn" onclick="move(' + i + ',1)" title="↓">↓</button>' +
      '<button class="btn btn-danger" onclick="del(' + i + ')">' + I18N[LANG].rowDel + '</button>' +
      '</div>' +
    '</td>' +
    '</tr>';
  }).join('');
}

function setStatus(msg, ok) {
  status.textContent = msg;
  status.className = ok ? 'ok' : 'err';
}

/* 价格输入: 输入数字后自动在前面加货币符号(商品 sym 优先, 否则全局默认) */
var _prRaw = '';                       // 输入框中当前的纯数字价格
function priceSym() {
  return document.getElementById('f_sym').value.trim() || (SITE_DEFAULT.currency || '$');
}
function syncPriceBox() {
  const el = document.getElementById('f_price');
  el.value = priceSym() + _prRaw;
}
function onPriceInput() {
  const el = document.getElementById('f_price');
  const sym = priceSym();
  let v = el.value;
  if (sym && v.indexOf(sym) === 0) v = v.slice(sym.length);
  v = v.replace(/[^0-9.,]/g, '');
  _prRaw = v;
  el.value = sym + v;
}
function onSymInput() {
  const el = document.getElementById('f_sym');
  if (_prRaw !== '') syncPriceBox();
  else if (!el.value.trim()) document.getElementById('f_price').value = SITE_DEFAULT.currency || '$';
}
function dispPrice(p) {
  const pr = (p.price || '').trim();
  if (!pr) return '';
  return /^[0-9.,]+$/.test(pr) ? ((p.sym || '').trim() || SITE_DEFAULT.currency || '$' || '') + pr : pr;
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
  document.getElementById('f_sym').value = p.sym || '';
  const pr = (p.price || '').trim();
  _prRaw = /^[0-9.,]+$/.test(pr) ? pr : '';
  document.getElementById('f_price').value = /^[0-9.,]+$/.test(pr) ? (priceSym() + pr) : pr;
  document.getElementById('f_desc').value = p.desc || '';
  document.getElementById('f_buy').value = p.buy || '';
  document.getElementById('f_buy_text').value = p.buy_text || '';
  formImgs = imgsOf(p).slice();
  pendingImgs = [];
  renderImgList();
  document.getElementById('formTitle').textContent = (I18N[LANG].editItem || '编辑商品');
  showForm();
}
function resetForm() {
  document.getElementById('f_idx').value = '';
  document.getElementById('f_name').value = '';
  _prRaw = '';
  document.getElementById('f_sym').value = '';
  document.getElementById('f_price').value = '';
  document.getElementById('f_desc').value = '';
  document.getElementById('f_buy').value = '';
  document.getElementById('f_buy_text').value = '';
  document.getElementById('f_file').value = '';
  formImgs = [];
  pendingImgs = [];
  renderImgList();
}
function showForm() { document.getElementById('overlay').classList.remove('hidden'); }
function hideForm() { document.getElementById('overlay').classList.add('hidden'); }

/* 多图列表(编辑弹窗内) */
function renderImgList() {
  const box = document.getElementById('imgList');
  const ls = (I18N[LANG] || I18N.zh);
  const mk = function (src, isNew, gi, pi) {
    return '<div class="img-item">' +
      '<div class="img-frame">' +
      '<img src="' + src + '" alt="">' +
      (gi === 0 && pi === -1 ? '<span class="img-cover">' + (ls.lblCover || '封面') + '</span>' : '') +
      '</div>' +
      '<div class="img-ops">' +
        '<button type="button" class="btn" onclick="imgMove(' + gi + ',' + pi + ',-1)" ' + (gi === 0 && pi === -1 ? 'disabled' : '') + '>←</button>' +
        '<button type="button" class="btn" onclick="imgMove(' + gi + ',' + pi + ',1)">→</button>' +
        '<button type="button" class="btn btn-danger" onclick="imgDel(' + gi + ',' + pi + ')">' + (ls.rowDel || '删') + '</button>' +
      '</div>' +
    '</div>';
  };
  let html = formImgs.map(function (im, gi) { return mk(im, false, gi, -1); }).join('');
  html += pendingImgs.map(function (p, pi) { return mk(p.data, true, -1, pi); }).join('');
  box.innerHTML = html;
  const count = formImgs.length + pendingImgs.length;
  box.style.display = count ? '' : 'none';
  box.dataset.count = count;
}
function imgMove(gi, pi, d) {
  if (pi === -1) {
    const j = gi + d;
    if (j < 0 || j >= formImgs.length) return;
    const t = formImgs[gi]; formImgs[gi] = formImgs[j]; formImgs[j] = t;
  } else {
    const j = pi + d;
    if (j < 0 || j >= pendingImgs.length) return;
    const t = pendingImgs[pi]; pendingImgs[pi] = pendingImgs[j]; pendingImgs[j] = t;
  }
  renderImgList();
}
function imgDel(gi, pi) {
  if (pi === -1) formImgs.splice(gi, 1);
  else pendingImgs.splice(pi, 1);
  renderImgList();
}

document.getElementById('f_file').addEventListener('change', function (e) {
  const files = Array.prototype.slice.call(e.target.files || []);
  if (!files.length) return;
  setStatus((I18N[LANG] || I18N.zh).compressing, true);
  files.forEach(function (file) {
    compressImage(file).then(function (res) {
      if (res && res.data) {
        pendingImgs.push({ filename: res.filename, data: res.data });
        renderImgList();
      }
    });
  });
  e.target.value = '';
});

/* 上传前压缩: 非 GIF 统一缩放到最大 1600px 并用 JPEG 质量 0.8 重编码, 省空间 */
const IMG_MAX = 1600;
const IMG_QUALITY = 0.8;
function compressImage(file) {
  return new Promise(function (resolve) {
    if (file.type === 'image/gif') {
      const r = new FileReader();
      r.onload = function () { resolve({ filename: file.name, data: r.result }); };
      r.onerror = function () { resolve(null); };
      r.readAsDataURL(file);
      return;
    }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = function () {
      let w = img.naturalWidth, h = img.naturalHeight;
      if (w > IMG_MAX || h > IMG_MAX) {
        if (w >= h) { h = Math.max(1, Math.round(h * IMG_MAX / w)); w = IMG_MAX; }
        else { w = Math.max(1, Math.round(w * IMG_MAX / h)); h = IMG_MAX; }
      }
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(img, 0, 0, w, h);
      let data = null, fn = file.name;
      try { data = c.toDataURL('image/jpeg', IMG_QUALITY); } catch (err) {}
      if (data) {
        const dot = file.name.lastIndexOf('.');
        fn = (dot > 0 ? file.name.slice(0, dot) : file.name) + '.jpg';
      }
      URL.revokeObjectURL(url);
      resolve(data ? { filename: fn, data: data } : null);
    };
    img.onerror = function () { URL.revokeObjectURL(url); resolve(null); };
    img.src = url;
  });
}

/* 外链图片: 直接粘贴 http/https 图片链接, 不占本地存储 */
function addLinkImg() {
  const u = prompt((I18N[LANG] || I18N.zh).promptImgUrl);
  if (!u) return;
  const t = u.trim();
  const okUrl = t.indexOf('http://') === 0 || t.indexOf('https://') === 0 || t.indexOf('//') === 0;
  if (!okUrl) {
    alert((I18N[LANG] || I18N.zh).errBadUrl);
    return;
  }
  formImgs.push(t);
  renderImgList();
}

/* 从 Marketplace 链接导入: 自动提取 名称/价格/简介/图片链接(不下载) */
async function scrapeLink() {
  const L = I18N[LANG] || I18N.zh;
  let u = (document.getElementById('f_buy').value || '').trim();
  // 购买链接框里的默认地址(非商品页)忽略, 弹窗让用户输入商品链接
  if (!/marketplace\\/item\\/\\d+/.test(u)) u = '';
  if (!u) {
    const p = prompt(L.promptScrapeUrl);
    if (!p) return;
    u = p.trim();
  }
  if (u.indexOf('http://') !== 0 && u.indexOf('https://') !== 0) {
    alert(L.errBadUrl);
    return;
  }
  setStatus(L.scraping, true);
  try {
    const idxVal = document.getElementById('f_idx').value;
    const resp = await fetch('/api/scrape/marketplace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: u, idx: idxVal === '' ? null : parseInt(idxVal, 10) })
    });
    const j = await resp.json();
    if (!j.ok) throw new Error(j.error || L.scrapeFail);
    if (j.name) document.getElementById('f_name').value = j.name;
    if (j.price !== undefined && j.price !== '') {
      _prRaw = String(j.price);
      const sym = j.sym || '';
      document.getElementById('f_sym').value = (sym && sym === (SITE_DEFAULT.currency || '$')) ? '' : sym;
      syncPriceBox();
    }
    if (j.desc) document.getElementById('f_desc').value = j.desc;
    if (j.buy) document.getElementById('f_buy').value = j.buy;
    if (Array.isArray(j.imgs) && j.imgs.length) {
      formImgs = j.imgs.slice();
      pendingImgs = [];
      renderImgList();
    }
    const n = (j.imgs && j.imgs.length) || 0;
    setStatus(L.scraped + (n ? L.foundImgs.replace('{n}', n) : ''), true);
  } catch (e) {
    setStatus((L.err || '出错：') + e.message, false);
    alert(e.message);
  }
}

/* 保存：先上传图（若有），再保存商品列表 */
async function save(ev) {
  ev.preventDefault();
  const idx = document.getElementById('f_idx').value;
  const item = idx === ''
    ? {}
    : Object.assign({}, PRODUCTS[parseInt(idx, 10)]);

  item.name = document.getElementById('f_name').value.trim();
  const boxPrice = document.getElementById('f_price').value.trim();
  item.price = (_prRaw !== '' || boxPrice === '') ? _prRaw : boxPrice;
  item.sym = document.getElementById('f_sym').value.trim();
  item.desc = document.getElementById('f_desc').value.trim();
  item.buy = document.getElementById('f_buy').value.trim() || 'https://www.facebook.com/marketplace/';
  item.buy_text = document.getElementById('f_buy_text').value.trim();

  try {
    const order = formImgs.slice();
    if (pendingImgs.length) {
      setStatus(I18N[LANG].uploading, true);
      for (const pi of pendingImgs) {
        const resp = await fetch('/api/upload', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: pi.filename || 'image.jpg', data: pi.data })
        });
        const j = await resp.json();
        if (!j.ok) throw new Error(j.error || '上传失败');
        order.push(j.img);
      }
    }
    item.imgs = order;
    item.img = order[0] || '';
    pendingImgs = [];
    formImgs = [];

    if (idx === '') {
      PRODUCTS.unshift(item);
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
    if (!j.ok) throw new Error(apiErr(j, '保存失败'));
    setStatus(I18N[LANG].statusSaved.replace('{n}', j.count) + (j.git ? I18N[LANG].gitPublished : I18N[LANG].notPushed + (j.git_msg||'')), true);
    renderRows();
    hideForm();
    loadStats();
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
    if (!j.ok) throw new Error(apiErr(j, '保存失败'));
    setStatus(I18N[LANG].statusSaved.replace('{n}', j.count) + (j.git ? I18N[LANG].gitPublished : I18N[LANG].notPushed + (j.git_msg||'')), true);
    renderRows();
    loadStats();
  } catch (e) {
    setStatus((I18N[LANG].err||'出错：') + e.message, false);
  }
}

async function loadStats() {
  const L = I18N[LANG] || I18N.zh;
  const el = document.getElementById('uploadStats');
  try {
    const r = await fetch('/api/stats');
    const j = await r.json();
    el.textContent = L.uploadStats
      .replace('{total}', j.total)
      .replace('{last12h}', j.last12h)
      .replace('{earnings}', j.earnings.toFixed(2));
  } catch (e) {
    el.textContent = '';
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
  document.getElementById('s_currency').value = s.currency || '$';
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
  document.getElementById('settingsOverlay').classList.remove('hidden');
  loadGitStatus();
  loadFbLoginStatus();
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
      el.textContent = '本地仓库: ✓  远程: ' + (j.remote_url || '未设置') + '  分支: ' + j.branch + '  自动发布: ' + (j.auto_enabled ? '开' : '关');
      // 用真实仓库状态补齐设置框里 site.json 缺失/为空的值
      const g = (SITE_DEFAULT.git || {});
      if (!g.remote_url && j.remote_url) document.getElementById('s_git_remote').value = j.remote_url;
      if (!g.branch && j.branch) document.getElementById('s_git_branch').value = j.branch;
      if (g.enabled === undefined) document.getElementById('s_git_enabled').checked = !!j.auto_enabled;
    } else {
      el.textContent = '未初始化 Git 仓库，请在 setup 或设置里配置远程地址。';
    }
  } catch (e) {
    document.getElementById('gitStatusHint').textContent = '读取 git 状态失败';
  }
}

async function loadFbLoginStatus() {
  const L = I18N[LANG] || I18N.zh;
  const el = document.getElementById('fbLoginHint');
  try {
    const r = await fetch('/api/fb/login-status');
    const j = await r.json();
    if (!j.available) { el.textContent = L.fbLoginUnavailable; return; }
    if (j.running) { el.textContent = L.fbLoginRunning; return; }
    el.textContent = j.logged_in ? L.fbLoginOk : L.fbLoginNone;
  } catch (e) {
    el.textContent = L.fbLoginUnknown;
  }
}

async function fbBrowserLogin() {
  const L = I18N[LANG] || I18N.zh;
  const el = document.getElementById('fbLoginHint');
  try {
    const r = await fetch('/api/fb/login', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) { el.textContent = (j.error || j.msg || L.fbLoginFail); return; }
    el.textContent = L.fbLoginRunning;
    // 轮询直到登录窗口关闭
    for (let i = 0; i < 300; i++) {
      await new Promise(res => setTimeout(res, 1000));
      const r2 = await fetch('/api/fb/login-status');
      const s = await r2.json();
      if (!s.running) {
        el.textContent = s.logged_in ? L.fbLoginOk : (s.msg || L.fbLoginNone);
        break;
      }
    }
  } catch (e) {
    el.textContent = L.fbLoginFail + e.message;
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

/* 保存 GitHub 用户名 + Token: 存入系统凭据管理器(不经 site.json/明文), 下次推送自动使用 */
async function saveGitAuth() {
  const L = I18N[LANG] || I18N.zh;
  const hint = document.getElementById('gitCredHint');
  const user = document.getElementById('s_git_user').value.trim();
  const token = document.getElementById('s_git_token').value.trim();
  if (!user || !token) { hint.textContent = L.gitCredNeedBoth; return; }
  let remote_url = document.getElementById('s_git_remote').value.trim();
  if (!remote_url) {
    try {
      const r = await fetch('/api/git/status');
      const j = await r.json();
      remote_url = j.remote_url || '';
    } catch (e) {}
  }
  hint.textContent = L.gitCredSaving;
  try {
    const r = await fetch('/api/git/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remote_url: remote_url, user: user, pass: token })
    });
    const j = await r.json();
    hint.textContent = j.ok ? (L.gitCredOk + (j.msg || '')) : (L.gitCredFail + (j.error || j.msg || ''));
    if (j.ok) document.getElementById('s_git_token').value = '';
  } catch (e) {
    hint.textContent = L.gitCredFail + e.message;
  }
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
    currency: document.getElementById('s_currency').value.trim() || '$',
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
      push: document.getElementById('s_git_enabled').checked,
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
    // 刷新内存中的站点配置, 下次打开设置弹窗显示已保存的值
    try {
      const r2 = await fetch('/api/settings');
      SITE_DEFAULT = await r2.json();
    } catch (e) {}
    setStatus((LANG==='zh'?'网站设置已保存 · ':'Settings saved · ') + (j.git ? I18N[LANG].gitPublished : I18N[LANG].notPushed + (j.git_msg||'')), true);
    hideSettings();
  } catch (e) {
    setStatus((I18N[LANG].err||'出错：') + e.message, false);
  }
}

/* ============ 中英文切换 ============ */
const I18N = {
  zh: {
    pageTitle:'商品管理后台', dupListing:'这个 Facebook Marketplace 商品已经添加过了：「{name}」',
    title:'商品管理后台', preview:'预览首页 →', settings:'⚙ 网站设置', addItem:'＋ 新增商品',
    tip:'改动后自动重写 index.html。图片上传到 images/ 文件夹。',
    thImg:'图片', thName:'名称', thPrice:'价格', thDesc:'简介', thLink:'购买链接', thOp:'操作',
    editItem:'编辑商品', lblName:'名称', lblPrice:'价格（直接输入数字，自动加符号）', lblSym:'货币符号（留空用全局默认）', lblDesc:'简介',
    lblBuyLink:'购买链接 (Facebook Marketplace 页)', lblBuyText:'购买按钮文案（留空用全局默认）', lblImg:'商品图片',
    lblImgHint:'可上传多张，第一张为列表封面。点 “＋ 添加图片” 继续选择。', lblCover:'封面',
    addImgBtn:'＋ 添加图片', btnLinkImg:'＋ 外链图片', uploading:'上传图片…',
    compressing:'压缩图片…', promptImgUrl:'粘贴外部图片链接 (http/https)，不占用本地存储：', errBadUrl:'链接无效，请输入 http:// 或 https:// 开头的图片地址',
    btnCancel:'取消', btnSave:'保存',
    settingsTitle:'网站设置', lblSiteTitle:'站点标题（浏览器标签）', lblLogo:'Logo 文字',
    lblTagline:'顶部副标题 Tagline（留空则不显示）', lblHeroTitle:'首页大标题', lblHeroSub:'首页副标题说明', lblHeroNote:'首页小徽标',
    lblBuyDefault:'购买按钮全局默认文案', lblCurrency:'货币符号（价格前缀，默认 $）', lblFooterMain:'页脚主文案（留空则不显示）', lblFooterSub:'页脚副文案（留空则不显示）',
    lblTheme:'默认配色主题', optAuto:'跟随系统 (auto)', optLight:'浅色', optDark:'深色',
    legendLight:'浅色模式配色', legendDark:'深色模式配色', spBg:'背景', spText:'文字', spSub:'次要文字',
    legendGit:'Git 自动发布', lblGitRemote:'远程仓库地址 (GitHub)', spBranch:'分支', spPrefix:'提交前缀',
    cbAutoPublish:'保存后自动发布（自动 commit + push）', btnPublishNow:'立即发布', btnSaveSettings:'保存设置',
    lblGitUser:'GitHub 用户名', lblGitToken:'GitHub Token (PAT)', btnSaveGitAuth:'保存 GitHub 凭据',
    gitCredNeedBoth:'请填写 GitHub 用户名和 Token（登录后推送用，不回显）', gitCredSaving:'正在保存凭据…',
    gitCredOk:'凭据已保存·', gitCredFail:'凭据保存失败: ',
    legendFbLogin:'Facebook 登录（抓取完整图片轮播 + 价格）',
    lblFbLoginHint:'默认只能抓到一张封面图、价格也常抓不到；登录一次后，导入商品时会用登录态渲染出完整的图片轮播和价格。',
    btnFbLogin:'登录 Facebook',
    fbLoginUnavailable:'未安装 playwright, 此功能不可用', fbLoginRunning:'登录窗口已打开，请在窗口里登录后关闭它…',
    fbLoginOk:'已登录 ✓', fbLoginNone:'尚未登录', fbLoginUnknown:'读取登录状态失败', fbLoginFail:'打开登录窗口失败: ',
    searchPlaceholder:'搜索商品（名称/简介/链接）…', countItems:'共 {n} 件商品', matchItems:' · 匹配 {n} 条',
    uploadStats:'· 累计上传 {total} · 过去12小时 {last12h} · 预计收益 ${earnings}',
    rowEdit:'编辑', rowDel:'删', btnAdd:'＋ 新增商品', openLink:'打开',
    btnScrape:'◆ 从 Marketplace 链接导入', lblScrapeHint:'自动提取 名称/价格/简介/图片链接（不下载）',
    promptScrapeUrl:'粘贴 Facebook Marketplace 商品链接（自动提取图片链接/价格/简介）：',
    scraping:'正在从链接提取信息…', scraped:'已从链接导入，请核对后保存。', foundImgs:'已提取 {n} 张图片（外链，未下载）。', scrapeFail:'提取失败，请检查链接或稍后再试。',
    ready:'就绪', statusSaved:'已保存 {n} 件商品 · ', gitPublished:'自动发布中', notPushed:'未提交:', saving:'保存设置…',
    err:'出错：', addProductTitle:'新增商品'
  },
  en: {
    pageTitle:'Product Admin', dupListing:'This Facebook Marketplace listing has already been added: "{name}"',
    title:'Item Admin', preview:'Preview →', settings:'⚙ Settings', addItem:'＋ Add Item',
    tip:'Every change rewrites index.html. Uploaded images go into images/.',
    thImg:'Image', thName:'Name', thPrice:'Price', thDesc:'Description', thLink:'Buy Link', thOp:'Actions',
    editItem:'Edit Item', lblName:'Name', lblPrice:'Price (type numbers, symbol added automatically)', lblSym:'Currency symbol (blank = global default)', lblDesc:'Description',
    lblBuyLink:'Buy Link (Facebook Marketplace)', lblBuyText:'Buy button text (blank = global default)', lblImg:'Images',
    lblImgHint:'You can add multiple images. The first one is the cover. Click “＋ Add image” to add more.', lblCover:'Cover',
    addImgBtn:'＋ Add image', btnLinkImg:'＋ Image URL', uploading:'Uploading…',
    compressing:'Compressing image…', promptImgUrl:'Paste an external image URL (http/https), no local storage used:', errBadUrl:'Invalid URL. Provide an address starting with http:// or https://',
    btnCancel:'Cancel', btnSave:'Save',
    settingsTitle:'Settings', lblSiteTitle:'Site title (browser tab)', lblLogo:'Logo text',
    lblTagline:'Tagline (blank = hidden)', lblHeroTitle:'Homepage headline', lblHeroSub:'Homepage subtitle', lblHeroNote:'Homepage badge',
    lblBuyDefault:'Default buy-button text', lblCurrency:'Currency symbol (price prefix, default $)', lblFooterMain:'Footer main text (blank = hidden)', lblFooterSub:'Footer sub text (blank = hidden)',
    lblTheme:'Default theme', optAuto:'Follow system (auto)', optLight:'Light', optDark:'Dark',
    legendLight:'Light palette', legendDark:'Dark palette', spBg:'Background', spText:'Text', spSub:'Muted text',
    legendGit:'Git auto-publish', lblGitRemote:'Remote repository (GitHub)', spBranch:'Branch', spPrefix:'Commit prefix',
    cbAutoPublish:'Auto publish on save (commit + push)', btnPublishNow:'Publish now', btnSaveSettings:'Save settings',
    lblGitUser:'GitHub username', lblGitToken:'GitHub Token (PAT)', btnSaveGitAuth:'Save GitHub credentials',
    gitCredNeedBoth:'Fill in both the GitHub username and a Token (used for push, never echoed)', gitCredSaving:'Saving credentials…',
    gitCredOk:'Credentials saved ·', gitCredFail:'Failed to save credentials: ',
    legendFbLogin:'Facebook login (fetch full photo carousel + price)',
    lblFbLoginHint:'Without logging in, only one cover photo can be scraped and price is often missed too. Log in once and imports will render the listing with a real session to grab all photos and the price.',
    btnFbLogin:'Log in to Facebook',
    fbLoginUnavailable:'playwright not installed, this feature is unavailable', fbLoginRunning:'Login window is open, log in and close it…',
    fbLoginOk:'Logged in ✓', fbLoginNone:'Not logged in yet', fbLoginUnknown:'Failed to read login status', fbLoginFail:'Failed to open login window: ',
    searchPlaceholder:'Search items (name/desc/link)…', countItems:'{n} items', matchItems:' · {n} shown',
    uploadStats:'· {total} uploaded all-time · {last12h} in the last 12h · ${earnings} earned',
    rowEdit:'Edit', rowDel:'Del', btnAdd:'＋ Add Item', openLink:'Open',
    btnScrape:'◆ Import from Marketplace link', lblScrapeHint:'Auto-fills name/price/description/image links (kept as links, not downloaded)',
    promptScrapeUrl:'Paste a Facebook Marketplace item link (extracts image links/price/description):',
    scraping:'Extracting info from link…', scraped:'Imported from link — please verify before saving.', foundImgs:'Extracted {n} images (external links, not downloaded).', scrapeFail:'Extraction failed. Check the link or try again.',
    ready:'Ready', statusSaved:'Saved {n} items · ', gitPublished:'auto-publishing', notPushed:'not pushed:', saving:'Saving…',
    err:'Error: ', addProductTitle:'Add Item'
  }
};
let LANG = localStorage.getItem('bw_admin_lang') || 'zh';

// 服务端错误按当前界面语言显示: 带 code 的错误用 I18N 文案, 其它原样
function apiErr(j, fallback) {
  const d = I18N[LANG] || I18N.zh;
  if (j && j.code === 'dup_listing' && d.dupListing) return d.dupListing.replace('{name}', j.name || '');
  return (j && j.error) || fallback;
}
function applyLang() {
  const d = I18N[LANG] || I18N.zh;
  if (d.pageTitle) document.title = d.pageTitle;
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.getAttribute('data-i18n');
    if (d[k] != null) el.textContent = d[k];
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {
    const k = el.getAttribute('data-i18n-ph');
    if (d[k] != null) el.placeholder = d[k];
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
loadStats();
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

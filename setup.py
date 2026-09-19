#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fb-image-check 一键装机脚本: 在专门跑图片自愈任务的 Raspberry Pi / Oracle 服务器上部署
check_fb_images.py 的 systemd 定时任务(见 check_fb_images.py 顶部说明)。

两种跑法:
  1) 本地已经 clone 好仓库, 在仓库目录里直接跑:
       sudo python3 setup.py
  2) 全新机器, 用 curl 一条命令跑(脚本自己会 git clone 仓库):
       curl -fsSL https://raw.githubusercontent.com/Ethan13322836698/nocode-zero-backend-webui-publisher/main/setup.py | sudo python3 -

     clone 到哪里默认是 ~/nocode-zero-backend-webui-publisher, 可用环境变量
     FBIC_INSTALL_DIR 覆盖, 例如:
       curl -fsSL .../setup.py | sudo FBIC_INSTALL_DIR=/opt/fb-shop python3 -

职责:
  1. 检查 sudo 权限(装 systemd unit 到 /etc/systemd/system/ 需要)。
  2. clone(或同步已有 checkout 到 origin/main) —— 这台机器的仓库应专门用来跑这个
     自愈任务, 不要在上面手动改东西: check_fb_images.py 靠 server.py 的
     git_commit_push() 发布, 那个函数是整仓库 git add -A, 手动改动会被自愈任务的
     commit 一起带走; 每次重跑本脚本都会把这份 checkout 强制同步回 origin/main。
  3. 建/复用 .venv, 装 requirements.txt + playwright chromium。
  4. 校验运行所需、但本脚本不该也不能自动生成的前置文件(site.json 里的 git 配置、
     Facebook 登录态 .fb_browser_profile/ 或 fb_cookie、fb.cookie.key) —— 这些是每
     台机器各自的敏感数据, 只能人工从已经配置好的机器上拷过来。**没有这些文件本脚本
     依然会把 systemd 定时任务装好并启用**: check_fb_images.py 在没有 Facebook 登录
     态时会优雅降级(抓不到图就跳过、记一条日志), 不会报错崩溃, 所以缺凭证不阻塞装机
     —— 之后随时把文件拷过来, 下一次 10 分钟 tick 就会自动生效, 不用重跑本脚本。
  5. 渲染 systemd/fb-image-check.{service,timer} 模板, 写到 /etc/systemd/system/,
     daemon-reload + enable --now。
"""
import os
import subprocess
import sys

REPO_URL = "https://github.com/Ethan13322836698/nocode-zero-backend-webui-publisher.git"
DEFAULT_INSTALL_DIR = os.environ.get(
    "FBIC_INSTALL_DIR", os.path.expanduser("~/nocode-zero-backend-webui-publisher"))

SERVICE_NAME = "fb-image-check.service"
TIMER_NAME = "fb-image-check.timer"
SYSTEMD_DIR = "/etc/systemd/system"


def fail(msg):
    print("[错误] " + msg)
    sys.exit(1)


def run(cmd):
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def check_sudo():
    if os.name != "posix" or os.geteuid() != 0:
        fail("需要 root 权限来安装 systemd 服务，请用: sudo python3 setup.py "
             "(或 curl ... | sudo python3 -)")


def bootstrap_clone(target):
    """把仓库 clone 到 target, 如果已经在那儿了就同步到最新 origin/main。这份
    checkout 是自愈任务专用的, 强制 reset --hard 是有意为之, 不是风险 —— 不该有人在
    这上面手动改东西又不提交。site.json / .fb_browser_profile / fb.cookie.key 都是
    git 忽略的本机文件, reset --hard 不会碰它们。"""
    if os.path.isdir(os.path.join(target, ".git")):
        print("仓库已存在于 %s, 同步到最新 origin/main ..." % target)
        run(["git", "-C", target, "fetch", "origin"])
        run(["git", "-C", target, "checkout", "main"])
        run(["git", "-C", target, "reset", "--hard", "origin/main"])
    elif os.path.exists(target):
        fail("%s 已存在但不是 git 仓库 —— 换个目录(设 FBIC_INSTALL_DIR 环境变量)"
             "或先清理掉这个目录。" % target)
    else:
        print("clone 仓库到 %s ..." % target)
        run(["git", "clone", REPO_URL, target])
    return target


def resolve_repo_dir():
    """本地已经在仓库目录里跑(`sudo python3 setup.py`) -> 直接用这个目录。
    通过 curl 管道跑(`curl ... | sudo python3 -`) -> 没有真实的 __file__, 走
    clone/同步流程。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.exists(os.path.join(here, "server.py")):
            return here
    except NameError:
        pass  # 管道运行没有 __file__, 必然走下面的 clone 流程
    return bootstrap_clone(DEFAULT_INSTALL_DIR)


def setup_venv(repo_dir):
    venv_dir = os.path.join(repo_dir, ".venv")
    venv_py = os.path.join(venv_dir, "bin", "python3")
    if not os.path.exists(venv_py):
        print("创建虚拟环境 .venv ...")
        run([sys.executable, "-m", "venv", venv_dir])
    pip = os.path.join(venv_dir, "bin", "pip")
    req = os.path.join(repo_dir, "requirements.txt")
    if os.path.exists(req):
        run([pip, "install", "-r", req])
    print("安装 Playwright Chromium(约 300MB+，请确保网络和磁盘空间足够；"
          "树莓派/Oracle ARM 云主机务必在真机上实测这一步能跑通，"
          "不要只在 x86 开发机上验证过就假设 ARM 也行)...")
    run([venv_py, "-m", "playwright", "install", "chromium"])
    return venv_py


def check_prerequisites(repo_dir, server):
    """校验 site.json / Facebook 登录态。这些不能由装机脚本自动生成，只能从已经配置
    好的机器上拷过来。返回问题列表(不为空只是警告，不阻塞装机 —— 见文件头说明)。"""
    problems = []

    site_file = os.path.join(repo_dir, "site.json")
    if not os.path.exists(site_file):
        problems.append(
            "缺少 site.json —— 请从已经配置好 Git 自动发布的管理机上把 site.json 拷贝"
            "过来(或手动创建并填好 git.remote_url / git.branch)。")
    else:
        git_cfg = server.load_git()
        if not (git_cfg.get("remote_url") or "").strip():
            problems.append("site.json 里的 git.remote_url 是空的 —— 没有它 push 无从谈起。")
        if not (git_cfg.get("branch") or "").strip():
            problems.append("site.json 里的 git.branch 是空的。")

    profile_dir = os.path.join(repo_dir, ".fb_browser_profile")
    has_profile = os.path.isdir(profile_dir) and bool(os.listdir(profile_dir))
    try:
        has_cookie = bool(server.fb_cookie())
    except Exception:
        has_cookie = False

    if not has_profile and not has_cookie:
        problems.append(
            "既没有 .fb_browser_profile/(Playwright 登录态)也没有可用的 fb_cookie —— "
            "抓取 Facebook Marketplace 大概率会被登录墙拦住。请在已经用管理后台"
            "「登录 Facebook」按钮登录过的机器上，把 .fb_browser_profile/ 整个目录"
            "(约 175MB，请规划好传输时间和存储空间)用 rsync/scp 拷贝过来。")

    key_file = os.path.join(repo_dir, "fb.cookie.key")
    if has_cookie and not os.path.exists(key_file):
        problems.append(
            "site.json 里有加密的 fb_cookie，但本机没有 fb.cookie.key 解密密钥 —— 同样"
            "需要从原机器拷过来；密钥丢了，对应的 Cookie 就永久废了，只能重新登录生成。")

    return problems


def render_units(repo_dir, venv_py):
    tmpl_dir = os.path.join(repo_dir, "systemd")
    rendered = {}
    for name in (SERVICE_NAME, TIMER_NAME):
        with open(os.path.join(tmpl_dir, name), "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("__REPO_DIR__", repo_dir).replace("__PYTHON__", venv_py)
        rendered[name] = content
    return rendered


def install_units(rendered):
    for name, content in rendered.items():
        dest = os.path.join(SYSTEMD_DIR, name)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(content)
        print("已写入 " + dest)
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "--now", TIMER_NAME])


def main():
    check_sudo()
    repo_dir = resolve_repo_dir()
    sys.path.insert(0, repo_dir)
    import server  # noqa: E402  (需要先确定 repo_dir 才能导入)

    venv_py = setup_venv(repo_dir)
    problems = check_prerequisites(repo_dir, server)
    rendered = render_units(repo_dir, venv_py)
    install_units(rendered)

    print("\n完成。仓库目录: %s" % repo_dir)
    print("查看状态:           systemctl status %s" % TIMER_NAME)
    print("查看日志:           journalctl -t fb-image-check -f")
    print("下次计划运行时间:   systemctl list-timers %s" % TIMER_NAME)
    print("手动立即触发一次:   systemctl start %s" % SERVICE_NAME)

    if problems:
        print("\n[提醒] 定时任务已启用，但下面这些前置条件还没满足，"
              "在此之前它每轮都会抓不到东西、什么也修不了(不会报错崩溃，只是白跑):\n")
        for p in problems:
            print("  - " + p)
        print("\n补齐后不用重跑本脚本，下一次 10 分钟 tick 会自动生效。")


if __name__ == "__main__":
    main()

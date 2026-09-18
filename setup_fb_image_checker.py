#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fb-image-check 装机脚本: 在专门跑图片自愈任务的 Raspberry Pi / Oracle 服务器上一键部署
check_fb_images.py 的 systemd 定时任务(见 check_fb_images.py 顶部说明)。

职责:
  1. 检查 sudo 权限(装 systemd unit 到 /etc/systemd/system/ 需要)。
  2. 建/复用 .venv, 装 requirements.txt + playwright chromium。
  3. 校验运行所需、但本脚本不该也不能自动生成的前置文件(site.json 里的 git 配置、
     Facebook 登录态) —— 这些是每台机器各自的敏感数据, 需要人工从已经配置好的机器上
     拷过来, 装机脚本只负责检查并给出清晰提示, 不代为生成。
  4. 渲染 systemd/fb-image-check.{service,timer} 模板, 写到 /etc/systemd/system/,
     daemon-reload + enable --now。

用法: sudo python3 setup_fb_image_checker.py

注意: 这台机器的仓库最好专门用来跑这个自愈任务。check_fb_images.py 靠 server.py 的
git_commit_push() 发布, 那个函数是整仓库 git add -A —— 如果同一份 checkout 还有人在
手动改东西又不提交, 会被自愈任务的 commit 一起带走。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # noqa: E402

SERVICE_NAME = "fb-image-check.service"
TIMER_NAME = "fb-image-check.timer"
SYSTEMD_DIR = "/etc/systemd/system"


def fail(msg):
    print("[错误] " + msg)
    sys.exit(1)


def run(cmd):
    import subprocess
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def check_sudo():
    if os.name != "posix" or os.geteuid() != 0:
        fail("需要 root 权限来安装 systemd 服务，请用: sudo python3 setup_fb_image_checker.py")


def setup_venv():
    venv_dir = os.path.join(HERE, ".venv")
    venv_py = os.path.join(venv_dir, "bin", "python3")
    if not os.path.exists(venv_py):
        print("创建虚拟环境 .venv ...")
        run([sys.executable, "-m", "venv", venv_dir])
    pip = os.path.join(venv_dir, "bin", "pip")
    req = os.path.join(HERE, "requirements.txt")
    if os.path.exists(req):
        run([pip, "install", "-r", req])
    print("安装 Playwright Chromium(约 300MB+，请确保网络和磁盘空间足够；"
          "树莓派/Oracle ARM 云主机务必在真机上实测这一步能跑通，"
          "不要只在 x86 开发机上验证过就假设 ARM 也行)...")
    run([venv_py, "-m", "playwright", "install", "chromium"])
    return venv_py


def check_prerequisites():
    """校验 site.json / Facebook 登录态。这些不能由装机脚本自动生成，只能从已经配置好
    的机器上拷过来(见 AGENTS.md 里对 site.json / .fb_browser_profile / fb.cookie.key
    的说明)。"""
    problems = []

    site_file = os.path.join(HERE, "site.json")
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

    profile_dir = os.path.join(HERE, ".fb_browser_profile")
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
            "(约 175MB，请规划好传输时间和存储空间)用 rsync/scp 拷贝过来 —— 这一步装机"
            "脚本不会代为处理，无头自动登录不现实(可能撞上二次验证)。")

    key_file = os.path.join(HERE, "fb.cookie.key")
    if has_cookie and not os.path.exists(key_file):
        problems.append(
            "site.json 里有加密的 fb_cookie，但本机没有 fb.cookie.key 解密密钥 —— 同样"
            "需要从原机器拷过来；密钥丢了，对应的 Cookie 就永久废了，只能重新登录生成。")

    if problems:
        print("\n以下前置条件没满足，装机脚本不会代为处理(涉及每台机器各自的敏感数据):\n")
        for p in problems:
            print("  - " + p)
        print("\n解决以上问题后重新运行本脚本。")
        sys.exit(1)


def render_units(venv_py):
    tmpl_dir = os.path.join(HERE, "systemd")
    rendered = {}
    for name in (SERVICE_NAME, TIMER_NAME):
        with open(os.path.join(tmpl_dir, name), "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("__REPO_DIR__", HERE).replace("__PYTHON__", venv_py)
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
    venv_py = setup_venv()
    check_prerequisites()
    rendered = render_units(venv_py)
    install_units(rendered)

    print("\n完成。")
    print("查看状态:           systemctl status %s" % TIMER_NAME)
    print("查看日志:           journalctl -t fb-image-check -f")
    print("下次计划运行时间:   systemctl list-timers %s" % TIMER_NAME)
    print("手动立即触发一次:   systemctl start %s" % SERVICE_NAME)


if __name__ == "__main__":
    main()

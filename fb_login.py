#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过 SSH 转发 Chromium 调试端口，在无头设备上完成 Facebook 登录。"""
import argparse
import errno
import getpass
import ipaddress
import os
import pwd
import re
import signal
import shlex
import socket
import subprocess
import time

import server


def paste_to_browser(port):
    """从交互终端隐式读取文本，仅填入 Facebook 当前选中的输入框。"""
    import sys
    from urllib.parse import urlsplit
    if not sys.stdin.isatty():
        raise RuntimeError("请在交互式 SSH 终端运行，输入内容不会回显")
    sync_playwright = server._playwright_sync()
    if not sync_playwright:
        raise RuntimeError("未安装 Playwright")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        # 不调用 browser.close()：这里只断开调试连接，保留用户的登录窗口。
        pages = [page for ctx in browser.contexts for page in ctx.pages
                 if urlsplit(page.url).hostname in ("facebook.com", "www.facebook.com", "m.facebook.com")]
        targets = []
        for page in pages:
            focused = page.locator("input:focus:not([type=hidden]):not([type=submit]):not([type=button]), textarea:focus")
            if focused.count() == 1 and focused.is_visible() and focused.is_editable():
                targets.append((page, focused))
        if len(targets) != 1:
            raise RuntimeError("请在远程画面中点击 Facebook 的邮箱或密码框，并只保留一个 Facebook 登录页面")
        page, field = targets[0]
        value = getpass.getpass("粘贴要填入的内容，然后按回车（不显示）：")
        if not value:
            print("内容为空，未填入。")
            return
        # 再次校验原来的目标仍在当前 Facebook 页面，避免等待输入期间页面跳转。
        if urlsplit(page.url).hostname not in ("facebook.com", "www.facebook.com", "m.facebook.com"):
            raise RuntimeError("页面已跳转，请重新选中 Facebook 输入框")
        field.fill(value, timeout=3000)
        print("已填入选中的输入框，未提交登录。")


def prepare_debug_port(port):
    """关闭占用此端口的本项目旧浏览器，其他程序不处理。"""
    def available():
        with socket.socket() as sock:
            # 浏览器退出后的 TIME_WAIT 不应误报端口占用。
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
                return False

    if available():
        return
    try:
        result = subprocess.run(["ss", "-ltnp", "sport = :%d" % port],
                                capture_output=True, text=True, timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("端口 %d 被占用，无法识别进程，请关闭旧登录窗口或使用 --port 9223" % port) from None
    stopped = False
    for pid in set(re.findall(r"pid=(\d+)", result.stdout)):
        proc = "/proc/%s" % pid
        try:
            if os.stat(proc).st_uid != os.getuid():
                continue
            with open(proc + "/cmdline", "rb") as f:
                args = f.read().decode(errors="replace").split("\0")
            if ("--user-data-dir=" + server.FB_PROFILE_DIR not in args
                    or "--remote-debugging-port=%d" % port not in args
                    or not any(word in os.path.basename(args[0]).lower() for word in ("chrome", "chromium"))):
                continue
            os.kill(int(pid), signal.SIGTERM)
            print("已关闭占用端口 %d 的本项目旧浏览器 (PID %s)，等待会话保存…" % (port, pid), flush=True)
            stopped = True
        except ProcessLookupError:
            continue
    if stopped:
        for _ in range(50):
            if available():
                return
            time.sleep(0.1)
    raise RuntimeError("端口 %d 仍被占用；未强制终止其他程序，请使用 --port 9223" % port)


def connection_command(port):
    """优先沿用当前 SSH 的设备地址和端口，否则探测本机局域网地址。"""
    username = pwd.getpwuid(os.getuid()).pw_name
    host = None
    ssh_port = 22
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) == 4:
        try:
            address = ipaddress.ip_address(connection[2])
            parsed_port = int(connection[3])
            if not address.is_loopback and not address.is_unspecified and 1 <= parsed_port <= 65535:
                host, ssh_port = str(address), parsed_port
        except ValueError:
            pass
    if host is None:
        try:
            result = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                                    timeout=3, check=True)
            addresses = []
            for value in result.stdout.split():
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if not (address.is_loopback or address.is_unspecified or address.is_link_local):
                    addresses.append(address)
            # 多网卡时优先选择局域网 IPv4，其他地址同时显示供用户判断。
            addresses.sort(key=lambda a: (a.version != 4, not a.is_private))
            if addresses:
                host = str(addresses[0])
                if len(addresses) > 1:
                    print("设备可用 IP：%s" % ", ".join(map(str, addresses)), flush=True)
        except (OSError, subprocess.SubprocessError):
            pass
    if host is None:
        host = socket.gethostname()
        print("未检测到可用 IP，改用设备主机名；请确认电脑能解析该名称。", flush=True)
    command = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes", "-L",
               "127.0.0.1:%d:127.0.0.1:%d" % (port, port)]
    if ssh_port != 22:
        command += ["-p", str(ssh_port)]
    command += ["-l", username, host]
    return shlex.join(command)


def login(port=9222, timeout=1800):
    """临时开放本机调试端口，退出时关闭浏览器并保存现有配置目录。"""
    sync_playwright = server._playwright_sync()
    if not sync_playwright:
        raise RuntimeError("未安装 Playwright，请先安装 requirements.txt 和 Chromium")
    # 提前检查占用，避免误把其他浏览器的调试页面当成本次登录。
    prepare_debug_port(port)

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                server.FB_PROFILE_DIR, headless=True, channel="chromium",
                args=["--remote-debugging-address=127.0.0.1",
                      "--remote-debugging-port=%d" % port],
                viewport={"width": 1280, "height": 900})
        except Exception as e:
            raise RuntimeError(
                "无法启动 Chromium (%s)。请确认已运行 python -m playwright install chromium，"
                "并关闭后台 Facebook 登录窗口、等待图片修复任务结束后重试。"
                % type(e).__name__) from None
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            print("调试端口：127.0.0.1:%d（仅本机）" % port, flush=True)
            print("已自动检测用户名和设备地址，在电脑另开终端复制执行：", flush=True)
            print(connection_command(port), flush=True)
            print("电脑 Chrome 打开 chrome://inspect/#devices，Configure 添加 localhost:%d；"
                  "点击 Facebook 页的 inspect，再打开 Toggle screencast，在画面中登录。" % port,
                  flush=True)
            print("登录完成后在本终端按 Ctrl+C 保存并退出；最多开放 %d 分钟。"
                  % (timeout // 60), flush=True)
            try:
                page.goto("https://www.facebook.com/login", wait_until="domcontentloaded",
                          timeout=30000)
            except Exception:
                print("登录页加载超时，请在远程 DevTools 中刷新或重新打开 Facebook。", flush=True)
            deadline = time.monotonic() + timeout
            try:
                while ctx.pages and time.monotonic() < deadline:
                    ctx.pages[0].wait_for_timeout(1000)
            except KeyboardInterrupt:
                pass
        finally:
            ctx.close()
            print("浏览器已关闭，调试端口已停止；会话保存在本机 .fb_browser_profile/。"
                  "是否登录成功请运行 ./fix.sh --dry-run 验证。", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9222, help="本机调试端口，默认 9222")
    parser.add_argument("--connection-info", action="store_true", help="只显示自动生成的 SSH 命令，不启动浏览器")
    parser.add_argument("--paste", action="store_true", help="从终端隐藏读取粘贴内容，填入 Facebook 当前选中的输入框")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("端口须在 1024–65535 之间")
    if args.connection_info:
        print(connection_command(args.port))
        return
    try:
        if args.paste:
            try:
                paste_to_browser(args.port)
            except (EOFError, KeyboardInterrupt):
                print("\n已取消。")
            except Exception:
                raise RuntimeError("填入失败：请确认登录浏览器仍运行，并在 Facebook 画面中选中输入框后重试") from None
            return
        login(args.port)
    except (OSError, RuntimeError) as e:
        parser.exit(1, "登录启动失败：%s\n" % e)


if __name__ == "__main__":
    main()

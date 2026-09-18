#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Facebook CDN 图片自愈检查器 (fb-image-check)

单次运行, 由 systemd timer 每 10 分钟触发一次(见 systemd/fb-image-check.{service,timer})。
只处理 products.json 里的 https://scontent... (Facebook CDN) 图片链接: 这些链接带签名
且会过期(oe= 十六进制过期时间戳), 过期后线上站点就会掉图, 但本地 images/ 里的图和其它
外链图片不受影响、本脚本也不碰它们。

流程: 找出已失效的 Facebook CDN 图片链接 -> 按商品分组 -> 用商品的 Marketplace 链接
(buy 字段)重新抓取该商品 -> 按 Facebook 稳定的"照片ID"把失效链接替换成新链接 -> 一次性
commit + push。

本脚本直接 `import server`, 复用 server.py 里已有的抓取/发布逻辑(server.py 的 HTTP 服务
启动被 `if __name__ == "__main__"` 保护, import 不会意外拉起后台服务)。

依赖: 与 server.py 一致, 仅标准库 + 可选 playwright(通过 server.scrape_marketplace 间接
使用, 未安装时该函数自动退化为只保留封面图, 不会报错)。锁文件用 fcntl, 目标平台为
Linux/macOS。
"""
import argparse
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # noqa: E402  (需要先把仓库目录塞进 sys.path)

# Facebook CDN 图片文件名里的稳定"照片ID"段, 和 server.py 的 _dedup_photo_urls 用同一个
# 正则(server.py:844) —— 同一张照片重新签名后 oh=/oe=/ohc= 会变, 但这段 ID 不变。
PHOTO_ID_RE = re.compile(r"/(\d+)_\d+_[a-z0-9_]*\.(?:jpg|jpeg|png|webp)", re.I)
OE_PARAM_RE = re.compile(r"[?&]oe=([0-9a-fA-F]+)")

MAX_WORKERS = 10
CIRCUIT_BREAKER_RATIO = 0.45  # 单轮里失效比例超过这个阈值, 判定为网络/CDN 被限流而非真过期
CHECK_TIMEOUT = 8
OE_PREFILTER_BUFFER = 3600  # oe= 过期时间超过这个缓冲(秒)以外的链接, 跳过本轮存活检测


def is_fb_cdn_url(u):
    return (u or "").startswith("https://scontent")


def extract_photo_id(u):
    m = PHOTO_ID_RE.search(u or "")
    return m.group(1) if m else None


def build_photo_id_map(urls):
    """新抓取结果的 照片ID -> 最新链接 映射, 保留先出现的。"""
    m = {}
    for u in urls or []:
        pid = extract_photo_id(u)
        if pid and pid not in m:
            m[pid] = u
    return m


def oe_expiry_ts(u):
    m = OE_PARAM_RE.search(u or "")
    if not m:
        return None
    try:
        return int(m.group(1), 16)
    except ValueError:
        return None


def needs_live_check(u, buffer_seconds=OE_PREFILTER_BUFFER):
    """oe= 签名过期时间明显还早的链接直接跳过存活检测, 省网络请求、也减少对 FB 的请求量。"""
    exp = oe_expiry_ts(u)
    if exp is None:
        return True
    return exp <= time.time() + buffer_seconds


def check_url_liveness(url, timeout=CHECK_TIMEOUT):
    """返回三态: True=存活 / False=确认失效 / None=无法判断(网络问题, 本轮当作未失效处理)。

    先用 HEAD(轮换 UA, 复用 server._UA_LIST), 403/404/410 视为确认失效; 其余不确定的
    响应(部分 fbcdn 边缘节点对 HEAD 处理不一致)退回小范围 GET(Range: bytes=0-1024)兜底。
    """
    for ua in server._UA_LIST:
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if 200 <= status < 300:
                    return True
                if status in (403, 404, 410):
                    return False
                break  # 状态不确定, 跳出去用 GET 兜底
        except urllib.error.HTTPError as e:
            if e.code in (403, 404, 410):
                return False
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            continue  # 网络层错误, 换下一个 UA 重试

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": server._UA_LIST[0], "Range": "bytes=0-1024"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= status < 300 or status == 206
    except urllib.error.HTTPError as e:
        return e.code not in (403, 404, 410)
    except Exception:
        return None


def load_products_from(path):
    if path is None:
        return server.load_products()
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_products_to(products, path):
    target = path or server.DATA_FILE
    with open(target, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)


def acquire_lock():
    """防止上一轮还没跑完时被下一次 timer 触发重叠执行。systemd 对同一个 oneshot 服务
    单元本身就不会并发起两个实例, 这里的文件锁是双保险, 也方便手动测试时不会跟 timer 撞车。
    锁文件特意放仓库目录之外(RuntimeDirectory 或 /tmp), 避免被 git add -A 误提交。"""
    lock_dir = os.environ.get("RUNTIME_DIRECTORY") or "/tmp"
    lock_path = os.path.join(lock_dir, "fb-image-check.lock")
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("fb-image-check: 已有一轮在跑(锁文件 %s), 本次跳过" % lock_path)
        sys.exit(0)
    return fh


def run(products_path=None, dry_run=False):
    products = load_products_from(products_path)

    candidates = []  # (product_idx, img_idx, url)
    for pi, p in enumerate(products):
        for ii, u in enumerate(server.product_imgs(p)):
            if is_fb_cdn_url(u):
                candidates.append((pi, ii, u))

    to_check = [c for c in candidates if needs_live_check(c[2])]

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(check_url_liveness, c[2]): c[2] for c in to_check}
        for fut in as_completed(futs):
            url = futs[fut]
            try:
                results[url] = fut.result()
            except Exception:
                results[url] = None

    checked = len(to_check)
    broken_count = sum(1 for v in results.values() if v is False)
    inconclusive_count = sum(1 for v in results.values() if v is None)

    # 熔断只看"判不出来"(None, 网络层失败/超时)的比例, 不看确认失效(403/404/410)的比例:
    # 后者是每张图各自独立拿到的明确 HTTP 状态码, 本身就是可信信号(哪怕一次性一大批同时
    # 过期也是真的, 这批号本来就常常是同批导入、oe= 过期时间彼此接近); 真正"分不清是被
    # 限流还是真过期"的情况, 表现是大量请求连不上/超时(None), 而不是干净的确认失效状态码。
    if checked and (inconclusive_count / checked) > CIRCUIT_BREAKER_RATIO:
        print("fb-image-check: 熔断 - 本轮 %d/%d 张判不出结果(超过 %.0f%%, 多为连接失败/超时), "
              "疑似网络或被 CDN 限流, 本轮跳过重新抓取(留到下一轮重试)"
              % (inconclusive_count, checked, CIRCUIT_BREAKER_RATIO * 100))
        print("fb-image-check: checked=%d broken=%d fixed=0 unresolved=0 "
              "products_skipped=0 pushed=False" % (checked, broken_count))
        return

    broken_by_product = defaultdict(list)
    for pi, ii, u in to_check:
        if results.get(u) is False:
            broken_by_product[pi].append((ii, u))

    fixed_total = 0
    unresolved_total = 0
    products_touched = 0
    products_skipped_no_buy = 0
    unresolved_lines = []

    for pi, items in broken_by_product.items():
        p = products[pi]
        name = p.get("name") or "?"
        buy = (p.get("buy") or "").strip()

        if not buy:
            products_skipped_no_buy += 1
            unresolved_total += len(items)
            unresolved_lines.append(
                'fb-image-check: UNRESOLVED product=%r buy="" reason="no buy URL, cannot rescrape"' % name)
            continue

        result = server.scrape_marketplace(buy)
        if not result.get("ok"):
            unresolved_total += len(items)
            unresolved_lines.append(
                'fb-image-check: UNRESOLVED product=%r buy=%s reason="scrape_marketplace failed: %s"'
                % (name, buy, result.get("error") or "unknown"))
            continue

        fresh_map = build_photo_id_map(result.get("imgs"))
        imgs = server.product_imgs(p)
        product_fixed = 0

        for ii, old_url in items:
            pid = extract_photo_id(old_url)
            new_url = fresh_map.get(pid) if pid else None
            if not new_url:
                unresolved_total += 1
                unresolved_lines.append(
                    'fb-image-check: UNRESOLVED product=%r buy=%s reason="photo id %s not found in fresh scrape"'
                    % (name, buy, pid or old_url))
                continue
            if ii < len(imgs) and imgs[ii] == old_url:
                imgs[ii] = new_url
            else:
                for k, v in enumerate(imgs):
                    if v == old_url:
                        imgs[k] = new_url
                        break
            if p.get("img") == old_url:
                p["img"] = new_url
            product_fixed += 1
            fixed_total += 1

        if product_fixed:
            p["imgs"] = imgs
            products_touched += 1

    pushed = False
    if fixed_total > 0:
        if dry_run:
            print("fb-image-check: DRY RUN - 会修复 %d 张图片, 涉及 %d 个商品, 本次不写文件不提交"
                  % (fixed_total, products_touched))
        else:
            products = server.verify_images(products)
            save_products_to(products, products_path)
            if products_path is None:
                with open(server.INDEX_FILE, "w", encoding="utf-8") as f:
                    f.write(server.render_index(products))
                msg = ("fb-image-check: refreshed %d expired photo URL(s) across %d product(s)"
                       % (fixed_total, products_touched))
                ok, push_msg = server.git_commit_push(msg, manual=True)
                pushed = ok
                print("fb-image-check: git_commit_push -> ok=%s msg=%s" % (ok, push_msg))

    for line in unresolved_lines:
        print(line)

    print("fb-image-check: checked=%d broken=%d fixed=%d unresolved=%d products_skipped=%d pushed=%s"
          % (checked, broken_count, fixed_total, unresolved_total, products_skipped_no_buy, pushed))


def main():
    parser = argparse.ArgumentParser(description="检查并自愈 products.json 里失效的 Facebook CDN 图片链接")
    parser.add_argument("--products", default=None,
                         help="覆盖 products.json 路径(测试用: 只读写这个文件, 不碰真实数据, 不触发 git)")
    parser.add_argument("--dry-run", action="store_true",
                         help="只检测并打印会修复什么, 不写文件也不提交")
    args = parser.parse_args()

    lock_fh = acquire_lock()
    try:
        run(products_path=args.products, dry_run=args.dry_run)
    finally:
        lock_fh.close()


if __name__ == "__main__":
    main()

# 零后端静态内容发布系统（GitHub Pages + 本地 WebUI + Git 自动推送）

一个**零后端、零数据库**的轻量静态内容发布系统。用本地 WebUI 编辑内容，每次保存都会**自动提交并推送**到 Git，由 GitHub Pages 托管 → 刷新页面即更新。

不需要服务器、不需要数据库、不需要第三方服务。**一个仓库 + GitHub Pages 就能跑起来。**

## 设计

```
本地 WebUI(server.py) ──保存──▶ 覆盖 index.html / images/ ──git commit+push──▶ GitHub ──Pages──▶ 线上实时更新
```

- **本地编辑**：`bash run.sh` 打开地址，WebUI 里改内容、传图片
- **自动发布**：每次「保存」自动 `git add / commit / push`，推上 GitHub 后 Pages 自动重新构建，访客刷新即看到新内容
- **纯静态产物**：线上只有 `index.html`、`css`、`图片` 等静态文件，无后端可被攻击、无数据库可维护

## 目录结构

```
.
├── index.html      # 生成的内容首页(工具自动覆盖, GitHub Pages 入口)
├── style.css       # 黑白极简样式
├── server.py       # 本地编辑 server: WebUI + 保存后自动 git 推送
├── run.sh          # 一键启动
├── products.json   # 内容数据(工具维护)
├── site.json       # 站点/主题配置(工具维护, 首次运行生成)
└── images/         # 上传的图片(自动生成)
```

## 快速开始

```bash
bash run.sh
```

打开 `http://127.0.0.1:8000/admin`（本地无密码）：
- 新增 / 编辑内容：标题、价格、分类、简介、链接
- 上传图片 → 存进 `images/`
- 删除 / ↑↓ 排序
- **每次「保存」自动覆盖首页并 git commit + push**

预览：`http://127.0.0.1:8000/` 即线上将效果。

## GitHub Pages 部署

1. 把本地改动 `git commit` 并 `git push` 到你的仓库（首次部署）
2. GitHub → 仓库 **Settings → Pages** → Source 选 `Deploy from a branch` → 分支 `main` + 根目录 `/`
3. 之后每次在后台「保存」，工具自动 commit + push，线上自动更新，无需再手动推送

> 需要已在本机配置好远程仓库与 `git user.name / user.email`。

## 自动发布的 Git 配置（`server.py` 顶部）

```python
GIT = {
    "enabled": True,          # 保存后是否自动提交
    "push": True,             # True=commit 后还 push; False=只 commit
    "commit_prefix": "chore(shop): ",  # 提交信息前缀
    "branch": "main",         # 当前分支
}
```

## 站点 / 主题设置

后台点「⚙ 网站设置」，可改：

- 站点标题、Logo、顶部副标题、页脚文案（留空即不显示）
- 首页大标题 / 副标题 / 小徽标
- 购买按钮全局默认文案（单个内容可单独覆盖）
- 默认配色主题（跟随系统 / 浅色 / 深色）与明暗两套配色
- 首页右上角**明暗切换按钮 ◐**（记忆选择）

改动保存后自动覆盖 `index.html`。也可直接在 `server.py` 顶部的 `SITE` 里改默认值。

## 本地依赖

- Python 3（标准库即可，无第三方依赖）
- `git`（用于自动提交推送）

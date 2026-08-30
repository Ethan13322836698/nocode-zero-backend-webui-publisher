# Zero-Backend Static Publishing System

**A no-code, zero-backend, zero-database content system.** Edit everything from a simple local WebUI. Every save **automatically commits and pushes** to Git, and GitHub Pages re-deploys — refresh and your site is live.

No server. No database. No third-party services. **One repo + GitHub Pages is all you need.**

> English version · 中文说明见 [README.md](README.md)

---

## What makes this different

| | With this tool | Traditional site |
|---|---|---|
| Backend | **None** | Server, API, auth needed |
| Database | **None** | SQL/NoSQL to maintain |
| Hosting cost | **$0** (GitHub Pages) | Server bills, DB fees |
| Editing | **WebUI, drag & drop, no code** | Learn HTML/back-end |
| Updating | **Save → auto push → live** | Manual deploy pipelines |
| Attack surface | **Static files only** | Servers get hacked |

**No code to touch.** Add content, upload images, pick a color theme — click Save, and it's published.

---

## How it works

```
Local WebUI (server.py) ─save→ overwrites index.html / images/ ─git add+commit+push→ GitHub ─Pages→ live update
```

- **Edit locally**: run `bash run.sh`, use the WebUI to edit content and upload images
- **Auto-publish**: every "Save" runs `git add / commit / push` automatically
- **Pure static output**: only `index.html`, CSS, and images go online — nothing to hack, nothing to maintain

---

## Quick start

```bash
bash run.sh
```

Open `http://127.0.0.1:8000/admin` (no password, local only).

- Add / edit items: title, price, category, description, link
- Upload images → stored in `images/`
- Delete / reorder with ↑ ↓
- **Every "Save" rebuilds the page and pushes to git automatically**

Preview the live result at `http://127.0.0.1:8000/`.

**First run?** A setup wizard walks you through it:
1. Paste your **GitHub repo URL** → "Connect repo" (runs `git remote add/ set-url origin`)
2. "Push content" → publishes your site for the first time
3. Set a title / store name (optional, editable later) → done, you're in.

---

## Deploy to GitHub Pages (one-time)

1. Create an **empty** repository on GitHub (or rename yours), note the URL
2. In the setup wizard (first run) or **Settings → Git auto-publish**, set that repo URL
3. Push once → go to GitHub **Settings → Pages**, set Source to **Deploy from a branch**, branch `main`, root `/`
4. From then on, every **Save** in the WebUI commits + pushes automatically → Pages rebuilds → live

> Requires `git` installed and `git user.name / user.email` configured locally.

---

## Auto-publish git config (`server.py` top)

```python
GIT = {
    "enabled": True,          # auto-commit on save
    "push": True,             # True=commit AND push; False=commit only
    "commit_prefix": "chore(shop): ",  # commit message prefix
    "branch": "main",
}
```

You can also edit all of this from **Settings → Git auto-publish** in the WebUI (repo URL, branch, prefix, toggles, plus a one-click **"Publish now"** button).

---

## Site & theme settings

From **Settings** in the WebUI, without touching code:

- Site title, logo, tagline, footer text (leave blank to hide)
- Homepage headline / sub-line / badge
- Default **buy-button text** (global default; each item can override)
- Color theme — follow system / light / dark — and custom light & dark palettes
- **Dark-mode toggle ◐** on the homepage (remembered)

---

## Project layout

```
.
├── index.html      # Generated homepage (auto-overwritten, the Pages entry)
├── style.css       # Minimal black & white styles
├── server.py       # Local editing server: WebUI + git auto-push
├── run.sh          # One-click launcher
├── products.json   # Content data (tool-maintained, committed)
├── site.json       # Site/theme config (local, git-ignored)
└── images/         # Uploaded images (auto-managed)
```

---

## Requirements

- **Python 3** (standard library only — zero dependencies)
- **git** (for auto commit/push)

*Free forever on GitHub Pages. Nothing else to install or pay for.*

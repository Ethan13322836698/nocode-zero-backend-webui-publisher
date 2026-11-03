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

- **Python 3** (standard library only for core features)
- **git** (for auto commit/push)
- *(optional)* **playwright** — only needed to grab a listing's **full photo carousel** when importing from Facebook Marketplace. Without it, import still works, just with a single cover photo. Install:

  ```bash
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  ```

  `bash run.sh` automatically prefers `.venv`'s Python once it exists. Before first use, open admin → **⚙ Settings → Facebook login** and click "Log in to Facebook" once — log in in the browser window that opens, then close it. The session is saved locally in `.fb_browser_profile/` (never committed to git).

*Free forever on GitHub Pages. Nothing else to install or pay for.*


## Facebook login on a headless device

With Playwright and Chromium installed, run `./fb-login.sh` as the same system user as the repair job (without sudo). The tool detects the current username and device IP and prints a complete SSH command; copy it into a second terminal on your computer. Existing SSH connection addresses and ports take priority. Use `./fb-login.sh --connection-info` to preview the command without starting Chromium. Command format:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:9222:127.0.0.1:9222 USER@DEVICE
```

Open `chrome://inspect/#devices` in desktop Chrome, enable **Discover network targets**, and add `localhost:9222` under **Configure**. Click **inspect** for the Facebook tab, enable **Toggle screencast**, and log in through the displayed page. Press Ctrl+C in the device's login terminal to close Chromium and save the session, then close the SSH tunnel. Run `./fix.sh --dry-run` to verify before publishing with `./fix.sh`.

The debug endpoint listens on loopback only and closes after 30 minutes. Use `./fb-login.sh --port 9223` for another port and update the tunnel and Chrome configuration accordingly. Wait for any active repair job and close other Facebook login windows before starting. The existing ignored `.fb_browser_profile/` stores the session; login does not publish products.

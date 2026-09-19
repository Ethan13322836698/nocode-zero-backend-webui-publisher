# AGENTS.md

Zero-backend static store: local WebUI edits products, every save rewrites `index.html` + `products.json` and auto `git commit + push` to GitHub Pages (`origin/main`). This is a live store — the auto-publish flow is real production behavior, not a demo.

## Everything lives in server.py
The whole app is one ~1850-line stdlib-only Python file: HTTP server, JSON API handlers, and the public/index + admin + setup pages as `INDEX_TEMPLATE` / `ADMIN_TEMPLATE` / `SETUP_TEMPLATE` string constants. Editing the admin UI means editing those template strings inside `server.py` (inline CSS and JS), not separate files.

**Deliberate exception**: `check_fb_images.py` + `setup.py` (+ `systemd/`) are separate files. They don't run inside the admin server process — they're deployed to a dedicated always-on box (Raspberry Pi / Oracle server) as a systemd timer that periodically re-scrapes and patches expired Facebook CDN image links, importing `server.py` for its data/scrape/publish functions rather than duplicating them. `setup.py` is self-bootstrapping: `curl -fsSL https://raw.githubusercontent.com/Ethan13322836698/nocode-zero-backend-webui-publisher/main/setup.py | sudo python3 -` clones the repo itself if it isn't already checked out. That box's checkout is dedicated to this job only — never hand-edit it, `git_commit_push()` runs `git add -A`.

## Commands
- Run: `bash run.sh` → `http://127.0.0.1:8000/admin` (default port `8000`, override via env `BWMARKET_PORT`). Prefers `.venv/bin/python3` if present, else falls back to system `python3`/`python`.
- No tests, no linter, no Makefile, no package.json. Verification limited to: `python3 -m py_compile server.py`
- **Standard library only, with one deliberate exception**: `playwright` (see `requirements.txt`), used solely to fetch the full Facebook Marketplace photo carousel (see Gotchas below). It's imported lazily and optionally — if not installed, that one feature silently degrades to cover-image-only and nothing else breaks. Don't add other third-party dependencies without discussing it first.
- Playwright setup (one-time, per machine): `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m playwright install chromium`.

## Gotchas
- A running server auto-commits changes on save; don't manually commit unless explicitly asked. Don't trigger `/api/git/push` or save flows during feature work.
- `site.json`, `setup.json`, `.git-credentials` are git-ignored local config. Everything else (`index.html`, `products.json`, `images/`, `style.css`, `server.py`) is committed.
- FB Marketplace login Cookie (the old HTTP-scrape one, `fb_cookie` in `site.json`) is stored **encrypted**: the plaintext never touches disk or the browser. `site.json` holds `FB-ENC:v1:` ciphertext; the decrypt key lives in machine-local `fb.cookie.key` (0600, git-ignored — never commit it, backups of it are useless). `/api/settings` and `/admin` only expose `fb_cookie_set: bool`, never the cookie.
- `index.html` and `products.json` are tool-maintained/generated — don't hand-edit them; change `server.py` then let the save flow regenerate.
- Marketplace **text/price** extraction is limited by FB's own design: logged-in item pages are a **JS shell with no price/photo data in HTML** (data comes from private `/api/graphql/` queries whose `doc_id`s rotate — don't rely on them). Anonymous SEO pages give the cleanest name/description/cover-image. Price is **not** reliably obtainable server-side.
- Marketplace **photos**: the anonymous/cookie HTTP fetch (`_extract_img_urls`) can only ever get one cover photo — confirmed empirically, the full carousel simply isn't in that HTML regardless of login state. To get all photos, `scrape_marketplace()` calls `browser_extract_images()`, which drives a real Playwright-controlled Chromium with a **persistent login profile** (`.fb_browser_profile/`, git-ignored, holds real session cookies — never commit it) to actually render the page and read the rendered `<img>` carousel from the DOM. The user logs in once via the "登录 Facebook" button in admin settings (`POST /api/fb/login` opens a headed browser window; `GET /api/fb/login-status` polls progress/login state) — that session is then reused headlessly for every future scrape. If Playwright isn't installed or nobody has logged in yet, `browser_extract_images()` returns `[]` and `scrape_marketplace()` silently keeps the single cover image, so nothing breaks.

## Product schema (`products.json`)
`name`, `price` (numeric string; currency symbol is prefixed at render time), `sym` (per-item symbol override), `desc`, `buy` (Facebook Marketplace URL), `buy_text`, `imgs` (image list), `img` (first/cover copy). Category (`cat`) was removed; `verify_images()` prunes it on save. On save `verify_images()` normalizes to `imgs` + `img`; external `http(s)://` images are kept as-is, local ones become `images/<name>`.

## Conventions
- Comments/docstrings are Chinese; keep new code in the same style.
- Escape all user input embedded into generated HTML via `esc()` / `esc_js()` / `esc_css_var()`.
- Admin UI is bilingual zh/en: every UI string needs a key in the inline `I18N.zh` and `I18N.en` dicts plus `data-i18n` on the element. `renderRows()`/table actions use `I18N[LANG]` lookups, not `data-i18n`.
- API responses are `{"ok": true, ...}` / `{"ok": false, "error": str}`; client JS posts JSON and checks `j.ok`.
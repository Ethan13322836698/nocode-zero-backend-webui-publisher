# AGENTS.md

Zero-backend static store: local WebUI edits products, every save rewrites `index.html` + `products.json` and auto `git commit + push` to GitHub Pages (`origin/main`). This is a live store — the auto-publish flow is real production behavior, not a demo.

## Everything lives in server.py
The whole app is one ~1850-line stdlib-only Python file: HTTP server, JSON API handlers, and the public/index + admin + setup pages as `INDEX_TEMPLATE` / `ADMIN_TEMPLATE` / `SETUP_TEMPLATE` string constants. Editing the admin UI means editing those template strings inside `server.py` (inline CSS and JS), not separate files.

## Commands
- Run: `bash run.sh` → `http://127.0.0.1:8000/admin` (default port `8000`, override via env `BWMARKET_PORT`)
- No tests, no linter, no Makefile, no package.json. Verification limited to: `python3 -m py_compile server.py`
- **No third-party dependencies allowed** — standard library only (stdlib `urllib.request`, `re`, `json`, etc. are fine; never suggest `pip install`).

## Gotchas
- A running server auto-commits changes on save; don't manually commit unless explicitly asked. Don't trigger `/api/git/push` or save flows during feature work.
- `site.json`, `setup.json`, `.git-credentials` are git-ignored local config. Everything else (`index.html`, `products.json`, `images/`, `style.css`, `server.py`) is committed.
- FB Marketplace login Cookie is stored **encrypted**: the plaintext never touches disk or the browser. `site.json` holds `FB-ENC:v1:` ciphertext; the decrypt key lives in machine-local `fb.cookie.key` (0600, git-ignored — never commit it, backups of it are useless). `/api/settings` and `/admin` only expose `fb_cookie_set: bool`, never the cookie.
- `index.html` and `products.json` are tool-maintained/generated — don't hand-edit them; change `server.py` then let the save flow regenerate.
- Marketplace extraction is limited by FB's own design: logged-in item pages are a **JS shell with no price/photo data in HTML** (data comes from private `/api/graphql/` queries whose `doc_id`s rotate — don't rely on them). Anonymous SEO pages give the cleanest name/description/cover-image. Price and the full photo set are **not** reliably obtainable server-side; the best result is name + description + cover image.

## Product schema (`products.json`)
`name`, `price` (numeric string; currency symbol is prefixed at render time), `sym` (per-item symbol override), `desc`, `buy` (Facebook Marketplace URL), `buy_text`, `imgs` (image list), `img` (first/cover copy). Category (`cat`) was removed; `verify_images()` prunes it on save. On save `verify_images()` normalizes to `imgs` + `img`; external `http(s)://` images are kept as-is, local ones become `images/<name>`.

## Conventions
- Comments/docstrings are Chinese; keep new code in the same style.
- Escape all user input embedded into generated HTML via `esc()` / `esc_js()` / `esc_css_var()`.
- Admin UI is bilingual zh/en: every UI string needs a key in the inline `I18N.zh` and `I18N.en` dicts plus `data-i18n` on the element. `renderRows()`/table actions use `I18N[LANG]` lookups, not `data-i18n`.
- API responses are `{"ok": true, ...}` / `{"ok": false, "error": str}`; client JS posts JSON and checks `j.ok`.
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
- `index.html` and `products.json` are tool-maintained/generated — don't hand-edit them; change `server.py` then let the save flow regenerate.

## Product schema (`products.json`)
`name`, `price` (numeric string; currency symbol is prefixed at render time), `sym` (per-item symbol override), `cat`, `desc`, `buy` (Facebook Marketplace URL), `buy_text`, `imgs` (image list), `img` (first/cover copy). On save `verify_images()` normalizes to `imgs` + `img`; external `http(s)://` images are kept as-is, local ones become `images/<name>`.

## Conventions
- Comments/docstrings are Chinese; keep new code in the same style.
- Escape all user input embedded into generated HTML via `esc()` / `esc_js()` / `esc_css_var()`.
- Admin UI is bilingual zh/en: every UI string needs a key in the inline `I18N.zh` and `I18N.en` dicts plus `data-i18n` on the element. `renderRows()`/table actions use `I18N[LANG]` lookups, not `data-i18n`.
- API responses are `{"ok": true, ...}` / `{"ok": false, "error": str}`; client JS posts JSON and checks `j.ok`.
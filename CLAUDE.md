# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Scrapers that archive the content of author "Joe Turan" from three sources into Markdown (plus title/main images), one file per post in year-based folders. Everything is German-language and heavily tuned to that specific author's blog/FB/Telegram output.

## Commands

```bash
# One-time setup
pip install -r requirements.txt
playwright install chromium          # required: Playwright drives a real Chromium

# Run the full pipeline (Blog -> Facebook -> Telegram, in that order)
python scrape_all.py

# Run a subset (flags combinable; no flag = all three)
python scrape_all.py --telegram
python scrape_all.py --blog --facebook
python scrape_all.py --visible       # show the browser window

# Path/target overrides (defaults usually fine)
python scrape_all.py --base-url ... --facebook-url ... --channel ... --cookies ... --*-output ...

# Post-processing: concatenate saved articles
python merge_years.py <archive-dir>  # -> <dir>/yearly/<dir>-<year>.md
python merge_all.py   <archive-dir>  # -> <dir>/<dir>_ALL.md (requires yearly/ from merge_years)

# There is no test suite. Quick sanity check:
python -m py_compile scrape_all.py
```

## Architecture

`scrape_all.py` is the single entry point. `run_all()` opens **one** async Playwright Chromium browser and runs the selected source scrapers **sequentially, continuing on error** (a failed source is logged with traceback; the rest still run; a per-source summary prints at the end). Each scraper creates its own browser `context` (Facebook needs cookies + `de-DE` locale + tall viewport) and returns `(saved, skipped)`.

Three sources, each with its own output dir, filename scheme, and Markdown layout — **these are load-bearing invariants**: the "already exists" skip check compares against these exact paths, so changing a naming/format rule causes the whole archive to be re-downloaded.

| Source | Dir | Filename | Slug source | Content extraction |
|---|---|---|---|---|
| Blog (`scrape_blog`) | `Joe_Turan_Archiv/<year>/` | `<date>_<slug>.md` + `.jpg` | last URL segment after first `_` | `bs4` meta-tags for metadata; `div.jw-element-imagetext-text` → `markdownify` |
| Facebook (`scrape_facebook`) | `Joe_Turan_Facebook/<year>/` | `<date>_Joe Turan - <slug>.md` + image | first sentence of body | longest `dir=auto` text block (plain text) |
| Telegram (`scrape_telegram`) | `Joe_Turan_Telegram/<year>/` | `<date>_Joe Turan - <slug>.md` | first sentence of body | `.tgme_widget_message_text` |

**Stop condition:** every source ends once `SKIP_LIMIT` (top of `scrape_all.py`) already-existing articles are seen — this is how re-runs stay incremental instead of re-crawling everything.

**Shared text-cleaning pipeline** (applied per post): `strip_leading_intro` / `strip_trailing_greeting` (greeting boilerplate) → CTA removal → `strip_signature` (collapse the trailing author/social footer down to a single `Joe Turan`, anchored on a fuzzy "Joe Turan" line followed by `joeturan.com`). CTA removal has **two variants sharing `_CTA_TRIGGERS`**: `strip_cta_block_paragraphs` (blog — paragraphs split on blank lines) vs `strip_cta_block_lines` (FB — each paragraph is a single `\n` line). `_CTA_TRIGGERS` is an evolving allowlist of promo phrases (string = substring match; tuple = all substrings required); expect to extend it.

**Source-specific notes:**
- **Facebook** requires `cookies.txt` (Netscape format) for the logged-in session and reads the page URL from `Abonenten-URL.txt` (unless `--facebook-url`). Instead of clicking each post, it **passively harvests** the real permalink + `creation_time` date from the JSON that FB already ships — embedded `<script type="application/json">` in the initial HTML plus captured `/graphql` responses (`page.on("response", ...)`) — keyed by a normalized text prefix (`lookup_post_meta`). DOM scraping of date/permalink is only a fallback. The feed is virtualized, so it scrolls incrementally and processes posts by stable `aria-posinset`.
- **Blog** derives *all* metadata from head meta-tags only (`og:title`/`og:url`/`og:image`, `itemprop=datePublished`), never body text; paginates via `data-page-next`. Title image is fetched via Playwright's request API and re-encoded to RGB JPEG (Pillow).
- **Telegram** uses the public `t.me/s/<channel>` web view and processes posts **bottom-up** (newest first, then scrolls toward older).

`merge_years.py` / `merge_all.py` are independent stdlib-only post-processors that concatenate the per-year Markdown files (separated by `---`) into yearly digests and then one combined file.

## Notes

- `requirements.txt` intentionally has no `requests`: image downloads go through Playwright's `context.request`. Only `markdownify` is irreplaceable for the blog (formatted HTML→MD); `bs4` and `Pillow` are kept for clean meta parsing and JPEG normalization.
- `session-context.md` documents the pre-merge design of the former standalone `blogdownload.py`; the architecture decisions still hold but file/dependency details there are outdated (that file and `scrape_facebook.py` were merged into `scrape_all.py`).
- Single unified log at `scrape_all.log` (also to console); it does not rotate.
- `node/` is a standalone JavaScript/ESM port of `scrape_all.py` (Playwright + cheerio + turndown + sharp + difflib), run with `node scrape_all.js` — same CLI/behavior, but self-contained (own `cookies.txt`/`Abonenten-URL.txt`/output dirs under `node/`). Keep it in sync with `scrape_all.py` when changing scraping logic; `turndown` output is intentionally not byte-identical to `markdownify`.

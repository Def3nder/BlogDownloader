import argparse
import difflib
import logging
import re
import sys
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert
from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


LOG_FILE = Path(__file__).parent / "blogdownload.log"

log = logging.getLogger("blogdownload")


def _setup_logging() -> None:
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(ch)


DEFAULT_BASE_URL = "https://www.joeturan.com/blog"
DEFAULT_OUTPUT_DIR = "Joe_Turan_Archiv"
CONTENT_CONTAINER_CLASS = "jw-element-imagetext-text"
REQUEST_TIMEOUT = 30
SKIP_LIMIT = 10
PAGE_WAIT_MS = 4000
ARTICLE_PAUSE_SECONDS = 0.4
PAGE_PAUSE_SECONDS = 0.8
TITLE_SUFFIX_RE = re.compile(r"\s*/\s*Blog\s*\|\s*www\.joeturan\.com\s*$", re.IGNORECASE)


@dataclass
class Article:
    url: str
    title: str
    date: str
    slug: str
    content_md: str
    image_url: str | None
    og_description: str | None


def normalize_whitespace(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned = []
    previous_blank = False
    for line in lines:
        if not line.strip():
            if not previous_blank:
                cleaned.append("")
            previous_blank = True
            continue
        cleaned.append(line)
        previous_blank = False
    return "\n".join(cleaned).strip()


def meta_content(soup: BeautifulSoup, selector: str) -> str | None:
    tag = soup.select_one(selector)
    if not tag:
        return None
    content = tag.get("content")
    if not content:
        return None
    return content.strip()


def extract_title(soup: BeautifulSoup) -> str:
    raw = meta_content(soup, "meta[property='og:title']")
    if not raw:
        return "Unbekannter Artikel"
    return TITLE_SUFFIX_RE.sub("", raw).strip() or "Unbekannter Artikel"


def extract_date(soup: BeautifulSoup) -> str:
    raw = meta_content(soup, "meta[itemprop='datePublished']")
    if raw and len(raw) >= 10 and re.match(r"\d{4}-\d{2}-\d{2}", raw[:10]):
        return raw[:10]
    return "1970-01-01"


def extract_og_url(soup: BeautifulSoup, fallback: str) -> str:
    return meta_content(soup, "meta[property='og:url']") or fallback


def build_slug_from_url(og_url: str) -> str:
    path = urlparse(og_url).path.rstrip("/")
    last_segment = path.rsplit("/", 1)[-1]
    if "_" in last_segment:
        slug = last_segment.split("_", 1)[1]
    else:
        slug = last_segment
    return slug or "artikel"


def extract_image_url(soup: BeautifulSoup) -> str | None:
    return meta_content(soup, "meta[property='og:image']")


def extract_og_description(soup: BeautifulSoup) -> str | None:
    raw = meta_content(soup, "meta[property='og:description']")
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return cleaned or None


def strip_signature(md_text: str) -> str:
    """Remove the trailing author/social-media block, keeping only 'Joe Turan' at the end.

    Strategy:
    1. Collect all lines that fuzzy-match 'Joe Turan' (ratio >= 0.75).
    2. Pick the *first* such line that is followed by 'joeturan.com' within the
       next 3 lines — that is the reliable start of the footer block.
    3. Fall back to the *last* matching line when no 'joeturan.com' anchor is found.
    4. Strip from the chosen line to the end and append a clean 'Joe Turan'.
    """
    lines = md_text.splitlines()
    target = "joe turan"

    joe_indices = []
    for i, line in enumerate(lines):
        normalized = re.sub(r"[*_\[\]`]", "", line).strip().lower()
        if not normalized:
            continue
        if difflib.SequenceMatcher(None, normalized, target).ratio() >= 0.75:
            joe_indices.append(i)

    if not joe_indices:
        return md_text

    footer_start = None
    for idx in joe_indices:
        window = lines[idx + 1: idx + 4]
        if any("joeturan.com" in wline.lower() for wline in window):
            footer_start = idx
            break

    if footer_start is None:
        footer_start = joe_indices[-1]

    trimmed = "\n".join(lines[:footer_start]).rstrip()
    return f"{trimmed}\n\nJoe Turan"


# Each entry is either a single phrase (any match removes the paragraph)
# or a tuple of phrases (ALL must be present — AND logic).
_CTA_TRIGGERS: list[str | tuple[str, ...]] = [
    ("schreib mir", "whatsapp"),
    "erstgespräch",
    "mit menschen arbeite",
    "arbeite ich mit menschen",
    "an dieser stelle arbeite ich",
    "daran arbeite ich",
    "kommst allein nicht",
]


def _matches_cta(normalized: str, trigger: str | tuple[str, ...]) -> bool:
    if isinstance(trigger, str):
        return trigger in normalized
    return all(t in normalized for t in trigger)


def strip_cta_block(md_text: str) -> str:
    """Remove promotional call-to-action paragraphs from the article text.

    String triggers: substring match. Tuple triggers: all substrings must be present (AND).
    Matching is case-insensitive; markdown formatting characters are ignored.
    """
    paragraphs = re.split(r"\n{2,}", md_text)
    filtered = []
    for para in paragraphs:
        normalized = re.sub(r"[*_\[\]`]", "", para).lower()
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if any(_matches_cta(normalized, trigger) for trigger in _CTA_TRIGGERS):
            continue
        filtered.append(para)
    return "\n\n".join(filtered)


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def extract_content_markdown(soup: BeautifulSoup, title: str) -> str:
    containers = soup.select(f".{CONTENT_CONTAINER_CLASS}")
    title_norm = _normalize_for_compare(title)
    chunks = []
    for container in containers:
        for heading in container.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
            if _normalize_for_compare(heading.get_text(" ", strip=True)) == title_norm:
                heading.decompose()

        if not container.get_text(strip=True):
            continue

        md = md_convert(
            str(container),
            heading_style="ATX",
            strip=["script", "style"],
        )
        md = normalize_whitespace(md)
        if md:
            chunks.append(md)
    result = "\n\n".join(chunks).strip()
    result = strip_cta_block(result)
    return strip_signature(result)


def parse_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "html.parser")
    og_url = extract_og_url(soup, url)
    title = extract_title(soup)
    date = extract_date(soup)
    slug = build_slug_from_url(og_url)
    return Article(
        url=url,
        title=title,
        date=date,
        slug=slug,
        content_md=extract_content_markdown(soup, title),
        image_url=extract_image_url(soup),
        og_description=extract_og_description(soup),
    )


def article_paths(output_dir: Path, article: Article) -> tuple[Path, Path]:
    year_dir = output_dir / article.date[:4]
    base_name = f"{article.date}_{article.slug}"
    return year_dir / f"{base_name}.md", year_dir / f"{base_name}.jpg"


def fetch_image_bytes(image_url: str) -> bytes:
    response = requests.get(image_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.content


def save_image(image_url: str, jpg_path: Path) -> None:
    image_bytes = fetch_image_bytes(image_url)
    image = Image.open(BytesIO(image_bytes))
    if image.mode != "RGB":
        image = image.convert("RGB")
    jpg_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(jpg_path, format="JPEG", quality=90)


def render_markdown(article: Article) -> str:
    parts = [f"# {article.title}", "", f"*Quelle: {article.url}*", "", f"**Datum: {article.date}**", ""]
    if article.content_md:
        parts.append(article.content_md)
        parts.append("")
    return "\n".join(parts)


def save_article(article: Article, output_dir: Path) -> tuple[Path | None, Path | None]:
    md_path, jpg_path = article_paths(output_dir, article)

    md_written: Path | None = None
    jpg_written: Path | None = None

    if not jpg_path.exists() and article.image_url:
        try:
            save_image(article.image_url, jpg_path)
            jpg_written = jpg_path
        except Exception as exc:
            log.warning("Bild konnte nicht geladen werden: %s (%s)", article.image_url, exc)

    if not md_path.exists():
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(article), encoding="utf-8")
        md_written = md_path

    return md_written, jpg_written


def visible_article_urls(page, base_url: str) -> list[str]:
    hrefs = page.eval_on_selector_all(
        "a[href]",
        """
        (elements) => elements
            .map((element) => element.href)
            .filter(Boolean)
        """,
    )
    normalized = []
    seen = set()
    blog_path = urlparse(base_url).path.rstrip("/")
    for href in hrefs:
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != urlparse(base_url).netloc:
            continue
        path = parsed.path.rstrip("/")
        if not path.startswith(blog_path):
            continue
        if path == blog_path:
            continue
        if href in seen:
            continue
        seen.add(href)
        normalized.append(href)
    return normalized[:12]


def click_next_page(page, base_url: str) -> bool:
    before = set(visible_article_urls(page, base_url))
    if not before:
        return False

    candidates = [
        "[data-page-next]",
        "a[data-page-next]",
        "button[data-page-next]",
    ]

    for selector in candidates:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            count = 0
        if count == 0:
            continue

        for index in range(count):
            entry = locator.nth(index)
            try:
                if not entry.is_visible():
                    continue
                if entry.is_disabled():
                    continue
            except Exception:
                pass

            try:
                entry.click(timeout=5000)
                page.wait_for_timeout(PAGE_WAIT_MS)
                after = set(visible_article_urls(page, base_url))
                if after and after != before:
                    return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    return False


def process_listing_page(page, output_dir: Path, processed_urls: set[str], skip_state: dict) -> int:
    article_urls = visible_article_urls(page, page.url)
    if not article_urls:
        return 0

    processed_this_page = 0
    for article_url in article_urls:
        if skip_state["stop"]:
            break
        if article_url in processed_urls:
            continue

        log.info("[Artikel] %s", article_url)
        article_page = page.context.new_page()
        try:
            article_page.goto(article_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
            article_html = article_page.content()
            article = parse_article(article_html, article_url)
            md_path, jpg_path = article_paths(output_dir, article)

            if md_path.exists() and jpg_path.exists():
                processed_urls.add(article_url)
                processed_this_page += 1
                skip_state["count"] += 1
                log.info(
                    "  -> uebersprungen (%d/%d), existiert bereits: %s",
                    skip_state["count"],
                    SKIP_LIMIT,
                    md_path,
                )
                if skip_state["count"] >= SKIP_LIMIT:
                    skip_state["stop"] = True
                    log.info(
                        "[Ende] %d Artikel uebersprungen, Abbruch wie konfiguriert.",
                        SKIP_LIMIT,
                    )
                continue

            md_written, jpg_written = save_article(article, output_dir)
            processed_urls.add(article_url)
            processed_this_page += 1
            if md_written and jpg_written:
                log.info("  -> gespeichert: %s (+ %s)", md_written, jpg_written.name)
            elif md_written:
                log.info("  -> gespeichert: %s (Bild existierte bereits)", md_written)
            elif jpg_written:
                log.info("  -> Bild nachgeladen: %s (Markdown existierte bereits)", jpg_written)
            else:
                log.info("  -> nichts zu tun fuer %s", md_path)
        except Exception as exc:
            log.error("  -> Fehler bei %s: %s", article_url, exc)
        finally:
            article_page.close()
        time.sleep(ARTICLE_PAUSE_SECONDS)

    return processed_this_page


def run(base_url: str, output_dir: Path, headless: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_urls: set[str] = set()
    skip_state = {"count": 0, "stop": False}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        log.info("[Start] Oeffne Blog: %s", base_url)
        page.goto(base_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        page.wait_for_timeout(PAGE_WAIT_MS)

        page_number = 1
        while True:
            log.info("[Seite %d] Sammle sichtbare Artikel", page_number)
            count = process_listing_page(page, output_dir, processed_urls, skip_state)
            if count == 0:
                log.info("[Seite %d] Keine neuen Artikel gefunden.", page_number)

            log.info("[Seite %d] %d Artikel verarbeitet oder uebersprungen.", page_number, count)

            if skip_state["stop"]:
                break

            time.sleep(PAGE_PAUSE_SECONDS)

            if not click_next_page(page, base_url):
                log.info("[Ende] Keine naechste Seite ueber data-page-next gefunden.")
                break
            page_number += 1

        context.close()
        browser.close()


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Laedt Blog-Artikel als Markdown plus Titelbild in Jahresordner herunter."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Startseite des Blogs")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Zielverzeichnis fuer Markdown und Bilder",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Browser sichtbar starten",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    _setup_logging()
    args = parse_args(argv)
    run(
        base_url=args.base_url,
        output_dir=Path(args.output_dir),
        headless=not args.show_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

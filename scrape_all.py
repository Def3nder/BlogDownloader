"""
scrape_all.py — Konsolidierter Scraper für Blog, Facebook und Telegram.

Führt drei Quellen als Ablaufplan NACHEINANDER aus (Default-Reihenfolge):
  1. Blog      -> joeturan.com/blog          -> Joe_Turan_Archiv/
  2. Facebook  -> login-pflichtige FB-Seite  -> Joe_Turan_Facebook/
  3. Telegram  -> t.me/s/<channel>           -> Joe_Turan_Telegram/

Pro Beitrag wird eine Markdown-Datei in einen Jahresordner geschrieben; Blog und
Facebook laden zusätzlich das Titel-/Hauptbild mit identischem Dateinamen-Stamm.
Jede Quelle bricht ab, sobald SKIP_LIMIT bereits vorhandene Artikel erkannt werden.
Beiträge mit "Kuschel Workshop" werden übersprungen.

CLI (Auswahl kombinierbar; ohne Flag laufen alle drei):
  python scrape_all.py                 # Blog -> Facebook -> Telegram
  python scrape_all.py --telegram      # nur Telegram
  python scrape_all.py --blog --facebook
  python scrape_all.py --visible       # Browser sichtbar

Bei einem Fehler in einer Quelle wird protokolliert und mit der nächsten Quelle
fortgefahren; am Ende folgt eine Zusammenfassung je Quelle.
"""

import argparse
import asyncio
import difflib
import json
import logging
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert
from PIL import Image
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent

CHANNEL_DISPLAY_NAME = "Joe Turan"
DEFAULT_CHANNEL = "joeturan"
DEFAULT_BASE_URL = "https://www.joeturan.com/blog"

BLOG_OUTPUT_DIR = SCRIPT_DIR / "Joe_Turan_Archiv"
FB_OUTPUT_DIR = SCRIPT_DIR / "Joe_Turan_Facebook"
TELEGRAM_OUTPUT_DIR = SCRIPT_DIR / "Joe_Turan_Telegram"

COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
URL_FILE = SCRIPT_DIR / "Abonenten-URL.txt"
LOG_FILE = SCRIPT_DIR / "scrape_all.log"

# Gemeinsame Abbruchschwelle: nach so vielen bereits vorhandenen Artikeln wird
# die jeweilige Quelle beendet.
SKIP_LIMIT = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Blog-spezifisch
CONTENT_CONTAINER_CLASS = "jw-element-imagetext-text"
REQUEST_TIMEOUT = 30
PAGE_WAIT_MS = 4000
ARTICLE_PAUSE_SECONDS = 0.4
PAGE_PAUSE_SECONDS = 0.8
TITLE_SUFFIX_RE = re.compile(r"\s*/\s*Blog\s*\|\s*www\.joeturan\.com\s*$", re.IGNORECASE)

EXCLUDE_RE = re.compile(r"kuschel[\s\-_]*workshop", re.IGNORECASE)
FACEBOOK_RE = re.compile(r"facebook\.com", re.IGNORECASE)

CANONICAL_ORDER = ["blog", "facebook", "telegram"]


# ---------------------------------------------------------------------------
# Logging (ein Logger, eine Datei)
# ---------------------------------------------------------------------------
logger = logging.getLogger("scrape_all")


def setup_logging() -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.handlers.clear()
    logger.addHandler(fh)
    logger.addHandler(sh)


# ---------------------------------------------------------------------------
# Gemeinsame Text-Bereinigung
# ---------------------------------------------------------------------------
SENTENCE_SPLIT_RE = re.compile(r"[.!?…\n\r]")
LEADING_INTRO_RE = re.compile(
    r"^\s*(?:"
    r"(?:ein|mein|dein|heutiger|kurzer|kleiner)?\s*"
    r"(?:morgen|abend|tages|gesundheits?|achtsamkeits?|lebens?|herz(?:ens)?|liebes?|beziehungs?)?[\s\-]*"
    r"(?:impuls|gedanke|frage|gru[ßss]|tipp|botschaft|reminder|erinnerung)"
    r"(?:\s+(?:des\s+tages|für\s+dich|an\s+dich|von\s+mir))?"
    # Zeilenende: optionales Satzzeichen, danach beliebige Nicht-Wort-Zeichen
    # (Whitespace, Emojis wie ☀️/🤍, Satzzeichen) – analog zu GREETING_RE.
    r")\s*[:\-–—]?[\s\W]*$",
    re.IGNORECASE,
)
SLUG_CLEAN_RE = re.compile(r"[^A-Za-z0-9äöüÄÖÜß ]+")
SLUG_MULTI_UNDERSCORE_RE = re.compile(r"_+")
# Maximale Länge des Titel-Teils im Dateinamen (ohne Datum/Autor-Präfix).
SLUG_MAX_LEN = 55

GREETING_RE = re.compile(
    r"^\s*(?:"
    r"(?:einen?\s+)?"
    r"(?:sch[öo]ne[nrs]?|gute[nrs]?|hab(?:t)?(?:\s+(?:einen?|noch))?|wünsche\s+(?:dir|euch|ihnen)?)"
    r"\s+"
    r"(?:rest(?:lich\w*)?\s+)?"
    r"(?:morgen|tag|abend|nachmittag|vormittag|nacht|woche(?:nende)?|sonntag|montag|dienstag|mittwoch|donnerstag|freitag|samstag)"
    r"(?:\s+\w+)?"
    r"[\s\W]*$"
    r")",
    re.IGNORECASE,
)


def strip_trailing_greeting(text: str) -> str:
    """Entfernt eine abschließende Grußzeile wie 'Schönen Abend noch 🤍'."""
    lines = text.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and GREETING_RE.match(lines[-1]):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines)


def strip_leading_intro(text: str) -> str:
    """Entfernt eine einleitende Begrüßungs- oder Intro-Zeile."""
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and (GREETING_RE.match(lines[0]) or LEADING_INTRO_RE.match(lines[0])):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines)


# Steuertexte, die Facebook an aufgeklappte Beiträge anhängt ("… Mehr/Weniger anzeigen").
_FB_UI_TAIL_RE = re.compile(
    r"\s*(?:…|\.\.\.)?\s*(?:weniger anzeigen|mehr anzeigen|mehr ansehen|see more|see less)\s*$",
    re.IGNORECASE,
)


def strip_fb_ui_text(text: str) -> str:
    """Entfernt Facebooks "Mehr/Weniger anzeigen"-Steuertext am Beitragsende."""
    text = text.rstrip()
    while True:
        stripped = _FB_UI_TAIL_RE.sub("", text).rstrip()
        if stripped == text:
            return text
        text = stripped


def strip_signature(md_text: str) -> str:
    """Entfernt den abschließenden Autoren-/Social-Media-Abspann, behält nur 'Joe Turan'.

    Strategie:
    1. Alle Zeilen sammeln, die unscharf auf 'Joe Turan' passen (ratio >= 0.75).
    2. Die *erste* solche Zeile wählen, der innerhalb der nächsten 3 Zeilen
       'joeturan.com' folgt – das ist der verlässliche Beginn des Abspanns.
    3. Fällt kein 'joeturan.com'-Anker an, die *letzte* Treffer-Zeile nehmen.
    4. Ab der gewählten Zeile bis zum Ende abschneiden und ein sauberes
       'Joe Turan' anhängen.
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


# Werbe-/Call-to-Action-Trigger. Einzel-String = Substring-Treffer;
# Tupel = ALLE Teilstrings müssen vorkommen (UND-Verknüpfung).
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


def strip_cta_block_paragraphs(md_text: str) -> str:
    """Entfernt werbliche Call-to-Action-Absätze (Blog: durch Leerzeilen getrennt).

    String-Trigger: Substring-Treffer. Tupel-Trigger: alle Teilstrings müssen
    vorkommen (UND). Vergleich case-insensitiv, Markdown-Zeichen werden ignoriert.
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


def strip_cta_block_lines(text: str) -> str:
    """Entfernt werbliche Call-to-Action-Absätze (Facebook: je Absatz eine Zeile).

    Im Facebook-Text steht jeder Absatz auf einer eigenen Zeile (einfaches \\n),
    nicht durch Leerzeilen getrennt.
    """
    kept = []
    for line in text.split("\n"):
        normalized = re.sub(r"[*_\[\]`]", "", line).lower()
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if normalized and any(_matches_cta(normalized, t) for t in _CTA_TRIGGERS):
            continue
        kept.append(line)
    return "\n".join(kept)


def first_sentence(text: str) -> str:
    """Gibt den ersten Satz bis zum ersten Satzzeichen zurück."""
    stripped = text.strip()
    if not stripped:
        return ""
    match = SENTENCE_SPLIT_RE.search(stripped)
    sentence = stripped[: match.start()] if match else stripped
    return sentence.strip()


def slugify(sentence: str, max_len: int = SLUG_MAX_LEN) -> str:
    """Erzeugt einen Dateinamen-Slug: Sonderzeichen werden zu Underscores und der
    Slug wird auf max_len Zeichen begrenzt – jedoch nur an Wortgrenzen
    (Leerzeichen/Underscore), sodass kein Wort mitten durchgeschnitten wird.
    """
    slug = SLUG_CLEAN_RE.sub("_", sentence)
    slug = SLUG_MULTI_UNDERSCORE_RE.sub("_", slug).strip("_ ")
    if len(slug) > max_len:
        cut = slug[:max_len]
        # Steht an der Schnittstelle mitten in einem Wort? -> bis zur letzten
        # Wortgrenze zurückgehen, damit kein Wort zerschnitten wird.
        if slug[max_len] not in " _" and cut[-1] not in " _":
            boundary = max(cut.rfind(" "), cut.rfind("_"))
            if boundary > 0:
                cut = cut[:boundary]
        slug = cut
    return slug.rstrip("_ ")


def build_filename(date_str: str, slug: str) -> str:
    """Dateiname für Telegram-/Facebook-Beiträge: '<date>_Joe Turan - <slug>.md'."""
    return f"{date_str}_{CHANNEL_DISPLAY_NAME} - {slug}.md"


def render_post_markdown(title: str, source_url: str, date_str: str, body: str) -> str:
    """Markdown-Layout für Telegram- und Facebook-Beiträge (mit ----Trenner)."""
    return (
        f"# {title}\n"
        f"\n"
        f"*Quelle: {source_url}*\n"
        f"\n"
        f"Datum: {date_str}\n"
        f"\n"
        f"---\n"
        f"{body}\n"
    )


def image_extension(content_type: str | None, url: str) -> str:
    """Bestimmt die Bildendung aus Content-Type bzw. URL (.jpg/.png)."""
    if content_type:
        ct = content_type.lower()
        if "png" in ct:
            return ".png"
        if "jpeg" in ct or "jpg" in ct:
            return ".jpg"
    low = url.lower()
    if ".png" in low:
        return ".png"
    return ".jpg"


# ---------------------------------------------------------------------------
# Cookies (Facebook-Login)
# ---------------------------------------------------------------------------
def load_netscape_cookies(path: Path) -> list[dict]:
    """Liest eine cookies.txt im Netscape-Format und liefert Playwright-Cookies."""
    cookies: list[dict] = []
    if not path.exists():
        logger.warning("[WARN] Cookie-Datei nicht gefunden: %s", path)
        return cookies
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # "#HttpOnly_"-Präfix ist erlaubt, sonstige Kommentare/Leerzeilen überspringen
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, c_path, secure, expiry, name, value = parts
        cookie: dict = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": c_path or "/",
            "secure": secure.upper() == "TRUE",
        }
        try:
            exp = int(expiry)
            if exp > 0:
                cookie["expires"] = exp
        except ValueError:
            pass
        cookies.append(cookie)
    logger.info("%d Cookie(s) aus %s geladen", len(cookies), path.name)
    return cookies


# ---------------------------------------------------------------------------
# Facebook-Datumsparser
# ---------------------------------------------------------------------------
GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11,
    "dezember": 12,
}
FB_RELATIVE_RE = re.compile(
    r"vor\s+(?:etwa\s+)?(\d+)\s*(min|minute|minuten|std|stunde|stunden|"
    r"t|tag|tagen|tg|w|woche|wochen)",
    re.IGNORECASE,
)
FB_ABS_DATE_RE = re.compile(
    r"(\d{1,2})\.\s*([A-Za-zäöü]+)\.?\s*(\d{4})?", re.IGNORECASE
)


def parse_fb_date(text: str, today: date | None = None) -> str | None:
    """Wandelt eine Facebook-Zeitangabe in ein ISO-Datum (YYYY-MM-DD) um."""
    if not text:
        return None
    today = today or date.today()
    low = text.strip().lower()

    if "gerade eben" in low or "jetzt" in low or low.startswith("vor wenigen"):
        return today.isoformat()
    if "gestern" in low:
        return (today - timedelta(days=1)).isoformat()

    rel = FB_RELATIVE_RE.search(low)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        if unit.startswith(("min", "std", "stunde")):
            return today.isoformat()  # heute
        if unit.startswith(("t", "tg", "tag")):
            return (today - timedelta(days=amount)).isoformat()
        if unit.startswith(("w", "woche")):
            return (today - timedelta(weeks=amount)).isoformat()

    abs_m = FB_ABS_DATE_RE.search(low)
    if abs_m:
        day = int(abs_m.group(1))
        month = GERMAN_MONTHS.get(abs_m.group(2).rstrip("."))
        year = int(abs_m.group(3)) if abs_m.group(3) else today.year
        if month:
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return None
    return None


# ===========================================================================
# Quelle 1: BLOG (joeturan.com/blog)
# ===========================================================================
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


def extract_blog_date(soup: BeautifulSoup) -> str:
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
    result = strip_leading_intro(result)
    result = strip_cta_block_paragraphs(result)
    return strip_signature(result)


def parse_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "html.parser")
    og_url = extract_og_url(soup, url)
    title = extract_title(soup)
    date_str = extract_blog_date(soup)
    slug = build_slug_from_url(og_url)
    return Article(
        url=url,
        title=title,
        date=date_str,
        slug=slug,
        content_md=extract_content_markdown(soup, title),
        image_url=extract_image_url(soup),
        og_description=extract_og_description(soup),
    )


def article_paths(output_dir: Path, article: Article) -> tuple[Path, Path]:
    year_dir = output_dir / article.date[:4]
    base_name = f"{article.date}_{article.slug}"
    return year_dir / f"{base_name}.md", year_dir / f"{base_name}.jpg"


def render_blog_markdown(article: Article) -> str:
    parts = [f"# {article.title}", "", f"*Quelle: {article.url}*", "", f"**Datum: {article.date}**", ""]
    if article.content_md:
        parts.append(article.content_md)
        parts.append("")
    return "\n".join(parts)


async def save_blog_image(context, image_url: str, jpg_path: Path) -> None:
    """Lädt das Titelbild via Playwright und speichert es als RGB-JPEG (q90)."""
    resp = await context.request.get(image_url, timeout=REQUEST_TIMEOUT * 1000)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status}")
    image_bytes = await resp.body()
    image = Image.open(BytesIO(image_bytes))
    if image.mode != "RGB":
        image = image.convert("RGB")
    jpg_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(jpg_path, format="JPEG", quality=90)


async def save_blog_article(context, article: Article, output_dir: Path) -> tuple[Path | None, Path | None]:
    md_path, jpg_path = article_paths(output_dir, article)

    md_written: Path | None = None
    jpg_written: Path | None = None

    if not jpg_path.exists() and article.image_url:
        try:
            await save_blog_image(context, article.image_url, jpg_path)
            jpg_written = jpg_path
        except Exception as exc:
            logger.warning("Bild konnte nicht geladen werden: %s (%s)", article.image_url, exc)

    if not md_path.exists():
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_blog_markdown(article), encoding="utf-8")
        md_written = md_path

    return md_written, jpg_written


async def visible_article_urls(page, base_url: str) -> list[str]:
    hrefs = await page.eval_on_selector_all(
        "a[href]",
        """
        (elements) => elements
            .map((element) => element.href)
            .filter(Boolean)
        """,
    )
    normalized = []
    seen = set()
    base_netloc = urlparse(base_url).netloc
    blog_path = urlparse(base_url).path.rstrip("/")
    for href in hrefs:
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != base_netloc:
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


async def click_next_page(page, base_url: str) -> bool:
    before = set(await visible_article_urls(page, base_url))
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
            count = await locator.count()
        except Exception:
            count = 0
        if count == 0:
            continue

        for index in range(count):
            entry = locator.nth(index)
            try:
                if not await entry.is_visible():
                    continue
                if await entry.is_disabled():
                    continue
            except Exception:
                pass

            try:
                await entry.click(timeout=5000)
                await page.wait_for_timeout(PAGE_WAIT_MS)
                after = set(await visible_article_urls(page, base_url))
                if after and after != before:
                    return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    return False


async def process_blog_listing_page(page, context, output_dir: Path, processed_urls: set[str], skip_state: dict) -> int:
    article_urls = await visible_article_urls(page, page.url)
    if not article_urls:
        return 0

    processed_this_page = 0
    for article_url in article_urls:
        if skip_state["stop"]:
            break
        if article_url in processed_urls:
            continue

        logger.info("[Artikel] %s", article_url)
        article_page = await page.context.new_page()
        try:
            await article_page.goto(article_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
            article_html = await article_page.content()
            article = parse_article(article_html, article_url)
            md_path, jpg_path = article_paths(output_dir, article)

            if md_path.exists() and jpg_path.exists():
                processed_urls.add(article_url)
                processed_this_page += 1
                skip_state["count"] += 1
                logger.info(
                    "  -> uebersprungen (%d/%d), existiert bereits: %s",
                    skip_state["count"],
                    SKIP_LIMIT,
                    md_path,
                )
                if skip_state["count"] >= SKIP_LIMIT:
                    skip_state["stop"] = True
                    logger.info(
                        "[Ende] %d Artikel uebersprungen, Abbruch wie konfiguriert.",
                        SKIP_LIMIT,
                    )
                continue

            md_written, jpg_written = await save_blog_article(context, article, output_dir)
            processed_urls.add(article_url)
            processed_this_page += 1
            if md_written:
                skip_state["saved"] += 1
            if md_written and jpg_written:
                logger.info("  -> gespeichert: %s (+ %s)", md_written, jpg_written.name)
            elif md_written:
                logger.info("  -> gespeichert: %s (Bild existierte bereits)", md_written)
            elif jpg_written:
                logger.info("  -> Bild nachgeladen: %s (Markdown existierte bereits)", jpg_written)
            else:
                logger.info("  -> nichts zu tun fuer %s", md_path)
        except Exception as exc:
            logger.error("  -> Fehler bei %s: %s", article_url, exc)
        finally:
            await article_page.close()
        await asyncio.sleep(ARTICLE_PAUSE_SECONDS)

    return processed_this_page


async def scrape_blog(browser, base_url: str, output_dir: Path) -> tuple[int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_urls: set[str] = set()
    skip_state = {"count": 0, "stop": False, "saved": 0}

    context = await browser.new_context()
    try:
        page = await context.new_page()

        logger.info("[Start] Oeffne Blog: %s", base_url)
        await page.goto(base_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        await page.wait_for_timeout(PAGE_WAIT_MS)

        page_number = 1
        while True:
            logger.info("[Seite %d] Sammle sichtbare Artikel", page_number)
            count = await process_blog_listing_page(page, context, output_dir, processed_urls, skip_state)
            if count == 0:
                logger.info("[Seite %d] Keine neuen Artikel gefunden.", page_number)

            logger.info("[Seite %d] %d Artikel verarbeitet oder uebersprungen.", page_number, count)

            if skip_state["stop"]:
                break

            await asyncio.sleep(PAGE_PAUSE_SECONDS)

            if not await click_next_page(page, base_url):
                logger.info("[Ende] Keine naechste Seite ueber data-page-next gefunden.")
                break
            page_number += 1
    finally:
        await context.close()

    logger.info("Blog: %d gespeichert, %d bereits vorhanden.", skip_state["saved"], skip_state["count"])
    return skip_state["saved"], skip_state["count"]


# ===========================================================================
# Quelle 3: TELEGRAM (t.me/s/<channel>)
# ===========================================================================
async def extract_telegram_date(post) -> str | None:
    time_el = await post.query_selector(".tgme_widget_message_date time")
    if not time_el:
        return None
    dt = await time_el.get_attribute("datetime")
    if not dt:
        return None
    return dt[:10]


async def scrape_telegram(browser, channel: str, output_dir: Path) -> tuple[int, int]:
    channel_url = f"https://t.me/s/{channel}"
    output_dir.mkdir(parents=True, exist_ok=True)

    context = await browser.new_context(user_agent=USER_AGENT)
    saved = 0
    skipped = 0
    try:
        page = await context.new_page()

        logger.info("Öffne %s", channel_url)
        await page.goto(channel_url, wait_until="networkidle")

        processed_ids: set[str] = set()
        no_new_count = 0
        MAX_NO_NEW = 5

        while True:
            posts = await page.query_selector_all(".tgme_widget_message_wrap")
            new_found = 0
            stop = False

            # Bei t.me/s steht der neueste Beitrag unten, ältere weiter oben.
            # Deshalb von unten nach oben verarbeiten -> neuester zuerst
            # gespeichert, danach schrittweise weiter in die Vergangenheit.
            for post in reversed(posts):
                post_id = await post.get_attribute("data-post")
                if post_id is None:
                    inner = await post.query_selector("[data-post]")
                    if inner:
                        post_id = await inner.get_attribute("data-post")
                if post_id is None:
                    continue
                if post_id in processed_ids:
                    continue

                processed_ids.add(post_id)
                new_found += 1

                text_el = await post.query_selector(".tgme_widget_message_text")
                if text_el is None:
                    continue
                text = (await text_el.inner_text()).strip()
                if not text:
                    continue

                text = strip_leading_intro(text)
                text = strip_trailing_greeting(text)
                if not text:
                    continue

                if EXCLUDE_RE.search(text):
                    logger.info("[SKIP] Post %s enthält 'Kuschel Workshop'", post_id)
                    continue

                date_str = await extract_telegram_date(post)
                if not date_str:
                    logger.warning("[WARN] Post %s: kein Datum gefunden", post_id)
                    continue

                title = first_sentence(text)
                if not title:
                    logger.warning("[WARN] Post %s: kein Titel ableitbar", post_id)
                    continue

                slug = slugify(title)
                if not slug:
                    logger.warning("[WARN] Post %s: leerer Slug nach Bereinigung", post_id)
                    continue

                year_dir = output_dir / date_str[:4]
                year_dir.mkdir(parents=True, exist_ok=True)
                out_path = year_dir / build_filename(date_str, slug)
                if out_path.exists():
                    logger.info("[SKIP] %s existiert bereits", out_path.name)
                    skipped += 1
                    if skipped >= SKIP_LIMIT:
                        logger.info(
                            "%d bereits vorhandene Artikel übersprungen – fertig.",
                            SKIP_LIMIT,
                        )
                        stop = True
                        break
                    continue

                post_url = f"https://t.me/s/{post_id}"
                markdown = render_post_markdown(title, post_url, date_str, text)
                out_path.write_text(markdown, encoding="utf-8")
                logger.info("[OK]   %s  ←  Post %s", out_path.name, post_id)
                saved += 1

            if stop:
                break

            if new_found == 0:
                no_new_count += 1
                if no_new_count >= MAX_NO_NEW:
                    logger.info("Keine neuen Posts mehr gefunden – fertig.")
                    break
            else:
                no_new_count = 0

            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(1500)
    finally:
        await context.close()

    logger.info("Telegram: %d gespeichert, %d bereits vorhanden.", saved, skipped)
    return saved, skipped


# ===========================================================================
# Quelle 2: FACEBOOK (login-pflichtige Seite)
# ===========================================================================
# Permalink-Kandidaten nach absteigender Priorität. Der eigentliche Post-Permalink
# (story_fbid / posts) ist am besten; viele Beiträge (v. a. die obersten) besitzen
# aber nur einen Foto-Link, der den Beitrag ebenfalls eindeutig öffnet.
_FB_PERMALINK_PATTERNS = (
    re.compile(r"permalink\.php\?.*story_fbid=", re.IGNORECASE),
    re.compile(r"story_fbid=", re.IGNORECASE),
    re.compile(r"/posts/[\w.-]+", re.IGNORECASE),
    re.compile(r"/permalink/[\w.-]+", re.IGNORECASE),
    re.compile(r"/videos/\d", re.IGNORECASE),
    re.compile(r"/photo/?\?.*fbid=\d", re.IGNORECASE),
)
# Nur diese Query-Parameter sind für einen Permalink relevant – der Rest (z. B.
# __cft__, __tn__, ref) ist Tracking-Ballast und wird entfernt.
_FB_KEEP_QUERY = {"story_fbid", "id", "fbid", "set"}


def clean_fb_permalink(href: str) -> str:
    """Macht eine FB-URL absolut und entfernt Tracking-Parameter."""
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    parts = urlsplit(href)
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if k in _FB_KEEP_QUERY]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


async def fb_extract_permalink(article) -> str | None:
    """Sucht den bestmöglichen Permalink direkt aus dem DOM des Beitrags.

    Dient nur als Notfall-Fallback: Den verlässlichen Artikel-Link liefern die
    aus HTML/GraphQL gelesenen Daten (lookup_post_meta); viele Beiträge tragen im
    DOM nur einen Foto-Link.
    """
    hrefs: list[str] = await article.evaluate(
        "(el) => [...el.querySelectorAll('a[role=link]')]"
        ".map(a => a.getAttribute('href') || '').filter(Boolean)"
    )
    for pattern in _FB_PERMALINK_PATTERNS:
        for href in hrefs:
            if pattern.search(href):
                return clean_fb_permalink(href)
    return None


# ---------------------------------------------------------------------------
# Permalink + Datum passiv aus den Daten lesen, die Facebook ohnehin lädt.
#
# Der echte Artikel-Permalink (story_fbid) und der Veröffentlichungszeitpunkt
# (creation_time) stehen NICHT verlässlich im sichtbaren DOM, wohl aber:
#   * für die obersten, server-gerenderten Beiträge im initialen HTML
#     (eingebettetes <script type="application/json">),
#   * für alle nachgeladenen Beiträge in den GraphQL-Antworten beim Scrollen.
# So entfällt das langsame Klicken/Navigieren pro Beitrag komplett.
# ---------------------------------------------------------------------------
def _gql_find_story_nodes(obj, out: list) -> None:
    """Sammelt alle Knoten mit ganzzahliger creation_time (Beitrags-/Kommentar-Story)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("creation_time"), int):
            out.append(obj)
        for v in obj.values():
            _gql_find_story_nodes(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _gql_find_story_nodes(v, out)


def _gql_collect_texts(obj, out: list, depth: int = 0) -> None:
    """Textfelder eines Story-Knotens – ohne in verschachtelte Stories/Kommentare
    (eigene creation_time) abzusteigen, damit der Beitragstext sauber bleibt."""
    if isinstance(obj, dict):
        if depth > 0 and isinstance(obj.get("creation_time"), int):
            return
        t = obj.get("text")
        if isinstance(t, str) and len(t) > 40:
            out.append(t)
        for v in obj.values():
            _gql_collect_texts(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _gql_collect_texts(v, out, depth + 1)


def _gql_collect_permalinks(obj, out: list, depth: int = 0) -> None:
    if isinstance(obj, dict):
        if depth > 0 and isinstance(obj.get("creation_time"), int):
            return
        for v in obj.values():
            if isinstance(v, str) and "story_fbid=pfbid" in v:
                out.append(v)
            else:
                _gql_collect_permalinks(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _gql_collect_permalinks(v, out, depth + 1)


def _post_text_key(text: str) -> str:
    """Normalisierter Textanfang als Schlüssel für den DOM↔Daten-Abgleich."""
    return re.sub(r"\s+", " ", text[:45]).strip().lower()


def harvest_story_nodes(data, index: dict) -> None:
    """Trägt (Textanfang -> (Permalink, ISO-Datum)) aus einem JSON-Objekt ein."""
    nodes: list = []
    _gql_find_story_nodes(data, nodes)
    for node in nodes:
        texts: list = []
        permas: list = []
        _gql_collect_texts(node, texts)
        _gql_collect_permalinks(node, permas)
        if not texts or not permas:
            continue
        try:
            date_iso = datetime.fromtimestamp(node["creation_time"]).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            continue
        text = max(texts, key=len)
        index.setdefault(_post_text_key(text), (clean_fb_permalink(permas[0]), date_iso))


_HTML_JSON_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL
)


def index_from_html(html: str, index: dict) -> None:
    """Liest die eingebetteten JSON-Blöcke des initialen HTML in den Index."""
    for m in _HTML_JSON_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        harvest_story_nodes(data, index)


def index_from_graphql_body(body: str, index: dict) -> None:
    """Liest eine (ggf. mehrteilige) GraphQL-Antwort in den Index."""
    if "story_fbid" not in body:
        return
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("{") or "story_fbid" not in line:
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        harvest_story_nodes(data, index)


def lookup_post_meta(dom_text: str, index: dict) -> tuple[str, str] | None:
    """Findet (Permalink, Datum) zu einem DOM-Beitragstext über den Textanfang."""
    key = _post_text_key(dom_text)
    if key in index:
        return index[key]
    short = key[:30]
    if short:
        for k, v in index.items():
            if short in k or k[:30] in key:
                return v
    return None


async def fb_expand_more(article, page) -> None:
    """Klickt jeden "Mehr anzeigen"-Button INNERHALB des Beitrags auf, um den
    abgeschnittenen Text vollständig zu laden.

    Facebook hängt bei langen Beiträgen "… Mehr anzeigen" an und lädt den Rest
    erst nach Klick nach. Der Klick erfolgt direkt per DOM (.click()), da das
    sticky Kopfmenü echte Maus-Klicks häufig abfängt.
    """
    for _ in range(3):
        clicked = await article.evaluate(
            r"""(el) => {
                let n = 0;
                const labels = ['Mehr anzeigen', 'Mehr ansehen', 'See more'];
                for (const b of el.querySelectorAll('div[role=button], span[role=button]')) {
                    const t = (b.innerText || '').trim();
                    if (labels.includes(t)) { b.click(); n++; }
                }
                return n;
            }"""
        )
        if not clicked:
            break
        await page.wait_for_timeout(400)


async def fb_extract_text(article) -> str:
    """Liefert den längsten zusammenhängenden Textblock (= den Beitragstext).

    Berücksichtigt sowohl div[dir=auto] als auch span[dir=auto]; Kommentare und
    UI-Schnipsel sind deutlich kürzer als der eigentliche Beitragstext. Die
    Auswertung erfolgt in EINEM evaluate-Aufruf (statt eines Round-Trips je
    Block), was bei vielen geladenen Kommentaren deutlich schneller ist.
    """
    return await article.evaluate(
        r"""(el) => {
            let best = '';
            for (const b of el.querySelectorAll('div[dir=auto], span[dir=auto]')) {
                const t = (b.innerText || '').trim();
                if (t.length > best.length) best = t;
            }
            return best;
        }"""
    )


# Wörter, die einen Kommentar-/Reaktions-Zeitstempel verraten (nicht das Post-Datum)
_FB_DATE_SKIP = re.compile(
    r"kommentar|antwort|person|abonnent|gef[äa]llt|love|umarmung|reaktion|geteilt",
    re.IGNORECASE,
)
# Absoluter Post-Zeitstempel, z. B. "Samstag, 20. Juni 2026 um 22:06"
_FB_ABS_MONTH_RE = re.compile(
    r"\d{1,2}\.\s*(?:" + "|".join(GERMAN_MONTHS) + r")", re.IGNORECASE
)


async def _fb_date_candidates(article) -> list[str]:
    """Sammelt mögliche Datums-Strings aus aria-label/title/Text des Beitrags."""
    cands: list[str] = await article.evaluate(
        r"""(el) => {
            const out = [];
            const push = (v) => { if (v) { const t = v.trim(); if (t && t.length < 60 && /\d/.test(t)) out.push(t); } };
            for (const e of el.querySelectorAll('a[role=link], abbr, time, [aria-label], [title]')) {
                push(e.getAttribute('aria-label'));
                push(e.getAttribute('title'));
                if (e.matches('a[role=link], abbr, time')) push(e.innerText);
            }
            return [...new Set(out)];
        }"""
    )
    return [c for c in cands if not _FB_DATE_SKIP.search(c)]


async def fb_extract_date(article, page) -> str | None:
    """Liest das Veröffentlichungsdatum eines Beitrags.

    Facebook verschleiert den Zeitstempel-Text per CSS (verwürfelte Zeichen), das
    echte Datum erscheint nur im Tooltip beim Hovern. Strategie:
      1. Bereits sichtbare absolute Angabe ("…, 20. Juni 2026 …") nutzen.
      2. Sonst den Zeitstempel-Link hovern und das Tooltip-Datum auslesen.
      3. Zuletzt jede parsebare Angabe (z. B. relative "vor 2 Std.").
    Kommentar-Zeitstempel ("vor 4 Tagen") werden ausgefiltert.
    """
    cands = await _fb_date_candidates(article)

    # 1) absolute Datumsangaben (mit Monatsnamen) – am verlässlichsten
    for c in cands:
        if _FB_ABS_MONTH_RE.search(c):
            iso = parse_fb_date(c)
            if iso:
                return iso

    # 2) Zeitstempel-Link hovern -> Tooltip mit absolutem Datum
    links = await article.query_selector_all(
        'a[role="link"][href*="__cft__"], a[href*="permalink.php"], a[href*="story_fbid="]'
    )
    for link in links[:3]:
        try:
            await link.scroll_into_view_if_needed(timeout=1200)
            await link.hover(timeout=1500, force=True)
            await page.wait_for_timeout(800)
        except Exception:
            continue
        tips: list[str] = await page.evaluate(
            "[...document.querySelectorAll('[role=tooltip]')]"
            ".map(t => t.innerText.trim()).filter(Boolean)"
        )
        for t in tips:
            iso = parse_fb_date(t)
            if iso:
                return iso

    # 3) Fallback: irgendeine parsebare (relative) Angabe des Post-Headers
    for c in cands:
        iso = parse_fb_date(c)
        if iso:
            return iso
    return None


async def fb_extract_image_url(article) -> str | None:
    """Liefert die URL des ersten echten Content-Bildes des Beitrags."""
    imgs = await article.query_selector_all("img")
    for img in imgs:
        src = await img.get_attribute("src")
        if not src or "scontent" not in src:
            continue
        # Profilbilder/Emoji/Reaktionen anhand der Anzeigegröße aussortieren
        try:
            box = await img.bounding_box()
        except Exception:
            box = None
        if box and (box["width"] < 130 or box["height"] < 130):
            continue
        return src
    return None


async def scrape_facebook(browser, url: str, cookies_file: Path, output_dir: Path) -> tuple[int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    cookies = load_netscape_cookies(cookies_file)

    context = await browser.new_context(
        user_agent=USER_AGENT,
        locale="de-DE",
        # Hohes Fenster -> größere Scrollschritte und weniger Zyklen pro Beitrag.
        viewport={"width": 1280, "height": 1600},
    )
    saved = 0
    skipped = 0
    try:
        if cookies:
            await context.add_cookies(cookies)
        page = await context.new_page()

        # Permalink + Datum passiv aus den GraphQL-Antworten mitlesen (Schlüssel:
        # Textanfang -> (Permalink, ISO-Datum)). Vor dem Laden registrieren, damit
        # auch die ersten Antworten erfasst werden.
        post_meta: dict[str, tuple[str, str]] = {}

        async def _capture_graphql(response) -> None:
            if "/graphql" not in response.url:
                return
            try:
                body = await response.text()
            except Exception:
                return
            index_from_graphql_body(body, post_meta)

        page.on("response", lambda r: asyncio.create_task(_capture_graphql(r)))

        logger.info("Öffne %s", url)
        await page.goto(url, wait_until="domcontentloaded")

        # Consent-/Cookie-Banner best effort wegklicken
        for label in ("Alle Cookies erlauben", "Optionale Cookies erlauben",
                      "Allow all cookies", "Nur erforderliche Cookies erlauben"):
            try:
                btn = page.get_by_role("button", name=label)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2000)
                    break
            except Exception:
                pass

        # Beiträge im Feed sind div[aria-posinset]; div[role="article"] trifft auf
        # dieser Seite nur leere Hüllen bzw. Marketing-Widgets.
        try:
            await page.wait_for_selector('div[aria-posinset]', timeout=15000)
        except Exception:
            logger.warning("[WARN] Keine Beiträge gefunden – Login/Cookies prüfen.")

        # Die obersten, server-gerenderten Beiträge stehen im initialen HTML.
        try:
            index_from_html(await page.content(), post_meta)
        except Exception:
            pass

        # Facebook virtualisiert den Feed: Beiträge außerhalb des Sichtbereichs
        # werden aus dem DOM entfernt. Daher schrittweise nach unten scrollen und
        # jeden neu erscheinenden Beitrag sofort verarbeiten. Permalink + Datum
        # kommen passiv aus HTML/GraphQL (post_meta) – ohne Klicken/Navigieren,
        # daher schnell und ohne die frühere Verlangsamung.
        MIN_TEXT_LEN = 60
        processed_pos: set[str] = set()   # Dedup über stabiles aria-posinset
        no_new_count = 0
        MAX_NO_NEW = 6

        while True:
            posts = await page.query_selector_all('div[aria-posinset]')
            progressed = False
            stop = False

            for post in posts:
                pos = await post.get_attribute("aria-posinset")
                if pos is None or pos in processed_pos:
                    continue  # bereits verarbeitet -> kein erneutes Auslesen

                preview = await fb_extract_text(post)
                if len(preview) < MIN_TEXT_LEN:
                    continue  # Platzhalter / noch nicht gerendert (nicht markieren)
                processed_pos.add(pos)
                progressed = True

                # Volltext aufklappen und für den Abgleich merken.
                await fb_expand_more(post, page)
                raw_text = await fb_extract_text(post)

                text = strip_fb_ui_text(raw_text)    # "… Weniger anzeigen" entfernen
                text = strip_leading_intro(text)
                text = strip_trailing_greeting(text)
                text = strip_cta_block_lines(text)   # Werbe-/Erstgespräch-Aufrufe
                text = strip_signature(text)         # Abspann (joeturan.com etc.)
                if not text:
                    continue

                if EXCLUDE_RE.search(text):
                    logger.info("[SKIP] Beitrag enthält 'Kuschel Workshop'")
                    continue

                # Permalink + Datum aus HTML/GraphQL (Textanfang als Schlüssel).
                meta = lookup_post_meta(raw_text, post_meta)
                if meta is None:
                    # GraphQL evtl. noch unterwegs – kurz warten, zusätzlich das
                    # aktuelle HTML einlesen und erneut prüfen.
                    await page.wait_for_timeout(700)
                    try:
                        index_from_html(await page.content(), post_meta)
                    except Exception:
                        pass
                    meta = lookup_post_meta(raw_text, post_meta)
                if meta:
                    permalink, date_str = meta
                else:
                    # Sollte praktisch nie passieren; ohne Navigation absichern.
                    logger.warning("[WARN] kein HTML/GraphQL-Treffer – DOM-Fallback")
                    permalink = await fb_extract_permalink(post)
                    date_str = await fb_extract_date(post, page) or date.today().isoformat()

                title = first_sentence(text)
                if not title:
                    logger.warning("[WARN] kein Titel ableitbar")
                    continue
                slug = slugify(title)
                if not slug:
                    continue

                year_dir = output_dir / date_str[:4]
                year_dir.mkdir(parents=True, exist_ok=True)
                out_path = year_dir / build_filename(date_str, slug)
                if out_path.exists():
                    logger.info("[SKIP] %s existiert bereits", out_path.name)
                    skipped += 1
                    if skipped >= SKIP_LIMIT:
                        logger.info(
                            "%d bereits vorhandene Artikel übersprungen – fertig.",
                            SKIP_LIMIT,
                        )
                        stop = True
                        break
                    continue

                # Hauptbild herunterladen (gleicher Dateiname-Stamm wie die .md).
                has_img = False
                img_url = await fb_extract_image_url(post)
                if img_url:
                    try:
                        resp = await context.request.get(img_url)
                        if resp.ok:
                            ext = image_extension(resp.headers.get("content-type"), img_url)
                            img_path = year_dir / (out_path.stem + ext)
                            img_path.write_bytes(await resp.body())
                            has_img = True
                        else:
                            logger.warning("[WARN] Bild-Download fehlgeschlagen (%s) für %s",
                                           resp.status, out_path.name)
                    except Exception as exc:
                        logger.warning("[WARN] Bildfehler für %s: %s", out_path.name, exc)

                source = permalink or url
                markdown = render_post_markdown(title, source, date_str, text)
                out_path.write_text(markdown, encoding="utf-8")
                saved += 1
                logger.info("[OK]   %s%s", out_path.name, "  (+ Bild)" if has_img else "")

            if stop:
                break

            # Ende erreicht?
            at_bottom = await page.evaluate(
                "Math.ceil(window.scrollY + window.innerHeight) >= "
                "document.body.scrollHeight - 4"
            )
            if not progressed:
                no_new_count += 1
                if no_new_count >= MAX_NO_NEW and at_bottom:
                    logger.info("Keine neuen Beiträge mehr gefunden – fertig.")
                    break
            else:
                no_new_count = 0

            await page.evaluate(
                "window.scrollBy(0, Math.floor(window.innerHeight * 0.85))"
            )
            await page.wait_for_timeout(1000)
    finally:
        await context.close()

    logger.info("Facebook: %d gespeichert, %d bereits vorhanden.", saved, skipped)
    return saved, skipped


# ===========================================================================
# Orchestrator
# ===========================================================================
def resolve_facebook_url(url_file: Path, explicit: str | None) -> str:
    """Liefert die Facebook-URL: explizites Argument oder Abonenten-URL.txt."""
    if explicit:
        return explicit
    if url_file.exists():
        url = url_file.read_text(encoding="utf-8").strip()
        if url:
            return url
    raise RuntimeError(
        f"Keine Facebook-URL übergeben und {url_file.name} nicht lesbar."
    )


async def run_all(phases: list[str], opts: dict) -> int:
    results: dict[str, tuple[int, int]] = {}
    failures: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=opts["headless"])
        try:
            for phase in phases:
                logger.info("=" * 70)
                logger.info("### Phase: %s", phase.upper())
                logger.info("=" * 70)
                try:
                    if phase == "blog":
                        results[phase] = await scrape_blog(
                            browser, opts["base_url"], opts["blog_output"]
                        )
                    elif phase == "facebook":
                        fb_url = resolve_facebook_url(opts["url_file"], opts["facebook_url"])
                        results[phase] = await scrape_facebook(
                            browser, fb_url, opts["cookies"], opts["facebook_output"]
                        )
                    elif phase == "telegram":
                        results[phase] = await scrape_telegram(
                            browser, opts["channel"], opts["telegram_output"]
                        )
                except Exception as exc:
                    logger.error("[FEHLER] Phase '%s' abgebrochen: %s", phase, exc)
                    logger.error("%s", traceback.format_exc())
                    failures.append(phase)
        finally:
            await browser.close()

    logger.info("=" * 70)
    logger.info("Zusammenfassung:")
    for phase in phases:
        if phase in results:
            saved, skipped = results[phase]
            logger.info("  %-9s  gespeichert=%d  bereits vorhanden=%d", phase, saved, skipped)
        else:
            logger.info("  %-9s  FEHLGESCHLAGEN", phase)
    logger.info("=" * 70)

    return 1 if failures else 0


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scrapt Blog, Facebook und Telegram nacheinander. Ohne Quellen-Flag "
            "laufen alle drei in der Reihenfolge Blog -> Facebook -> Telegram."
        )
    )
    parser.add_argument("--blog", action="store_true", help="Blog-Quelle einschließen")
    parser.add_argument("--facebook", action="store_true", help="Facebook-Quelle einschließen")
    parser.add_argument("--telegram", action="store_true", help="Telegram-Quelle einschließen")
    parser.add_argument("--visible", action="store_true", help="Browser sichtbar starten")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Blog-Startseite")
    parser.add_argument("--facebook-url", default=None, help="Facebook-URL (sonst Abonenten-URL.txt)")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="Telegram-Channel-Name")
    parser.add_argument("--cookies", default=None, help="Pfad zu cookies.txt")
    parser.add_argument("--url-file", default=None, help="Pfad zu Abonenten-URL.txt")
    parser.add_argument("--blog-output", default=None, help="Zielordner Blog")
    parser.add_argument("--facebook-output", default=None, help="Zielordner Facebook")
    parser.add_argument("--telegram-output", default=None, help="Zielordner Telegram")
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    setup_logging()
    args = parse_args(argv)

    selected = [p for p in CANONICAL_ORDER if getattr(args, p)]
    phases = selected or list(CANONICAL_ORDER)

    opts = {
        "headless": not args.visible,
        "base_url": args.base_url,
        "facebook_url": args.facebook_url,
        "channel": args.channel,
        "cookies": Path(args.cookies) if args.cookies else COOKIES_FILE,
        "url_file": Path(args.url_file) if args.url_file else URL_FILE,
        "blog_output": Path(args.blog_output) if args.blog_output else BLOG_OUTPUT_DIR,
        "facebook_output": Path(args.facebook_output) if args.facebook_output else FB_OUTPUT_DIR,
        "telegram_output": Path(args.telegram_output) if args.telegram_output else TELEGRAM_OUTPUT_DIR,
    }

    logger.info("Ablaufplan: %s", " -> ".join(phases))
    return asyncio.run(run_all(phases, opts))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env node
/**
 * scrape_all.js — Konsolidierter Scraper für Blog, Facebook und Telegram.
 *
 * Node.js-Portierung von scrape_all.py. Führt drei Quellen als Ablaufplan
 * NACHEINANDER aus (Default-Reihenfolge):
 *   1. Blog      -> joeturan.com/blog          -> Joe_Turan_Archiv/
 *   2. Facebook  -> login-pflichtige FB-Seite  -> Joe_Turan_Facebook/
 *   3. Telegram  -> t.me/s/<channel>           -> Joe_Turan_Telegram/
 *
 * Pro Beitrag wird eine Markdown-Datei in einen Jahresordner geschrieben; Blog und
 * Facebook laden zusätzlich das Titel-/Hauptbild mit identischem Dateinamen-Stamm.
 * Jede Quelle bricht ab, sobald SKIP_LIMIT bereits vorhandene Artikel erkannt werden.
 * Beiträge mit "Kuschel Workshop" werden übersprungen.
 *
 * CLI (Auswahl kombinierbar; ohne Flag laufen alle drei):
 *   node scrape_all.js                 # Blog -> Facebook -> Telegram
 *   node scrape_all.js --telegram      # nur Telegram
 *   node scrape_all.js --blog --facebook
 *   node scrape_all.js --visible       # Browser sichtbar
 *
 * Bei einem Fehler in einer Quelle wird protokolliert und mit der nächsten Quelle
 * fortgefahren; am Ende folgt eine Zusammenfassung je Quelle.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import * as cheerio from "cheerio";
import { Command } from "commander";
import difflib from "difflib";
import { chromium } from "playwright";
import sharp from "sharp";
import TurndownService from "turndown";

// ---------------------------------------------------------------------------
// Konfiguration
// ---------------------------------------------------------------------------
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

const CHANNEL_DISPLAY_NAME = "Joe Turan";
const DEFAULT_CHANNEL = "joeturan";
const DEFAULT_BASE_URL = "https://www.joeturan.com/blog";

const BLOG_OUTPUT_DIR = path.join(SCRIPT_DIR, "www", "Joe Turan");
const FB_OUTPUT_DIR = path.join(SCRIPT_DIR, "www", "Facebook");
const TELEGRAM_OUTPUT_DIR = path.join(SCRIPT_DIR, "www", "Telegram");

const COOKIES_FILE = path.join(SCRIPT_DIR, "cookies.txt");
const URL_FILE = path.join(SCRIPT_DIR, "Abonenten-URL.txt");
const LOG_FILE = path.join(SCRIPT_DIR, "scrape_all.log");

// Gemeinsame Abbruchschwelle: nach so vielen bereits vorhandenen Artikeln wird
// die jeweilige Quelle beendet.
const SKIP_LIMIT = 5;

const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/124.0.0.0 Safari/537.36";

// Blog-spezifisch
const CONTENT_CONTAINER_CLASS = "jw-element-imagetext-text";
const REQUEST_TIMEOUT = 30; // Sekunden
const PAGE_WAIT_MS = 4000;
const ARTICLE_PAUSE_SECONDS = 0.4;
const PAGE_PAUSE_SECONDS = 0.8;
const TITLE_SUFFIX_RE = /\s*\/\s*Blog\s*\|\s*www\.joeturan\.com\s*$/i;

const EXCLUDE_RE = /kuschel[\s\-_]*workshop/i;

const CANONICAL_ORDER = ["blog", "facebook", "telegram"];

// ---------------------------------------------------------------------------
// Logging (ein Logger, eine Datei)
// ---------------------------------------------------------------------------
function _timestamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

function _emit(level, msg) {
  const line = `${_timestamp()} [${level}] ${msg}`;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + "\n", { encoding: "utf-8" });
  } catch {
    /* Logdatei nicht schreibbar -> nur Konsole */
  }
}

const logger = {
  info: (msg) => _emit("INFO", msg),
  warn: (msg) => _emit("WARN", msg),
  error: (msg) => _emit("ERROR", msg),
};

// ---------------------------------------------------------------------------
// Kleine Hilfsfunktionen (Python-Stdlib-Ersatz)
// ---------------------------------------------------------------------------

/** Wie Pythons str.splitlines(): kein abschließendes '' bei Zeilenende. */
function splitlines(s) {
  if (!s) return [];
  const parts = s.split(/\r\n|\r|\n/);
  if (parts.length && parts[parts.length - 1] === "") parts.pop();
  return parts;
}

/** Entfernt führende/abschließende Zeichen aus einem Zeichenvorrat (wie str.strip(chars)). */
function stripChars(s, chars) {
  let start = 0;
  let end = s.length;
  while (start < end && chars.includes(s[start])) start++;
  while (end > start && chars.includes(s[end - 1])) end--;
  return s.slice(start, end);
}

function rstripChars(s, chars) {
  let end = s.length;
  while (end > 0 && chars.includes(s[end - 1])) end--;
  return s.slice(0, end);
}

/** ISO-Datum (YYYY-MM-DD) in lokaler Zeitzone, wie Pythons date.isoformat(). */
function isoDate(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Datum um n Tage/Wochen verschieben (kalendarisch, ohne DST-Drift). */
function addDays(d, delta) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + delta);
}

function todayLocal() {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

const sleep = (seconds) => new Promise((r) => setTimeout(r, seconds * 1000));

// ---------------------------------------------------------------------------
// Gemeinsame Text-Bereinigung
// ---------------------------------------------------------------------------
const SENTENCE_SPLIT_RE = /[.!?…\n\r]/;
const LEADING_INTRO_RE =
  /^\s*(?:(?:ein|mein|dein|heutiger|kurzer|kleiner)?\s*(?:morgen|abend|tages|gesundheits?|achtsamkeits?|lebens?|herz(?:ens)?|liebes?|beziehungs?)?[\s\-]*(?:impuls|gedanke|frage|gru[ßss]|tipp|botschaft|reminder|erinnerung)(?:\s+(?:des\s+tages|für\s+dich|an\s+dich|von\s+mir))?)\s*[:\-–—]?[^\p{L}\p{N}_]*$/iu;
const SLUG_CLEAN_RE = /[^A-Za-z0-9äöüÄÖÜß ]+/g;
const SLUG_MULTI_UNDERSCORE_RE = /_+/g;
// Maximale Länge des Titel-Teils im Dateinamen (ohne Datum/Autor-Präfix).
const SLUG_MAX_LEN = 55;

// Hinweis: Pythons \w/\W matchen Umlaute; in JS werden sie durch Unicode-
// Property-Klassen ersetzt (u-Flag), damit das Verhalten erhalten bleibt.
const GREETING_RE =
  /^\s*(?:(?:einen?\s+)?(?:sch[öo]ne[nrs]?|gute[nrs]?|hab(?:t)?(?:\s+(?:einen?|noch))?|wünsche\s+(?:dir|euch|ihnen)?)\s+(?:rest(?:lich[\p{L}\p{N}_]*)?\s+)?(?:morgen|tag|abend|nachmittag|vormittag|nacht|woche(?:nende)?|sonntag|montag|dienstag|mittwoch|donnerstag|freitag|samstag)(?:\s+[\p{L}\p{N}_]+)?[^\p{L}\p{N}_]*$)/iu;

/** Entfernt eine abschließende Grußzeile wie 'Schönen Abend noch 🤍'. */
function stripTrailingGreeting(text) {
  const lines = splitlines(text.replace(/\s+$/, ""));
  while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  if (lines.length && GREETING_RE.test(lines[lines.length - 1])) {
    lines.pop();
    while (lines.length && !lines[lines.length - 1].trim()) lines.pop();
  }
  return lines.join("\n");
}

/** Entfernt eine einleitende Begrüßungs- oder Intro-Zeile. */
function stripLeadingIntro(text) {
  const lines = splitlines(text);
  while (lines.length && !lines[0].trim()) lines.shift();
  if (lines.length && (GREETING_RE.test(lines[0]) || LEADING_INTRO_RE.test(lines[0]))) {
    lines.shift();
    while (lines.length && !lines[0].trim()) lines.shift();
  }
  return lines.join("\n");
}

// Steuertexte, die Facebook an aufgeklappte Beiträge anhängt ("… Mehr/Weniger anzeigen").
const _FB_UI_TAIL_RE =
  /\s*(?:…|\.\.\.)?\s*(?:weniger anzeigen|mehr anzeigen|mehr ansehen|see more|see less)\s*$/i;

/** Entfernt Facebooks "Mehr/Weniger anzeigen"-Steuertext am Beitragsende. */
function stripFbUiText(text) {
  text = text.replace(/\s+$/, "");
  for (;;) {
    const stripped = text.replace(_FB_UI_TAIL_RE, "").replace(/\s+$/, "");
    if (stripped === text) return text;
    text = stripped;
  }
}

/**
 * Entfernt den abschließenden Autoren-/Social-Media-Abspann, behält nur 'Joe Turan'.
 *
 * 1. Alle Zeilen sammeln, die unscharf auf 'Joe Turan' passen (ratio >= 0.75).
 * 2. Die *erste* solche Zeile wählen, der innerhalb der nächsten 3 Zeilen
 *    'joeturan.com' folgt – das ist der verlässliche Beginn des Abspanns.
 * 3. Fällt kein 'joeturan.com'-Anker an, die *letzte* Treffer-Zeile nehmen.
 * 4. Ab der gewählten Zeile bis zum Ende abschneiden und ein sauberes
 *    'Joe Turan' anhängen.
 */
function stripSignature(mdText) {
  const lines = splitlines(mdText);
  const target = "joe turan";

  const joeIndices = [];
  for (let i = 0; i < lines.length; i++) {
    const normalized = lines[i].replace(/[*_[\]`]/g, "").trim().toLowerCase();
    if (!normalized) continue;
    if (new difflib.SequenceMatcher(null, normalized, target).ratio() >= 0.75) {
      joeIndices.push(i);
    }
  }

  if (joeIndices.length === 0) return mdText;

  let footerStart = null;
  for (const idx of joeIndices) {
    const window = lines.slice(idx + 1, idx + 4);
    if (window.some((wline) => wline.toLowerCase().includes("joeturan.com"))) {
      footerStart = idx;
      break;
    }
  }

  if (footerStart === null) footerStart = joeIndices[joeIndices.length - 1];

  const trimmed = lines.slice(0, footerStart).join("\n").replace(/\s+$/, "");
  return `${trimmed}\n\nJoe Turan`;
}

// Werbe-/Call-to-Action-Trigger. String = Substring-Treffer;
// Array = ALLE Teilstrings müssen vorkommen (UND-Verknüpfung).
const _CTA_TRIGGERS = [
  ["schreib mir", "whatsapp"],
  "erstgespräch",
  "mit menschen arbeite",
  "arbeite ich mit menschen",
  "an dieser stelle arbeite ich",
  "daran arbeite ich",
  "kommst allein nicht",
];

function _matchesCta(normalized, trigger) {
  if (typeof trigger === "string") return normalized.includes(trigger);
  return trigger.every((t) => normalized.includes(t));
}

/**
 * Entfernt werbliche Call-to-Action-Absätze (Blog: durch Leerzeilen getrennt).
 * String-Trigger: Substring-Treffer. Array-Trigger: alle Teilstrings (UND).
 */
function stripCtaBlockParagraphs(mdText) {
  const paragraphs = mdText.split(/\n{2,}/);
  const filtered = [];
  for (const para of paragraphs) {
    let normalized = para.replace(/[*_[\]`]/g, "").toLowerCase();
    normalized = normalized.replace(/\s+/g, " ").trim();
    if (_CTA_TRIGGERS.some((trigger) => _matchesCta(normalized, trigger))) continue;
    filtered.push(para);
  }
  return filtered.join("\n\n");
}

/** Entfernt werbliche Call-to-Action-Absätze (Facebook: je Absatz eine Zeile). */
function stripCtaBlockLines(text) {
  const kept = [];
  for (const line of text.split("\n")) {
    let normalized = line.replace(/[*_[\]`]/g, "").toLowerCase();
    normalized = normalized.replace(/\s+/g, " ").trim();
    if (normalized && _CTA_TRIGGERS.some((t) => _matchesCta(normalized, t))) continue;
    kept.push(line);
  }
  return kept.join("\n");
}

/** Gibt den ersten Satz bis zum ersten Satzzeichen zurück. */
function firstSentence(text) {
  const stripped = text.trim();
  if (!stripped) return "";
  const match = SENTENCE_SPLIT_RE.exec(stripped);
  const sentence = match ? stripped.slice(0, match.index) : stripped;
  return sentence.trim();
}

/**
 * Erzeugt einen Dateinamen-Slug: Sonderzeichen werden zu Underscores und der
 * Slug wird auf maxLen Zeichen begrenzt – jedoch nur an Wortgrenzen
 * (Leerzeichen/Underscore), sodass kein Wort mitten durchgeschnitten wird.
 */
function slugify(sentence, maxLen = SLUG_MAX_LEN) {
  let slug = sentence.replace(SLUG_CLEAN_RE, "_");
  slug = stripChars(slug.replace(SLUG_MULTI_UNDERSCORE_RE, "_"), "_ ");
  if (slug.length > maxLen) {
    let cut = slug.slice(0, maxLen);
    // Steht an der Schnittstelle mitten in einem Wort? -> bis zur letzten
    // Wortgrenze zurückgehen, damit kein Wort zerschnitten wird.
    if (slug[maxLen] !== " " && slug[maxLen] !== "_" &&
        cut[cut.length - 1] !== " " && cut[cut.length - 1] !== "_") {
      const boundary = Math.max(cut.lastIndexOf(" "), cut.lastIndexOf("_"));
      if (boundary > 0) cut = cut.slice(0, boundary);
    }
    slug = cut;
  }
  return rstripChars(slug, "_ ");
}

/** Dateiname für Telegram-/Facebook-Beiträge: '<date>_Joe Turan - <slug>.md'. */
function buildFilename(dateStr, slug) {
  return `${dateStr}_${CHANNEL_DISPLAY_NAME} - ${slug}.md`;
}

/** Markdown-Layout für Telegram- und Facebook-Beiträge (mit ----Trenner). */
function renderPostMarkdown(title, sourceUrl, dateStr, body) {
  return (
    `# ${title}\n` +
    `\n` +
    `*Quelle: ${sourceUrl}*\n` +
    `\n` +
    `Datum: ${dateStr}\n` +
    `\n` +
    `---\n` +
    `${body}\n`
  );
}

/** Bestimmt die Bildendung aus Content-Type bzw. URL (.jpg/.png). */
function imageExtension(contentType, url) {
  if (contentType) {
    const ct = contentType.toLowerCase();
    if (ct.includes("png")) return ".png";
    if (ct.includes("jpeg") || ct.includes("jpg")) return ".jpg";
  }
  const low = url.toLowerCase();
  if (low.includes(".png")) return ".png";
  return ".jpg";
}

// ---------------------------------------------------------------------------
// Cookies (Facebook-Login)
// ---------------------------------------------------------------------------
/** Liest eine cookies.txt im Netscape-Format und liefert Playwright-Cookies. */
function loadNetscapeCookies(cookiePath) {
  const cookies = [];
  if (!fs.existsSync(cookiePath)) {
    logger.warn(`[WARN] Cookie-Datei nicht gefunden: ${cookiePath}`);
    return cookies;
  }
  const raw = fs.readFileSync(cookiePath, "utf-8");
  for (const rawLine of raw.split(/\r?\n/)) {
    let line = rawLine.trim();
    // "#HttpOnly_"-Präfix ist erlaubt, sonstige Kommentare/Leerzeilen überspringen
    if (!line || (line.startsWith("#") && !line.startsWith("#HttpOnly_"))) continue;
    if (line.startsWith("#HttpOnly_")) line = line.slice("#HttpOnly_".length);
    const parts = line.split("\t");
    if (parts.length !== 7) continue;
    const [domain, , cPath, secure, expiry, name, value] = parts;
    const cookie = {
      name,
      value,
      domain,
      path: cPath || "/",
      secure: secure.toUpperCase() === "TRUE",
    };
    const exp = parseInt(expiry, 10);
    if (!Number.isNaN(exp) && exp > 0) cookie.expires = exp;
    cookies.push(cookie);
  }
  logger.info(`${cookies.length} Cookie(s) aus ${path.basename(cookiePath)} geladen`);
  return cookies;
}

// ---------------------------------------------------------------------------
// Facebook-Datumsparser
// ---------------------------------------------------------------------------
const GERMAN_MONTHS = {
  januar: 1, februar: 2, märz: 3, april: 4, mai: 5, juni: 6,
  juli: 7, august: 8, september: 9, oktober: 10, november: 11, dezember: 12,
};
const FB_RELATIVE_RE =
  /vor\s+(?:etwa\s+)?(\d+)\s*(min|minute|minuten|std|stunde|stunden|t|tag|tagen|tg|w|woche|wochen)/i;
const FB_ABS_DATE_RE = /(\d{1,2})\.\s*([A-Za-zäöü]+)\.?\s*(\d{4})?/i;

/** Wandelt eine Facebook-Zeitangabe in ein ISO-Datum (YYYY-MM-DD) um. */
function parseFbDate(text, today = null) {
  if (!text) return null;
  today = today || todayLocal();
  const low = text.trim().toLowerCase();

  if (low.includes("gerade eben") || low.includes("jetzt") || low.startsWith("vor wenigen")) {
    return isoDate(today);
  }
  if (low.includes("gestern")) return isoDate(addDays(today, -1));

  const rel = FB_RELATIVE_RE.exec(low);
  if (rel) {
    const amount = parseInt(rel[1], 10);
    const unit = rel[2].toLowerCase();
    if (unit.startsWith("min") || unit.startsWith("std") || unit.startsWith("stunde")) {
      return isoDate(today); // heute
    }
    if (unit.startsWith("t") || unit.startsWith("tg") || unit.startsWith("tag")) {
      return isoDate(addDays(today, -amount));
    }
    if (unit.startsWith("w") || unit.startsWith("woche")) {
      return isoDate(addDays(today, -amount * 7));
    }
  }

  const absM = FB_ABS_DATE_RE.exec(low);
  if (absM) {
    const day = parseInt(absM[1], 10);
    const month = GERMAN_MONTHS[rstripChars(absM[2], ".")];
    const year = absM[3] ? parseInt(absM[3], 10) : today.getFullYear();
    if (month) {
      const d = new Date(year, month - 1, day);
      // Gültigkeit prüfen (JS rollt ungültige Daten stillschweigend um).
      if (d.getFullYear() === year && d.getMonth() === month - 1 && d.getDate() === day) {
        return isoDate(d);
      }
      return null;
    }
  }
  return null;
}

// ===========================================================================
// Quelle 1: BLOG (joeturan.com/blog)
// ===========================================================================
const turndownService = new TurndownService({ headingStyle: "atx" });
turndownService.remove(["script", "style"]);

function normalizeWhitespace(text) {
  const lines = text.split(/\r?\n/).map((l) => l.replace(/\s+$/, ""));
  const cleaned = [];
  let previousBlank = false;
  for (const line of lines) {
    if (!line.trim()) {
      if (!previousBlank) cleaned.push("");
      previousBlank = true;
      continue;
    }
    cleaned.push(line);
    previousBlank = false;
  }
  return cleaned.join("\n").trim();
}

function metaContent($, selector) {
  const tag = $(selector).first();
  if (!tag.length) return null;
  const content = tag.attr("content");
  if (!content) return null;
  return content.trim();
}

function extractTitle($) {
  const raw = metaContent($, "meta[property='og:title']");
  if (!raw) return "Unbekannter Artikel";
  return raw.replace(TITLE_SUFFIX_RE, "").trim() || "Unbekannter Artikel";
}

function extractBlogDate($) {
  const raw = metaContent($, "meta[itemprop='datePublished']");
  if (raw && raw.length >= 10 && /^\d{4}-\d{2}-\d{2}/.test(raw.slice(0, 10))) {
    return raw.slice(0, 10);
  }
  return "1970-01-01";
}

function extractOgUrl($, fallback) {
  return metaContent($, "meta[property='og:url']") || fallback;
}

function buildSlugFromUrl(ogUrl) {
  let pathname;
  try {
    pathname = new URL(ogUrl).pathname;
  } catch {
    pathname = ogUrl;
  }
  pathname = rstripChars(pathname, "/");
  const lastSegment = pathname.split("/").pop() || "";
  let slug;
  if (lastSegment.includes("_")) {
    slug = lastSegment.slice(lastSegment.indexOf("_") + 1);
  } else {
    slug = lastSegment;
  }
  return slug || "artikel";
}

function extractImageUrl($) {
  return metaContent($, "meta[property='og:image']");
}

function extractOgDescription($) {
  const raw = metaContent($, "meta[property='og:description']");
  if (!raw) return null;
  const cleaned = raw.replace(/\s+/g, " ").trim();
  return cleaned || null;
}

function _normalizeForCompare(text) {
  return text.replace(/\s+/g, " ").trim().toLowerCase();
}

function extractContentMarkdown($, title) {
  const titleNorm = _normalizeForCompare(title);
  const chunks = [];
  $(`.${CONTENT_CONTAINER_CLASS}`).each((_i, container) => {
    const $container = $(container);
    $container.find("h1, h2, h3, h4, h5, h6").each((_j, heading) => {
      if (_normalizeForCompare($(heading).text()) === titleNorm) {
        $(heading).remove();
      }
    });

    if (!$container.text().trim()) return; // continue

    let md = turndownService.turndown($.html(container));
    md = normalizeWhitespace(md);
    if (md) chunks.push(md);
  });
  let result = chunks.join("\n\n").trim();
  result = stripLeadingIntro(result);
  result = stripCtaBlockParagraphs(result);
  return stripSignature(result);
}

function parseArticle(html, url) {
  const $ = cheerio.load(html);
  const ogUrl = extractOgUrl($, url);
  const title = extractTitle($);
  const dateStr = extractBlogDate($);
  const slug = buildSlugFromUrl(ogUrl);
  return {
    url,
    title,
    date: dateStr,
    slug,
    content_md: extractContentMarkdown($, title),
    image_url: extractImageUrl($),
    og_description: extractOgDescription($),
  };
}

function articlePaths(outputDir, article) {
  const yearDir = path.join(outputDir, article.date.slice(0, 4));
  const baseName = `${article.date}_${article.slug}`;
  return [path.join(yearDir, `${baseName}.md`), path.join(yearDir, `${baseName}.jpg`)];
}

function renderBlogMarkdown(article) {
  const parts = [`# ${article.title}`, "", `*Quelle: ${article.url}*`, "", `**Datum: ${article.date}**`, ""];
  if (article.content_md) {
    parts.push(article.content_md);
    parts.push("");
  }
  return parts.join("\n");
}

/** Lädt das Titelbild via Playwright und speichert es als RGB-JPEG (q90). */
async function saveBlogImage(context, imageUrl, jpgPath) {
  const resp = await context.request.get(imageUrl, { timeout: REQUEST_TIMEOUT * 1000 });
  if (!resp.ok()) throw new Error(`HTTP ${resp.status()}`);
  const buf = await resp.body();
  await fs.promises.mkdir(path.dirname(jpgPath), { recursive: true });
  await sharp(buf).flatten({ background: { r: 0, g: 0, b: 0 } }).jpeg({ quality: 90 }).toFile(jpgPath);
}

async function saveBlogArticle(context, article, outputDir) {
  const [mdPath, jpgPath] = articlePaths(outputDir, article);

  let mdWritten = null;
  let jpgWritten = null;

  if (!fs.existsSync(jpgPath) && article.image_url) {
    try {
      await saveBlogImage(context, article.image_url, jpgPath);
      jpgWritten = jpgPath;
    } catch (exc) {
      logger.warn(`Bild konnte nicht geladen werden: ${article.image_url} (${exc})`);
    }
  }

  if (!fs.existsSync(mdPath)) {
    await fs.promises.mkdir(path.dirname(mdPath), { recursive: true });
    await fs.promises.writeFile(mdPath, renderBlogMarkdown(article), "utf-8");
    mdWritten = mdPath;
  }

  return [mdWritten, jpgWritten];
}

async function visibleArticleUrls(page, baseUrl) {
  const hrefs = await page.$$eval("a[href]", (elements) =>
    elements.map((element) => element.href).filter(Boolean)
  );
  const normalized = [];
  const seen = new Set();
  let base;
  try {
    base = new URL(baseUrl);
  } catch {
    return [];
  }
  const blogPath = rstripChars(base.pathname, "/");
  for (const href of hrefs) {
    let parsed;
    try {
      parsed = new URL(href);
    } catch {
      continue;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") continue;
    if (parsed.host !== base.host) continue;
    const p = rstripChars(parsed.pathname, "/");
    if (!p.startsWith(blogPath)) continue;
    if (p === blogPath) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    normalized.push(href);
  }
  return normalized.slice(0, 12);
}

function _setsEqual(a, b) {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

async function clickNextPage(page, baseUrl) {
  const before = new Set(await visibleArticleUrls(page, baseUrl));
  if (before.size === 0) return false;

  const candidates = ["[data-page-next]", "a[data-page-next]", "button[data-page-next]"];

  for (const selector of candidates) {
    const locator = page.locator(selector);
    let count;
    try {
      count = await locator.count();
    } catch {
      count = 0;
    }
    if (count === 0) continue;

    for (let index = 0; index < count; index++) {
      const entry = locator.nth(index);
      try {
        if (!(await entry.isVisible())) continue;
        if (await entry.isDisabled()) continue;
      } catch {
        /* is_visible/is_disabled dürfen scheitern -> wie Python: ignorieren */
      }

      try {
        await entry.click({ timeout: 5000 });
        await page.waitForTimeout(PAGE_WAIT_MS);
        const after = new Set(await visibleArticleUrls(page, baseUrl));
        if (after.size && !_setsEqual(after, before)) return true;
      } catch {
        continue;
      }
    }
  }
  return false;
}

async function processBlogListingPage(page, context, outputDir, processedUrls, skipState) {
  const articleUrls = await visibleArticleUrls(page, page.url());
  if (articleUrls.length === 0) return 0;

  let processedThisPage = 0;
  for (const articleUrl of articleUrls) {
    if (skipState.stop) break;
    if (processedUrls.has(articleUrl)) continue;

    logger.info(`[Artikel] ${articleUrl}`);
    const articlePage = await context.newPage();
    try {
      await articlePage.goto(articleUrl, { waitUntil: "networkidle", timeout: REQUEST_TIMEOUT * 1000 });
      const articleHtml = await articlePage.content();
      const article = parseArticle(articleHtml, articleUrl);
      const [mdPath, jpgPath] = articlePaths(outputDir, article);

      if (fs.existsSync(mdPath) && fs.existsSync(jpgPath)) {
        processedUrls.add(articleUrl);
        processedThisPage += 1;
        skipState.count += 1;
        logger.info(`  -> uebersprungen (${skipState.count}/${SKIP_LIMIT}), existiert bereits: ${mdPath}`);
        if (skipState.count >= SKIP_LIMIT) {
          skipState.stop = true;
          logger.info(`[Ende] ${SKIP_LIMIT} Artikel uebersprungen, Abbruch wie konfiguriert.`);
        }
        continue;
      }

      const [mdWritten, jpgWritten] = await saveBlogArticle(context, article, outputDir);
      processedUrls.add(articleUrl);
      processedThisPage += 1;
      if (mdWritten) skipState.saved += 1;
      if (mdWritten && jpgWritten) {
        logger.info(`  -> gespeichert: ${mdWritten} (+ ${path.basename(jpgWritten)})`);
      } else if (mdWritten) {
        logger.info(`  -> gespeichert: ${mdWritten} (Bild existierte bereits)`);
      } else if (jpgWritten) {
        logger.info(`  -> Bild nachgeladen: ${jpgWritten} (Markdown existierte bereits)`);
      } else {
        logger.info(`  -> nichts zu tun fuer ${mdPath}`);
      }
    } catch (exc) {
      logger.error(`  -> Fehler bei ${articleUrl}: ${exc}`);
    } finally {
      await articlePage.close();
    }
    await sleep(ARTICLE_PAUSE_SECONDS);
  }

  return processedThisPage;
}

async function scrapeBlog(browser, baseUrl, outputDir) {
  await fs.promises.mkdir(outputDir, { recursive: true });
  const processedUrls = new Set();
  const skipState = { count: 0, stop: false, saved: 0 };

  const context = await browser.newContext();
  try {
    const page = await context.newPage();

    logger.info(`[Start] Oeffne Blog: ${baseUrl}`);
    await page.goto(baseUrl, { waitUntil: "networkidle", timeout: REQUEST_TIMEOUT * 1000 });
    await page.waitForTimeout(PAGE_WAIT_MS);

    let pageNumber = 1;
    for (;;) {
      logger.info(`[Seite ${pageNumber}] Sammle sichtbare Artikel`);
      const count = await processBlogListingPage(page, context, outputDir, processedUrls, skipState);
      if (count === 0) logger.info(`[Seite ${pageNumber}] Keine neuen Artikel gefunden.`);

      logger.info(`[Seite ${pageNumber}] ${count} Artikel verarbeitet oder uebersprungen.`);

      if (skipState.stop) break;

      await sleep(PAGE_PAUSE_SECONDS);

      if (!(await clickNextPage(page, baseUrl))) {
        logger.info("[Ende] Keine naechste Seite ueber data-page-next gefunden.");
        break;
      }
      pageNumber += 1;
    }
  } finally {
    await context.close();
  }

  logger.info(`Blog: ${skipState.saved} gespeichert, ${skipState.count} bereits vorhanden.`);
  return [skipState.saved, skipState.count];
}

// ===========================================================================
// Quelle 3: TELEGRAM (t.me/s/<channel>)
// ===========================================================================
async function extractTelegramDate(post) {
  const timeEl = await post.$(".tgme_widget_message_date time");
  if (!timeEl) return null;
  const dt = await timeEl.getAttribute("datetime");
  if (!dt) return null;
  return dt.slice(0, 10);
}

async function scrapeTelegram(browser, channel, outputDir) {
  const channelUrl = `https://t.me/s/${channel}`;
  await fs.promises.mkdir(outputDir, { recursive: true });

  const context = await browser.newContext({ userAgent: USER_AGENT });
  let saved = 0;
  let skipped = 0;
  try {
    const page = await context.newPage();

    logger.info(`Öffne ${channelUrl}`);
    await page.goto(channelUrl, { waitUntil: "networkidle" });

    const processedIds = new Set();
    let noNewCount = 0;
    const MAX_NO_NEW = 5;

    for (;;) {
      const posts = await page.$$(".tgme_widget_message_wrap");
      let newFound = 0;
      let stop = false;

      // Bei t.me/s steht der neueste Beitrag unten, ältere weiter oben.
      // Deshalb von unten nach oben verarbeiten -> neuester zuerst gespeichert.
      for (const post of [...posts].reverse()) {
        let postId = await post.getAttribute("data-post");
        if (postId === null) {
          const inner = await post.$("[data-post]");
          if (inner) postId = await inner.getAttribute("data-post");
        }
        if (postId === null) continue;
        if (processedIds.has(postId)) continue;

        processedIds.add(postId);
        newFound += 1;

        const textEl = await post.$(".tgme_widget_message_text");
        if (textEl === null) continue;
        let text = (await textEl.innerText()).trim();
        if (!text) continue;

        text = stripLeadingIntro(text);
        text = stripTrailingGreeting(text);
        if (!text) continue;

        if (EXCLUDE_RE.test(text)) {
          logger.info(`[SKIP] Post ${postId} enthält 'Kuschel Workshop'`);
          continue;
        }

        const dateStr = await extractTelegramDate(post);
        if (!dateStr) {
          logger.warn(`[WARN] Post ${postId}: kein Datum gefunden`);
          continue;
        }

        const title = firstSentence(text);
        if (!title) {
          logger.warn(`[WARN] Post ${postId}: kein Titel ableitbar`);
          continue;
        }

        const slug = slugify(title);
        if (!slug) {
          logger.warn(`[WARN] Post ${postId}: leerer Slug nach Bereinigung`);
          continue;
        }

        const yearDir = path.join(outputDir, dateStr.slice(0, 4));
        await fs.promises.mkdir(yearDir, { recursive: true });
        const outPath = path.join(yearDir, buildFilename(dateStr, slug));
        if (fs.existsSync(outPath)) {
          logger.info(`[SKIP] ${path.basename(outPath)} existiert bereits`);
          skipped += 1;
          if (skipped >= SKIP_LIMIT) {
            logger.info(`${SKIP_LIMIT} bereits vorhandene Artikel übersprungen – fertig.`);
            stop = true;
            break;
          }
          continue;
        }

        const postUrl = `https://t.me/s/${postId}`;
        const markdown = renderPostMarkdown(title, postUrl, dateStr, text);
        await fs.promises.writeFile(outPath, markdown, "utf-8");
        logger.info(`[OK]   ${path.basename(outPath)}  ←  Post ${postId}`);
        saved += 1;
      }

      if (stop) break;

      if (newFound === 0) {
        noNewCount += 1;
        if (noNewCount >= MAX_NO_NEW) {
          logger.info("Keine neuen Posts mehr gefunden – fertig.");
          break;
        }
      } else {
        noNewCount = 0;
      }

      await page.evaluate(() => window.scrollTo(0, 0));
      await page.waitForTimeout(1500);
    }
  } finally {
    await context.close();
  }

  logger.info(`Telegram: ${saved} gespeichert, ${skipped} bereits vorhanden.`);
  return [saved, skipped];
}

// ===========================================================================
// Quelle 2: FACEBOOK (login-pflichtige Seite)
// ===========================================================================
// Permalink-Kandidaten nach absteigender Priorität.
const _FB_PERMALINK_PATTERNS = [
  /permalink\.php\?.*story_fbid=/i,
  /story_fbid=/i,
  /\/posts\/[\w.-]+/i,
  /\/permalink\/[\w.-]+/i,
  /\/videos\/\d/i,
  /\/photo\/?\?.*fbid=\d/i,
];
// Nur diese Query-Parameter sind für einen Permalink relevant.
const _FB_KEEP_QUERY = new Set(["story_fbid", "id", "fbid", "set"]);

/** Macht eine FB-URL absolut und entfernt Tracking-Parameter. */
function cleanFbPermalink(href) {
  if (href.startsWith("/")) href = "https://www.facebook.com" + href;
  let u;
  try {
    u = new URL(href);
  } catch {
    return href;
  }
  const keep = new URLSearchParams();
  for (const [k, v] of u.searchParams) {
    if (_FB_KEEP_QUERY.has(k)) keep.append(k, v);
  }
  const qs = keep.toString();
  return `${u.protocol}//${u.host}${u.pathname}${qs ? "?" + qs : ""}`;
}

/** Sucht den bestmöglichen Permalink direkt aus dem DOM des Beitrags (Fallback). */
async function fbExtractPermalink(article) {
  const hrefs = await article.evaluate((el) =>
    [...el.querySelectorAll("a[role=link]")].map((a) => a.getAttribute("href") || "").filter(Boolean)
  );
  for (const pattern of _FB_PERMALINK_PATTERNS) {
    for (const href of hrefs) {
      if (pattern.test(href)) return cleanFbPermalink(href);
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Permalink + Datum passiv aus den Daten lesen, die Facebook ohnehin lädt.
// ---------------------------------------------------------------------------
function _isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/** Sammelt alle Knoten mit ganzzahliger creation_time. */
function _gqlFindStoryNodes(obj, out) {
  if (_isPlainObject(obj)) {
    if (Number.isInteger(obj.creation_time)) out.push(obj);
    for (const v of Object.values(obj)) _gqlFindStoryNodes(v, out);
  } else if (Array.isArray(obj)) {
    for (const v of obj) _gqlFindStoryNodes(v, out);
  }
}

/** Textfelder eines Story-Knotens – ohne in verschachtelte Stories abzusteigen. */
function _gqlCollectTexts(obj, out, depth = 0) {
  if (_isPlainObject(obj)) {
    if (depth > 0 && Number.isInteger(obj.creation_time)) return;
    const t = obj.text;
    if (typeof t === "string" && t.length > 40) out.push(t);
    for (const v of Object.values(obj)) _gqlCollectTexts(v, out, depth + 1);
  } else if (Array.isArray(obj)) {
    for (const v of obj) _gqlCollectTexts(v, out, depth + 1);
  }
}

function _gqlCollectPermalinks(obj, out, depth = 0) {
  if (_isPlainObject(obj)) {
    if (depth > 0 && Number.isInteger(obj.creation_time)) return;
    for (const v of Object.values(obj)) {
      if (typeof v === "string" && v.includes("story_fbid=pfbid")) {
        out.push(v);
      } else {
        _gqlCollectPermalinks(v, out, depth + 1);
      }
    }
  } else if (Array.isArray(obj)) {
    for (const v of obj) _gqlCollectPermalinks(v, out, depth + 1);
  }
}

/** Normalisierter Textanfang als Schlüssel für den DOM<->Daten-Abgleich. */
function _postTextKey(text) {
  return text.slice(0, 45).replace(/\s+/g, " ").trim().toLowerCase();
}

/** Trägt (Textanfang -> [Permalink, ISO-Datum]) aus einem JSON-Objekt ein. */
function harvestStoryNodes(data, index) {
  const nodes = [];
  _gqlFindStoryNodes(data, nodes);
  for (const node of nodes) {
    const texts = [];
    const permas = [];
    _gqlCollectTexts(node, texts);
    _gqlCollectPermalinks(node, permas);
    if (!texts.length || !permas.length) continue;
    const d = new Date(node.creation_time * 1000);
    if (Number.isNaN(d.getTime())) continue;
    const dateIso = isoDate(d);
    const text = texts.reduce((a, b) => (b.length > a.length ? b : a));
    const key = _postTextKey(text);
    if (!(key in index)) index[key] = [cleanFbPermalink(permas[0]), dateIso];
  }
}

const _HTML_JSON_RE = /<script type="application\/json"[^>]*>([\s\S]*?)<\/script>/g;

/** Liest die eingebetteten JSON-Blöcke des initialen HTML in den Index. */
function indexFromHtml(html, index) {
  let m;
  _HTML_JSON_RE.lastIndex = 0;
  while ((m = _HTML_JSON_RE.exec(html)) !== null) {
    let data;
    try {
      data = JSON.parse(m[1]);
    } catch {
      continue;
    }
    harvestStoryNodes(data, index);
  }
}

/** Liest eine (ggf. mehrteilige) GraphQL-Antwort in den Index. */
function indexFromGraphqlBody(body, index) {
  if (!body.includes("story_fbid")) return;
  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line.startsWith("{") || !line.includes("story_fbid")) continue;
    let data;
    try {
      data = JSON.parse(line);
    } catch {
      continue;
    }
    harvestStoryNodes(data, index);
  }
}

/** Findet [Permalink, Datum] zu einem DOM-Beitragstext über den Textanfang. */
function lookupPostMeta(domText, index) {
  const key = _postTextKey(domText);
  if (key in index) return index[key];
  const short = key.slice(0, 30);
  if (short) {
    for (const [k, v] of Object.entries(index)) {
      if (k.includes(short) || key.includes(k.slice(0, 30))) return v;
    }
  }
  return null;
}

/** Klickt jeden "Mehr anzeigen"-Button INNERHALB des Beitrags auf. */
async function fbExpandMore(article, page) {
  for (let i = 0; i < 3; i++) {
    const clicked = await article.evaluate((el) => {
      let n = 0;
      const labels = ["Mehr anzeigen", "Mehr ansehen", "See more"];
      for (const b of el.querySelectorAll("div[role=button], span[role=button]")) {
        const t = (b.innerText || "").trim();
        if (labels.includes(t)) {
          b.click();
          n++;
        }
      }
      return n;
    });
    if (!clicked) break;
    await page.waitForTimeout(400);
  }
}

/** Liefert den längsten zusammenhängenden Textblock (= den Beitragstext). */
async function fbExtractText(article) {
  return article.evaluate((el) => {
    let best = "";
    for (const b of el.querySelectorAll("div[dir=auto], span[dir=auto]")) {
      const t = (b.innerText || "").trim();
      if (t.length > best.length) best = t;
    }
    return best;
  });
}

// Wörter, die einen Kommentar-/Reaktions-Zeitstempel verraten (nicht das Post-Datum)
const _FB_DATE_SKIP =
  /kommentar|antwort|person|abonnent|gef[äa]llt|love|umarmung|reaktion|geteilt/i;
// Absoluter Post-Zeitstempel, z. B. "Samstag, 20. Juni 2026 um 22:06"
const _FB_ABS_MONTH_RE = new RegExp("\\d{1,2}\\.\\s*(?:" + Object.keys(GERMAN_MONTHS).join("|") + ")", "i");

/** Sammelt mögliche Datums-Strings aus aria-label/title/Text des Beitrags. */
async function _fbDateCandidates(article) {
  const cands = await article.evaluate((el) => {
    const out = [];
    const push = (v) => {
      if (v) {
        const t = v.trim();
        if (t && t.length < 60 && /\d/.test(t)) out.push(t);
      }
    };
    for (const e of el.querySelectorAll("a[role=link], abbr, time, [aria-label], [title]")) {
      push(e.getAttribute("aria-label"));
      push(e.getAttribute("title"));
      if (e.matches("a[role=link], abbr, time")) push(e.innerText);
    }
    return [...new Set(out)];
  });
  return cands.filter((c) => !_FB_DATE_SKIP.test(c));
}

/** Liest das Veröffentlichungsdatum eines Beitrags. */
async function fbExtractDate(article, page) {
  const cands = await _fbDateCandidates(article);

  // 1) absolute Datumsangaben (mit Monatsnamen) – am verlässlichsten
  for (const c of cands) {
    if (_FB_ABS_MONTH_RE.test(c)) {
      const iso = parseFbDate(c);
      if (iso) return iso;
    }
  }

  // 2) Zeitstempel-Link hovern -> Tooltip mit absolutem Datum
  const links = await article.$$(
    'a[role="link"][href*="__cft__"], a[href*="permalink.php"], a[href*="story_fbid="]'
  );
  for (const link of links.slice(0, 3)) {
    try {
      await link.scrollIntoViewIfNeeded({ timeout: 1200 });
      await link.hover({ timeout: 1500, force: true });
      await page.waitForTimeout(800);
    } catch {
      continue;
    }
    const tips = await page.evaluate(() =>
      [...document.querySelectorAll("[role=tooltip]")].map((t) => t.innerText.trim()).filter(Boolean)
    );
    for (const t of tips) {
      const iso = parseFbDate(t);
      if (iso) return iso;
    }
  }

  // 3) Fallback: irgendeine parsebare (relative) Angabe des Post-Headers
  for (const c of cands) {
    const iso = parseFbDate(c);
    if (iso) return iso;
  }
  return null;
}

/** Liefert die URL des ersten echten Content-Bildes des Beitrags. */
async function fbExtractImageUrl(article) {
  const imgs = await article.$$("img");
  for (const img of imgs) {
    const src = await img.getAttribute("src");
    if (!src || !src.includes("scontent")) continue;
    // Profilbilder/Emoji/Reaktionen anhand der Anzeigegröße aussortieren
    let box;
    try {
      box = await img.boundingBox();
    } catch {
      box = null;
    }
    if (box && (box.width < 130 || box.height < 130)) continue;
    return src;
  }
  return null;
}

async function scrapeFacebook(browser, url, cookiesFile, outputDir) {
  await fs.promises.mkdir(outputDir, { recursive: true });
  const cookies = loadNetscapeCookies(cookiesFile);

  const context = await browser.newContext({
    userAgent: USER_AGENT,
    locale: "de-DE",
    // Hohes Fenster -> größere Scrollschritte und weniger Zyklen pro Beitrag.
    viewport: { width: 1280, height: 1600 },
  });
  let saved = 0;
  let skipped = 0;
  try {
    if (cookies.length) await context.addCookies(cookies);
    const page = await context.newPage();

    // Permalink + Datum passiv aus den GraphQL-Antworten mitlesen. Vor dem Laden
    // registrieren, damit auch die ersten Antworten erfasst werden.
    const postMeta = {};

    page.on("response", async (response) => {
      try {
        if (!response.url().includes("/graphql")) return;
        let body;
        try {
          body = await response.text();
        } catch {
          return;
        }
        indexFromGraphqlBody(body, postMeta);
      } catch {
        /* Response-Handler darf niemals unhandled rejecten */
      }
    });

    logger.info(`Öffne ${url}`);
    await page.goto(url, { waitUntil: "domcontentloaded" });

    // Consent-/Cookie-Banner best effort wegklicken
    for (const label of [
      "Alle Cookies erlauben",
      "Optionale Cookies erlauben",
      "Allow all cookies",
      "Nur erforderliche Cookies erlauben",
    ]) {
      try {
        const btn = page.getByRole("button", { name: label });
        if ((await btn.count()) > 0) {
          await btn.first().click({ timeout: 2000 });
          break;
        }
      } catch {
        /* ignorieren */
      }
    }

    // Beiträge im Feed sind div[aria-posinset].
    try {
      await page.waitForSelector("div[aria-posinset]", { timeout: 15000 });
    } catch {
      logger.warn("[WARN] Keine Beiträge gefunden – Login/Cookies prüfen.");
    }

    // Die obersten, server-gerenderten Beiträge stehen im initialen HTML.
    try {
      indexFromHtml(await page.content(), postMeta);
    } catch {
      /* ignorieren */
    }

    // Facebook virtualisiert den Feed: schrittweise nach unten scrollen und jeden
    // neu erscheinenden Beitrag sofort verarbeiten. Permalink + Datum kommen
    // passiv aus HTML/GraphQL (postMeta) – ohne Klicken/Navigieren.
    const MIN_TEXT_LEN = 60;
    const processedPos = new Set(); // Dedup über stabiles aria-posinset
    let noNewCount = 0;
    const MAX_NO_NEW = 6;

    for (;;) {
      const posts = await page.$$("div[aria-posinset]");
      let progressed = false;
      let stop = false;

      for (const post of posts) {
        const pos = await post.getAttribute("aria-posinset");
        if (pos === null || processedPos.has(pos)) continue;

        const preview = await fbExtractText(post);
        if (preview.length < MIN_TEXT_LEN) continue; // Platzhalter (nicht markieren)
        processedPos.add(pos);
        progressed = true;

        // Volltext aufklappen und für den Abgleich merken.
        await fbExpandMore(post, page);
        const rawText = await fbExtractText(post);

        let text = stripFbUiText(rawText); // "… Weniger anzeigen" entfernen
        text = stripLeadingIntro(text);
        text = stripTrailingGreeting(text);
        text = stripCtaBlockLines(text); // Werbe-/Erstgespräch-Aufrufe
        text = stripSignature(text); // Abspann (joeturan.com etc.)
        if (!text) continue;

        if (EXCLUDE_RE.test(text)) {
          logger.info("[SKIP] Beitrag enthält 'Kuschel Workshop'");
          continue;
        }

        // Permalink + Datum aus HTML/GraphQL (Textanfang als Schlüssel).
        let meta = lookupPostMeta(rawText, postMeta);
        if (meta === null) {
          // GraphQL evtl. noch unterwegs – kurz warten, HTML erneut einlesen.
          await page.waitForTimeout(700);
          try {
            indexFromHtml(await page.content(), postMeta);
          } catch {
            /* ignorieren */
          }
          meta = lookupPostMeta(rawText, postMeta);
        }
        let permalink;
        let dateStr;
        if (meta) {
          [permalink, dateStr] = meta;
        } else {
          logger.warn("[WARN] kein HTML/GraphQL-Treffer – DOM-Fallback");
          permalink = await fbExtractPermalink(post);
          dateStr = (await fbExtractDate(post, page)) || isoDate(todayLocal());
        }

        const title = firstSentence(text);
        if (!title) {
          logger.warn("[WARN] kein Titel ableitbar");
          continue;
        }
        const slug = slugify(title);
        if (!slug) continue;

        const yearDir = path.join(outputDir, dateStr.slice(0, 4));
        await fs.promises.mkdir(yearDir, { recursive: true });
        const outPath = path.join(yearDir, buildFilename(dateStr, slug));
        if (fs.existsSync(outPath)) {
          logger.info(`[SKIP] ${path.basename(outPath)} existiert bereits`);
          skipped += 1;
          if (skipped >= SKIP_LIMIT) {
            logger.info(`${SKIP_LIMIT} bereits vorhandene Artikel übersprungen – fertig.`);
            stop = true;
            break;
          }
          continue;
        }

        // Hauptbild herunterladen (gleicher Dateiname-Stamm wie die .md).
        let hasImg = false;
        const imgUrl = await fbExtractImageUrl(post);
        if (imgUrl) {
          try {
            const resp = await context.request.get(imgUrl);
            if (resp.ok()) {
              const ext = imageExtension(resp.headers()["content-type"], imgUrl);
              const stem = path.basename(outPath, ".md");
              const imgPath = path.join(yearDir, stem + ext);
              await fs.promises.writeFile(imgPath, await resp.body());
              hasImg = true;
            } else {
              logger.warn(`[WARN] Bild-Download fehlgeschlagen (${resp.status()}) für ${path.basename(outPath)}`);
            }
          } catch (exc) {
            logger.warn(`[WARN] Bildfehler für ${path.basename(outPath)}: ${exc}`);
          }
        }

        const source = permalink || url;
        const markdown = renderPostMarkdown(title, source, dateStr, text);
        await fs.promises.writeFile(outPath, markdown, "utf-8");
        saved += 1;
        logger.info(`[OK]   ${path.basename(outPath)}${hasImg ? "  (+ Bild)" : ""}`);
      }

      if (stop) break;

      // Ende erreicht?
      const atBottom = await page.evaluate(
        () => Math.ceil(window.scrollY + window.innerHeight) >= document.body.scrollHeight - 4
      );
      if (!progressed) {
        noNewCount += 1;
        if (noNewCount >= MAX_NO_NEW && atBottom) {
          logger.info("Keine neuen Beiträge mehr gefunden – fertig.");
          break;
        }
      } else {
        noNewCount = 0;
      }

      await page.evaluate(() => window.scrollBy(0, Math.floor(window.innerHeight * 0.85)));
      await page.waitForTimeout(1000);
    }
  } finally {
    await context.close();
  }

  logger.info(`Facebook: ${saved} gespeichert, ${skipped} bereits vorhanden.`);
  return [saved, skipped];
}

// ===========================================================================
// Orchestrator
// ===========================================================================
/** Liefert die Facebook-URL: explizites Argument oder Abonenten-URL.txt. */
function resolveFacebookUrl(urlFile, explicit) {
  if (explicit) return explicit;
  if (fs.existsSync(urlFile)) {
    const url = fs.readFileSync(urlFile, "utf-8").trim();
    if (url) return url;
  }
  throw new Error(`Keine Facebook-URL übergeben und ${path.basename(urlFile)} nicht lesbar.`);
}

async function runAll(phases, opts) {
  const results = {};
  const failures = [];

  const browser = await chromium.launch({ headless: opts.headless });
  try {
    for (const phase of phases) {
      logger.info("=".repeat(70));
      logger.info(`### Phase: ${phase.toUpperCase()}`);
      logger.info("=".repeat(70));
      try {
        if (phase === "blog") {
          results[phase] = await scrapeBlog(browser, opts.baseUrl, opts.blogOutput);
        } else if (phase === "facebook") {
          const fbUrl = resolveFacebookUrl(opts.urlFile, opts.facebookUrl);
          results[phase] = await scrapeFacebook(browser, fbUrl, opts.cookies, opts.facebookOutput);
        } else if (phase === "telegram") {
          results[phase] = await scrapeTelegram(browser, opts.channel, opts.telegramOutput);
        }
      } catch (exc) {
        logger.error(`[FEHLER] Phase '${phase}' abgebrochen: ${exc}`);
        logger.error(exc && exc.stack ? exc.stack : String(exc));
        failures.push(phase);
      }
    }
  } finally {
    await browser.close();
  }

  logger.info("=".repeat(70));
  logger.info("Zusammenfassung:");
  for (const phase of phases) {
    if (phase in results) {
      const [saved, skipped] = results[phase];
      logger.info(`  ${phase.padEnd(9)}  gespeichert=${saved}  bereits vorhanden=${skipped}`);
    } else {
      logger.info(`  ${phase.padEnd(9)}  FEHLGESCHLAGEN`);
    }
  }
  logger.info("=".repeat(70));

  return failures.length ? 1 : 0;
}

function parseArgs(argv) {
  const program = new Command();
  program
    .name("scrape_all.js")
    .description(
      "Scrapt Blog, Facebook und Telegram nacheinander. Ohne Quellen-Flag laufen " +
        "alle drei in der Reihenfolge Blog -> Facebook -> Telegram."
    )
    .option("--blog", "Blog-Quelle einschließen")
    .option("--facebook", "Facebook-Quelle einschließen")
    .option("--telegram", "Telegram-Quelle einschließen")
    .option("--visible", "Browser sichtbar starten")
    .option("--base-url <url>", "Blog-Startseite", DEFAULT_BASE_URL)
    .option("--facebook-url <url>", "Facebook-URL (sonst Abonenten-URL.txt)")
    .option("--channel <name>", "Telegram-Channel-Name", DEFAULT_CHANNEL)
    .option("--cookies <path>", "Pfad zu cookies.txt")
    .option("--url-file <path>", "Pfad zu Abonenten-URL.txt")
    .option("--blog-output <path>", "Zielordner Blog")
    .option("--facebook-output <path>", "Zielordner Facebook")
    .option("--telegram-output <path>", "Zielordner Telegram")
    .allowExcessArguments(false);
  program.parse(argv, { from: "user" });
  return program.opts();
}

async function main(argv) {
  const args = parseArgs(argv);

  const selected = CANONICAL_ORDER.filter((p) => args[p]);
  const phases = selected.length ? selected : [...CANONICAL_ORDER];

  const resolvePath = (p, fallback) => (p ? path.resolve(p) : fallback);

  const opts = {
    headless: !args.visible,
    baseUrl: args.baseUrl,
    facebookUrl: args.facebookUrl || null,
    channel: args.channel,
    cookies: resolvePath(args.cookies, COOKIES_FILE),
    urlFile: resolvePath(args.urlFile, URL_FILE),
    blogOutput: resolvePath(args.blogOutput, BLOG_OUTPUT_DIR),
    facebookOutput: resolvePath(args.facebookOutput, FB_OUTPUT_DIR),
    telegramOutput: resolvePath(args.telegramOutput, TELEGRAM_OUTPUT_DIR),
  };

  logger.info(`Ablaufplan: ${phases.join(" -> ")}`);
  return runAll(phases, opts);
}

// Reine Hilfsfunktionen für Tests exportieren (mirror der Python-Funktionen).
export {
  slugify,
  firstSentence,
  buildFilename,
  splitlines,
  stripLeadingIntro,
  stripTrailingGreeting,
  stripSignature,
  parseFbDate,
  cleanFbPermalink,
};

// Nur bei Direktaufruf (node scrape_all.js …) ausführen, nicht beim Import.
const _isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (_isMain) {
  // Kein process.exit(): sonst kann Node bei einer stdout-Pipe (spawn) die
  // letzten Zeilen (Zusammenfassung) abschneiden. exitCode setzen und den
  // Prozess natürlich auslaufen lassen -> stdout wird vollständig geleert.
  main(process.argv.slice(2))
    .then((code) => { process.exitCode = code; })
    .catch((exc) => {
      logger.error(`Unerwarteter Fehler: ${exc && exc.stack ? exc.stack : exc}`);
      process.exitCode = 1;
    });
}

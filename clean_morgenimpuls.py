#!/usr/bin/env python3
"""Entfernt die "Ein Morgenimpuls für dich"-Zeile aus dem Body archivierter
Artikel und legt die korrigierten Dateien unter <dir>/Korrektur/<Jahr>/ ab.

Das als Argument übergebene Verzeichnis wird nach Jahres-Unterordnern
(z. B. 2024, 2025, 2026) durchsucht. In den darin liegenden *.md-Dateien wird
im Body-Teil jede Zeile entfernt, die nur aus "Ein Morgenimpuls für dich"
besteht – optional gefolgt von einem Emoji (☀️ / 🤍 / 🌟) oder anderen
Deko-Zeichen. Die Titelzeile ("# ...") im Header bleibt unangetastet.

Header vs. Body: Telegram-/Facebook-Dateien trennen mit einer "---"-Zeile,
Blog-Dateien enden den Header mit der "**Datum: ...**"-Zeile – beides wird
erkannt.

Standard: nur tatsächlich geänderte Dateien werden nach Korrektur/ geschrieben.
  --all      auch unveränderte Dateien mitkopieren (vollständiger Spiegel)
  --dry-run  nichts schreiben, nur berichten, was geändert würde

Beispiel:
  python clean_morgenimpuls.py Joe_Turan_Archiv
  python clean_morgenimpuls.py Joe_Turan_Telegram --dry-run
"""
import argparse
import re
import sys
from pathlib import Path

# Zu entfernende Zeile: "(Ein) Morgenimpuls für dich" + optional nachfolgende
# Deko-Zeichen (Emoji/Whitespace/Satzzeichen) bis zum Zeilenende.
MORGENIMPULS_RE = re.compile(
    r"^\s*(?:ein\s+)?morgenimpuls\s+für\s+dich[\s\W]*$",
    re.IGNORECASE,
)

# Header-Ende erkennen: der Blog beendet den Header mit "**Datum: ...**".
DATUM_RE = re.compile(r"^\*{0,2}\s*Datum:", re.IGNORECASE)

# Nur die ersten Zeilen einer Datei sind der Header.
HEADER_SCAN_LINES = 15


def split_header_body(lines: list[str]) -> tuple[list[str], list[str]]:
    """Trennt Header (Titel/Quelle/Datum) vom Body und liefert (header, body).

    Bevorzugt den "---"-Trenner (Telegram/Facebook); fällt sonst auf die
    "Datum:"-Zeile zurück (Blog). Wird nichts erkannt, gilt alles als Body.
    """
    for i, line in enumerate(lines[:HEADER_SCAN_LINES]):
        if line.strip() == "---":
            return lines[: i + 1], lines[i + 1:]
    for i, line in enumerate(lines[:HEADER_SCAN_LINES]):
        if DATUM_RE.match(line.strip()):
            return lines[: i + 1], lines[i + 1:]
    return [], lines


def clean_text(text: str) -> tuple[str, int]:
    """Entfernt Morgenimpuls-Zeilen aus dem Body.

    Liefert (neuer_text, anzahl_entfernter_zeilen). Ist anzahl == 0, wird der
    Originaltext unverändert zurückgegeben (Datei wird dann nicht neu geschrieben).
    """
    lines = text.split("\n")
    header, body = split_header_body(lines)

    kept: list[str] = []
    removed = 0
    for line in body:
        if MORGENIMPULS_RE.match(line):
            removed += 1
            continue
        kept.append(line)

    if removed == 0:
        return text, 0

    # Body aufräumen: führende/abschließende Leerzeilen weg, mehrfach-
    # Leerzeilen (durch das Entfernen entstanden) auf eine reduzieren.
    body_text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept).strip("\n"))

    if not header:
        return f"{body_text}\n" if body_text else "", removed

    header_text = "\n".join(header).rstrip("\n")
    if not body_text:
        return f"{header_text}\n", removed
    # Telegram/Facebook: Body folgt direkt auf "---". Blog: eine Leerzeile
    # zwischen "**Datum: ...**" und Body.
    joiner = "\n" if header[-1].strip() == "---" else "\n\n"
    return f"{header_text}{joiner}{body_text}\n", removed


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Entfernt die "Ein Morgenimpuls für dich"-Zeile aus dem Body '
        "archivierter .md-Artikel und speichert Korrekturen unter <dir>/Korrektur/.",
    )
    parser.add_argument("directory", help="Archiv-Verzeichnis mit Jahres-Unterordnern")
    parser.add_argument(
        "--all", action="store_true",
        help="auch unveränderte Dateien nach Korrektur/ kopieren (voller Spiegel)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="nichts schreiben, nur berichten, was geändert würde",
    )
    args = parser.parse_args()

    root = Path(args.directory)
    if not root.is_dir():
        print(f"Kein Verzeichnis: {root}", file=sys.stderr)
        return 1

    out_root = root / "Korrektur"
    year_dirs = sorted(
        p for p in root.iterdir() if p.is_dir() and p.name.isdigit()
    )
    if not year_dirs:
        print(f"Keine Jahres-Verzeichnisse in {root} gefunden.", file=sys.stderr)
        return 1

    total_files = total_changed = total_lines = 0
    for year_dir in year_dirs:
        md_files = sorted(year_dir.glob("*.md"))
        changed_here = 0
        for md in md_files:
            total_files += 1
            text = md.read_text(encoding="utf-8")
            new_text, removed = clean_text(text)
            changed = removed > 0
            if changed:
                total_changed += 1
                total_lines += removed
                changed_here += 1
                suffix = "Zeile" if removed == 1 else "Zeilen"
                verb = "würde ändern" if args.dry_run else "korrigiert"
                print(f"  [{verb}] {year_dir.name}/{md.name} ({removed} {suffix})")

            if args.dry_run:
                continue
            if changed or args.all:
                out_dir = out_root / year_dir.name
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / md.name).write_text(new_text, encoding="utf-8")

        if md_files:
            print(f"{year_dir.name}: {len(md_files)} Dateien geprüft, "
                  f"{changed_here} betroffen")

    print("-" * 60)
    verb = "würden korrigiert" if args.dry_run else "korrigiert"
    print(f"Gesamt: {total_files} Dateien geprüft, {total_changed} {verb} "
          f"({total_lines} Zeilen entfernt).")
    if not args.dry_run and (total_changed or args.all):
        print(f"Ausgabe unter: {out_root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

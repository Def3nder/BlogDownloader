#!/usr/bin/env python3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python merge_years.py <directory>", file=sys.stderr)
        return 1

    base = Path(sys.argv[1])
    if not base.is_dir():
        print(f"Not a directory: {base}", file=sys.stderr)
        return 1

    prefix = base.name
    separator = "\n\n---\n\n"
    out_dir = base / "yearly"
    out_dir.mkdir(exist_ok=True)

    for year_dir in sorted(p for p in base.iterdir() if p.is_dir() and p.name.isdigit()):
        md_files = sorted(year_dir.glob("*.md"))
        if not md_files:
            continue

        texts = [f.read_text(encoding="utf-8").rstrip() for f in md_files]
        output = out_dir / f"{prefix}-{year_dir.name}.md"
        output.write_text(separator.join(texts) + "\n", encoding="utf-8")
        print(f"Wrote {output} ({len(md_files)} files)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

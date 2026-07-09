#!/usr/bin/env python3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python merge_all.py <directory>", file=sys.stderr)
        return 1

    base = Path(sys.argv[1])
    if not base.is_dir():
        print(f"Not a directory: {base}", file=sys.stderr)
        return 1

    yearly = base / "yearly"
    if not yearly.is_dir():
        print(f"No yearly subdirectory found in {base}", file=sys.stderr)
        return 1

    prefix = base.name
    separator = "\n\n---\n\n"

    md_files = sorted(yearly.glob(f"{prefix}-*.md"))
    if not md_files:
        print(f"No yearly files found in {yearly}", file=sys.stderr)
        return 1

    texts = [f.read_text(encoding="utf-8").rstrip() for f in md_files]
    output = base / f"{prefix}_ALL.md"
    output.write_text(separator.join(texts) + "\n", encoding="utf-8")
    print(f"Wrote {output} ({len(md_files)} files)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

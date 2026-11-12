"""
Build the man page and the packaged manual from docs/MANUAL.md.

The options section of the manual is generated from the settings schema, so the docs never drift from the code.
Then the Markdown is converted to roff for ``man huntaway``, and both are copied into the package.

    python scripts/build_man.py           # write docs/MANUAL.md, src/huntaway/data/MANUAL.md and huntaway.1
    python scripts/build_man.py --check   # fail when any of them is out of date
"""

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from huntaway import config  # noqa: E402

MANUAL = ROOT / "docs" / "MANUAL.md"
PACKAGED = ROOT / "src" / "huntaway" / "data" / "MANUAL.md"
MANPAGE = ROOT / "src" / "huntaway" / "data" / "huntaway.1"
START, END = "<!-- options start -->", "<!-- options end -->"


def options_markdown() -> str:
    """
    Render every option from the schema as Markdown.

    :returns: The options section body
    """
    out: list[str] = []
    for section, about in config.SECTIONS.items():
        out += [f"### [{section}]", "", about, ""]
        for opt in config.SCHEMA:
            if not opt.key.startswith(section + "."):
                continue
            name = opt.key.split(".", 1)[1]
            default = json.dumps(opt.default)
            choices = f" One of: {', '.join(opt.choices)}." if opt.choices else ""
            item = f"- `{name}` (default `{default}`): {opt.help}{choices}"
            out.append(textwrap.fill(item, 116, subsequent_indent="  ", break_on_hyphens=False, break_long_words=False))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def with_options(text: str) -> str:
    """
    Put the generated options between the markers.

    :param text: The manual
    :returns: The manual with a fresh options section
    :raises SystemExit: When the markers are missing
    """
    if START not in text or END not in text:
        raise SystemExit("docs/MANUAL.md lost its options markers")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n{options_markdown()}{END}{tail}"


def _escape(text: str) -> str:
    text = text.replace("\\", "\\e")
    return text


def _inline(text: str) -> str:
    out = []
    for part in re.split(r"(`[^`]+`)", text):
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append("\\fB" + _escape(part[1:-1]).replace("-", "\\-") + "\\fR")
            continue
        part = _escape(part)
        part = re.sub(r"\*\*([^*]+)\*\*", r"\\fB\1\\fR", part)
        part = re.sub(r"\*([^*]+)\*", r"\\fI\1\\fR", part)
        part = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", part)
        out.append(part)
    line = "".join(out)
    if line.startswith((".", "'")):
        line = "\\&" + line
    return line


def to_roff(markdown: str, version: str = "") -> str:
    """
    Convert the manual's Markdown subset to roff with the man macros.

    :param markdown: The manual
    :param version: Shown in the footer
    :returns: The man page
    """
    lines = markdown.splitlines()
    out = [f'.TH HUNTAWAY 1 "" "huntaway{" " + version if version else ""}" "User Commands"']
    para: list[str] = []
    in_code = False
    in_table = False

    def flush() -> None:
        if para:
            out.append(".PP")
            out.append(_inline(" ".join(para)))
            para.clear()

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("```"):
            flush()
            if in_code:
                out += [".fi", ".RE"]
            else:
                out += [".RS 4", ".nf"]
            in_code = not in_code
            continue
        if in_code:
            out.append(_escape(line).replace("-", "\\-") if not line.startswith((".", "'")) else "\\&" + _escape(line))
            continue
        if line.startswith("<!--"):
            continue
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            flush()
            out.append(f".SH {line[3:].upper()}")
            continue
        if line.startswith("### "):
            flush()
            out.append(f".SS {_inline(line[4:])}")
            continue
        if line.startswith("|"):
            flush()
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("- "):
                continue
            if not in_table:
                in_table = True
                continue
            out += [".TP", f"\\fB{_escape(cells[0])}\\fR", _inline(" ".join(cells[1:]))]
            continue
        in_table = False
        if line.startswith("- "):
            flush()
            out += [".IP \\(bu 2", _inline(line[2:])]
            continue
        if line.startswith("  ") and out and out[-2:-1] == [".IP \\(bu 2"]:
            out[-1] += " " + _inline(line.strip())
            continue
        if not line.strip():
            flush()
            continue
        para.append(line.strip())
    flush()
    page = "\n".join(out) + "\n"
    page = page.replace(".SH NAME\n.PP\nhuntaway \\- ", ".SH NAME\nhuntaway \\- ").replace(
        ".SH NAME\n.PP\nhuntaway - ", ".SH NAME\nhuntaway \\- "
    )
    return page


def main() -> int:
    """
    Build or check the generated files.

    :returns: Exit code
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--check", action="store_true", help="fail when a generated file is out of date")
    args = parser.parse_args()
    manual = with_options(MANUAL.read_text(encoding="utf-8"))
    page = to_roff(manual)
    wanted = {MANUAL: manual, PACKAGED: manual, MANPAGE: page}
    stale = [path for path, text in wanted.items() if not path.exists() or path.read_text(encoding="utf-8") != text]
    if args.check:
        for path in stale:
            print(f"out of date: {path.relative_to(ROOT)} (run: python scripts/build_man.py)")
        return 1 if stale else 0
    for path in stale:
        path.write_text(wanted[path], encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

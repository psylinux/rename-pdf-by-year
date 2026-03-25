#!/usr/bin/env python3
"""
pdf_rename_by_year.py
─────────────────────
Renames PDFs to the format YYYY_original_name.pdf inferring the publication
year from multiple sources, in descending order of confidence:

  1. High-confidence text patterns (copyright, "published YYYY", ISBN...)
  2. PDF metadata                  (pdfinfo CreationDate)
  3. Weighted frequency of years   (position in text)
  4. Filename                      (fallback)
  5. File modification date        (last resort)

Consensus: if ≥ 2 independent sources agree, that year has priority.

Usage:
  python3 pdf_rename_by_year.py <folder> [options]

Usage Examples:
  1. Simulate renaming (Dry Run) (Test before applying):
     python3 pdf_rename_by_year.py ./my_books --dry-run --verbose
  
  2. Rename files skipping those that already have a date, without confirmation:
     python3 pdf_rename_by_year.py ./articles --skip-dated --yes

  3. Use a year window between 2000-2023 and create a log:
     python3 pdf_rename_by_year.py ./docs --min-year 2000 --max-year 2023 --log report.txt

Options:
  -n, --dry-run     Shows the plan without renaming anything
  -y, --yes         Does not ask for confirmation (executes directly)
  -v, --verbose     Shows details of each source used
  -s, --skip-dated  Skips files that already start with YYYY_
  --log <file>      Saves report to a text file
  --min-year YYYY   Minimum accepted year          (default: 1970)
  --max-year YYYY   Maximum accepted year          (default: current year)
"""

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# ─── Constants ───────────────────────────────────────────────────────────────

CURRENT_YEAR = datetime.now().year

# ANSI text formatting colors (disabled if not connected to a terminal)
def _ansi(code): return f"\033[{code}m" if sys.stdout.isatty() else ""
RST  = _ansi("0")
BOLD = _ansi("1")
RED  = _ansi("31")
GRN  = _ansi("32")
YLW  = _ansi("33")
CYN  = _ansi("36")
DIM  = _ansi("2")

# Textual patterns sorted by confidence (applied to the first ~10 000 chars)
TEXT_PATTERNS = [
    # ── High confidence ──────────────────────────────────────────────────────
    (r"[Cc]opyright\s*[©(Cc)]*\s*((?:19|20)\d{2})",                      "copyright"),
    (r"[Ff]irst\s+[Ee]dition\s+[Pp]ublished\s+((?:19|20)\d{2})",         "first-edition"),
    (r"[Pp]ublished\s+in\s+((?:19|20)\d{2})",                             "published-in"),
    (r"(?<!\d)((?:19|20)\d{2})\s+[A-Z][a-z]+\s+[A-Z][a-z]+\.?\s+All\s+[Rr]ights", "all-rights"),
    (r"[Pp]rint(?:ed)?\s+in\s+(?:[A-Z][A-Za-z\s]{2,20},?\s+)?((?:19|20)\d{2})", "printed-in"),
    (r"[Pp]ublication\s+[Dd]ate[:\s]+((?:19|20)\d{2})",                  "pub-date"),
    (r"[Ee]dition\s*[:\-]?\s*((?:19|20)\d{2})",                          "edition"),
    # ── Medium confidence ─────────────────────────────────────────────────────
    (r"ISBN[:\s\-]+[\d\-X]+[,\s]+((?:19|20)\d{2})",                      "isbn-year"),
    (r"[Aa]ll\s+rights\s+reserved[.\s]{0,15}((?:19|20)\d{2})",           "rights-reserved"),
    (r"© ((?:19|20)\d{2})",                                               "copyright-symbol"),
    (r"\(c\)\s*((?:19|20)\d{2})",                                         "copyright-c"),
    (r"[Vv]ersion\s+\d[\d.]*\s*[,\-–]\s*((?:19|20)\d{2})",              "version-year"),
    (r"[Rr]evised?\s+((?:19|20)\d{2})",                                   "revised"),
    (r"[Uu]pdated?\s+((?:19|20)\d{2})",                                   "updated"),
]

# ─── Text extraction via pdftotext ───────────────────────────────────────────

def _pdftotext(path: Path, full=False) -> str:
    try:
        out = subprocess.check_output(
            ["pdftotext", str(path), "-"],
            stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=30
        )
        return out if full else out[:10_000]
    except Exception:
        return ""

# ─── Source 1: text patterns ─────────────────────────────────────────────────

def source_text_pattern(path: Path, min_year: int, max_year: int):
    """
    Source 1: Parses text extracted from the first few pages of the PDF looking for 
    pre-defined text patterns (e.g., "Copyright 2020", "Published in 1998").
    Returns the first year found validated by the min_year and max_year window.
    """
    text = _pdftotext(path)
    if not text:
        return None, None
    
    # TEXT_PATTERNS are ordered by confidence degree (most to least reliable).
    # The first 'match' (greedy behavior of the loop) ensures the best text attempt.
    for pattern, label in TEXT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                y = int(m.group(1))
            except (IndexError, ValueError):
                continue
            if min_year <= y <= max_year:
                return y, label
    return None, None

# ─── Source 2: pdfinfo metadata ──────────────────────────────────────────────

def source_pdfinfo(path: Path, min_year: int, max_year: int):
    """
    Source 2: Reads PDF metadata based on the `pdfinfo` tool.
    Primarily searches for the `CreationDate` line embedded by PDF generators.
    """
    try:
        # A 15-second timeout is a precaution for giant or corrupted files not freezing the main script.
        out = subprocess.check_output(
            ["pdfinfo", str(path)], stderr=subprocess.DEVNULL, text=True, timeout=15
        )
    except Exception:
        return None, None
    
    # Example read output: "CreationDate:    Mon Feb 13 03:43:02 2017 WET"
    # The regex scans the output and groups only the final 4 digits representing the year.
    m = re.search(r"CreationDate:\s+\w+\s+\w+\s+\d+\s+[\d:]+\s+(\d{4})", out)
    if m:
        y = int(m.group(1))
        if min_year <= y <= max_year:
            return y, "pdfinfo:CreationDate"
    return None, None

# ─── Source 3: weighted frequency of years in text ───────────────────────────

def source_frequency(path: Path, min_year: int, max_year: int):
    """
    Source 3: Weighted Frequency Extraction Algorithm
    Evaluates the exhaustive presence of years in the entire document. Years presented
    in the initial pages receive higher multiplier weights reflecting title pages or abstracts.
    """
    text = _pdftotext(path, full=True)
    if not text:
        return None, None

    # Looks only for 4-digit words starting with 19 or 20 isolated by word boundaries (\b).
    matches = list(re.finditer(r"\b((?:19|20)\d{2})\b", text))
    total = max(len(matches), 1)

    scored: dict[int, float] = {}
    for i, m in enumerate(matches):
        y = int(m.group(1))
        if not (min_year <= y <= max_year):
            continue
            
        # Context Evaluation (common noise): If there is an indication of biography ("born in 1990"),
        # we ignore the citation of that year to mitigate false positives.
        start = max(0, m.start() - 30)
        end = min(len(text), m.end() + 10)
        context = text[start:end].lower()
        if any(word in context for word in ["birth", "born", "dob", "age"]):
            continue

        # Positional heuristic weight: 
        # - Extreme beginning (Top 10%) has extreme editorial relevance; max multiplier (3x)
        # - Next 20% (Introduction) medium multiplier (1.5x)
        # - Rest of the body (Methodology/References) standard multiplier (1x)
        ratio = i / total
        weight = 3.0 if ratio < 0.10 else (1.5 if ratio < 0.30 else 1.0)
        scored[y] = scored.get(y, 0) + weight

    if not scored:
        return None, None

    best = max(scored, key=lambda y: scored[y])
    score = scored[best]
    
    # Requires a minimum score ("Threshold") to avoid sparse years in the bibliography
    # of a short text from defining the document's year and generating undue noise.
    if score < 2.0:
        return None, None
    return best, f"freq(score={score:.1f})"

# ─── Source 4: filename ──────────────────────────────────────────────────────

def source_filename(path: Path, min_year: int, max_year: int):
    m = re.search(r"((?:19|20)\d{2})", path.stem)
    if m:
        y = int(m.group(1))
        if min_year <= y <= max_year:
            return y, "filename"
    return None, None

# ─── Source 5: modification date ─────────────────────────────────────────────

def source_mtime(path: Path, min_year: int, max_year: int):
    y = datetime.fromtimestamp(path.stat().st_mtime).year
    if min_year <= y <= max_year:
        return y, "mtime"
    return None, None

# ─── Inference Pipeline ──────────────────────────────────────────────────────

SOURCES = [
    ("text_pattern", source_text_pattern),
    ("pdfinfo",      source_pdfinfo),
    ("frequency",    source_frequency),
    ("filename",     source_filename),
    ("mtime",        source_mtime),
]

PRIORITY = ["text_pattern", "pdfinfo", "frequency", "filename", "mtime"]

def infer_year(path: Path, min_year: int, max_year: int) -> tuple[int | None, str, dict]:
    """
    Central two-stage inference engine.
    Returns the tuple (inferred_year, validating_rule, diagnostic_dictionary).
    
    Hybrid Selection Logic:
    1. Active Consensus: If ≥ 2 of all heuristics agree on the same year, we apply it 
       directly regardless of priority.
    2. Priority Fallback: In a tie-breaker (e.g., 3 different years from 3 sources), the source 
       declared in `PRIORITY` acts as the 'tiebreaker' and more robust option (text_pattern > pdfinfo...).
    """
    results: dict[str, tuple[int, str]] = {}

    # We execute the complete checklist of available data sources in the SOURCES constant
    for name, fn in SOURCES:
        y, label = fn(path, min_year, max_year)
        if y is not None:
            results[name] = (y, label)

    if not results:
        return None, "unknown", results

    # ── Consensus: If ≥ 2 independent heuristics confirm. Incredibly Reliable.
    votes = Counter(y for y, _ in results.values())
    top_year, top_count = votes.most_common(1)[0]
    if top_count >= 2:
        sources_agreed = [results[k][1] for k in results if results[k][0] == top_year]
        return top_year, f"consensus({', '.join(sources_agreed)})", results

    # ── Hierarchical Priority Fallback in case the previous analysis is inconclusive or divergent.
    for p in PRIORITY:
        if p in results:
            y, label = results[p]
            return y, label, results

    return None, "unknown", results

# ─── Output / formatting ─────────────────────────────────────────────────────

def fmt_sources(results: dict) -> str:
    parts = []
    for name, (y, label) in results.items():
        parts.append(f"{DIM}{name}={y}({label}){RST}")
    return "  ".join(parts)

def confidence_badge(source: str) -> str:
    if source.startswith("consensus"):   return f"{GRN}●●● HIGH{RST}"
    if source.startswith("copyright"):   return f"{GRN}●●● HIGH{RST}"
    if source in ("pdfinfo:CreationDate", "first-edition", "published-in",
                  "pub-date", "copyright-symbol", "copyright-c"):
        return f"{GRN}●●  HIGH{RST}"
    if source in ("freq", "frequency") or source.startswith("freq("):
        return f"{YLW}●●  MED {RST}"
    if source == "filename":             return f"{YLW}●   LOW {RST}"
    if source == "mtime":                return f"{RED}●   LOW {RST}"
    return f"{DIM}?   UNKN{RST}"

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="pdf_rename_by_year.py",
        description="Renames PDFs to YYYY_name.pdf inferring the publication year.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("folder", help="Folder containing PDFs to process")
    parser.add_argument("-n", "--dry-run",    action="store_true", help="Simulate without renaming")
    parser.add_argument("-y", "--yes",        action="store_true", help="Do not ask for confirmation")
    parser.add_argument("-v", "--verbose",    action="store_true", help="Show details of each source")
    parser.add_argument("-s", "--skip-dated", action="store_true",
                        help="Skip files that already start with YYYY_")
    parser.add_argument("--log",      metavar="FILE", help="Save report to file")
    parser.add_argument("--min-year", type=int, default=1970,        metavar="YYYY")
    parser.add_argument("--max-year", type=int, default=CURRENT_YEAR, metavar="YYYY")
    args = parser.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"{RED}Error: '{folder}' is not a valid folder.{RST}", file=sys.stderr)
        sys.exit(1)

    # Check dependencies
    for tool in ("pdfinfo", "pdftotext"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            print(f"{RED}Error: '{tool}' not found. Install with:  apt install poppler-utils{RST}",
                  file=sys.stderr)
            sys.exit(1)

    # Collect PDFs
    already_dated_re = re.compile(r"^((?:19|20)\d{2})_")
    pdfs = sorted(
        p for p in folder.glob("*.pdf")
        if not (
            args.skip_dated 
            and already_dated_re.match(p.name) 
            and already_dated_re.match(p.name).group(1) not in p.stem[5:]
        )
    )

    if not pdfs:
        print(f"{YLW}No PDFs found in {folder}{RST}")
        sys.exit(0)

    # ── Analysis ─────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"{BOLD} PDF Year Inferencer  ·  {folder}{RST}")
    print(f"{BOLD}{'─'*72}{RST}\n")
    print(f"  Analyzing {len(pdfs)} file(s)...\n")

    plan: list[tuple[Path, int | None, str, dict]] = []

    for i, pdf in enumerate(pdfs, 1):
        print(f"  [{i}/{len(pdfs)}] {DIM}{pdf.name}{RST}", end="", flush=True)
        year, source, details = infer_year(pdf, args.min_year, args.max_year)
        print(f"\r  [{i}/{len(pdfs)}] {pdf.name:<55} ", end="")
        if year:
            badge = confidence_badge(source)
            print(f"{BOLD}{year}{RST}  {badge}  {DIM}[{source}]{RST}")
        else:
            print(f"{RED}⚠  year not identified{RST}")
        if args.verbose:
            print(f"         {fmt_sources(details)}")
        plan.append((pdf, year, source, details))

    # ── Renaming Plan ────────────────────────────────────────────────────────
    # Intelligent categorization of lists based on the overall evaluation
    # Mappings for failure logs, suppressions, and executions to be made.
    to_rename   = [(p, y, s, d) for p, y, s, d in plan if y]
    no_year     = [(p, y, s, d) for p, y, s, d in plan if not y]
    already_ok  = [p for p, y, s, d in plan
                   if y and p.name.startswith(f"{y}_") and str(y) not in p.stem[5:]]

    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"{BOLD} RENAMING PLAN{RST}\n")

    for pdf, year, source, _ in to_rename:
        # Hygienic Name Normalization Process:
        # 1. Cleans previous dirt/long date prefixes at the beginning of the string (e.g., '20171103_article.pdf').
        # 2. Removes internal duplicative occurrences with ".replace" to avoid something like `2020_project_2020`.
        # 3. Consolidates and cleans resulting parasitic underscores on the edges (___ -> _).
        clean_name = pdf.stem
        if clean_name.startswith(str(year)):
            clean_name = re.sub(rf"^{year}\d*_+", "", clean_name)
        clean_name = clean_name.replace(str(year), "")
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        
        new_name = f"{year}_{clean_name}.pdf"
        
        if pdf.name.startswith(f"{year}_") and str(year) not in pdf.stem[5:]:
            print(f"  {DIM}(already correct) {pdf.name}{RST}")
        else:
            print(f"  {GRN}✎{RST}  {pdf.name}")
            print(f"       → {BOLD}{new_name}{RST}")

    for pdf, _, _, _ in no_year:
        print(f"  {RED}⚠{RST}  {pdf.name}  {DIM}(kept — unknown year){RST}")

    effective = [(p, y, s, d) for p, y, s, d in to_rename
                 if not (p.name.startswith(f"{y}_") and str(y) not in p.stem[5:])]

    print(f"\n  {BOLD}Summary:{RST}  {len(effective)} to rename  ·  "
          f"{len(already_ok)} already correct  ·  {len(no_year)} no year\n")

    if not effective:
        print(f"  {GRN}Nothing to do!{RST}\n")
        _write_log(args.log, folder, plan, effective, no_year, dry_run=args.dry_run)
        sys.exit(0)

    if args.dry_run:
        print(f"  {YLW}[dry-run] No files were changed.{RST}\n")
        _write_log(args.log, folder, plan, effective, no_year, dry_run=True)
        sys.exit(0)

    # ── Confirmation ─────────────────────────────────────────────────────────
    if not args.yes:
        print(f"{BOLD}{'─'*72}{RST}")
        try:
            resp = input(f"  Confirm renaming {len(effective)} file(s)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {YLW}Canceled.{RST}\n")
            sys.exit(0)
        if resp != "y":
            print(f"  {YLW}Canceled.{RST}\n")
            sys.exit(0)

    # ── Execution ────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"{BOLD} RENAMING...{RST}\n")

    renamed, failed = [], []
    for pdf, year, source, details in effective:
        # Applies the very same Hygienic Normalization explained in detail
        # in the initial step (Renaming Plan) at the moment of executing the actual `rename()`
        clean_name = pdf.stem
        if clean_name.startswith(str(year)):
            clean_name = re.sub(rf"^{year}\d*_+", "", clean_name)
        clean_name = clean_name.replace(str(year), "")
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        
        new_name = f"{year}_{clean_name}.pdf"
        new_path = pdf.parent / new_name
        try:
            pdf.rename(new_path)
            print(f"  {GRN}✓{RST}  {pdf.name}  →  {BOLD}{new_path.name}{RST}")
            renamed.append((pdf, new_path, year))
        except OSError as e:
            print(f"  {RED}✗{RST}  {pdf.name}  —  {RED}{e}{RST}")
            failed.append((pdf, str(e)))

    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"  {GRN}✓ {len(renamed)} renamed{RST}", end="")
    if failed:
        print(f"   {RED}✗ {len(failed)} error(s){RST}", end="")
    if no_year:
        print(f"   {YLW}⚠ {len(no_year)} no year{RST}", end="")
    print(f"\n{BOLD}{'─'*72}{RST}\n")

    _write_log(args.log, folder, plan, effective, no_year, dry_run=False,
               renamed=renamed, failed=failed)

# ─── Optional Logging ────────────────────────────────────────────────────────

def _write_log(log_path, folder, plan, effective, no_year,
               dry_run=False, renamed=None, failed=None):
    if not log_path:
        return
    renamed = renamed or []
    failed  = failed  or []
    lines = [
        f"pdf_rename_by_year — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Folder : {folder}",
        f"Total  : {len(plan)} file(s) analyzed",
        f"Mode   : {'DRY-RUN' if dry_run else 'EXECUTION'}",
        "",
        "─" * 60,
        "PLAN",
        "─" * 60,
    ]
    for pdf, year, source, details in plan:
        status = "OK " if year else "???"
        new = f"{year}_{pdf.name}" if year else "(no change)"
        lines.append(f"[{status}] {pdf.name}")
        lines.append(f"       year={year}  source={source}")
        lines.append(f"       → {new}")
        src_str = "  ".join(f"{k}={v[0]}({v[1]})" for k, v in details.items())
        lines.append(f"       sources: {src_str}")
        lines.append("")

    if not dry_run:
        lines += ["─" * 60, "RESULT", "─" * 60]
        for old, new, year in renamed:
            lines.append(f"✓  {old.name}  →  {new.name}")
        for pdf, err in failed:
            lines.append(f"✗  {pdf.name}  —  {err}")
        for pdf, _, _, _ in no_year:
            lines.append(f"⚠  {pdf.name}  (no year — kept)")

    try:
        Path(log_path).write_text("\n".join(lines), encoding="utf-8")
        print(f"  {DIM}Report saved to: {log_path}{RST}")
    except OSError as e:
        print(f"  {RED}Could not save report: {e}{RST}", file=sys.stderr)

# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()

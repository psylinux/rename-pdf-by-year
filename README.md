# Rename PDF by Year

`pdf_rename_by_year.py` is an automated tool that infers the publication year of PDF documents from various sources and renames them to a standardized format: `YYYY_original_name.pdf`.

It uses heuristic analysis—combining textual content patterns, PDF metadata, frequency analysis, and file system attributes—to ensure precise mass renaming of files.

## Features

- **Multi-Source Inference Algorithm:** Inspects documents using five distinct heuristics (ordered by confidence) to accurately determine their year.
- **Consensus System:** Employs an intelligent consensus logic (active agreement by ≥ 2 independent sources) for highly reliable inference.
- **Dry-run Mode:** Allows you to preview changes without physically modifying files.
- **Smart Renaming:** Automatically sanitizes the resulting filename to prevent duplicated years or redundant underscores.
- **Terminal Friendly:** Formats output cleanly for ANSI-compatible terminals, using distinctive color badges to reflect confidence levels.

## Requirements

The script relies on the `poppler-utils` suite to retrieve PDF metadata and extract text efficiently.

### Installation

On Ubuntu/Debian based systems:
```bash
sudo apt install poppler-utils
```

For macOS (using Homebrew):
```bash
brew install poppler
```

You must ensure that both `pdfinfo` and `pdftotext` binaries are available in your system's PATH.

## Usage

```bash
python3 pdf_rename_by_year.py <folder> [options]
```

### Options

| Flag | Name | Description |
| ---- | ---- | ----------- |
| `-n` | `--dry-run` | Simulates the renaming process, showing a plan without altering your files. |
| `-y` | `--yes` | Bypasses the confirmation prompt, executing renaming immediately. |
| `-v` | `--verbose` | Outputs details revealing specifically which inference sources matched for each file. |
| `-s` | `--skip-dated` | Automatically bypasses files that already begin with a valid `YYYY_` prefix. |
| | `--log FILE` | Exports a detailed text report of the operation to `FILE`. |
| | `--min-year YYYY` | Lower bound for an acceptable inferred year (Default: 1970). |
| | `--max-year YYYY` | Upper bound for an acceptable inferred year (Default: current year). |

### Examples

**1. Dry Run / Simulation**
Safely test what the script would do on your directory before applying any changes:
```bash
python3 pdf_rename_by_year.py ./my_books --dry-run --verbose
```

**2. Automated Mass Renaming**
Process your directory automatically, skipping files that already include a date prefix:
```bash
python3 pdf_rename_by_year.py ./articles --skip-dated --yes
```

**3. Advanced Execution with Constraint and Logging**
Apply constraints to only capture dates between 2000 and 2023, while generating an execution log:
```bash
python3 pdf_rename_by_year.py ./docs --min-year 2000 --max-year 2023 --log run_report.txt
```

## How It Works

The tool assesses the original document executing five consecutive heuristics:

1. **High-Confidence Text Patterns:** Extracts the first 10,000 characters and runs them against predefined regex patterns checking publication data, copyright info, text such as "Published in YYYY", explicit edition dates, etc.
2. **Metadata Evaluation:** Retrieves embedded file properties via `pdfinfo`, primarily examining the document's `CreationDate`. 
3. **Text Frequency:** Extracts the whole text of the document and measures the presence of isolated years using an algorithmic weighting system. Mentions close to the beginning of the document receive higher context multipliers.
4. **Filename Fallback:** Scans the original PDF file name to detect matching year patterns in case the document lacks parsable properties.
5. **Modification Date:** Last resort measuring the file system timestamp (`mtime`).

If two or more independent heuristics indicate the exact same year, a **Consensus** is successfully reached, overriding general priority. Without consensus, the order of precedence follows the exact rank previously listed.

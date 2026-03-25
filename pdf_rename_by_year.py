#!/usr/bin/env python3
"""
pdf_rename_by_year.py
─────────────────────
Renomeia PDFs para o formato  YYYY_nome_original.pdf  inferindo o ano de
publicação a partir de múltiplas fontes, em ordem decrescente de confiança:

  1. Padrões textuais de alta confiança  (copyright, "published YYYY", ISBN…)
  2. Metadados PDF                        (pdfinfo CreationDate)
  3. Frequência ponderada de anos         (posição no texto)
  4. Nome do arquivo                      (fallback)
  5. Data de modificação do arquivo       (último recurso)

Consenso: se ≥ 2 fontes independentes concordam, esse ano tem prioridade.

Uso:
  python3 pdf_rename_by_year.py <pasta> [opções]

Opções:
  -n, --dry-run     Mostra o plano sem renomear nada
  -y, --yes         Não pede confirmação (executa direto)
  -v, --verbose     Mostra detalhes de cada fonte usada
  -s, --skip-dated  Pula arquivos que já começam com YYYY_
  --log <arquivo>   Salva relatório em arquivo texto
  --min-year YYYY   Ano mínimo aceito              (padrão: 1970)
  --max-year YYYY   Ano máximo aceito              (padrão: ano atual)
"""

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# ─── Constantes ──────────────────────────────────────────────────────────────

CURRENT_YEAR = datetime.now().year

# Cores ANSI (desativadas se não for terminal)
def _ansi(code): return f"\033[{code}m" if sys.stdout.isatty() else ""
RST  = _ansi("0")
BOLD = _ansi("1")
RED  = _ansi("31")
GRN  = _ansi("32")
YLW  = _ansi("33")
CYN  = _ansi("36")
DIM  = _ansi("2")

# Padrões textuais ordenados por confiança (aplicados nas primeiras ~10 000 chars)
TEXT_PATTERNS = [
    # ── Alta confiança ──────────────────────────────────────────────────────
    (r"[Cc]opyright\s*[©(Cc)]*\s*((?:19|20)\d{2})",                      "copyright"),
    (r"[Ff]irst\s+[Ee]dition\s+[Pp]ublished\s+((?:19|20)\d{2})",         "first-edition"),
    (r"[Pp]ublished\s+in\s+((?:19|20)\d{2})",                             "published-in"),
    (r"(?<!\d)((?:19|20)\d{2})\s+[A-Z][a-z]+\s+[A-Z][a-z]+\.?\s+All\s+[Rr]ights", "all-rights"),
    (r"[Pp]rint(?:ed)?\s+in\s+(?:[A-Z][A-Za-z\s]{2,20},?\s+)?((?:19|20)\d{2})", "printed-in"),
    (r"[Pp]ublication\s+[Dd]ate[:\s]+((?:19|20)\d{2})",                  "pub-date"),
    (r"[Ee]dition\s*[:\-]?\s*((?:19|20)\d{2})",                          "edition"),
    # ── Média confiança ─────────────────────────────────────────────────────
    (r"ISBN[:\s\-]+[\d\-X]+[,\s]+((?:19|20)\d{2})",                      "isbn-year"),
    (r"[Aa]ll\s+rights\s+reserved[.\s]{0,15}((?:19|20)\d{2})",           "rights-reserved"),
    (r"© ((?:19|20)\d{2})",                                               "copyright-symbol"),
    (r"\(c\)\s*((?:19|20)\d{2})",                                         "copyright-c"),
    (r"[Vv]ersion\s+\d[\d.]*\s*[,\-–]\s*((?:19|20)\d{2})",              "version-year"),
    (r"[Rr]evised?\s+((?:19|20)\d{2})",                                   "revised"),
    (r"[Uu]pdated?\s+((?:19|20)\d{2})",                                   "updated"),
]

# ─── Extração de texto via pdftotext ─────────────────────────────────────────

def _pdftotext(path: Path, full=False) -> str:
    try:
        out = subprocess.check_output(
            ["pdftotext", str(path), "-"],
            stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=30
        )
        return out if full else out[:10_000]
    except Exception:
        return ""

# ─── Fonte 1: padrões textuais ───────────────────────────────────────────────

def source_text_pattern(path: Path, min_year: int, max_year: int):
    text = _pdftotext(path)
    if not text:
        return None, None
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

# ─── Fonte 2: metadados pdfinfo ──────────────────────────────────────────────

def source_pdfinfo(path: Path, min_year: int, max_year: int):
    try:
        out = subprocess.check_output(
            ["pdfinfo", str(path)], stderr=subprocess.DEVNULL, text=True, timeout=15
        )
    except Exception:
        return None, None
    # CreationDate:    Mon Feb 13 03:43:02 2017 WET
    m = re.search(r"CreationDate:\s+\w+\s+\w+\s+\d+\s+[\d:]+\s+(\d{4})", out)
    if m:
        y = int(m.group(1))
        if min_year <= y <= max_year:
            return y, "pdfinfo:CreationDate"
    return None, None

# ─── Fonte 3: frequência ponderada de anos no texto ──────────────────────────

def source_frequency(path: Path, min_year: int, max_year: int):
    text = _pdftotext(path, full=True)
    if not text:
        return None, None

    matches = list(re.finditer(r"\b((?:19|20)\d{2})\b", text))
    total = max(len(matches), 1)

    scored: dict[int, float] = {}
    for i, m in enumerate(matches):
        y = int(m.group(1))
        if not (min_year <= y <= max_year):
            continue
            
        # Ignora ruídos comuns (ex: ano de nascimento em perfis/exemplos)
        start = max(0, m.start() - 30)
        end = min(len(text), m.end() + 10)
        context = text[start:end].lower()
        if any(word in context for word in ["birth", "born", "dob", "age"]):
            continue

        # Peso posicional: primeiros 10% do texto valem 3×, próximos 20% valem 1.5×
        ratio = i / total
        weight = 3.0 if ratio < 0.10 else (1.5 if ratio < 0.30 else 1.0)
        scored[y] = scored.get(y, 0) + weight

    if not scored:
        return None, None

    best = max(scored, key=lambda y: scored[y])
    score = scored[best]
    # Exige pontuação mínima para evitar falsos positivos
    if score < 2.0:
        return None, None
    return best, f"freq(score={score:.1f})"

# ─── Fonte 4: nome do arquivo ─────────────────────────────────────────────────

def source_filename(path: Path, min_year: int, max_year: int):
    m = re.search(r"((?:19|20)\d{2})", path.stem)
    if m:
        y = int(m.group(1))
        if min_year <= y <= max_year:
            return y, "filename"
    return None, None

# ─── Fonte 5: data de modificação ────────────────────────────────────────────

def source_mtime(path: Path, min_year: int, max_year: int):
    y = datetime.fromtimestamp(path.stat().st_mtime).year
    if min_year <= y <= max_year:
        return y, "mtime"
    return None, None

# ─── Pipeline de inferência ───────────────────────────────────────────────────

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
    Retorna (ano, descrição_da_fonte, detalhes_de_todas_fontes).
    """
    results: dict[str, tuple[int, str]] = {}

    for name, fn in SOURCES:
        y, label = fn(path, min_year, max_year)
        if y is not None:
            results[name] = (y, label)

    if not results:
        return None, "unknown", results

    # ── Consenso: ≥ 2 fontes independentes concordam ─────────────────────────
    votes = Counter(y for y, _ in results.values())
    top_year, top_count = votes.most_common(1)[0]
    if top_count >= 2:
        sources_agreed = [results[k][1] for k in results if results[k][0] == top_year]
        return top_year, f"consensus({', '.join(sources_agreed)})", results

    # ── Sem consenso: prioridade das fontes ──────────────────────────────────
    for p in PRIORITY:
        if p in results:
            y, label = results[p]
            return y, label, results

    return None, "unknown", results

# ─── Formatação / saída ───────────────────────────────────────────────────────

def fmt_sources(results: dict) -> str:
    parts = []
    for name, (y, label) in results.items():
        parts.append(f"{DIM}{name}={y}({label}){RST}")
    return "  ".join(parts)

def confidence_badge(source: str) -> str:
    if source.startswith("consensus"):    return f"{GRN}●●● ALTO{RST}"
    if source.startswith("copyright"):   return f"{GRN}●●● ALTO{RST}"
    if source in ("pdfinfo:CreationDate", "first-edition", "published-in",
                  "pub-date", "copyright-symbol", "copyright-c"):
        return f"{GRN}●●  ALTO{RST}"
    if source in ("freq", "frequency") or source.startswith("freq("):
        return f"{YLW}●●  MÉDIO{RST}"
    if source == "filename":             return f"{YLW}●   BAIXO{RST}"
    if source == "mtime":                return f"{RED}●   BAIXO{RST}"
    return f"{DIM}?   DESCON{RST}"

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="pdf_rename_by_year.py",
        description="Renomeia PDFs para YYYY_nome.pdf inferindo o ano de publicação.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("folder", help="Pasta com os PDFs a processar")
    parser.add_argument("-n", "--dry-run",    action="store_true", help="Simula sem renomear")
    parser.add_argument("-y", "--yes",        action="store_true", help="Não pede confirmação")
    parser.add_argument("-v", "--verbose",    action="store_true", help="Mostra detalhes de cada fonte")
    parser.add_argument("-s", "--skip-dated", action="store_true",
                        help="Pula arquivos que já começam com YYYY_")
    parser.add_argument("--log",      metavar="ARQUIVO", help="Salva relatório em arquivo")
    parser.add_argument("--min-year", type=int, default=1970,        metavar="YYYY")
    parser.add_argument("--max-year", type=int, default=CURRENT_YEAR, metavar="YYYY")
    args = parser.parse_args()

    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"{RED}Erro: '{folder}' não é uma pasta válida.{RST}", file=sys.stderr)
        sys.exit(1)

    # Verifica dependências
    for tool in ("pdfinfo", "pdftotext"):
        if subprocess.run(["which", tool], capture_output=True).returncode != 0:
            print(f"{RED}Erro: '{tool}' não encontrado. Instale com:  apt install poppler-utils{RST}",
                  file=sys.stderr)
            sys.exit(1)

    # Coleta PDFs
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
        print(f"{YLW}Nenhum PDF encontrado em {folder}{RST}")
        sys.exit(0)

    # ── Análise ───────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"{BOLD} PDF Year Inferencer  ·  {folder}{RST}")
    print(f"{BOLD}{'─'*72}{RST}\n")
    print(f"  Analisando {len(pdfs)} arquivo(s)...\n")

    plan: list[tuple[Path, int | None, str, dict]] = []

    for i, pdf in enumerate(pdfs, 1):
        print(f"  [{i}/{len(pdfs)}] {DIM}{pdf.name}{RST}", end="", flush=True)
        year, source, details = infer_year(pdf, args.min_year, args.max_year)
        print(f"\r  [{i}/{len(pdfs)}] {pdf.name:<55} ", end="")
        if year:
            badge = confidence_badge(source)
            print(f"{BOLD}{year}{RST}  {badge}  {DIM}[{source}]{RST}")
        else:
            print(f"{RED}⚠  ano não identificado{RST}")
        if args.verbose:
            print(f"         {fmt_sources(details)}")
        plan.append((pdf, year, source, details))

    # ── Plano de renomeação ───────────────────────────────────────────────────
    to_rename   = [(p, y, s, d) for p, y, s, d in plan if y]
    no_year     = [(p, y, s, d) for p, y, s, d in plan if not y]
    already_ok  = [p for p, y, s, d in plan
                   if y and p.name.startswith(f"{y}_") and str(y) not in p.stem[5:]]

    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"{BOLD} PLANO DE RENOMEAÇÃO{RST}\n")

    for pdf, year, source, _ in to_rename:
        # Limpa prefixos de data longa (ex. 20171103_) e depois remove o ano de todo o nome
        clean_name = pdf.stem
        if clean_name.startswith(str(year)):
            clean_name = re.sub(rf"^{year}\d*_+", "", clean_name)
        clean_name = clean_name.replace(str(year), "")
        clean_name = re.sub(r"_+", "_", clean_name).strip("_")
        
        new_name = f"{year}_{clean_name}.pdf"
        
        if pdf.name.startswith(f"{year}_") and str(year) not in pdf.stem[5:]:
            print(f"  {DIM}(já correto) {pdf.name}{RST}")
        else:
            print(f"  {GRN}✎{RST}  {pdf.name}")
            print(f"       → {BOLD}{new_name}{RST}")

    for pdf, _, _, _ in no_year:
        print(f"  {RED}⚠{RST}  {pdf.name}  {DIM}(mantido — ano desconhecido){RST}")

    effective = [(p, y, s, d) for p, y, s, d in to_rename
                 if not (p.name.startswith(f"{y}_") and str(y) not in p.stem[5:])]

    print(f"\n  {BOLD}Resumo:{RST}  {len(effective)} a renomear  ·  "
          f"{len(already_ok)} já correto(s)  ·  {len(no_year)} sem ano\n")

    if not effective:
        print(f"  {GRN}Nada a fazer!{RST}\n")
        _write_log(args.log, folder, plan, effective, no_year, dry_run=args.dry_run)
        sys.exit(0)

    if args.dry_run:
        print(f"  {YLW}[dry-run] Nenhum arquivo foi alterado.{RST}\n")
        _write_log(args.log, folder, plan, effective, no_year, dry_run=True)
        sys.exit(0)

    # ── Confirmação ───────────────────────────────────────────────────────────
    if not args.yes:
        print(f"{BOLD}{'─'*72}{RST}")
        try:
            resp = input(f"  Confirmar renomeação de {len(effective)} arquivo(s)? [s/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {YLW}Cancelado.{RST}\n")
            sys.exit(0)
        if resp != "s":
            print(f"  {YLW}Cancelado.{RST}\n")
            sys.exit(0)

    # ── Execução ──────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*72}{RST}")
    print(f"{BOLD} RENOMEANDO...{RST}\n")

    renamed, failed = [], []
    for pdf, year, source, details in effective:
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
    print(f"  {GRN}✓ {len(renamed)} renomeado(s){RST}", end="")
    if failed:
        print(f"   {RED}✗ {len(failed)} erro(s){RST}", end="")
    if no_year:
        print(f"   {YLW}⚠ {len(no_year)} sem ano{RST}", end="")
    print(f"\n{BOLD}{'─'*72}{RST}\n")

    _write_log(args.log, folder, plan, effective, no_year, dry_run=False,
               renamed=renamed, failed=failed)

# ─── Log opcional ─────────────────────────────────────────────────────────────

def _write_log(log_path, folder, plan, effective, no_year,
               dry_run=False, renamed=None, failed=None):
    if not log_path:
        return
    renamed = renamed or []
    failed  = failed  or []
    lines = [
        f"pdf_rename_by_year — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Pasta : {folder}",
        f"Total : {len(plan)} arquivo(s) analisado(s)",
        f"Modo  : {'DRY-RUN (simulação)' if dry_run else 'EXECUÇÃO'}",
        "",
        "─" * 60,
        "PLANO",
        "─" * 60,
    ]
    for pdf, year, source, details in plan:
        status = "OK " if year else "???"
        new = f"{year}_{pdf.name}" if year else "(sem alteração)"
        lines.append(f"[{status}] {pdf.name}")
        lines.append(f"       ano={year}  fonte={source}")
        lines.append(f"       → {new}")
        src_str = "  ".join(f"{k}={v[0]}({v[1]})" for k, v in details.items())
        lines.append(f"       fontes: {src_str}")
        lines.append("")

    if not dry_run:
        lines += ["─" * 60, "RESULTADO", "─" * 60]
        for old, new, year in renamed:
            lines.append(f"✓  {old.name}  →  {new.name}")
        for pdf, err in failed:
            lines.append(f"✗  {pdf.name}  —  {err}")
        for pdf, _, _, _ in no_year:
            lines.append(f"⚠  {pdf.name}  (sem ano — mantido)")

    try:
        Path(log_path).write_text("\n".join(lines), encoding="utf-8")
        print(f"  {DIM}Relatório salvo em: {log_path}{RST}")
    except OSError as e:
        print(f"  {RED}Não foi possível salvar o relatório: {e}{RST}", file=sys.stderr)

# ─── Entrada ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()

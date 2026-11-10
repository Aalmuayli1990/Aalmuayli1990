# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.2,<2",
#   "curl_cffi>=0.7",
#   "beautifulsoup4>=4.12",
#   "lxml>=5.0",
# ]
# ///
"""tasi-filings: stdio MCP server for Saudi Exchange (Tadawul) financial statements.

EDGAR-shaped workflow: search -> download PDF -> extract -> get_table.

Hard rules
- Statement tables come only from extracted PDFs (Extracted/*_tables.json).
  Never use Tadawul company-profile summary pages as a statement source.
- PDF downloads go through Safari + AppleScript (tadawul_pdf.py); direct HTTP is often 403.
- Extraction goes through local pdf_read.py (Markdown + JSON; optional Google Sheets).
- Tools return JSON strings (or plain error text); they do not raise into the host.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cf
from mcp.server.fastmcp import FastMCP

BASE = "https://www.saudiexchange.sa"
HOME = f"{BASE}/wps/portal/saudiexchange"

_CACHE_ROOT = Path.home() / ".cache" / "tasi-filings"
_LIST_TTL = 60 * 60
_TEMPLATE_TTL = 24 * 3600
_MIN_GAP = 0.4  # seconds between exchange HTTP requests

# Sibling tools by default (../pdf-read, ../tadawul-pdf). Override with env vars.
_SIBLINGS = Path(__file__).resolve().parent.parent
_PDF_READ = Path(os.environ.get("PDF_READ_PATH", _SIBLINGS / "pdf-read" / "pdf_read.py"))
_TADAWUL = Path(os.environ.get("TADAWUL_PDF_PATH", _SIBLINGS / "tadawul-pdf" / "tadawul_pdf.py"))

_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_FORBIDDEN_MARKERS = ("lost_and_found", "DriveFS", os.path.join("Application Support", "Google"),
                      "OneDrive-Personal", "OneDrive-شخصي")

mcp = FastMCP("tasi-filings")
_session: cf.Session | None = None
_rate_lock = asyncio.Lock()
_last_request = 0.0
_WS = re.compile(r"\s+")
_WATCH_HREF = re.compile(
    r'href="(/wps/portal/saudiexchange/ourmarkets/(?:main-market-watch|nomuc-market-watch)/!ut/p/[^"?#]+)'
)
_JS_COMPANY = re.compile(
    r'company:\s*"(?P<symbol>\d+)"\s*,\s*companyDisplay:\s*"(?P<name>[^"]+)"', re.I
)

STATEMENT_QUERY = {
    "income": ["income statement", "statement of income", "profit or loss",
               "comprehensive income", "قائمة الدخل"],
    "balance": ["balance sheet", "financial position", "statement of financial position",
                "قائمة المركز"],
    "cashflow": ["cash flow", "cash flows", "قائمة التدفقات"],
    "equity": ["changes in equity", "statement of changes", "التغيرات في حقوق"],
}


# ---------------------------------------------------------------- paths & guards

def guard_path(path: Path | str, *, for_write: bool = False) -> Path:
    """Reject DriveFS / lost_and_found / OneDrive paths and Drive mirrors without My Drive."""
    p = Path(path).expanduser()
    try:
        s = str(p.resolve())
    except OSError:
        s = str(p)
    for bad in _FORBIDDEN_MARKERS:
        if bad in s:
            raise PermissionError(f"Blocked path (not the Google Drive My Drive mirror): {p}")
    if "CloudStorage" in s and "GoogleDrive-" in s and "My Drive" not in s:
        raise PermissionError(f"Blocked path (missing My Drive): {p}")
    return p


def investment_root() -> Path:
    raw = (os.environ.get("INVESTMENT_ROOT") or os.environ.get("GOOGLEDRIVE_ROOT") or "").strip()
    if not raw:
        raise FileNotFoundError("Set INVESTMENT_ROOT to .../My Drive/<folder>/investment")
    p = guard_path(raw)
    if not p.is_dir():
        raise FileNotFoundError(f"INVESTMENT_ROOT is not a directory: {p}")
    return p


def stocks_root() -> Path:
    raw = os.environ.get("SAUDI_STOCKS_ROOT", "").strip()
    if raw:
        p = guard_path(raw, for_write=True)
    else:
        p = guard_path(investment_root() / "Saudi stocks", for_write=True)
    return p


def _company_dir(symbol: str) -> Path:
    return stocks_root() / _norm_symbol(symbol)


def _sheets_registry() -> Path:
    return Path.home() / ".config" / "investment" / "Sheets" / "registry.json"


def _no_sheets() -> bool:
    return os.environ.get("INVESTMENT_NO_SHEETS", "1").strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------- helpers

def _clean(text: str) -> str:
    return _WS.sub(" ", (text or "").replace("\xa0", " ")).strip()


def _cache_path(kind: str, key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()[:32]
    p = _CACHE_ROOT / kind / f"{h}.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cache_read(kind: str, key: str, ttl: int) -> bytes | None:
    p = _cache_path(kind, key)
    if not p.exists() or time.time() - p.stat().st_mtime > ttl:
        return None
    return p.read_bytes()


def _cache_write(kind: str, key: str, data: bytes) -> None:
    _cache_path(kind, key).write_bytes(data)


def _client() -> cf.Session:
    global _session
    if _session is None:
        _session = cf.Session(impersonate="chrome")
        _session.headers.update({
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
    return _session


def _get_sync(url: str) -> str:
    r = _client().get(url, timeout=45, allow_redirects=True)
    if r.status_code != 200:
        raise ValueError(f"Saudi Exchange HTTP {r.status_code} for {url}")
    return r.text


async def _get(url: str, *, cache_kind: str | None = None, ttl: int = 0) -> str:
    if cache_kind:
        cached = _cache_read(cache_kind, url, ttl)
        if cached is not None:
            return cached.decode("utf-8", errors="replace")
    global _last_request
    async with _rate_lock:
        wait = _MIN_GAP - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()
    html = await asyncio.to_thread(_get_sync, url)
    if cache_kind:
        _cache_write(cache_kind, url, html.encode("utf-8", errors="replace"))
    return html


def _abs(href: str) -> str:
    return urljoin(BASE + "/", href) if href else href


def _with_locale(url: str, locale: str) -> str:
    url = re.sub(r"[?&]locale=\w+", "", url).rstrip("&?")
    return url + ("&" if "?" in url else "?") + f"locale={locale}"


def _norm_symbol(symbol: str) -> str:
    s = symbol.strip().upper().translate(_AR_DIGITS)
    m = re.search(r"(\d{4})", s)
    if not m:
        raise ValueError(f"Expected a 4-digit Saudi listed symbol, got '{symbol}'.")
    return m.group(1)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_tadawul():
    if not _TADAWUL.is_file():
        raise ValueError(f"tadawul_pdf.py not found at {_TADAWUL} (set TADAWUL_PDF_PATH)")
    return _load_module("tadawul_pdf", _TADAWUL)


def _parse_kinds(kinds: str) -> set[str]:
    out = set()
    for k in (kinds or "fy,q1,q2,q3,q4").split(","):
        k = k.strip().lower()
        if k in {"fy", "q1", "q2", "q3", "q4"}:
            out.add("FY" if k == "fy" else k.upper())
        elif k:
            out.add(k)
    return out or {"FY", "Q1", "Q2", "Q3", "Q4"}


def _parse_period(period: str) -> dict[str, str]:
    s = (period or "").strip().upper().translate(_AR_DIGITS)
    year, kind = "", ""
    m = re.search(r"(20\d{2})", s)
    if m:
        year = m.group(1)
    if re.search(r"\bFY\b|سنوي|ANNUAL", s):
        kind = "FY"
    else:
        qm = re.search(r"\bQ([1-4])\b", s)
        if qm:
            kind = f"Q{qm.group(1)}"
    return {"year": year, "kind": kind}


def _period_of_name(name: str) -> dict[str, str]:
    m = re.search(r"_((?:20\d{2}))_(FY|Q[1-4])", name.upper())
    if m:
        return {"year": m.group(1), "kind": m.group(2)}
    return {"year": "", "kind": ""}


def _sheet_url_for_stem(stem: str) -> str:
    reg = _sheets_registry()
    if not reg.is_file():
        return ""
    try:
        data = json.loads(reg.read_text(encoding="utf-8"))
        wb = (data.get("workbooks") or {}).get(f"pdf:{stem}_tables") or {}
        return wb.get("url") or ""
    except Exception:
        return ""


def _local_pdfs(symbol: str) -> list[dict[str, Any]]:
    folder = _company_dir(symbol)
    if not folder.is_dir():
        return []
    rows = []
    for p in sorted(folder.glob("*.pdf")):
        per = _period_of_name(p.name)
        extracted = folder / "Extracted"
        stem = p.stem
        notes = extracted / f"{stem}_notes.md"
        tables = extracted / f"{stem}_tables.json"
        xlsx = extracted / f"{stem}_tables.xlsx"
        rows.append({
            "file": p.name,
            "path": str(p),
            "year": per["year"],
            "kind": per["kind"],
            "bytes": p.stat().st_size,
            "extracted": {
                "notes": str(notes) if notes.exists() else "",
                "tables_json": str(tables) if tables.exists() else "",
                "tables_xlsx": str(xlsx) if xlsx.exists() else "",
                "google_sheet": _sheet_url_for_stem(stem) if tables.exists() else "",
            },
        })
    return rows


def _safari_list(symbol: str, lang: str, years: str, kinds: str) -> dict[str, Any]:
    tad = _load_tadawul()
    kinds_set = _parse_kinds(kinds)
    year_range = tad.parse_years(years) if years else None
    sf = tad.Safari()
    try:
        sym, items = tad.fetch_listing(sf, symbol, lang)
        items = tad.filter_items(items, kinds_set, {"pdf"}, year_range, False)
        tad.head_sizes(sf, items)
        items = [i for i in items if not (0 < i.get("bytes", 0) < 1000)]
        return {"symbol": sym, "items": items}
    finally:
        sf.close()


def _safari_download(symbol: str, lang: str, years: str, kinds: str) -> dict[str, Any]:
    tad = _load_tadawul()
    kinds_set = _parse_kinds(kinds)
    year_range = tad.parse_years(years) if years else None
    out = guard_path(_company_dir(symbol), for_write=True)
    sf = tad.Safari()
    try:
        sym, items = tad.fetch_listing(sf, symbol, lang)
        items = tad.filter_items(items, kinds_set, {"pdf"}, year_range, False)
        tad.head_sizes(sf, items)
        items = [i for i in items if not (0 < i.get("bytes", 0) < 1000)]
        if not items:
            return {"symbol": sym, "folder": str(out), "files": [],
                    "note": "No matching PDFs on Saudi Exchange."}
        return tad.download_selected(sf, sym, items, out)
    finally:
        sf.close()


def _extract_pdf(pdf: Path, *, push_sheets: bool = False) -> dict[str, str]:
    if not _PDF_READ.is_file():
        raise ValueError(f"pdf_read.py not found at {_PDF_READ} (set PDF_READ_PATH)")
    guard_path(pdf, for_write=True)
    outdir = pdf.parent / "Extracted"
    cmd = ["uv", "run", str(_PDF_READ), str(pdf), "-o", str(outdir)]
    if not push_sheets:
        cmd.append("--no-sheets")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise ValueError((r.stderr or r.stdout or "pdf-read failed")[-2000:])
    stem = pdf.stem
    sheet_url = ""
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("-> ") and "docs.google.com/spreadsheets" in line:
            sheet_url = line.removeprefix("-> ").strip()
            break
    return {
        "pdf": str(pdf),
        "notes": str(outdir / f"{stem}_notes.md"),
        "tables_json": str(outdir / f"{stem}_tables.json"),
        "tables_xlsx": str(outdir / f"{stem}_tables.xlsx")
        if (outdir / f"{stem}_tables.xlsx").is_file() else "",
        "google_sheet": sheet_url,
        "sheets_registry": str(_sheets_registry()),
        "storage_root": str(stocks_root()),
        "log": (r.stdout or "")[-1500:],
    }


def _match_period(file_period: dict[str, str], want: dict[str, str]) -> bool:
    if want.get("year") and file_period.get("year") != want["year"]:
        return False
    if want.get("kind") and file_period.get("kind") != want["kind"]:
        return False
    return True


def _compact_table(tbl: dict[str, Any], max_rows: int = 80) -> dict[str, Any]:
    rows = []
    for row in tbl.get("rows") or []:
        label = row.get("label") or ""
        vals = row.get("values") or []
        if not label and not any(vals):
            continue
        rows.append({"label": label, "note": row.get("note"), "values": vals})
        if len(rows) >= max_rows:
            break
    return {
        "sheet": tbl.get("sheet"),
        "page": tbl.get("page"),
        "title": tbl.get("title") or "",
        "columns": tbl.get("columns") or [],
        "source": tbl.get("source") or "",
        "rows": rows,
    }


def _score_table(tbl: dict[str, Any], needles: list[str], kind: str = "") -> int:
    sheet = str(tbl.get("sheet") or "").lower()
    blob = " ".join([
        sheet,
        str(tbl.get("title") or ""),
        " ".join(str(c) for c in (tbl.get("columns") or [])[:4]),
        " ".join(str(r.get("label") or "") for r in (tbl.get("rows") or [])[:8]),
    ]).lower()
    score = sum(1 for n in needles if n in blob)
    if kind == "income" and sheet.startswith("income statement"):
        score += 5
    elif kind == "balance" and "balance sheet" in sheet:
        score += 5
    elif kind == "cashflow" and "cash flow" in sheet:
        score += 5
    elif kind == "equity" and "equity" in sheet:
        score += 5
    if "comprehensive income" in blob and kind == "income":
        score -= 1
    return score


def _iter_extracted(symbol: str, period: str = "") -> list[dict[str, Any]]:
    want = _parse_period(period)
    hits = []
    for rec in _local_pdfs(symbol):
        if not _match_period({"year": rec["year"], "kind": rec["kind"]}, want):
            continue
        path = rec["extracted"].get("tables_json")
        if not path:
            continue
        tables = json.loads(Path(path).read_text(encoding="utf-8"))
        hits.append({**rec, "tables": tables, "notes": rec["extracted"].get("notes") or ""})
    return hits


def statement_table(symbol: str, statement: str = "income", period: str = "") -> dict[str, Any]:
    """Pure get_table logic over extracted JSON only (used by the MCP tool and the smoke test)."""
    sym = _norm_symbol(symbol)
    recs = _iter_extracted(sym, period)
    if not recs:
        return {"error": f"No extracted tables for {sym} {period}. Run ingest_filings first "
                         "(website profile tables are summaries only)."}
    recs.sort(key=lambda r: (r["year"], r["kind"]), reverse=True)
    if not period:
        recs = recs[:1]
    key = statement.strip().lower()
    needles = STATEMENT_QUERY.get(key, [key])
    hits = []
    for rec in recs:
        scored = []
        for tbl in rec["tables"]:
            sc = _score_table(tbl, needles, key)
            if sc:
                scored.append((sc, tbl))
        scored.sort(key=lambda x: -x[0])
        for sc, tbl in scored[:4]:
            hits.append({
                "filing": rec["file"],
                "year": rec["year"],
                "kind": rec["kind"],
                "notes": rec.get("notes") or "",
                "score": sc,
                "table": _compact_table(tbl),
            })
    if not hits:
        return {
            "symbol": sym,
            "query": statement,
            "note": "No table matched. Call list_extracted_tables and pass a sheet name fragment.",
            "filings": [r["file"] for r in recs],
        }
    return {"symbol": sym, "query": statement, "source": "extracted PDF tables", "matches": hits}


async def _watch_tickers(locale: str) -> list[dict[str, str]]:
    key = f"tickers:{locale}"
    cached = _cache_read("lists", key, _LIST_TTL)
    if cached:
        return json.loads(cached)
    home = await _get(_with_locale(HOME, locale), cache_kind="home", ttl=_TEMPLATE_TTL)
    hrefs: list[str] = []
    for m in _WATCH_HREF.finditer(home):
        if m.group(1) not in hrefs:
            hrefs.append(m.group(1))
    if not hrefs:
        hrefs = [
            "/wps/portal/saudiexchange/ourmarkets/main-market-watch",
            "/wps/portal/saudiexchange/ourmarkets/nomuc-market-watch",
        ]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in hrefs:
        html = await _get(_with_locale(_abs(path), locale), cache_kind="watch", ttl=_LIST_TTL)
        market = "Nomu" if "nomuc" in path else "Main"
        for m in _JS_COMPANY.finditer(html):
            if m.group("symbol") in seen:
                continue
            seen.add(m.group("symbol"))
            rows.append({"symbol": m.group("symbol"), "name": m.group("name"), "market": market})
        soup = BeautifulSoup(html, "lxml")
        for table in soup.find_all("table", id=re.compile(r"marketWatchTable")):
            for tr in table.find_all("tr"):
                a = tr.find("a", href=re.compile(r"companySymbol="))
                if not a:
                    continue
                sm = re.search(r"companySymbol=(\d+)", a.get("href") or "")
                if not sm or sm.group(1) in seen:
                    continue
                seen.add(sm.group(1))
                rows.append({"symbol": sm.group(1), "name": _clean(a.get_text(" ", strip=True)),
                             "market": market})
    _cache_write("lists", key, json.dumps(rows, ensure_ascii=False).encode())
    return rows


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- MCP tools

@mcp.tool()
async def search_company(query: str, limit: int = 10) -> str:
    """Find a Saudi Exchange listed company by 4-digit symbol or English name fragment.

    Uses the exchange's public market-watch lists over HTTP (no Safari needed).
    Never use the profile summary page figures as financial statements.
    """
    try:
        q = query.strip().lower().translate(_AR_DIGITS)
        if not q:
            return "Provide a 4-digit symbol or a company name fragment."
        rows = await _watch_tickers("en")
        exact = [r for r in rows if r["symbol"] == q or r["name"].lower() == q]
        part = [r for r in rows if q in r["symbol"] or q in r["name"].lower()]
        seen, out = set(), []
        for r in exact + part:
            if r["symbol"] in seen:
                continue
            seen.add(r["symbol"])
            out.append(r)
            if len(out) >= limit:
                break
        if not out and q.isdigit() and len(q) == 4:
            out = [{"symbol": q, "name": "", "market": "",
                    "note": "Not in watch list cache; use the 4-digit symbol directly."}]
        if not out:
            return f"No Saudi Exchange listing matched '{query}'."
        return _dumps(out)
    except Exception as e:
        return f"search_company failed: {e}"


@mcp.tool()
async def list_local_filings(symbol: str) -> str:
    """List financial-statement PDFs already on disk for a symbol, and whether Markdown/JSON extraction exists.

    No Safari needed. Call this before ingesting to see what is already downloaded.
    """
    try:
        sym = _norm_symbol(symbol)
        files = _local_pdfs(sym)
        meta = {"storage_root": str(stocks_root()), "sheets_registry": str(_sheets_registry())}
        folder = str(_company_dir(sym))
        if not files:
            return _dumps({"symbol": sym, "folder": folder, "files": [],
                           "note": "No PDFs yet. Call ingest_filings.", **meta})
        return _dumps({"symbol": sym, "folder": folder, "files": files, **meta})
    except Exception as e:
        return f"list_local_filings failed: {e}"


@mcp.tool()
async def list_filings(symbol: str, years: str = "", kinds: str = "fy,q1,q2,q3,q4") -> str:
    """List the company's financial-statement PDF links on the exchange (does NOT download).

    Requires Safari: Develop > Allow JavaScript from Apple Events. Never use profile summary tables as statements.
    """
    try:
        sym = _norm_symbol(symbol)
        data = await asyncio.to_thread(_safari_list, sym, "en", years, kinds)
        slim = [{"year": i["year"], "kind": i["kind"], "lang": i["lang"], "filed": i["filed"],
                 "bytes": i.get("bytes", 0), "href": i["href"]} for i in data["items"]]
        return _dumps({"symbol": data["symbol"], "reports": slim})
    except SystemExit as e:
        return f"list_filings failed: {e}"
    except Exception as e:
        return (f"list_filings failed: {e}. Enable Safari JavaScript from Apple Events "
                "if the window did not load.")


@mcp.tool()
async def ingest_filings(symbol: str, years: str = "", kinds: str = "fy,q1,q2,q3,q4",
                         extract: bool = True, sheets: bool = False) -> str:
    """Download financial-statement PDFs from Saudi Exchange via Safari, then extract Markdown/JSON.

    Requires Safari: Develop > Allow JavaScript from Apple Events, and allow downloads from saudiexchange.sa.
    Files land in {Saudi stocks}/<symbol>/ as <symbol>_<year>_<FY|Q1|Q2|Q3>.pdf; existing PDFs are skipped.
    Default sheets=false so batch runs do not block on the Google Sheets API.
    Statements come only from the extracted tables; never use website profile summaries.
    """
    try:
        sym = _norm_symbol(symbol)
        downloaded = await asyncio.to_thread(_safari_download, sym, "en", years, kinds)
        extracted, errors = [], []
        if extract:
            pdfs = [Path(rec["path"]) for rec in downloaded.get("files") or []
                    if Path(rec["path"]).exists()]
            if not pdfs:
                pdfs = [Path(r["path"]) for r in _local_pdfs(sym)]
            push = sheets and not _no_sheets()
            for pdf in pdfs:
                notes = pdf.parent / "Extracted" / f"{pdf.stem}_notes.md"
                tables = pdf.parent / "Extracted" / f"{pdf.stem}_tables.json"
                if notes.exists() and tables.exists():
                    extracted.append({"pdf": pdf.name, "status": "exists",
                                      "notes": str(notes), "tables_json": str(tables)})
                    continue
                try:
                    res = await asyncio.to_thread(_extract_pdf, pdf, push_sheets=push)
                    extracted.append({"status": "extracted", **res})
                except Exception as e:
                    errors.append({"pdf": pdf.name, "error": str(e)[:500]})
        return _dumps({
            "symbol": downloaded.get("symbol") or sym,
            "folder": downloaded.get("folder") or str(_company_dir(sym)),
            "download": {
                "new": [x["file"] for x in downloaded.get("downloaded") or []],
                "already_on_disk": [x["file"] for x in downloaded.get("skipped") or []],
            },
            "extracted": extracted,
            "errors": errors,
            "next": "Call get_table or search_notes. Do not use company-profile summary figures.",
        })
    except SystemExit as e:
        return f"ingest_filings failed: {e}"
    except Exception as e:
        return (f"ingest_filings failed: {e}. Enable Safari JavaScript from Apple Events "
                "if the download did not start.")


@mcp.tool()
async def extract_filings(symbol: str, period: str = "", sheets: bool = False) -> str:
    """Run local pdf_read on PDFs that are on disk but not yet extracted (Markdown + JSON; optional Sheets).

    No Safari needed. Default sheets=false. Use period like '2026 Q3' or '2025 FY'; empty = all.
    """
    try:
        sym = _norm_symbol(symbol)
        want = _parse_period(period)
        done, errors = [], []
        push = sheets and not _no_sheets()
        for rec in _local_pdfs(sym):
            if not _match_period({"year": rec["year"], "kind": rec["kind"]}, want):
                continue
            if rec["extracted"]["notes"] and rec["extracted"]["tables_json"]:
                done.append({"pdf": rec["file"], "status": "exists", **rec["extracted"]})
                continue
            try:
                res = await asyncio.to_thread(_extract_pdf, Path(rec["path"]), push_sheets=push)
                done.append({"status": "extracted", **res})
            except Exception as e:
                errors.append({"pdf": rec["file"], "error": str(e)[:500]})
        if not done and not errors:
            return (f"No PDFs matched symbol {sym} period '{period}'. "
                    "Download first with ingest_filings.")
        return _dumps({"symbol": sym, "extracted": done, "errors": errors})
    except Exception as e:
        return f"extract_filings failed: {e}"


@mcp.tool()
async def list_extracted_tables(symbol: str, period: str = "") -> str:
    """Index of extracted tables (sheet, page, title, columns) from pdf_read JSON for a symbol and period."""
    try:
        sym = _norm_symbol(symbol)
        index = []
        for rec in _iter_extracted(sym, period):
            for tbl in rec["tables"]:
                index.append({
                    "filing": rec["file"],
                    "year": rec["year"],
                    "kind": rec["kind"],
                    "sheet": tbl.get("sheet"),
                    "page": tbl.get("page"),
                    "title": tbl.get("title") or "",
                    "columns": (tbl.get("columns") or [])[:6],
                    "n_rows": len(tbl.get("rows") or []),
                })
        if not index:
            return f"No extracted tables for {sym} {period}. Run ingest_filings or extract_filings first."
        return _dumps({"symbol": sym, "period": period, "tables": index})
    except Exception as e:
        return f"list_extracted_tables failed: {e}"


@mcp.tool()
async def get_table(symbol: str, statement: str = "income", period: str = "") -> str:
    """Return the full statement table from extracted PDF JSON, never the website summary.

    Parameters:
      symbol: 4-digit Saudi symbol, e.g. 1150.
      statement: income | balance | cashflow | equity, or a free-text query (e.g. 'financing, net').
      period: e.g. '2026 Q3' or '2025 FY'. Empty = latest extracted filing.
    Requires prior ingest_filings or extract_filings. Does not need Safari.
    """
    try:
        return _dumps(statement_table(symbol, statement, period))
    except Exception as e:
        return f"get_table failed: {e}"


@mcp.tool()
async def get_statement_table(symbol: str, statement: str = "income", period: str = "") -> str:
    """Alias of get_table. Same extracted-PDF statement table (income | balance | cashflow | equity or free text)."""
    return await get_table(symbol, statement, period)


@mcp.tool()
async def get_filing_text(symbol: str, period: str = "", offset: int = 0, max_chars: int = 30000) -> str:
    """Read the extracted Markdown notes for a filing, in pages (offset / max_chars). No Safari needed."""
    try:
        sym = _norm_symbol(symbol)
        want = _parse_period(period)
        recs = [r for r in _local_pdfs(sym) if _match_period({"year": r["year"], "kind": r["kind"]}, want)]
        recs = [r for r in recs if r["extracted"].get("notes")]
        recs.sort(key=lambda r: (r["year"], r["kind"]), reverse=True)
        if not recs:
            return f"No extracted notes for {sym} {period}. Run extract_filings first."
        rec = recs[0]
        text = Path(rec["extracted"]["notes"]).read_text(encoding="utf-8", errors="replace")
        start = max(0, offset)
        chunk = text[start:start + max_chars]
        nxt = start + len(chunk)
        return _dumps({
            "symbol": sym,
            "filing": rec["file"],
            "notes": rec["extracted"]["notes"],
            "total_chars": len(text),
            "offset": start,
            "next_offset": nxt if nxt < len(text) else None,
            "text": chunk,
        })
    except Exception as e:
        return f"get_filing_text failed: {e}"


@mcp.tool()
async def search_notes(symbol: str, query: str, period: str = "", max_hits: int = 8) -> str:
    """Search the extracted Markdown notes for a phrase and return surrounding text, including Markdown tables.

    No Safari needed. Example queries: 'expected credit loss', 'segment'.
    """
    try:
        sym = _norm_symbol(symbol)
        q = query.strip()
        if not q:
            return "Provide a search query, e.g. 'expected credit loss' or 'segment'."
        want = _parse_period(period)
        hits = []
        for rec in _local_pdfs(sym):
            if not _match_period({"year": rec["year"], "kind": rec["kind"]}, want):
                continue
            notes = rec["extracted"].get("notes")
            if not notes:
                continue
            text = Path(notes).read_text(encoding="utf-8", errors="replace")
            low, needle = text.lower(), q.lower()
            start = 0
            while len(hits) < max_hits:
                i = low.find(needle, start)
                if i < 0:
                    break
                a = max(0, i - 400)
                b = min(len(text), i + len(q) + 700)
                heading = ""
                for line in reversed(text[:i].splitlines()[-30:]):
                    if line.startswith("#"):
                        heading = line.strip()
                        break
                hits.append({"filing": rec["file"], "notes": notes, "heading": heading,
                             "excerpt": text[a:b].strip()})
                start = i + len(q)
        if not hits:
            return f"No match for '{query}' in extracted notes for {sym} {period}."
        return _dumps({"symbol": sym, "query": q, "hits": hits})
    except Exception as e:
        return f"search_notes failed: {e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

# tasi-filings

stdio MCP server for Saudi Exchange (Tadawul) financial statements.
EDGAR-shaped workflow: **search → download PDF → extract → get_table**.

## Hard rules

- Statements come **only** from extracted PDFs (`Extracted/*_tables.json`, `*_notes.md`).
  Tadawul company-profile summary pages are never a statement source.
- PDFs are downloaded through Safari + AppleScript (`tadawul_pdf.py`); direct HTTP is often 403.
- Extraction uses local `pdf_read.py` (Markdown + JSON; Google Sheets optional, off by default).
- Tools return JSON strings (or plain error text), never raised errors.

## Tools (10)

| Tool | Needs Safari |
|---|---|
| `search_company` | no |
| `list_filings` | yes |
| `list_local_filings` | no |
| `ingest_filings` | yes |
| `extract_filings` | no |
| `list_extracted_tables` | no |
| `get_table` / `get_statement_table` (alias) | no |
| `get_filing_text` | no |
| `search_notes` | no |

`statement`: `income | balance | cashflow | equity`, or free text (e.g. `financing, net`).
`period`: `2026 Q3`, `2025 FY`; empty = latest extracted.

## On-disk layout

```
{Saudi stocks}/{symbol}/{symbol}_{year}_{FY|Q1|Q2|Q3}.pdf
{Saudi stocks}/{symbol}/Extracted/{stem}_notes.md
{Saudi stocks}/{symbol}/Extracted/{stem}_tables.json
```

## Environment

| Variable | Purpose |
|---|---|
| `INVESTMENT_ROOT` | `…/My Drive/<folder>/investment` (must include `My Drive`) |
| `GOOGLEDRIVE_ROOT` | alias of `INVESTMENT_ROOT` |
| `SAUDI_STOCKS_ROOT` | override; default `{INVESTMENT_ROOT}/Saudi stocks` |
| `INVESTMENT_NO_SHEETS` | default `1`: never push to Google Sheets, even if `sheets=true` |
| `TADAWUL_PDF_PATH` | path to `tadawul_pdf.py` (default `../tadawul-pdf/tadawul_pdf.py`) |
| `PDF_READ_PATH` | path to `pdf_read.py` (default `../pdf-read/pdf_read.py`) |

Paths containing `lost_and_found`, `DriveFS`, `OneDrive`, or a Google Drive CloudStorage path
without `My Drive` are rejected.

## Safari (for `ingest_filings` and `list_filings`)

1. Safari → Settings → Advanced → Show features for web developers
2. Develop → Developer Settings → Allow JavaScript from Apple Events
3. Allow downloads from saudiexchange.sa on the first prompt

## MCP config (Cursor / Claude Desktop)

```json
{
  "mcpServers": {
    "tasi-filings": {
      "command": "uv",
      "args": ["run", "/ABS/PATH/tasi-filings/server.py"],
      "env": {
        "INVESTMENT_ROOT": "/Users/…/My Drive/…/investment",
        "INVESTMENT_NO_SHEETS": "1"
      }
    }
  }
}
```

## Test

```bash
cd tasi-filings
uv run python test_tasi_filings.py
```

Always checks that all 10 tools are registered. With extracts for `1150` / `2026 Q3` under
`INVESTMENT_ROOT` it also checks `list_local_filings`, `get_table` (income), the
`get_statement_table` alias and `list_extracted_tables`. Without them the data checks are
skipped; set `REQUIRE_FIXTURE=1` to make that a failure.

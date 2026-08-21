# Fresh Parliament + SPRS CSV crawler

This version starts from the two websites on every run. It does NOT require an input CSV.

Outputs:
- `data/parliament_order_papers.csv`: Parliament Order Paper metadata + extracted PDF text in `content_text`.
- `data/sprs_official_report_sections.csv`: SPRS Official Report sections + extracted text from the JSON `content` field.
- `data/crawl_status.csv`: request/discovery status.
- `data/crawl_summary.json`: run summary.
- `data/pdfs/`: Parliament PDFs successfully downloaded for text extraction.

First GitHub test: use `parliament_limit=1`, `parliament_pages=1`, and `sprs_dates=08-04-2026`.

When the test works, set `parliament_limit=0` to process all discovered Parliament PDFs.

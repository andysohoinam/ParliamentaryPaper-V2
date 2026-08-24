from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import socket
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse

import pdfplumber
import requests
from bs4 import BeautifulSoup

PARLIAMENT_BASE = "https://www.parliament.gov.sg"
ORDER_PAPER_LIST = PARLIAMENT_BASE + "/parliamentary-business/order-paper"
ORDER_PAPER_DOCS = PARLIAMENT_BASE + "/docs/default-source/order-paper/"
SPRS_BASE = "https://sprs.parl.gov.sg"
SPRS_REPORT = SPRS_BASE + "/search/getHansardReport/?sittingDate={date}"

DEFAULT_ORDER_FALLBACKS = [
    "https://www.parliament.gov.sg/api/media/07fd3bdb-cb5f-64e2-b198-ff00006af031/order-paper---8apr2026.pdf",
    "https://www.parliament.gov.sg/api/media/6c3f3cdb-cb5f-64e2-b198-ff00006af031/orderpaper-7may2026.pdf",
]
DEFAULT_SPRS_DATES = ["08-04-2026", "04-03-2026", "04-02-2026"]

UA = os.getenv(
    "CRAWLER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-SG,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
CHALLENGE_MARKERS = (
    "enable javascript", "javascript is required", "verify you are human",
    "checking your browser", "access denied", "request rejected",
    "security check", "captcha", "bot verification",
)
PDF_RE = re.compile(r"(?:https?://[^\"'<\s>]+|/[^\"'<\s>]+)\.pdf(?:\?[^\"'<\s>]*)?", re.I)

PARLIAMENT_FIELDS = [
    "document_type", "title", "sitting_date", "parliament", "document_url",
    "listing_url", "listing_page", "pdf_url_used", "download_method",
    "download_status", "download_bytes", "page_count", "content_chars",
    "content_text", "local_pdf_path", "error",
]

SPRS_FIELDS = [
    "parliament_no", "session_no", "volume_no", "sitting_no", "sitting_date",
    "section_type", "title", "sub_title", "question_no", "start_page",
    "end_page", "content_html", "content_text", "report_url",
]


def parse_args():
    p = argparse.ArgumentParser(description="Freshly crawl Parliament.gov.sg and SPRS into consistent CSV outputs.")
    p.add_argument("--source", choices=("all", "order_paper", "official_report"), default="all")
    p.add_argument("--parliament-pages", type=int, default=1)
    p.add_argument("--parliament-limit", type=int, default=1, help="0 = all discovered PDFs")
    p.add_argument("--sprs-dates", default=",".join(DEFAULT_SPRS_DATES))
    p.add_argument("--output-dir", default="data")
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-browser", action="store_true")
    return p.parse_args()


def unique(items: Iterable[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        value = html.unescape(str(item)).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def is_pdf(data: bytes) -> bool:
    return data.lstrip().startswith(b"%PDF")


def classify(data: bytes, status: int) -> str:
    text = data[:6000].decode("utf-8", errors="ignore").lower()
    if status in {401, 403, 407, 429, 451, 503}:
        return f"http_block_{status}"
    if any(x in text for x in CHALLENGE_MARKERS):
        return "javascript_or_bot_challenge"
    if text.lstrip().startswith("<"):
        return "html_instead_of_pdf"
    return "non_pdf_response"


def exception_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, socket.gaierror) or "name resolution" in text or "failed to resolve" in text:
        return "dns_resolution_failed"
    if "timeout" in text or "timed out" in text:
        return "network_timeout"
    if "proxy" in text:
        return "proxy_error"
    return "network_error"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_requests(s: requests.Session, url: str, timeout: int, accept: str):
    try:
        r = s.get(url, timeout=timeout, allow_redirects=True, headers={"Accept": accept})
        body = r.content
        reason = "" if is_pdf(body) else classify(body, r.status_code)
        return r.status_code, dict(r.headers), body, r.url, reason
    except requests.RequestException as exc:
        return 0, {}, b"", url, exception_reason(exc)


def browser_get(url: str, referer: str, timeout: int, headed: bool):
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return 0, {}, b"", url, f"playwright_not_installed:{exc}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            context = browser.new_context(user_agent=UA, locale="en-SG", accept_downloads=True)
            page = context.new_page()
            try:
                page.goto(referer, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
            try:
                response = page.goto(url, wait_until="commit", timeout=timeout * 1000, referer=referer)
                if response is not None:
                    body = response.body()
                    headers = dict(response.headers)
                    if is_pdf(body):
                        browser.close()
                        return response.status, headers, body, response.url, ""
            except Exception:
                pass
            try:
                response = context.request.get(
                    url,
                    headers={"Accept": "application/pdf,*/*", "Referer": referer},
                    timeout=timeout * 1000,
                    fail_on_status_code=False,
                )
                body = response.body()
                headers = dict(response.headers)
                reason = "" if is_pdf(body) else classify(body, response.status)
                final = response.url
                status = response.status
                context.close()
                browser.close()
                return status, headers, body, final, reason
            except Exception as exc:
                context.close()
                browser.close()
                return 0, {}, b"", url, exception_reason(exc)
    except Exception as exc:
        return 0, {}, b"", url, exception_reason(exc)


def parliament_candidates(url: str) -> list[str]:
    urls = [url]
    p = urlparse(url)
    name = Path(unquote(p.path)).name
    if p.netloc.lower() == "www.parliament.gov.sg" and name.lower().endswith(".pdf"):
        alt = ORDER_PAPER_DOCS + name
        if alt not in urls:
            urls.append(alt)
    return urls


def extract_urls_from_html(body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="ignore")
    candidates = PDF_RE.findall(text)
    soup = BeautifulSoup(text, "lxml")
    for tag in soup.find_all(True):
        for attr in ("href", "data-url", "data-href", "data-link", "onclick"):
            value = tag.get(attr)
            if value:
                candidates += PDF_RE.findall(str(value))
    return unique(urljoin(PARLIAMENT_BASE, x) for x in candidates)


def dedupe_records(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        value = row.get(key, "")
        if value and value not in seen:
            seen.add(value)
            out.append(row)
    return out


def discover_parliament(s: requests.Session, pages: int, timeout: int, browser: bool, headed: bool):
    records = []
    statuses = []
    for page_no in range(1, max(1, pages) + 1):
        url = ORDER_PAPER_LIST if page_no == 1 else f"{ORDER_PAPER_LIST}?page={page_no}"
        status, headers, body, final, reason = get_requests(
            s, url, timeout, "text/html,application/xhtml+xml,*/*"
        )
        if not body and browser and reason not in {"dns_resolution_failed", "network_error"}:
            status, headers, body, final, reason = browser_get(url, PARLIAMENT_BASE + "/", timeout, headed)
        statuses.append({"source": "parliament", "stage": "listing", "url": url, "status": status, "reason": reason})
        if body:
            for pdf_url in extract_urls_from_html(body):
                records.append({
                    "source": "parliament",
                    "document_type": "order_paper",
                    "document_url": pdf_url,
                    "listing_url": final,
                    "listing_page": page_no,
                    "source_url": pdf_url,
                })
    records = dedupe_records(records, "document_url")
    if not records:
        for url in DEFAULT_ORDER_FALLBACKS:
            records.append({
                "source": "parliament",
                "document_type": "order_paper",
                "document_url": url,
                "listing_url": ORDER_PAPER_LIST,
                "listing_page": "fallback",
                "source_url": url,
            })
        statuses.append({"source": "parliament", "stage": "discovery", "url": ORDER_PAPER_LIST, "status": 0, "reason": "used_genuine_fallback_urls"})
    return records, statuses


def safe_name(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "_", unquote(value or "")).strip(" .")
    return clean if clean.lower().endswith(".pdf") else (clean + ".pdf" if clean else fallback)


def extract_pdf_text(path: Path) -> tuple[int, str]:
    parts = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            parts.append(f"[PAGE {i}]\n{text}")
        return len(pdf.pages), "\n\n".join(parts).strip()


def enrich_parliament_row(s: requests.Session, row: dict[str, Any], pdf_dir: Path, timeout: int, browser: bool, headed: bool):
    url = row["document_url"]
    name = safe_name(Path(unquote(urlparse(url).path)).stem.replace("-", " "), "order_paper.pdf")
    destination = pdf_dir / name
    result = {
        "sub_title": "",
        "parliament": "15th",
        "parliament_no": "15",
        "session_no": "",
        "volume_no": "",
        "sitting_no": "",
        "sitting_date": "",
        "section_type": "order_paper",
        "question_no": "",
        "start_page": "",
        "end_page": "",
        "content_html": "",
        "pdf_url_used": "",
        "download_method": "none",
        "download_status": "failed",
        "download_bytes": 0,
        "page_count": 0,
        "content_chars": 0,
        "content_text": "",
        "local_pdf_path": "",
        "error": "",
    }
    for candidate in parliament_candidates(url):
        status, headers, body, final, reason = get_requests(s, candidate, timeout, "application/pdf,application/octet-stream,*/*")
        if is_pdf(body):
            destination.write_bytes(body)
            result.update({
                "pdf_url_used": final,
                "download_method": "requests",
                "download_status": "success",
                "download_bytes": len(body),
            })
            break
        result["error"] = reason
    if result["download_status"] != "success" and browser and result.get("error") not in {"dns_resolution_failed"}:
        for candidate in parliament_candidates(url):
            status, headers, body, final, reason = browser_get(candidate, ORDER_PAPER_LIST, timeout, headed)
            if is_pdf(body):
                destination.write_bytes(body)
                result.update({
                    "pdf_url_used": final,
                    "download_method": "playwright",
                    "download_status": "success",
                    "download_bytes": len(body),
                    "error": "",
                })
                break
            result["error"] = reason
    if result["download_status"] == "success":
        try:
            pages, text = extract_pdf_text(destination)
            result.update({
                "page_count": pages,
                "content_chars": len(text),
                "content_text": text,
                "local_pdf_path": str(destination),
            })
        except Exception as exc:
            result.update({"download_status": "pdf_parse_failed", "error": f"pdf_extract_error:{exc}"})
    return {**row, **result}


def find_section_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("takesSectionVOList", "takesSectionVoList", "sectionList"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        for value in payload.values():
            found = find_section_list(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_section_list(value)
            if found:
                return found
    return []


def strip_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(html.unescape(value), "lxml")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()


def sprs_rows(payload: dict[str, Any], report_url: str) -> list[dict[str, Any]]:
    meta = payload.get("metadata") or {}
    rows = []
    for section in find_section_list(payload):
        content_html = section.get("content", "") or ""
        content_text = strip_html(content_html)
        rows.append({
            "source": "sprs",
            "document_type": "official_report",
            "title": section.get("title", ""),
            "sub_title": section.get("subTitle", "") or "",
            "parliament": "15th" if str(meta.get("parlimentNO", "")) == "15" else meta.get("parlimentNO", ""),
            "parliament_no": meta.get("parlimentNO", ""),
            "session_no": meta.get("sessionNO", ""),
            "volume_no": meta.get("volumeNO", ""),
            "sitting_no": meta.get("sittingNO", ""),
            "sitting_date": meta.get("sittingDate", ""),
            "section_type": section.get("sectionType", ""),
            "question_no": section.get("questionNo", "") or "",
            "start_page": section.get("startPgNo", ""),
            "end_page": section.get("endPgNo", ""),
            "content_html": content_html,
            "content_text": content_text,
            "source_url": report_url,
            "report_url": report_url,
            "document_url": "",
            "pdf_url_used": "",
            "download_method": "json_api",
            "download_status": "success",
            "download_bytes": "",
            "page_count": "",
            "content_chars": len(content_text),
            "local_pdf_path": "",
            "listing_url": "",
            "listing_page": "",
            "error": "",
        })
    return rows


def crawl_sprs(s: requests.Session, dates: list[str], timeout: int, browser: bool, headed: bool):
    rows = []
    statuses = []
    for date in dates:
        url = SPRS_REPORT.format(date=date)
        payload = None
        status, headers, body, final, reason = get_requests(s, url, timeout, "application/json,text/plain,*/*")
        if body:
            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except Exception:
                payload = None
        if payload is None and browser and reason not in {"dns_resolution_failed"}:
            status, headers, body, final, reason = browser_get(url, SPRS_BASE + "/", timeout, headed)
            if body:
                try:
                    payload = json.loads(body.decode("utf-8-sig"))
                except Exception:
                    payload = None
        if isinstance(payload, dict):
            current = sprs_rows(payload, final)
            rows.extend(current)
            statuses.append({"source": "sprs", "stage": "report", "url": url, "status": status, "reason": "", "rows": len(current)})
        else:
            statuses.append({"source": "sprs", "stage": "report", "url": url, "status": status, "reason": reason or "json_unavailable", "rows": 0})
    return rows, statuses


def single_line(value: Any) -> str:
    """Keep each logical record on one physical CSV line. Preserve content, normalize only line breaks."""
    if value is None:
        return ""
    text = str(value)
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def normalize_row(row: dict[str, Any], fields: list[str]) -> dict[str, str]:
    return {field: single_line(row.get(field, "")) for field in fields}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            normalized = normalize_row(row, fields)
            if len(normalized) != len(fields):
                raise ValueError(f"CSV schema mismatch: expected {len(fields)} fields, got {len(normalized)}")
            writer.writerow(normalized)


def normalize_row_status(row: dict[str, Any]) -> dict[str, Any]:
    return {"source": row.get("source", ""), "stage": row.get("stage", ""), "url": row.get("url", ""), "status": row.get("status", ""), "reason": row.get("reason", ""), "rows": row.get("rows", "")}


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    selected = ["order_paper", "official_report"] if args.source == "all" else [args.source]

    statuses = []
    parliament_rows = []
    sprs_rows_out = []

    if "order_paper" in selected:
        discovered, discovery_status = discover_parliament(
            session, args.parliament_pages, args.timeout, not args.no_browser, args.headed
        )
        statuses.extend(discovery_status)
        chosen = discovered if args.parliament_limit == 0 else discovered[:args.parliament_limit]
        for row in chosen:
            title = Path(unquote(urlparse(row["document_url"]).path)).stem.replace("-", " ").strip()
            row["title"] = title
            row = enrich_parliament_row(
                session, row, pdf_dir, args.timeout, not args.no_browser, args.headed
            )
            parliament_rows.append(row)

    if "official_report" in selected:
        dates = [value.strip() for value in args.sprs_dates.split(",") if value.strip()]
        sprs_rows_out, sprs_status = crawl_sprs(
            session, dates, args.timeout, not args.no_browser, args.headed
        )
        statuses.extend(sprs_status)

    # Keep each site's CSV schema independent and stable.
    write_csv(output_dir / "parliament_order_papers.csv", parliament_rows, PARLIAMENT_FIELDS)
    write_csv(output_dir / "sprs_official_report_sections.csv", sprs_rows_out, SPRS_FIELDS)

    statuses = [normalize_row_status(s) for s in statuses]
    status_fields = ["source", "stage", "url", "status", "reason", "rows"]
    with (output_dir / "crawl_status.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=status_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: item.get(k, "") for k in status_fields} for item in statuses)

    summary = {
        "parliament_rows": len(parliament_rows),
        "parliament_success": sum(1 for row in parliament_rows if row.get("download_status") == "success"),
        "sprs_rows": len(sprs_rows_out),
        "combined_rows": len(parliament_rows) + len(sprs_rows_out),
        "parliament_columns": len(PARLIAMENT_FIELDS),
        "sprs_columns": len(SPRS_FIELDS),
        "status_rows": len(statuses),
    }
    (output_dir / "crawl_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

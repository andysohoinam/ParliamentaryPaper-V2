from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlparse

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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
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
PDF_RE = re.compile(r"(?:https?://[^\"'<>\s]+|/[^\"'<>\s]+)\.pdf(?:\?[^\"'<>\s]*)?", re.I)


def parse_args():
    p = argparse.ArgumentParser(description="Freshly crawl Parliament.gov.sg and SPRS into CSV outputs.")
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
    seen = set(); out = []
    for x in items:
        x = html.unescape(str(x)).strip()
        if x and x not in seen:
            seen.add(x); out.append(x)
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


def session() -> requests.Session:
    s = requests.Session(); s.headers.update(HEADERS); return s


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
                r = page.goto(url, wait_until="commit", timeout=timeout * 1000, referer=referer)
                if r is not None:
                    body = r.body(); headers = dict(r.headers)
                    if is_pdf(body):
                        browser.close(); return r.status, headers, body, r.url, ""
            except Exception:
                pass
            try:
                r = context.request.get(url, headers={"Accept": "application/pdf,*/*", "Referer": referer}, timeout=timeout * 1000, fail_on_status_code=False)
                body = r.body(); headers = dict(r.headers)
                reason = "" if is_pdf(body) else classify(body, r.status)
                final = r.url; status = r.status
                context.close(); browser.close()
                return status, headers, body, final, reason
            except Exception as exc:
                context.close(); browser.close(); return 0, {}, b"", url, exception_reason(exc)
    except Exception as exc:
        return 0, {}, b"", url, exception_reason(exc)


def parliament_candidates(url: str) -> list[str]:
    urls = [url]
    p = urlparse(url)
    name = Path(unquote(p.path)).name
    if p.netloc.lower() == "www.parliament.gov.sg" and name.lower().endswith(".pdf"):
        alt = ORDER_PAPER_DOCS + name
        if alt not in urls: urls.append(alt)
    return urls


def extract_urls_from_html(body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="ignore")
    candidates = PDF_RE.findall(text)
    soup = BeautifulSoup(text, "lxml")
    for tag in soup.find_all(True):
        for attr in ("href", "data-url", "data-href", "data-link", "onclick"):
            v = tag.get(attr)
            if v:
                candidates += PDF_RE.findall(str(v))
    return unique(urljoin(PARLIAMENT_BASE, x) for x in candidates)


def discover_parliament(s: requests.Session, pages: int, timeout: int, browser: bool, headed: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = []; statuses = []
    for page_no in range(1, max(1, pages) + 1):
        url = ORDER_PAPER_LIST if page_no == 1 else f"{ORDER_PAPER_LIST}?page={page_no}"
        status, headers, body, final, reason = get_requests(s, url, timeout, "text/html,application/xhtml+xml,*/*")
        if not body and browser and reason not in {"dns_resolution_failed", "network_error"}:
            status, headers, body, final, reason = browser_get(url, PARLIAMENT_BASE + "/", timeout, headed)
        statuses.append({"source":"parliament", "stage":"listing", "url":url, "status":status, "reason":reason})
        if body:
            found = extract_urls_from_html(body)
            for pdf_url in found:
                records.append({"document_type":"order_paper", "document_url":pdf_url, "listing_url":final, "listing_page":page_no})
    records = dedupe_records(records, "document_url")
    if not records:
        for u in DEFAULT_ORDER_FALLBACKS:
            records.append({"document_type":"order_paper", "document_url":u, "listing_url":ORDER_PAPER_LIST, "listing_page":"fallback"})
        statuses.append({"source":"parliament", "stage":"discovery", "url":ORDER_PAPER_LIST, "status":0, "reason":"used_genuine_fallback_urls"})
    return records, statuses


def dedupe_records(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen=set(); out=[]
    for row in rows:
        v=row.get(key,"" )
        if v and v not in seen:
            seen.add(v); out.append(row)
    return out


def safe_name(value: str, fallback: str) -> str:
    x = re.sub(r"[^A-Za-z0-9._ -]+", "_", unquote(value or "")).strip(" .")
    return x if x.lower().endswith(".pdf") else (x + ".pdf" if x else fallback)


def extract_pdf_text(path: Path) -> tuple[int, str]:
    parts=[]
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            txt=(page.extract_text() or "").replace("\r\n","\n").replace("\r","\n").strip()
            parts.append(f"[PAGE {i}]\n{txt}")
        return len(pdf.pages), "\n\n".join(parts).strip()


def enrich_parliament_row(s: requests.Session, row: dict[str, Any], pdf_dir: Path, timeout: int, browser: bool, headed: bool) -> dict[str, Any]:
    url=row["document_url"]
    name=safe_name(row.get("title") or Path(unquote(urlparse(url).path)).name, "order_paper.pdf")
    dest=pdf_dir/name
    result={"pdf_url_used":"", "download_method":"none", "download_status":"failed", "download_bytes":0, "page_count":0, "content_chars":0, "content_text":"", "error":""}
    for candidate in parliament_candidates(url):
        status, headers, body, final, reason=get_requests(s, candidate, timeout, "application/pdf,application/octet-stream,*/*")
        if is_pdf(body):
            dest.write_bytes(body); result.update({"pdf_url_used":final,"download_method":"requests","download_status":"success","download_bytes":len(body)}); break
        result["error"]=reason
    if result["download_status"] != "success" and browser and result.get("error") not in {"dns_resolution_failed"}:
        for candidate in parliament_candidates(url):
            status, headers, body, final, reason=browser_get(candidate, ORDER_PAPER_LIST, timeout, headed)
            if is_pdf(body):
                dest.write_bytes(body); result.update({"pdf_url_used":final,"download_method":"playwright","download_status":"success","download_bytes":len(body),"error":""}); break
            result["error"]=reason
    if result["download_status"] == "success":
        try:
            pages, text=extract_pdf_text(dest)
            result.update({"page_count":pages,"content_chars":len(text),"content_text":text,"local_pdf_path":str(dest)})
        except Exception as exc:
            result.update({"download_status":"pdf_parse_failed","error":f"pdf_extract_error:{exc}"})
    else:
        result["local_pdf_path"]=""
    return {**row, **result}


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)


def find_section_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("takesSectionVOList", "takesSectionVoList", "sectionList"):
            value=payload.get(key)
            if isinstance(value, list): return [x for x in value if isinstance(x, dict)]
        for value in payload.values():
            found=find_section_list(value)
            if found: return found
    elif isinstance(payload, list):
        for value in payload:
            found=find_section_list(value)
            if found: return found
    return []


def strip_html(value: str) -> str:
    if not value: return ""
    soup=BeautifulSoup(html.unescape(value), "lxml")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()


def sprs_rows(payload: dict[str, Any], report_url: str) -> list[dict[str, Any]]:
    meta=payload.get("metadata") or {}
    rows=[]
    for sec in find_section_list(payload):
        rows.append({
            "parliament_no": meta.get("parlimentNO", ""),
            "session_no": meta.get("sessionNO", ""),
            "volume_no": meta.get("volumeNO", ""),
            "sitting_no": meta.get("sittingNO", ""),
            "sitting_date": meta.get("sittingDate", ""),
            "section_type": sec.get("sectionType", ""),
            "title": sec.get("title", ""),
            "sub_title": sec.get("subTitle", "") or "",
            "question_no": sec.get("questionNo", "") or "",
            "start_page": sec.get("startPgNo", ""),
            "end_page": sec.get("endPgNo", ""),
            "content_html": sec.get("content", "") or "",
            "content_text": strip_html(sec.get("content", "") or ""),
            "report_url": report_url,
        })
    return rows


def crawl_sprs(s: requests.Session, dates: list[str], timeout: int, browser: bool, headed: bool):
    rows=[]; statuses=[]
    for date in dates:
        url=SPRS_REPORT.format(date=date)
        payload=None; reason=""
        status, headers, body, final, reason=get_requests(s, url, timeout, "application/json,text/plain,*/*")
        if body:
            try: payload=json.loads(body.decode("utf-8-sig"))
            except Exception: payload=None
        if payload is None and browser and reason not in {"dns_resolution_failed"}:
            status, headers, body, final, reason=browser_get(url, SPRS_BASE + "/", timeout, headed)
            if body:
                try: payload=json.loads(body.decode("utf-8-sig"))
                except Exception: payload=None
        if isinstance(payload, dict):
            current=sprs_rows(payload, final)
            rows.extend(current)
            statuses.append({"source":"sprs","stage":"report","url":url,"status":status,"reason":"","rows":len(current)})
        else:
            statuses.append({"source":"sprs","stage":"report","url":url,"status":status,"reason":reason or "json_unavailable","rows":0})
    return rows, statuses


def main() -> int:
    a=parse_args(); out=Path(a.output_dir); pdf_dir=out/"pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    s=session(); selected=(['order_paper','official_report'] if a.source=='all' else [a.source])
    all_status=[]; parliament=[]; sprs=[]
    if 'order_paper' in selected:
        discovered, statuses=discover_parliament(s, a.parliament_pages, a.timeout, not a.no_browser, a.headed); all_status.extend(statuses)
        limit=a.parliament_limit
        chosen=discovered if limit==0 else discovered[:limit]
        for row in chosen:
            # derive a human title from the filename; metadata on listing is retained where available
            row["title"]=Path(unquote(urlparse(row["document_url"]).path)).stem.replace("-", " ").strip()
            row= enrich_parliament_row(s, row, pdf_dir, a.timeout, not a.no_browser, a.headed)
            parliament.append(row)
    if 'official_report' in selected:
        dates=[x.strip() for x in a.sprs_dates.split(',') if x.strip()]
        sprs, statuses=crawl_sprs(s, dates, a.timeout, not a.no_browser, a.headed); all_status.extend(statuses)
    write_csv(out/"parliament_order_papers.csv", parliament, [
        "document_type","title","sitting_date","parliament","document_url","listing_url","listing_page",
        "pdf_url_used","download_method","download_status","download_bytes","page_count","content_chars","content_text","local_pdf_path","error"
    ])
    write_csv(out/"sprs_official_report_sections.csv", sprs, [
        "parliament_no","session_no","volume_no","sitting_no","sitting_date","section_type","title","sub_title","question_no",
        "start_page","end_page","content_html","content_text","report_url"
    ])
    write_csv(out/"crawl_status.csv", all_status, ["source","stage","url","status","reason","rows"])
    summary={"parliament_rows":len(parliament),"parliament_success":sum(1 for r in parliament if r.get('download_status')=='success'),"sprs_rows":len(sprs),"status_rows":len(all_status)}
    (out/"crawl_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

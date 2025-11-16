#!/usr/bin/env python3
"""
Download all Mushoku Tensei Fandom wiki pages as text and compile them into a single PDF.

Usage:
    python download_mushoku_tensei_wiki.py [--output-dir OUTPUT_DIR]
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from tqdm import tqdm


API_URL = "https://mushokutensei.fandom.com/api.php"
USER_AGENT = (
    "MushokuTenseiWikiDownloader/1.0 "
    "(https://github.com/openai/cursor, contact: support@openai.com)"
)

_thread_local = threading.local()


@dataclass
class PageContent:
    title: str
    text: str


def _get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _thread_local.session = session
    return session


def fetch_all_page_titles(session: requests.Session) -> List[str]:
    titles: List[str] = []
    params = {
        "action": "query",
        "list": "allpages",
        "apnamespace": 0,
        "aplimit": 500,
        "format": "json",
        "formatversion": 2,
    }

    logging.info("Fetching page list from %s", API_URL)
    while True:
        response = session.get(API_URL, params=params, timeout=60)
        response.raise_for_status()
        data = response.json()
        batch = [page["title"] for page in data["query"]["allpages"]]
        titles.extend(batch)
        logging.debug("Fetched %d titles (total so far: %d)", len(batch), len(titles))

        cont = data.get("continue")
        if not cont:
            break
        params.update(cont)
        time.sleep(0.2)

    return titles


def extract_clean_text(html_fragment: str) -> str:
    soup = BeautifulSoup(html_fragment, "html.parser")

    for selector in [
        "script",
        "style",
        ".mw-editsection",
        ".mw-editsection-like",
        ".portable-infobox",
        ".toc",
        ".reference",
        ".references",
        ".infobox",
        ".navbox",
        ".gallery",
        ".wds-tab__content-nav",
        ".wds-is-current",
    ]:
        for element in soup.select(selector):
            element.decompose()

    text = soup.get_text(separator="\n")
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = text.strip()
    return text


def fetch_page_content(title: str, session: Optional[requests.Session] = None) -> PageContent:
    params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "format": "json",
        "formatversion": 2,
    }
    session = session or _get_thread_session()
    response = session.get(API_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    html_fragment = data.get("parse", {}).get("text", "")
    clean_text = extract_clean_text(html_fragment) if html_fragment else ""
    return PageContent(title=title, text=clean_text)


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("mushoku_tensei_output"),
        help="Directory where text files and PDF will be saved.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay (in seconds) between page downloads to avoid hitting rate limits.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional limit on the number of pages to download (for testing).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent download workers (1 for sequential).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    output_dir: Path = args.output_dir
    text_dir = output_dir / "pages"
    combined_text_path = output_dir / "mushoku_tensei_wiki.txt"
    pdf_path = output_dir / "mushoku_tensei_wiki.pdf"

    text_dir.mkdir(parents=True, exist_ok=True)
    combined_text_path.parent.mkdir(parents=True, exist_ok=True)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    delay = max(args.delay, 0.0)
    workers = max(args.workers, 1)

    listing_session = requests.Session()
    listing_session.headers.update({"User-Agent": USER_AGENT})
    try:
        titles = fetch_all_page_titles(listing_session)
    finally:
        listing_session.close()

    if args.max_pages:
        titles = titles[: args.max_pages]
    total_titles = len(titles)
    logging.info("Found %d pages to download", total_titles)

    if total_titles == 0:
        logging.warning("No page titles retrieved; nothing to download.")
        return 0

    successful_pages = 0
    next_index_to_write = 0
    pending_results: Dict[int, Optional[PageContent]] = {}

    with combined_text_path.open("w", encoding="utf-8") as combined_file:
        first_combined_entry = True

        def write_page(page: PageContent) -> None:
            nonlocal first_combined_entry, successful_pages
            safe_title = re.sub(r"[^0-9A-Za-z._-]+", "_", page.title).strip("_")
            file_path = text_dir / f"{safe_title or 'untitled'}.txt"
            file_path.write_text(page.text, encoding="utf-8")

            if not first_combined_entry:
                combined_file.write("\n")
            else:
                first_combined_entry = False
            combined_file.write(f"{page.title}\n")
            combined_file.write(f"{'=' * len(page.title)}\n\n")
            combined_file.write(f"{page.text}\n")
            combined_file.flush()

            pdf.add_page()
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(0, 10, _latin1(page.title))
            pdf.ln(4)

            pdf.set_font("Helvetica", size=11)
            for paragraph in page.text.split("\n"):
                paragraph = paragraph.strip()
                if not paragraph:
                    pdf.ln(5)
                    continue
                pdf.multi_cell(0, 6, _latin1(paragraph))
                pdf.ln(1)

            successful_pages += 1

        def process_ready_results() -> None:
            nonlocal next_index_to_write
            while next_index_to_write in pending_results:
                page = pending_results.pop(next_index_to_write)
                if page and page.text:
                    write_page(page)
                next_index_to_write += 1

        progress = tqdm(total=total_titles, desc="Downloading pages", unit="page")

        if workers == 1:
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            try:
                for idx, title in enumerate(titles):
                    try:
                        page = fetch_page_content(title, session)
                    except Exception as exc:  # pylint: disable=broad-except
                        logging.error("Failed to fetch '%s': %s", title, exc)
                        pending_results[idx] = None
                    else:
                        pending_results[idx] = page
                    process_ready_results()
                    progress.update(1)
                    if delay:
                        time.sleep(delay)
            finally:
                session.close()
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures: Dict[object, tuple[int, str]] = {}
                next_submit = 0

                def submit_task(index: int) -> None:
                    title = titles[index]
                    future = executor.submit(fetch_page_content, title)
                    futures[future] = (index, title)

                initial = min(workers, total_titles)
                while next_submit < initial:
                    submit_task(next_submit)
                    next_submit += 1

                while futures:
                    done, _ = wait(list(futures.keys()), return_when=FIRST_COMPLETED)
                    for future in done:
                        idx, title = futures.pop(future)
                        try:
                            page = future.result()
                        except Exception as exc:  # pylint: disable=broad-except
                            logging.error("Failed to fetch '%s': %s", title, exc)
                            pending_results[idx] = None
                        else:
                            pending_results[idx] = page
                        process_ready_results()
                        progress.update(1)
                        if next_submit < total_titles:
                            submit_task(next_submit)
                            next_submit += 1
                        if delay:
                            time.sleep(delay)

        progress.close()
        process_ready_results()

    if successful_pages == 0:
        logging.warning("No page content found; skipping PDF generation.")
        return 0

    pdf.output(str(pdf_path))

    logging.info("Saved %d pages", successful_pages)
    logging.info("Saved combined text to %s", combined_text_path)
    logging.info("Saved PDF to %s", pdf_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

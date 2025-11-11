#!/usr/bin/env python3
"""
Download all Eternal Supreme Fandom wiki pages as text and compile them into a single PDF.

Usage:
    python download_eternal_supreme_wiki.py [--output-dir OUTPUT_DIR]
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

import requests
from bs4 import BeautifulSoup
from fpdf import FPDF
from tqdm import tqdm


API_URL = "https://eternal-supreme.fandom.com/api.php"
USER_AGENT = (
    "EternalSupremeWikiDownloader/1.0 "
    "(https://github.com/openai/cursor, contact: support@openai.com)"
)


@dataclass
class PageContent:
    title: str
    text: str


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
        time.sleep(0.2)  # be polite to the API

    return titles


def extract_clean_text(html_fragment: str) -> str:
    soup = BeautifulSoup(html_fragment, "html.parser")

    # Remove non-content elements
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
    ]:
        for element in soup.select(selector):
            element.decompose()

    text = soup.get_text(separator="\n")
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    # Normalize whitespace: collapse 3+ blank lines to 2.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = text.strip()
    return text


def fetch_page_content(session: requests.Session, title: str) -> PageContent:
    params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "format": "json",
        "formatversion": 2,
    }
    response = session.get(API_URL, params=params, timeout=60)
    response.raise_for_status()
    data = response.json()

    html_fragment = data.get("parse", {}).get("text", "")
    clean_text = extract_clean_text(html_fragment) if html_fragment else ""
    return PageContent(title=title, text=clean_text)


def save_individual_texts(pages: Iterable[PageContent], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for page in pages:
        if not page.text:
            continue
        safe_title = re.sub(r"[^0-9A-Za-z._-]+", "_", page.title).strip("_")
        file_path = directory / f"{safe_title or 'untitled'}.txt"
        file_path.write_text(page.text, encoding="utf-8")


def save_combined_text(pages: Iterable[PageContent], file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    for page in pages:
        if not page.text:
            continue
        lines.append(page.title)
        lines.append("=" * len(page.title))
        lines.append("")
        lines.append(page.text)
        lines.append("")
    file_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def build_pdf(pages: Iterable[PageContent], file_path: Path) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    has_content = False
    for page in pages:
        if not page.text:
            continue

        pdf.add_page()
        has_content = True
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

    if not has_content:
        logging.warning("No page content found; skipping PDF generation.")
        return

    file_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(file_path))


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("wiki_output"),
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
    combined_text_path = output_dir / "eternal_supreme_wiki.txt"
    pdf_path = output_dir / "eternal_supreme_wiki.pdf"

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    titles = fetch_all_page_titles(session)
    if args.max_pages:
        titles = titles[: args.max_pages]
    logging.info("Found %d pages to download", len(titles))

    pages: List[PageContent] = []
    for title in tqdm(titles, desc="Downloading pages", unit="page"):
        try:
            page = fetch_page_content(session, title)
            pages.append(page)
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Failed to fetch '%s': %s", title, exc)
        time.sleep(args.delay)

    save_individual_texts(pages, text_dir)
    save_combined_text(pages, combined_text_path)
    build_pdf(pages, pdf_path)

    logging.info("Saved combined text to %s", combined_text_path)
    logging.info("Saved PDF to %s", pdf_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

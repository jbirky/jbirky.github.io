#!/usr/bin/env python3
"""
Generate Jekyll publication markdown files from a BibTeX file.

Usage:
    python scripts/generate_publications.py [--fetch-covers]

Reads:  assets/jbirky.bib
Writes: _publications/<year>/<key>.md
        assets/images/covers/<slug>.jpg  (when --fetch-covers is used)

Configuration is at the top of this script (SELF_NAMES, CODE_LINKS, etc.).
"""

import re
import os
import sys
import shutil
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# Config – edit these to match your info
# ---------------------------------------------------------------------------

BIB_PATH = Path("assets/jbirky.bib")
PUB_DIR = Path("_publications")
COVERS_DIR = Path("assets/images/covers")

# Names (as they appear in bib author fields) that should be bolded as "you"
SELF_NAMES = ["Birky"]

# Map bib keys to cover images (relative to site root).
# These take priority over auto-fetched covers.
COVER_IMAGES = {
    "2025ApJ...990..124H": "/assets/images/covers/mc_results_tau_-1_resize2.png",
    "2020ApJ...892...31B": "/assets/images/covers/demo_derivatives_teff1.png",
    "birky2026alabi": "/assets/images/covers/alabi_initial_gp_fit.png",
}

# Map bib keys to extra links (code repos, etc.)
EXTRA_LINKS = {
    "2020ApJ...892...31B": {"Code": "https://github.com/jbirky/Mdwarf_project"},
}

# Map bib keys -> True to mark as selected (shown on homepage)
SELECTED = {
    "2025ApJ...992..133B": True,
    "2025ApJ...990..124H": True,
    "2020ApJ...892...31B": True,
}

# Journal macro expansions
JOURNAL_MACROS = {
    r"\apj": "ApJ",
    r"\apjl": "ApJL",
    r"\apjs": "ApJS",
    r"\aj": "AJ",
    r"\mnras": "MNRAS",
    r"\aap": "A&A",
    r"\psj": "PSJ",
    r"\pasp": "PASP",
    r"\araa": "ARA&A",
    r"\nat": "Nature",
    r"\rnaas": "RNAAS",
}

# User-Agent for HTTP requests
USER_AGENT = "Mozilla/5.0 (academic-homepage-generator; +https://github.com/luost26/academic-homepage)"

# ---------------------------------------------------------------------------
# BibTeX parser (simple, handles standard ADS exports)
# ---------------------------------------------------------------------------

def parse_bib(path):
    """Parse a .bib file and return a list of entry dicts."""
    text = path.read_text()
    entries = []
    raw_entries = re.split(r'@(\w+)\s*\{', text)[1:]
    for i in range(0, len(raw_entries), 2):
        entry_type = raw_entries[i].upper()
        body = raw_entries[i + 1]
        key, _, rest = body.partition(',')
        key = key.strip()
        entry = {"type": entry_type, "key": key}
        for m in re.finditer(
            r'(\w+)\s*=\s*(?:\{((?:[^{}]|\{[^{}]*\})*)\}|"([^"]*)"|(\w+))',
            rest,
        ):
            field = m.group(1).lower()
            value = m.group(2) if m.group(2) is not None else (
                m.group(3) if m.group(3) is not None else m.group(4)
            )
            value = value.strip()
            entry[field] = value
        entries.append(entry)
    return entries


def clean_author(name):
    """Convert '{Last}, First' BibTeX format to 'First Last'."""
    name = re.sub(r'[{}]', '', name)
    name = name.replace('~', ' ')
    name = re.sub(r'\\["\'^`~v][{]?(\w)[}]?', r'\1', name)
    name = re.sub(r'\\[\w]+\s*', '', name)
    name = re.sub(r'\s+', ' ', name)
    name = name.strip()
    if ',' in name:
        parts = name.split(',', 1)
        last = parts[0].strip()
        first = parts[1].strip()
        return f"{first} {last}"
    return name


def format_authors(raw, max_display=6):
    """Parse author string into a list of display names."""
    raw_authors = re.split(r'\s+and\s+', raw)
    authors = [clean_author(a) for a in raw_authors]
    return authors


def is_self(author_name):
    """Check if an author name matches self."""
    for sn in SELF_NAMES:
        if sn.lower() in author_name.lower():
            return True
    return False


def expand_journal(raw):
    """Expand journal macros."""
    raw = raw.strip().strip('{}')
    return JOURNAL_MACROS.get(raw, raw)


def clean_title(raw):
    """Remove LaTeX braces from title."""
    return re.sub(r'[{}]', '', raw).strip()


# ---------------------------------------------------------------------------
# Cover image fetcher
# ---------------------------------------------------------------------------

def fetch_url(url, max_redirects=5):
    """Fetch a URL following redirects, return (final_url, html_text) or None."""
    for _ in range(max_redirects):
        req = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            resp = urlopen(req, timeout=15)
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" in content_type:
                return resp.geturl(), resp.read().decode("utf-8", errors="replace")
            else:
                return resp.geturl(), None
        except (URLError, HTTPError) as e:
            print(f"    Warning: failed to fetch {url}: {e}")
            return None, None
    return None, None


def extract_og_image(html):
    """Extract og:image URL from HTML."""
    # Try og:image first
    m = re.search(
        r'<meta\s+[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if not m:
        m = re.search(
            r'<meta\s+[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
            html, re.IGNORECASE
        )
    if m:
        return m.group(1)
    return None


def extract_first_figure(html, base_url):
    """Extract the first article/figure image from the HTML as a fallback."""
    # Look for images inside <figure> tags or common article figure patterns
    patterns = [
        # IOP/AAS figure images
        r'<img[^>]+src=["\']([^"\']*(?:apj|psj|aj|apjs|mnras)[^"\']*\.(?:jpg|png|gif|jpeg))["\']',
        # Generic figure images
        r'<figure[^>]*>.*?<img[^>]+src=["\']([^"\']+)["\']',
        # Any article body image that looks like a figure
        r'<img[^>]+src=["\']([^"\']*(?:Fig|fig|figure|Figure)[^"\']*\.(?:jpg|png|gif|jpeg))["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            img_url = m.group(1)
            if not img_url.startswith("http"):
                img_url = urljoin(base_url, img_url)
            return img_url
    return None


def download_image(url, dest_path):
    """Download an image URL to a local file. Returns True on success."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urlopen(req, timeout=20)
        content_type = resp.headers.get("Content-Type", "")
        if "image" not in content_type:
            return False
        data = resp.read()
        if len(data) < 1000:
            # Too small, probably a placeholder/icon
            return False
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(data)
        return True
    except (URLError, HTTPError) as e:
        print(f"    Warning: failed to download {url}: {e}")
        return False


def fetch_cover_from_arxiv(eprint, base_url=None):
    """Try to get the first figure image from an arXiv abstract page."""
    url = f"https://arxiv.org/abs/{eprint}"
    print(f"    Trying arXiv: {url}")
    _, html = fetch_url(url)
    if not html:
        return None

    # Skip abstract page images — they're almost always just license icons.
    # Go straight to HTML renderings.

    # Try ar5iv (renders more papers to HTML than arxiv.org/html)
    # and arxiv.org/html with version suffixes
    html2 = None
    final_url = None
    urls_to_try = [
        f"https://ar5iv.labs.arxiv.org/html/{eprint}",
    ]
    for ver in ["v1", "v2", "v3", ""]:
        urls_to_try.append(f"https://arxiv.org/html/{eprint}{ver}")
    for html_url in urls_to_try:
        print(f"    Trying: {html_url}")
        final_url, html2 = fetch_url(html_url)
        if html2:
            break
    if html2:
        # Find first <figure> or <img> with figure-like src
        m = re.search(
            r'<(?:figure|div)[^>]*class=["\'][^"\']*(?:ltx_figure|figure)["\'][^>]*>.*?<img[^>]+src=["\']([^"\']+)["\']',
            html2, re.IGNORECASE | re.DOTALL
        )
        if m:
            img = m.group(1)
            if not img.startswith("http"):
                img = urljoin(final_url or html_url, img)
            return img
        # Broader: any img in the article body
        for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html2, re.IGNORECASE):
            img = m.group(1)
            # Skip tiny icons, logos, mathjax
            if any(skip in img.lower() for skip in ['logo', 'icon', 'mathjax', 'badge', '.svg', 'avatar']):
                continue
            if not img.startswith("http"):
                img = urljoin(final_url or html_url, img)
            return img

    return None


def fetch_cover_from_semantic_scholar(doi):
    """Try to get a paper thumbnail from the Semantic Scholar API."""
    import json
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=openAccessPdf"
    print(f"    Trying Semantic Scholar API...")
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        resp = urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        # S2 doesn't directly give thumbnails, but we can try the PDF
        # Actually not useful for images. Skip.
        return None
    except Exception:
        return None


def fetch_cover_for_entry(doi, eprint, slug):
    """
    Try to fetch a cover image for a paper.
    Strategy:
      1. DOI landing page (og:image or first figure)
      2. arXiv abstract/HTML page (first figure)
    Returns the site-relative path on success, or "" on failure.
    """
    img_url = None

    # Strategy 1: DOI landing page
    if doi:
        doi_url = f"https://doi.org/{doi}"
        print(f"    Fetching cover via DOI: {doi}")
        final_url, html = fetch_url(doi_url)
        if html:
            img_url = extract_og_image(html)
            if not img_url and final_url:
                img_url = extract_first_figure(html, final_url)
            if img_url and not img_url.startswith("http"):
                img_url = urljoin(final_url or doi_url, img_url)

    # Strategy 2: arXiv
    if not img_url and eprint:
        img_url = fetch_cover_from_arxiv(eprint)

    if not img_url:
        print(f"    No image found")
        return ""

    # Filter out obviously bad images
    bad_patterns = ['logo', 'icon', 'badge', 'avatar', 'banner', 'header',
                    'placeholder', '1x1', 'spacer', 'blank', 'license',
                    'creative-commons', 'cc-by', 'orcid', 'crossmark']
    if any(bp in img_url.lower() for bp in bad_patterns):
        print(f"    Skipping likely non-figure image: {img_url}")
        return ""

    # Determine extension
    ext_match = re.search(r'\.(jpg|jpeg|png|gif|webp)', img_url, re.IGNORECASE)
    ext = ext_match.group(1).lower() if ext_match else "jpg"
    if ext == "jpeg":
        ext = "jpg"

    dest_path = COVERS_DIR / f"{slug}.{ext}"

    if dest_path.exists():
        print(f"    Cover already exists: {dest_path}")
        return f"/assets/images/covers/{dest_path.name}"

    if download_image(img_url, dest_path):
        print(f"    Downloaded cover: {dest_path}")
        return f"/assets/images/covers/{dest_path.name}"
    else:
        print(f"    Failed to download image from {img_url}")
        return ""


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------

def entry_to_md(entry, fetch_covers=False):
    """Convert a parsed bib entry to Jekyll front-matter markdown."""
    key = entry["key"]
    title = clean_title(entry.get("title", "Untitled"))
    year = entry.get("year", "2025").strip()
    month = entry.get("month", "jan").strip()
    journal = expand_journal(entry.get("journal", ""))
    volume = entry.get("volume", "")
    pages = entry.get("pages", entry.get("eid", ""))
    doi = entry.get("doi", "")
    eprint = entry.get("eprint", "")
    adsurl = entry.get("adsurl", "")

    # Date for sorting
    month_map = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    mm = month_map.get(month[:3].lower(), "01")
    date_str = f"{year}-{mm}-01 00:00:00 +0000"

    # Authors
    raw_authors = entry.get("author", "")
    authors = format_authors(raw_authors)

    # Is first/second author?
    first_is_self = len(authors) > 0 and is_self(authors[0])
    second_is_self = len(authors) > 1 and is_self(authors[1])
    is_lead = first_is_self or second_is_self

    selected = SELECTED.get(key, is_lead)

    # Pub string
    pub_parts = [journal]
    if volume:
        pub_parts.append(volume)
    if pages:
        pub_parts.append(pages)
    pub_str = ", ".join(p for p in pub_parts if p)

    # Links
    links = {}
    if doi:
        links["Paper"] = f"https://doi.org/{doi}"
    if eprint:
        links["arXiv"] = f"https://arxiv.org/abs/{eprint}"
    if adsurl:
        links["ADS"] = re.sub(r'[{}]', '', adsurl)
    if key in EXTRA_LINKS:
        links.update(EXTRA_LINKS[key])

    # Cover image: priority is bib cover field > COVER_IMAGES dict > --fetch-covers
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', key).strip('-').lower()
    bib_cover = entry.get("cover", "").strip()
    if bib_cover:
        cover = f"/assets/images/covers/{bib_cover}"
    else:
        cover = COVER_IMAGES.get(key, "")

    # If still no cover and --fetch-covers, try to get one
    if not cover and fetch_covers and (doi or eprint):
        # Check if we already have a downloaded cover for this slug
        existing = list(COVERS_DIR.glob(f"{slug}.*"))
        if existing:
            cover = f"/assets/images/covers/{existing[0].name}"
            print(f"    Using existing cover: {cover}")
        else:
            cover = fetch_cover_for_entry(doi, eprint, slug)
            time.sleep(1)  # Be polite to servers

    # Build YAML front matter
    lines = ["---"]
    lines.append(f'title:          "{title}"')
    lines.append(f"date:           {date_str}")
    lines.append(f"selected:       {'true' if selected else 'false'}")
    lines.append(f"first_author:   {'true' if is_lead else 'false'}")
    lines.append(f'pub:            "{pub_str}"')
    lines.append(f'pub_date:       "{year}"')
    if cover:
        lines.append(f"cover:          {cover}")

    # Authors list
    lines.append("authors:")
    for a in authors:
        lines.append(f'  - "{a}"')

    # Links
    if links:
        lines.append("links:")
        for label, url in links.items():
            lines.append(f"  {label}: {url}")

    lines.append("---")
    return "\n".join(lines) + "\n", year, key


def main():
    fetch_covers = "--fetch-covers" in sys.argv

    if not BIB_PATH.exists():
        print(f"Error: {BIB_PATH} not found")
        return

    entries = parse_bib(BIB_PATH)
    print(f"Parsed {len(entries)} entries from {BIB_PATH}")

    if fetch_covers:
        print("Cover fetching enabled (--fetch-covers)")
        COVERS_DIR.mkdir(parents=True, exist_ok=True)

    # Clear existing publications
    if PUB_DIR.exists():
        shutil.rmtree(PUB_DIR)

    for entry in entries:
        md_content, year, key = entry_to_md(entry, fetch_covers=fetch_covers)
        year_dir = PUB_DIR / year
        year_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', key).strip('-').lower()
        out_path = year_dir / f"{slug}.md"
        out_path.write_text(md_content)
        print(f"  -> {out_path}")

    print(f"\nDone! Generated {len(entries)} publication files in {PUB_DIR}/")
    print("Run 'bundle exec jekyll build' to rebuild the site.")


if __name__ == "__main__":
    main()

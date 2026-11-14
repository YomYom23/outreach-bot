"""choose_links (design item 35): which extra pages of a company's site to fetch after the home page.

Only links in the header, nav and footer are considered: that's where small-business sites put "About" and
"Contact". Links must stay on the claimed domain. Each candidate gets a score from `score_link`, and the best
`extra_pages_per_company` paths win. With no usable links, it falls back to /contact and /about.
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urljoin, urlsplit

from outreach.research.extract import parse, same_site

FALLBACK_PATHS = ("/contact", "/about")
SKIP_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".doc", ".docx", ".mp4")
CONTAINERS = "header a[href], nav a[href], footer a[href]"


def score_link(path: str, anchor_text: str, keywords: Sequence[str]) -> int:
    """How useful a link is for finding owner names and email addresses. 0 means "don't fetch".

    `path` is lowercase and starts with "/" (no query or fragment), for example "/about-us" or "/blog/2024/hail".
    `anchor_text` is the link's visible text, lowercase and whitespace-collapsed, for example "meet the team".
    `keywords` is TargetProfile.link_keywords, in priority order:
        ["about", "contact", "team", "our-story", "who-we-are", "owner", "company"]
    """
    best = 0
    for rank, keyword in enumerate(keywords):
        in_path = keyword in path
        in_text = keyword.replace("-", " ") in anchor_text
        if in_path or in_text:
            # Earlier keywords weigh more; agreement between path and anchor breaks ties.
            best = max(best, 2 * (len(keywords) - rank) + (in_path and in_text))
    if best and path.count("/") > 2:  # /blog/2024/about-our-hail-work: a keyword by accident, deep in the site
        best = max(1, best // 2)
    return best


def candidate_links(html: str, page_url: str, domain_key: str) -> list[tuple[str, str]]:
    """(path, anchor_text) pairs from header/nav/footer, same-site, deduplicated, in page order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for a in parse(html).css(CONTAINERS):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not same_site(url, domain_key):
            continue
        path = (parts.path or "/").lower().rstrip("/") or "/"
        if path == "/" or path.endswith(SKIP_SUFFIXES) or path in seen:
            continue
        seen.add(path)
        out.append((path, " ".join(a.text().split()).lower()))
    return out


def choose_links(html: str, page_url: str, domain_key: str, keywords: Sequence[str], limit: int) -> list[str]:
    if limit <= 0:
        return []
    scored = [(score_link(path, text, keywords), i, path)
              for i, (path, text) in enumerate(candidate_links(html, page_url, domain_key))]
    best = [path for score, _, path in sorted(scored, key=lambda t: (-t[0], t[1])) if score > 0][:limit]
    return best or list(FALLBACK_PATHS[:limit])

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download recent Fantasy Westward Journey Wushentan replay files.

Version 2 adds support for Discuz forum attachment URLs such as
``forum.php?mod=attachment&aid=...`` whose URL does not contain ``.mhw``.
It also recognizes ZIP/RAR/7z bundles and extracts .mhw files from ZIP files.

Examples:
    python download_wst_replays_v2.py --edition 236 --count 1
    python download_wst_replays_v2.py --edition 236 --count 5 --dry-run
    python download_wst_replays_v2.py --article-url http://bbs.yzz.cn/thread-795348-1-1.html
    python download_wst_replays_v2.py --self-test

No third-party Python package is required.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import Message
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

BASE = "https://xyq.yzz.cn"
INDEX_URLS = (
    "https://xyq.yzz.cn/video/?yzz_no_device=1",
    "https://xyq.yzz.cn/video/match/?yzz_no_device=1",
    "https://xyq.yzz.cn/?yzz_no_device=1",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

ARTICLE_TITLE_RE = re.compile(
    r"第\s*(?P<edition>\d+)\s*联[^\n]{0,40}?武神坛[^\n]{0,50}?录像",
    re.IGNORECASE,
)
RESOURCE_EXTENSIONS = (".mhw", ".zip", ".rar", ".7z")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
DIRECT_RESOURCE_RE = re.compile(
    r"(?P<url>(?:https?:)?//[^\s\"'<>]+?\.(?:mhw|zip|rar|7z)(?:\?[^\s\"'<>]*)?"
    r"|(?:\.\.?/|/)[^\s\"'<>]+?\.(?:mhw|zip|rar|7z)(?:\?[^\s\"'<>]*)?)",
    re.IGNORECASE,
)
JS_REDIRECT_PATTERNS = (
    re.compile(r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", re.I),
    re.compile(r"location\.replace\(\s*['\"]([^'\"]+)['\"]\s*\)", re.I),
    re.compile(r"location\.assign\(\s*['\"]([^'\"]+)['\"]\s*\)", re.I),
)


@dataclass(frozen=True)
class Link:
    url: str
    text: str = ""
    attrs: dict[str, str] | None = None
    score: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Article:
    edition: int
    title: str
    url: str


@dataclass
class DownloadRecord:
    edition: int
    article_title: str
    article_url: str
    source_url: str
    final_url: str
    filename: str
    bytes: int
    sha256: str
    detected_type: str
    extracted_replays: list[str]
    downloaded_at_utc: str


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.meta_refresh_urls: list[str] = []
        self.title = ""
        self._in_title = False
        self._title_text: list[str] = []
        self._current_href: Optional[str] = None
        self._current_attrs: dict[str, str] = {}
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        lower_tag = tag.lower()
        if lower_tag == "a":
            self._current_href = attr_map.get("href", "")
            self._current_attrs = attr_map
            self._current_text = []
        elif lower_tag == "meta":
            equiv = attr_map.get("http-equiv", "").lower()
            content = attr_map.get("content", "")
            if equiv == "refresh" and content:
                match = re.search(r"url\s*=\s*['\"]?([^'\";]+)", content, re.I)
                if match:
                    self.meta_refresh_urls.append(match.group(1).strip())
        elif lower_tag == "title":
            self._in_title = True
            self._title_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)
        if self._in_title:
            self._title_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag == "a" and self._current_href is not None:
            text = " ".join("".join(self._current_text).split())
            self.links.append(Link(self._current_href, text, dict(self._current_attrs)))
            self._current_href = None
            self._current_attrs = {}
            self._current_text = []
        elif lower_tag == "title" and self._in_title:
            self.title = " ".join("".join(self._title_text).split())
            self._in_title = False


class Downloader:
    def __init__(
        self,
        timeout: float = 25.0,
        delay: float = 0.8,
        verbose: bool = True,
        cookie: str = "",
    ) -> None:
        self.timeout = timeout
        self.delay = delay
        self.verbose = verbose
        self.cookie = cookie.strip()
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()), HTTPRedirectHandler())

    def log(self, message: str) -> None:
        if self.verbose:
            print(message, flush=True)

    def _headers(self, referer: Optional[str] = None, binary: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/octet-stream,application/zip,*/*;q=0.8"
                if binary
                else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "close",
        }
        if referer:
            headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def request(self, url: str, referer: Optional[str] = None, binary: bool = False):
        request_url = iri_to_uri(url)
        req = Request(request_url, headers=self._headers(referer, binary=binary))
        try:
            return self.opener.open(req, timeout=self.timeout)
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {url}") from exc
        except URLError as exc:
            raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc

    def fetch_page(self, url: str, referer: Optional[str] = None) -> tuple[str, str, bytes]:
        with self.request(url, referer=referer, binary=False) as response:
            raw = response.read()
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
        text = decode_html(raw, content_type)
        time.sleep(self.delay)
        return final_url, text, raw


def add_query_param(url: str, key: str, value: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault(key, value)
    return urlunparse(parsed._replace(query=urlencode(query)))


def iri_to_uri(url: str) -> str:
    """Convert a human-readable URL containing Unicode into an HTTP-safe URI."""
    parsed = urlparse(url)
    try:
        hostname = (parsed.hostname or "").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"Invalid hostname in URL: {url}") from exc
    if parsed.port:
        hostname += f":{parsed.port}"
    if parsed.username:
        userinfo = quote(parsed.username, safe="")
        if parsed.password:
            userinfo += ":" + quote(parsed.password, safe="")
        hostname = userinfo + "@" + hostname
    return urlunparse(
        parsed._replace(
            netloc=hostname,
            path=quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"),
            query=quote(parsed.query, safe="=&?/:;+,%@!$'()*-._~"),
            fragment="",
        )
    )


def prepare_page_url(url: str) -> str:
    # yzz_no_device helps article pages, but can interfere with old Discuz URLs.
    if urlparse(url).netloc.lower().startswith("bbs."):
        return url
    return add_query_param(url, "yzz_no_device", "1")


def clean_url(raw_url: str, base_url: str) -> Optional[str]:
    if not raw_url:
        return None
    candidate = html.unescape(raw_url.strip()).replace("\\/", "/")
    candidate = candidate.strip(" \t\r\n\"'()[]<>")
    if candidate.lower().startswith(("javascript:", "mailto:", "#")):
        return None
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    absolute = urljoin(base_url, candidate)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return absolute


def decode_html(raw: bytes, content_type: str = "") -> str:
    candidates: list[str] = []
    match = re.search(r"charset\s*=\s*([\w-]+)", content_type, re.I)
    if match:
        candidates.append(match.group(1))
    head = raw[:4096]
    for pattern in (
        rb"<meta[^>]+charset=[\"']?\s*([\w-]+)",
        rb"<meta[^>]+content=[\"'][^\"']*charset=([\w-]+)",
    ):
        match_b = re.search(pattern, head, re.I)
        if match_b:
            candidates.append(match_b.group(1).decode("ascii", "ignore"))
    candidates.extend(["utf-8", "gb18030", "gbk", "big5"])
    seen: set[str] = set()
    for encoding in candidates:
        encoding = encoding.lower()
        if encoding in seen:
            continue
        seen.add(encoding)
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    return raw.decode("utf-8", errors="replace")


def parse_page(text: str) -> PageParser:
    parser = PageParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    return parser


def find_articles(index_html: str, index_url: str) -> list[Article]:
    parser = parse_page(index_html)
    found: dict[tuple[int, str], Article] = {}
    for link in parser.links:
        full_text = " ".join(link.text.split())
        match = ARTICLE_TITLE_RE.search(full_text)
        if not match:
            continue
        url = clean_url(link.url, index_url)
        if not url:
            continue
        edition = int(match.group("edition"))
        found[(edition, url)] = Article(edition=edition, title=full_text, url=url)
    return sorted(found.values(), key=lambda item: item.edition, reverse=True)


def extract_redirects(page_html: str, page_url: str) -> list[str]:
    parser = parse_page(page_html)
    raw_urls: list[str] = list(parser.meta_refresh_urls)
    for pattern in JS_REDIRECT_PATTERNS:
        raw_urls.extend(pattern.findall(page_html))
    for link in parser.links:
        if any(word in link.text for word in ("转向", "跳转", "继续")):
            raw_urls.append(link.url)
    results: list[str] = []
    for raw in raw_urls:
        url = clean_url(raw, page_url)
        if url and url not in results:
            results.append(url)
    return results


def resolve_article_page(client: Downloader, article_url: str, max_hops: int = 5) -> tuple[str, str]:
    current = prepare_page_url(article_url)
    visited: set[str] = set()
    referer: Optional[str] = None
    for _ in range(max_hops):
        if current in visited:
            break
        visited.add(current)
        final_url, page_html, _ = client.fetch_page(current, referer=referer)
        parsed_page = parse_page(page_html)
        title = parsed_page.title.strip()
        # Content pages frequently contain unrelated ad/tracking snippets that
        # assign window.location. Only treat those as a page redirect when the
        # response actually looks like a small redirect shell.
        redirect_shell = (
            any(word in title for word in ("转向", "跳转", "Redirect", "redirect"))
            or len(page_html.encode("utf-8")) < 12_000
        )
        if not redirect_shell:
            return final_url, page_html
        redirects = extract_redirects(page_html, final_url)
        if not redirects:
            return final_url, page_html
        redirects.sort(key=lambda u: (urlparse(u).netloc != urlparse(final_url).netloc, u in visited))
        next_url = next((u for u in redirects if u not in visited), None)
        if not next_url:
            return final_url, page_html
        referer, current = final_url, prepare_page_url(next_url)
    raise RuntimeError(f"Too many or cyclic redirects while opening {article_url}")


def extract_discuz_tid(url: str) -> Optional[str]:
    match = re.search(r"thread-(\d+)-", url)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    return params.get("tid")


def article_variants(url: str) -> list[str]:
    results = [url]
    tid = extract_discuz_tid(url)
    if tid and urlparse(url).netloc.lower().startswith("bbs."):
        root = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}/"
        variants = [
            f"forum.php?mod=viewthread&tid={tid}&mobile=no",
            f"forum.php?mod=viewthread&tid={tid}&mobile=2",
            f"thread-{tid}-1-1.html?mobile=no",
            f"thread-{tid}-1-1.html?mobile=2",
        ]
        for item in variants:
            full = urljoin(root, item)
            if full not in results:
                results.append(full)
    return results


def is_attachment_endpoint(value: str) -> bool:
    lower = html.unescape(value).lower()
    return any(
        token in lower
        for token in (
            "mod=attachment",
            "attachment.php",
            "/attachment/",
            "/attachments/",
            "download.php",
            "aid=",
        )
    )


def has_resource_extension(value: str) -> bool:
    lower = html.unescape(value).lower()
    return any(ext in lower for ext in RESOURCE_EXTENSIONS)


def link_score(url: str, text: str, attrs: dict[str, str], reason: str) -> int:
    combined = " ".join([url, text, attrs.get("title", ""), attrs.get("class", "")]).lower()
    score = 0
    if ".mhw" in combined:
        score += 120
    if any(ext in combined for ext in (".zip", ".rar", ".7z")):
        score += 90
    if is_attachment_endpoint(url):
        score += 55
    if any(word in combined for word in ("录像", "下载", "附件", "武神坛", "wst", "replay")):
        score += 25
    if any(ext in combined for ext in IMAGE_EXTENSIONS):
        score -= 100
    if reason == "raw-direct-resource":
        score += 15
    return score


def candidate_strings(raw_value: str) -> list[str]:
    """Extract URLs embedded in href/onclick/data-* attribute values."""
    value = html.unescape(raw_value).replace("\\/", "/").strip()
    if not value:
        return []
    results = [value]
    # Discuz often wraps attachment URLs inside showWindow('name', 'URL').
    for match in re.finditer(r"['\"]([^'\"]+)['\"]", value):
        part = match.group(1)
        if is_attachment_endpoint(part) or has_resource_extension(part):
            results.append(part)
    return results


def extract_download_links(page_html: str, page_url: str) -> list[Link]:
    parser = parse_page(page_html)
    candidates: list[Link] = []

    for link in parser.links:
        attrs = link.attrs or {}
        raw_values = [
            link.url,
            attrs.get("onclick", ""),
            attrs.get("data-url", ""),
            attrs.get("data-href", ""),
            attrs.get("zoomfile", ""),
            attrs.get("file", ""),
        ]
        for raw_value in raw_values:
            for extracted in candidate_strings(raw_value):
                if not (has_resource_extension(extracted) or is_attachment_endpoint(extracted)):
                    continue
                url = clean_url(extracted, page_url)
                if not url:
                    continue
                reason = "attachment-endpoint" if is_attachment_endpoint(extracted) else "direct-resource"
                candidates.append(
                    Link(
                        url=url,
                        text=link.text,
                        attrs=attrs,
                        score=link_score(url, link.text, attrs, reason),
                        reason=reason,
                    )
                )

    normalized = html.unescape(page_html).replace("\\/", "/")
    for match in DIRECT_RESOURCE_RE.finditer(normalized):
        url = clean_url(match.group("url"), page_url)
        if url:
            candidates.append(
                Link(
                    url=url,
                    text=Path(unquote(urlparse(url).path)).name,
                    score=link_score(url, "", {}, "raw-direct-resource"),
                    reason="raw-direct-resource",
                )
            )

    # Catch unquoted Discuz attachment URLs present in scripts or malformed HTML.
    attachment_patterns = (
        re.compile(r"(?P<url>(?:https?:)?//[^\s\"'<>]+?(?:mod=attachment|attachment\.php)[^\s\"'<>]*)", re.I),
        re.compile(r"(?P<url>(?:forum|home|plugin)\.php\?[^\s\"'<>]*?(?:mod=attachment|aid=)[^\s\"'<>]*)", re.I),
    )
    for pattern in attachment_patterns:
        for match in pattern.finditer(normalized):
            url = clean_url(match.group("url"), page_url)
            if url:
                candidates.append(
                    Link(
                        url=url,
                        text="",
                        score=link_score(url, "", {}, "raw-attachment-endpoint"),
                        reason="raw-attachment-endpoint",
                    )
                )

    unique: dict[str, Link] = {}
    for candidate in candidates:
        url = candidate.url.rstrip(".,;，。；）)]}")
        current = unique.get(url)
        normalized_candidate = Link(
            url=url,
            text=candidate.text,
            attrs=candidate.attrs,
            score=candidate.score,
            reason=candidate.reason,
        )
        if current is None or normalized_candidate.score > current.score:
            unique[url] = normalized_candidate
    return sorted(unique.values(), key=lambda item: (-item.score, item.url))


def extract_nested_replay_pages(page_html: str, page_url: str, edition: int) -> list[Link]:
    """Find stage pages linked by a WST summary page, with the final first."""
    parser = parse_page(page_html)
    candidates: list[Link] = []
    priorities = {
        "决赛": 100,
        "季军": 90,
        "四强": 80,
        "八强": 70,
        "十六强": 60,
    }
    for link in parser.links:
        text = " ".join(link.text.split())
        if "录像" not in text or (edition and str(edition) not in text):
            continue
        url = clean_url(link.url, page_url)
        if not url or not urlparse(url).netloc.lower().startswith("bbs.yzz.cn"):
            continue
        score = max((value for key, value in priorities.items() if key in text), default=10)
        candidates.append(Link(url=url, text=text, score=score, reason="nested-replay-page"))
    unique = {item.url: item for item in candidates}
    return sorted(unique.values(), key=lambda item: (-item.score, item.url))


def safe_filename(name: str, fallback: str = "download") -> str:
    name = html.unescape(unquote(name)).strip().strip(". ")
    name = re.sub(r"[<>:\\/?*|\"\x00-\x1f]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return (name or fallback)[:180]


def fix_header_mojibake(name: str) -> str:
    """Repair filenames whose UTF-8/GBK bytes were decoded as Latin-1.

    http.client decodes header bytes as Latin-1, but Discuz sends raw
    UTF-8 (some forums GBK) in the plain ``filename=`` parameter.
    """
    try:
        raw = name.encode("latin-1")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name


def filename_from_headers(headers: Message, final_url: str, link_text: str = "") -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", disposition, re.I)
    if match:
        return safe_filename(unquote(match.group(1)))
    match = re.search(r"filename\s*=\s*[\"']?([^\"';]+)", disposition, re.I)
    if match:
        return safe_filename(fix_header_mojibake(match.group(1)))
    text_match = re.search(r"([^/\\<>]+?\.(?:mhw|zip|rar|7z))", link_text, re.I)
    if text_match:
        return safe_filename(text_match.group(1))
    path_name = Path(unquote(urlparse(final_url).path)).name
    if path_name and ".php" not in path_name.lower():
        return safe_filename(path_name)
    return "download"


def detect_type(prefix: bytes, content_type: str, filename: str) -> tuple[str, str]:
    lower_name = filename.lower()
    lower_type = content_type.lower()
    if prefix.startswith(b"PK\x03\x04") or "application/zip" in lower_type or lower_name.endswith(".zip"):
        return "zip", ".zip"
    if prefix.startswith(b"Rar!\x1a\x07") or "rar" in lower_type or lower_name.endswith(".rar"):
        return "rar", ".rar"
    if prefix.startswith(b"7z\xbc\xaf\x27\x1c") or "7z" in lower_type or lower_name.endswith(".7z"):
        return "7z", ".7z"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image", ".gif"
    if prefix.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<script")) or "text/html" in lower_type:
        return "html", ".html"
    if lower_name.endswith(".mhw"):
        return "mhw", ".mhw"
    # A Discuz attachment without useful headers is most likely the replay itself.
    return "mhw", ".mhw"


def deduplicate_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create a unique filename for {path}")


def ensure_suffix(filename: str, suffix: str) -> str:
    path = Path(filename)
    if path.suffix.lower() in RESOURCE_EXTENSIONS:
        return filename
    return filename + suffix


def extract_zip_replays(archive: Path, output_dir: Path, limit: int) -> list[str]:
    extracted: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        members = [item for item in zf.infolist() if not item.is_dir() and item.filename.lower().endswith(".mhw")]
        for member in members[:limit]:
            name = safe_filename(Path(member.filename).name, fallback="replay.mhw")
            if not name.lower().endswith(".mhw"):
                name += ".mhw"
            target = deduplicate_path(output_dir / name)
            with zf.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted.append(target.name)
    return extracted


def extract_external_archive(archive: Path, output_dir: Path, limit: int) -> list[str]:
    tool = shutil.which("7z") or shutil.which("7zz")
    if not tool:
        return []
    temp_dir = output_dir / ("_extract_" + archive.stem)
    temp_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [tool, "x", "-y", f"-o{temp_dir}", str(archive)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    extracted: list[str] = []
    for source in sorted(temp_dir.rglob("*.mhw"))[:limit]:
        target = deduplicate_path(output_dir / safe_filename(source.name, "replay.mhw"))
        shutil.move(str(source), target)
        extracted.append(target.name)
    shutil.rmtree(temp_dir, ignore_errors=True)
    return extracted


def download_resource(
    client: Downloader,
    link: Link,
    output_dir: Path,
    referer: str,
    edition: int,
    article_title: str,
    article_url: str,
    replay_limit: int,
) -> DownloadRecord:
    output_dir.mkdir(parents=True, exist_ok=True)
    temp = output_dir / (".partial-" + hashlib.sha1(link.url.encode("utf-8")).hexdigest()[:12])
    with client.request(link.url, referer=referer, binary=True) as response:
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        suggested_name = filename_from_headers(response.headers, final_url, link.text)
        hasher = hashlib.sha256()
        total = 0
        prefix = b""
        with temp.open("wb") as handle:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                if len(prefix) < 1024:
                    prefix += chunk[: 1024 - len(prefix)]
                handle.write(chunk)
                hasher.update(chunk)
                total += len(chunk)

    detected_type, suffix = detect_type(prefix, content_type, suggested_name)
    if total == 0:
        temp.unlink(missing_ok=True)
        raise RuntimeError(f"The attachment endpoint returned an empty file: {link.url}")
    if detected_type == "html":
        diagnostic = output_dir / "_debug_attachment_response.html"
        temp.replace(diagnostic)
        raise RuntimeError(
            "The attachment endpoint returned an HTML page instead of a file. "
            "It may require login/cookies; saved response as " + str(diagnostic)
        )
    if detected_type == "image":
        temp.unlink(missing_ok=True)
        raise RuntimeError("Skipped an image attachment")

    filename = ensure_suffix(safe_filename(suggested_name, "download"), suffix)
    target = deduplicate_path(output_dir / filename)
    temp.replace(target)

    extracted: list[str] = []
    if detected_type == "mhw":
        extracted = [target.name]
    elif detected_type == "zip":
        try:
            extracted = extract_zip_replays(target, output_dir, replay_limit)
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"Downloaded file looks like ZIP but cannot be opened: {target}") from exc
    elif detected_type in ("rar", "7z"):
        extracted = extract_external_archive(target, output_dir, replay_limit)

    return DownloadRecord(
        edition=edition,
        article_title=article_title,
        article_url=article_url,
        source_url=link.url,
        final_url=final_url,
        filename=target.name,
        bytes=total,
        sha256=hasher.hexdigest(),
        detected_type=detected_type,
        extracted_replays=extracted,
        downloaded_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def save_debug_page(output_dir: Path, edition: int, page_url: str, page_html: str) -> Path:
    debug_dir = output_dir / "_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(page_url.encode("utf-8")).hexdigest()[:8]
    path = debug_dir / f"edition_{edition}_{digest}.html"
    path.write_text(page_html, encoding="utf-8", errors="replace")
    return path


def page_diagnostic(page_html: str) -> str:
    parser = parse_page(page_html)
    plain = re.sub(r"<[^>]+>", " ", page_html)
    plain = " ".join(html.unescape(plain).split())
    warnings = []
    for phrase in ("登录后", "无权下载", "访问被拒绝", "验证码", "安全验证", "帖子不存在", "指定的主题不存在"):
        if phrase in plain:
            warnings.append(phrase)
    detail = f"title={parser.title!r}, html_bytes={len(page_html.encode('utf-8'))}"
    if warnings:
        detail += ", detected=" + "/".join(warnings)
    return detail


def discover_articles(client: Downloader) -> list[Article]:
    articles: dict[tuple[int, str], Article] = {}
    errors: list[str] = []
    for index_url in INDEX_URLS:
        try:
            client.log(f"[index] {index_url}")
            final_url, page_html, _ = client.fetch_page(index_url)
            for article in find_articles(page_html, final_url):
                articles[(article.edition, article.url)] = article
        except Exception as exc:
            errors.append(str(exc))
            client.log(f"  skipped: {exc}")
    if not articles:
        details = "\n".join(f"  - {item}" for item in errors)
        raise RuntimeError(f"No Wushentan replay-summary article was found.\n{details}")
    return sorted(articles.values(), key=lambda item: item.edition, reverse=True)


def choose_articles(articles: list[Article], edition: Optional[int]) -> list[Article]:
    if edition is None:
        return articles
    selected = [article for article in articles if article.edition == edition]
    if not selected:
        available = sorted({article.edition for article in articles}, reverse=True)
        raise RuntimeError(f"Edition {edition} was not found. Available recent editions: {available[:10]}")
    return selected


def save_manifest(output_dir: Path, records: list[DownloadRecord]) -> Path:
    manifest_path = output_dir / "manifest.json"
    source_hosts = sorted(
        {
            urlparse(record.source_url).netloc
            for record in records
            if urlparse(record.source_url).netloc
        }
    )
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_hosts": source_hosts,
        "downloads": [asdict(record) for record in records],
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def run_download(args: argparse.Namespace) -> int:
    cookie = args.cookie
    if args.cookie_file:
        cookie_path = Path(args.cookie_file).expanduser()
        try:
            cookie = cookie_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Could not read cookie file: {cookie_path}") from exc
        if not cookie:
            raise RuntimeError(f"Cookie file is empty: {cookie_path}")
    client = Downloader(
        timeout=args.timeout,
        delay=args.delay,
        verbose=not args.quiet,
        cookie=cookie,
    )
    output_dir = Path(args.output).expanduser().resolve()
    automatic_latest = not args.article_url and args.edition is None

    if args.article_url:
        match = re.search(r"第\s*(\d+)\s*联", args.article_title or "")
        edition = args.edition or (int(match.group(1)) if match else 0)
        articles = [Article(edition=edition, title=args.article_title or "Manual article", url=args.article_url)]
    else:
        articles = choose_articles(discover_articles(client), args.edition)
        if automatic_latest:
            # Strict latest mode: do not silently replace an inaccessible new
            # replay with a much older public file.
            articles = articles[:1]

    client.log("\nRecent replay-summary pages:")
    for article in articles[:8]:
        client.log(f"  第{article.edition}联  {article.url}")

    records: list[DownloadRecord] = []
    replay_names: list[str] = []
    failed_links: list[str] = []

    for article in articles:
        if len(replay_names) >= args.count:
            break
        client.log(f"\n[article] 第{article.edition}联: {article.url}")

        best_page_url = ""
        best_page_html = ""
        links: list[Link] = []
        variant_errors: list[str] = []

        for variant in article_variants(article.url):
            try:
                final_page_url, page_html = resolve_article_page(client, variant)
                candidate_links = extract_download_links(page_html, final_page_url)
                if not candidate_links:
                    nested_pages = extract_nested_replay_pages(
                        page_html, final_page_url, article.edition
                    )
                    for nested in nested_pages:
                        client.log(f"  nested page: {nested.text} -> {nested.url}")
                        nested_url, nested_html = resolve_article_page(client, nested.url)
                        nested_links = extract_download_links(nested_html, nested_url)
                        if nested_links:
                            final_page_url = nested_url
                            page_html = nested_html
                            candidate_links = nested_links
                            break
                client.log(
                    f"  page: {final_page_url}\n"
                    f"  diagnostic: {page_diagnostic(page_html)}\n"
                    f"  found {len(candidate_links)} candidate attachment/replay link(s)"
                )
                if args.dry_run:
                    debug_path = save_debug_page(
                        output_dir, article.edition, final_page_url, page_html
                    )
                    client.log(f"  saved dry-run page: {debug_path}")
                if not best_page_html or len(candidate_links) > len(links):
                    best_page_url, best_page_html, links = final_page_url, page_html, candidate_links
                if links:
                    break
            except Exception as exc:
                variant_errors.append(f"{variant}: {exc}")
                client.log(f"  variant failed: {exc}")

        if not links:
            if best_page_html:
                debug_path = save_debug_page(output_dir, article.edition, best_page_url, best_page_html)
                client.log(f"  saved HTML for diagnosis: {debug_path}")
            for item in variant_errors:
                client.log(f"  skipped variant: {item}")
            if args.edition is not None:
                break
            continue

        for link in links:
            if len(replay_names) >= args.count:
                break
            client.log(
                f"  candidate score={link.score} reason={link.reason}: "
                f"{link.text or '(unnamed)'} -> {link.url}"
            )
            if args.dry_run:
                continue
            try:
                remaining = args.count - len(replay_names)
                record = download_resource(
                    client=client,
                    link=link,
                    output_dir=output_dir,
                    referer=best_page_url,
                    edition=article.edition,
                    article_title=article.title,
                    article_url=best_page_url,
                    replay_limit=remaining,
                )
                records.append(record)
                replay_names.extend(record.extracted_replays)
                client.log(
                    f"  saved: {record.filename} ({record.bytes:,} bytes, {record.detected_type})"
                )
                if record.extracted_replays:
                    client.log("  replay file(s): " + ", ".join(record.extracted_replays))
                elif record.detected_type in ("rar", "7z"):
                    client.log(
                        "  archive kept but not extracted. Install 7-Zip/7z, then rerun, "
                        "or extract it manually."
                    )
                time.sleep(client.delay)
            except Exception as exc:
                failed_links.append(f"{link.url}: {exc}")
                client.log(f"  failed: {exc}")

        if args.edition is not None:
            break

    if args.dry_run:
        return 0

    if records:
        manifest = save_manifest(output_dir, records)
        print(f"\nDone: downloaded {len(records)} attachment(s) to {output_dir}")
        print(f"Usable .mhw replay(s): {len(replay_names)}")
        if replay_names:
            for name in replay_names:
                print(f"  - {name}")
        print(f"Manifest: {manifest}")
        # An archive can be a valid result even when automatic extraction is unavailable.
        return 0

    diagnostic = "\n".join(f"  - {item}" for item in failed_links[:12])
    raise RuntimeError(
        "No replay attachment was downloaded. The saved _debug HTML can reveal whether the "
        "forum requires login or changed its attachment markup."
        + (f"\nFailed candidates:\n{diagnostic}" if diagnostic else "")
    )


def self_test() -> int:
    index_html = """
    <html><body>
      <a href='/video/match/202603/1763000.shtml'>〖录像汇总〗第236联武神坛淘汰赛阶段录像</a>
      <a href='/video/match/202603/1762580.shtml'>〖录像汇总〗第235联武神坛淘汰赛阶段录像</a>
    </body></html>
    """
    articles = find_articles(index_html, "https://xyq.yzz.cn/video/")
    assert [a.edition for a in articles] == [236, 235]

    article_html = """
    <a href='//download.example.com/a/final.mhw'>决赛.mhw</a>
    <a class='xw1' href='forum.php?mod=attachment&amp;aid=ABC123'>236联录像合集.zip</a>
    <a onclick="showWindow('attachpay', 'forum.php?mod=attachment&aid=XYZ987')">下载附件</a>
    <a href='forum.php?mod=attachment&amp;aid=IMG1'><img src='photo.jpg'>截图.jpg</a>
    """
    links = extract_download_links(article_html, "http://bbs.yzz.cn/thread-795348-1-1.html")
    urls = {link.url for link in links}
    assert "https://download.example.com/a/final.mhw" in urls
    assert "http://bbs.yzz.cn/forum.php?mod=attachment&aid=ABC123" in urls
    assert "http://bbs.yzz.cn/forum.php?mod=attachment&aid=XYZ987" in urls
    assert links[0].score >= links[-1].score

    assert extract_discuz_tid("http://bbs.yzz.cn/thread-795348-1-1.html") == "795348"
    nested = extract_nested_replay_pages(
        "<a href='thread-3-1-1.html'>梦幻西游武神坛236联 决赛录像</a>"
        "<a href='thread-2-1-1.html'>梦幻西游武神坛236联 八强录像</a>",
        "http://bbs.yzz.cn/thread-1-1-1.html",
        236,
    )
    assert [item.text for item in nested] == [
        "梦幻西游武神坛236联 决赛录像",
        "梦幻西游武神坛236联 八强录像",
    ]
    assert safe_filename("紫禁城:曲阜?.mhw") == "紫禁城_曲阜_.mhw"
    utf8_mojibake = "紫禁城VS曲阜孔庙.mhw".encode("utf-8").decode("latin-1")
    assert fix_header_mojibake(utf8_mojibake) == "紫禁城VS曲阜孔庙.mhw"
    gbk_mojibake = "决赛录像.mhw".encode("gbk").decode("latin-1")
    assert fix_header_mojibake(gbk_mojibake) == "决赛录像.mhw"
    assert fix_header_mojibake("plain-ascii.mhw") == "plain-ascii.mhw"
    headers = Message()
    headers["Content-Disposition"] = (
        'attachment; filename="' + utf8_mojibake + '"'
    )
    assert (
        filename_from_headers(headers, "http://bbs.yzz.cn/forum.php?mod=attachment")
        == "紫禁城VS曲阜孔庙.mhw"
    )
    assert (
        iri_to_uri("https://example.com/录像/紫禁城 VS 曲阜.mhw?名称=决赛")
        == "https://example.com/%E5%BD%95%E5%83%8F/%E7%B4%AB%E7%A6%81%E5%9F%8E%20VS%20%E6%9B%B2%E9%98%9C.mhw?"
        "%E5%90%8D%E7%A7%B0=%E5%86%B3%E8%B5%9B"
    )
    assert detect_type(b"PK\x03\x04", "", "x")[0] == "zip"
    assert detect_type(b"Rar!\x1a\x07", "", "x")[0] == "rar"
    print("Self-test passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download recent Wushentan replay files, including Discuz forum attachments."
    )
    parser.add_argument("--count", type=int, default=1, help="number of usable .mhw files wanted (default: 1)")
    parser.add_argument("--edition", type=int, help="only use this Wushentan edition, e.g. 236")
    parser.add_argument("--output", default="wst_replays", help="download directory (default: wst_replays)")
    parser.add_argument("--timeout", type=float, default=25.0, help="network timeout in seconds")
    parser.add_argument("--delay", type=float, default=0.8, help="polite delay between requests in seconds")
    parser.add_argument("--dry-run", action="store_true", help="show candidate attachment URLs without downloading")
    parser.add_argument("--quiet", action="store_true", help="reduce console output")
    parser.add_argument("--self-test", action="store_true", help="run offline parser tests and exit")
    parser.add_argument("--article-url", help="use a replay-summary article or forum thread URL directly")
    parser.add_argument("--article-title", help="optional title used with --article-url")
    cookie_group = parser.add_mutually_exclusive_group()
    cookie_group.add_argument(
        "--cookie",
        default="",
        help="optional raw Cookie header copied from a logged-in browser if attachments require login",
    )
    cookie_group.add_argument(
        "--cookie-file",
        help="read the raw Cookie header from a UTF-8 file (safer than exposing it on the command line)",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if args.count < 1:
        parser.error("--count must be at least 1")
    try:
        return run_download(args)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        print(
            "Tip: rerun with --dry-run and inspect wst_replays/_debug/. "
            "For edition 236 you can also pass --article-url "
            "http://bbs.yzz.cn/thread-795348-1-1.html",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

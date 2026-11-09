"""RSS 2.0, RSS 1.0 (RDF) and Atom feeds, parsed with the standard library's ElementTree.

Python's XML parser (expat 2.4.1 or newer, bundled with current Python releases) is not vulnerable to the
"billion laughs" and quadratic-blowup entity attacks and does not fetch external entities, so parsing a feed from a
site you do not control is safe enough for a digest. Feeds larger than ``MAX_FEED_BYTES`` are rejected.
"""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from automation.http import HttpClient

MAX_FEED_BYTES = 5_000_000
ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class FeedError(Exception):
    pass


@dataclass(frozen=True)
class FeedItem:
    id: str
    title: str
    link: str | None
    published: datetime | None
    summary: str = ""


@dataclass
class Feed:
    url: str
    title: str
    items: list[FeedItem] = field(default_factory=list)


def plain_text(fragment: str | None, limit: int = 300) -> str:
    """HTML fragment to one line of plain text, cut at ``limit`` characters."""
    if not fragment:
        return ""
    text = _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", fragment))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)  # RFC 822, used by RSS 2.0
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)  # RFC 3339 / ISO 8601, used by Atom and dc:date
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _item_id(guid: str | None, link: str | None, title: str) -> str:
    if guid:
        return guid
    if link:
        return link
    return "sha1:" + hashlib.sha1(title.encode(), usedforsecurity=False).hexdigest()


def parse_feed(xml: bytes | str, url: str = "") -> Feed:
    try:
        root = ET.fromstring(xml)  # noqa: S314  (see the module docstring)
    except ET.ParseError as exc:
        raise FeedError(f"{url or 'feed'}: not valid XML: {exc}") from exc

    if root.tag == f"{ATOM}feed":
        feed = Feed(url, _text(root.find(f"{ATOM}title")) or url)
        for entry in root.findall(f"{ATOM}entry"):
            link_el = entry.find(f"{ATOM}link[@rel='alternate']")
            if link_el is None:
                link_el = entry.find(f"{ATOM}link")
            link = link_el.get("href") if link_el is not None else None
            title = plain_text(_text(entry.find(f"{ATOM}title")), 200) or "(untitled)"
            summary = _text(entry.find(f"{ATOM}summary")) or _text(entry.find(f"{ATOM}content"))
            published = parse_date(
                _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
            )
            item_id = _item_id(_text(entry.find(f"{ATOM}id")), link, title)
            feed.items.append(FeedItem(item_id, title, link, published, plain_text(summary)))
        return feed

    channel = root.find("channel")
    if root.tag == "rss" and channel is not None:  # RSS 2.0
        feed = Feed(url, _text(channel.find("title")) or url)
        for item in channel.findall("item"):
            link = _text(item.find("link"))
            title = plain_text(_text(item.find("title")), 200) or "(untitled)"
            summary = _text(item.find("description")) or _text(item.find(f"{CONTENT}encoded"))
            published = parse_date(_text(item.find("pubDate")) or _text(item.find(f"{DC}date")))
            item_id = _item_id(_text(item.find("guid")), link, title)
            feed.items.append(FeedItem(item_id, title, link, published, plain_text(summary)))
        return feed

    if root.tag.endswith("RDF"):  # RSS 1.0
        rss1_channel = root.find(f"{RSS1}channel")
        feed = Feed(url, _text(rss1_channel.find(f"{RSS1}title")) if rss1_channel is not None else url)
        for item in root.findall(f"{RSS1}item"):
            link = _text(item.find(f"{RSS1}link"))
            title = plain_text(_text(item.find(f"{RSS1}title")), 200) or "(untitled)"
            published = parse_date(_text(item.find(f"{DC}date")))
            feed.items.append(
                FeedItem(
                    _item_id(None, link, title),
                    title,
                    link,
                    published,
                    plain_text(_text(item.find(f"{RSS1}description"))),
                )
            )
        return feed

    raise FeedError(f"{url or 'feed'}: not an RSS or Atom feed (root element {root.tag})")


def fetch_feed(http: HttpClient, url: str) -> Feed:
    response = http.get(
        url, headers={"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9"}
    )
    if len(response.content) > MAX_FEED_BYTES:
        raise FeedError(f"{url}: feed is larger than {MAX_FEED_BYTES} bytes")
    return parse_feed(response.content, url)

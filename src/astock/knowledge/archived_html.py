"""Offline HTML decoding for preserved visual evidence; no collection transport."""

from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser

_ARTICLE_BODY_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "figcaption",
    "figure",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta"}


class _ZhihuArticleHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.capture_depth: int | None = None
        self.captured_once = False
        self.root_closed = False
        self.body_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.canonical_url: str | None = None
        self.title: str | None = None
        self.published_timestamp: float | None = None
        self.updated_timestamp: float | None = None
        self._inside_title = False
        self._title_parts: list[str] = []
        self._suppressed_depth: int | None = None

    @property
    def body_html(self) -> str | None:
        value = "".join(self.body_parts).strip()
        return value or None

    @property
    def visible_character_count(self) -> int:
        return len("".join(self.visible_parts).strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        values = {key.lower(): value for key, value in attrs}
        self._capture_metadata(normalized, values)
        classes = set((values.get("class") or "").split())
        starts_body = (
            not self.captured_once and self.capture_depth is None and "Post-RichText" in classes
        )
        if starts_body:
            self.capture_depth = self.depth
            self.captured_once = True
        elif (
            self.capture_depth is not None
            and self._suppressed_depth is None
            and normalized in {"script", "style", "template", "noscript"}
        ):
            self._suppressed_depth = self.depth
        elif (
            self.capture_depth is not None
            and self._suppressed_depth is None
            and normalized in _ARTICLE_BODY_TAGS
        ):
            self.body_parts.append(_safe_start_tag(normalized, values))
        if normalized not in _VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        values = {key.lower(): value for key, value in attrs}
        self._capture_metadata(normalized, values)
        if self.capture_depth is not None and normalized in _ARTICLE_BODY_TAGS:
            self.body_parts.append(_safe_start_tag(normalized, values))

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized not in _VOID_TAGS:
            self.depth = max(0, self.depth - 1)
        if self._suppressed_depth is not None and self.depth == self._suppressed_depth:
            self._suppressed_depth = None
            return
        if self.capture_depth is not None:
            if self.depth == self.capture_depth:
                self.capture_depth = None
                self.root_closed = True
            elif normalized in _ARTICLE_BODY_TAGS and normalized not in _VOID_TAGS:
                self.body_parts.append(f"</{normalized}>")
        if normalized == "title":
            self._inside_title = False
            if self.title is None:
                fallback = "".join(self._title_parts).strip()
                self.title = fallback or None

    def handle_data(self, data: str) -> None:
        if self.capture_depth is not None and self._suppressed_depth is None:
            self.body_parts.append(escape(data))
            self.visible_parts.append(data)
        if self._inside_title:
            self._title_parts.append(data)

    def _capture_metadata(self, tag: str, attrs: dict[str, str | None]) -> None:
        if tag == "title":
            self._inside_title = True
        if tag == "link" and "canonical" in (attrs.get("rel") or "").lower().split():
            self.canonical_url = attrs.get("href")
        if tag != "meta":
            return
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        value = attrs.get("content")
        if not value:
            return
        if key in {"og:url", "url"} and self.canonical_url is None:
            self.canonical_url = value
        elif key in {"og:title", "twitter:title", "headline"} and self.title is None:
            self.title = value.strip() or None
        elif key in {"article:published_time", "datepublished"}:
            self.published_timestamp = _parse_html_timestamp(value)
        elif key in {"article:modified_time", "datemodified"}:
            self.updated_timestamp = _parse_html_timestamp(value)


def _safe_start_tag(tag: str, attrs: dict[str, str | None]) -> str:
    kept: dict[str, str] = {}
    if tag == "a" and attrs.get("href"):
        kept["href"] = str(attrs["href"])
    if tag == "img":
        source = attrs.get("data-actualsrc") or attrs.get("src")
        if source:
            kept["src"] = source
        if attrs.get("alt"):
            kept["alt"] = str(attrs["alt"])
    rendered = "".join(f' {key}="{escape(value, quote=True)}"' for key, value in kept.items())
    return f"<{tag}{rendered}>"


def _parse_html_timestamp(value: str) -> float | None:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()

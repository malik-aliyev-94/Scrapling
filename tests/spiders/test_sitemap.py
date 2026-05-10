"""Tests for `SitemapParser` and `SitemapSpider`."""

import gzip
import pickle

import pytest

from scrapling.engines.toolbelt.custom import Response
from scrapling.spiders.links import LinkExtractor
from scrapling.spiders.request import Request
from scrapling.spiders.sitemap import SitemapParser, SitemapResult, SitemapSpider, SitemapUrl
from scrapling.spiders.templates import CrawlRule
from scrapling.core._types import Any, AsyncGenerator, Dict, Union


URLSET_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/posts/1</loc>
    <lastmod>2026-01-15</lastmod>
    <changefreq>daily</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://example.com/posts/2</loc>
    <lastmod>2026-02-20</lastmod>
  </url>
  <url>
    <loc>https://example.com/about</loc>
  </url>
</urlset>
"""

URLSET_WITH_ALTERNATES = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>https://example.com/en/page</loc>
    <xhtml:link rel="alternate" hreflang="fr" href="https://example.com/fr/page"/>
    <xhtml:link rel="alternate" hreflang="de" href="https://example.com/de/page"/>
  </url>
</urlset>
"""

INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/posts-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://example.com/products-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://example.com/skip-sitemap.xml</loc></sitemap>
</sitemapindex>
"""

# Sitemap without the standard namespace (some sites do this)
URLSET_NO_NS = b"""<?xml version="1.0"?>
<urlset>
  <url><loc>https://example.com/x</loc></url>
</urlset>
"""


class TestSitemapParserUrlset:
    def test_parse_urlset_with_full_metadata(self):
        result = SitemapParser().parse(URLSET_XML)
        assert len(result.urls) == 3
        assert result.sitemaps == []
        first = result.urls[0]
        assert first.loc == "https://example.com/posts/1"
        assert first.lastmod == "2026-01-15"
        assert first.changefreq == "daily"
        assert first.priority == 0.8

    def test_parse_handles_partial_metadata(self):
        result = SitemapParser().parse(URLSET_XML)
        second = result.urls[1]
        assert second.lastmod == "2026-02-20"
        assert second.changefreq is None
        assert second.priority is None

    def test_parse_handles_no_namespace(self):
        result = SitemapParser().parse(URLSET_NO_NS)
        assert len(result.urls) == 1
        assert result.urls[0].loc == "https://example.com/x"


class TestSitemapParserAlternates:
    def test_alternate_links_off_by_default(self):
        result = SitemapParser().parse(URLSET_WITH_ALTERNATES)
        assert result.urls[0].alternates == []

    def test_alternate_links_on_collects_them(self):
        result = SitemapParser(alternate_links=True).parse(URLSET_WITH_ALTERNATES)
        assert result.urls[0].alternates == [
            "https://example.com/fr/page",
            "https://example.com/de/page",
        ]


class TestSitemapParserIndex:
    def test_parse_sitemapindex_returns_child_sitemaps(self):
        result = SitemapParser().parse(INDEX_XML)
        assert result.urls == []
        assert result.sitemaps == [
            "https://example.com/posts-sitemap.xml",
            "https://example.com/products-sitemap.xml",
            "https://example.com/skip-sitemap.xml",
        ]


class TestSitemapParserDecompression:
    def test_gz_body_via_magic_bytes(self):
        compressed = gzip.compress(URLSET_XML)
        result = SitemapParser().parse(compressed)
        assert len(result.urls) == 3

    def test_gz_body_via_content_type_hint(self):
        compressed = gzip.compress(URLSET_XML)
        result = SitemapParser().parse(compressed, content_type="application/x-gzip")
        assert len(result.urls) == 3

    def test_corrupt_gz_logged_not_raised(self):
        # Body starts with gzip magic but is not valid gzip
        body = b"\x1f\x8b" + b"junk data"
        result = SitemapParser().parse(body)
        assert result == SitemapResult()


class TestSitemapParserMalformed:
    def test_invalid_xml_returns_empty_result(self):
        result = SitemapParser().parse(b"<not valid xml")
        assert result == SitemapResult()

    def test_unknown_root_returns_empty_result(self):
        result = SitemapParser().parse(b"<?xml version='1.0'?><foo><bar/></foo>")
        assert result == SitemapResult()


class TestFromRobotsTxt:
    def test_extracts_sitemap_directives(self):
        body = """
        User-agent: *
        Disallow: /admin
        Sitemap: https://example.com/sitemap.xml
        Sitemap: https://example.com/posts-sitemap.xml
        """
        urls = SitemapParser.from_robots_txt(body)
        assert urls == [
            "https://example.com/sitemap.xml",
            "https://example.com/posts-sitemap.xml",
        ]

    def test_ignores_comments_and_blank_lines(self):
        body = """
        # This is a comment
        Sitemap: https://example.com/sitemap.xml  # inline comment
        # Sitemap: https://commented.example.com/sitemap.xml
        """
        urls = SitemapParser.from_robots_txt(body)
        assert urls == ["https://example.com/sitemap.xml"]

    def test_directive_match_is_case_insensitive(self):
        body = "SITEMAP: https://example.com/sitemap.xml\nsitemap: https://example.com/other.xml"
        urls = SitemapParser.from_robots_txt(body)
        assert urls == [
            "https://example.com/sitemap.xml",
            "https://example.com/other.xml",
        ]

    def test_returns_empty_when_no_directives(self):
        body = "User-agent: *\nDisallow: /admin"
        urls = SitemapParser.from_robots_txt(body)
        assert urls == []


def _make_response(body: bytes, url: str = "https://example.com/sitemap.xml", headers: dict | None = None) -> Response:
    resp = Response(
        url=url,
        content=body,
        status=200,
        reason="OK",
        cookies={},
        headers=headers or {},
        request_headers={},
    )
    resp.request = Request(url, sid="default")
    return resp


async def _collect(agen: AsyncGenerator) -> list:
    return [item async for item in agen]


class TestSitemapSpiderFlow:
    @pytest.mark.asyncio
    async def test_urlset_dispatched_through_rules(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml"]

            def rules(self):
                return [CrawlRule(LinkExtractor(allow=r"/posts/"), callback=self.parse_post)]

            async def parse_post(self, response):
                yield {"post": response.url}

        spider = S()
        out = await _collect(spider._parse_sitemap(_make_response(URLSET_XML)))
        post_reqs = [r for r in out if "/posts/" in r.url]
        about_reqs = [r for r in out if "/about" in r.url]
        # Two posts dispatched to parse_post; about falls through (callback inherited from None → None)
        assert len(post_reqs) == 2
        assert all(r.callback == spider.parse_post for r in post_reqs)
        assert len(about_reqs) == 1
        assert about_reqs[0].callback is None

    @pytest.mark.asyncio
    async def test_no_rules_means_all_urls_fall_through(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml"]

        spider = S()
        out = await _collect(spider._parse_sitemap(_make_response(URLSET_XML)))
        assert len(out) == 3
        assert all(r.callback is None for r in out)

    @pytest.mark.asyncio
    async def test_sitemapindex_descends_into_children(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml"]

        spider = S()
        out = await _collect(spider._parse_sitemap(_make_response(INDEX_XML)))
        # No urls in this index, just three child sitemap fetches
        assert len(out) == 3
        assert all(r.callback == spider._parse_sitemap for r in out)
        assert {r.url for r in out} == {
            "https://example.com/posts-sitemap.xml",
            "https://example.com/products-sitemap.xml",
            "https://example.com/skip-sitemap.xml",
        }

    @pytest.mark.asyncio
    async def test_sitemap_follow_filters_child_sitemaps(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml"]
            sitemap_follow = LinkExtractor(allow=r"posts-sitemap")

        spider = S()
        out = await _collect(spider._parse_sitemap(_make_response(INDEX_XML)))
        assert {r.url for r in out} == {"https://example.com/posts-sitemap.xml"}

    @pytest.mark.asyncio
    async def test_alternate_links_dispatched_when_enabled(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml"]
            sitemap_alternate_links = True

        spider = S()
        out = await _collect(spider._parse_sitemap(_make_response(URLSET_WITH_ALTERNATES)))
        urls = {r.url for r in out}
        assert urls == {
            "https://example.com/en/page",
            "https://example.com/fr/page",
            "https://example.com/de/page",
        }

    @pytest.mark.asyncio
    async def test_gzipped_sitemap_handled_via_magic_bytes(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml.gz"]

        spider = S()
        body = gzip.compress(URLSET_XML)
        out = await _collect(spider._parse_sitemap(_make_response(body, url="https://example.com/sitemap.xml.gz")))
        assert len(out) == 3


class TestSitemapSpiderStartRequests:
    @pytest.mark.asyncio
    async def test_start_requests_uses_sitemap_urls(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://a.com/s.xml", "https://b.com/s.xml"]

        spider = S()
        out = [req async for req in spider.start_requests()]
        assert {r.url for r in out} == {"https://a.com/s.xml", "https://b.com/s.xml"}
        assert all(r.callback == spider._parse_sitemap for r in out)

    @pytest.mark.asyncio
    async def test_start_requests_falls_back_to_robots_txt(self):
        class S(SitemapSpider):
            name = "s"
            allowed_domains = {"example.com"}

        spider = S()
        out = [req async for req in spider.start_requests()]
        assert len(out) == 1
        assert out[0].url == "https://example.com/robots.txt"
        assert out[0].callback == spider._parse_robots

    @pytest.mark.asyncio
    async def test_start_requests_uses_start_urls_if_no_sitemap_urls(self):
        class S(SitemapSpider):
            name = "s"
            start_urls = ["https://example.com/seed"]

        spider = S()
        out = [req async for req in spider.start_requests()]
        assert len(out) == 1
        assert out[0].url == "https://example.com/seed"
        # Should NOT have _parse_sitemap as callback (start_urls path treats them as regular pages)
        assert out[0].callback is None

    @pytest.mark.asyncio
    async def test_start_requests_raises_when_nothing_configured(self):
        class S(SitemapSpider):
            name = "s"

        spider = S()
        with pytest.raises(RuntimeError, match="needs `sitemap_urls`"):
            [req async for req in spider.start_requests()]


class TestParseRobots:
    @pytest.mark.asyncio
    async def test_parse_robots_yields_sitemap_requests(self):
        class S(SitemapSpider):
            name = "s"
            allowed_domains = {"example.com"}

        spider = S()
        body = b"User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        resp = _make_response(body, url="https://example.com/robots.txt")
        out = await _collect(spider._parse_robots(resp))
        assert len(out) == 1
        assert out[0].url == "https://example.com/sitemap.xml"
        assert out[0].callback == spider._parse_sitemap

    @pytest.mark.asyncio
    async def test_parse_robots_with_no_directives_warns(self):
        # Spider's logger has propagate=False, so we attach our own handler to it.
        import logging

        class S(SitemapSpider):
            name = "s"
            allowed_domains = {"example.com"}

        spider = S()
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        spider.logger.addHandler(_Capture())

        body = b"User-agent: *\nDisallow: /\n"
        resp = _make_response(body, url="https://example.com/robots.txt")
        out = await _collect(spider._parse_robots(resp))
        assert out == []
        assert any("No Sitemap:" in r.getMessage() for r in records if r.levelno == logging.WARNING)


class TestSitemapSpiderPickle:
    @pytest.mark.asyncio
    async def test_pickle_request_with_bound_method_callback_via_rules(self):
        class S(SitemapSpider):
            name = "s"
            sitemap_urls = ["https://example.com/sitemap.xml"]

            def rules(self):
                return [CrawlRule(LinkExtractor(allow=r"/posts/"), callback=self.parse_post)]

            async def parse_post(self, response):
                yield {"post": response.url}

        spider = S()
        out = await _collect(spider._parse_sitemap(_make_response(URLSET_XML)))
        post_req = next(r for r in out if "/posts/" in r.url)
        state = post_req.__getstate__()
        assert state["_callback_name"] == "parse_post"
        # Round-trip
        pickled = pickle.dumps(post_req)
        restored = pickle.loads(pickled)
        fresh = S()
        restored._restore_callback(fresh)
        assert restored.callback == fresh.parse_post

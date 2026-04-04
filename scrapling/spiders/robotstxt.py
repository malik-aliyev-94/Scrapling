from urllib.parse import urlparse

from anyio import create_task_group
from protego import Protego

from scrapling.core._types import Dict, Optional, Callable, Awaitable
from scrapling.core.utils import log


class RobotsTxtManager:
    """Manages fetching, parsing, and caching of robots.txt files.

    Accepts a fetch callable ``(url: str, sid: str) -> Awaitable[Response]``
    so it stays decoupled from any specific session or transport layer.

    All public methods accept only ``(url, sid)`` — domain and scheme are
    derived internally from the URL so callers don't pass redundant data.

    Handles all standard robots.txt directives including:
    - User-agent specific rules
    - Allow/Disallow directives (including wildcards and $ anchors)
    - Crawl-delay directives

    robots.txt is a domain-level document and does not vary by session, so the
    cache is keyed by domain only. The ``sid`` parameter on public methods
    controls which session is used for the initial fetch if the domain is not
    yet cached, but all sessions share the same parsed result afterwards.
    """

    def __init__(self, fetch_fn: Callable[[str, str], Awaitable]):
        self._fetch_fn = fetch_fn
        self._cache: Dict[str, Protego] = {}

    async def _get_parser(self, url: str, sid: str) -> Protego:
        parsed = urlparse(url)
        domain = parsed.netloc

        if domain in self._cache:
            return self._cache[domain]

        scheme = parsed.scheme or "https"
        robots_url = f"{scheme}://{domain}/robots.txt"
        content = ""
        try:
            response = await self._fetch_fn(robots_url, sid)
            if response.status == 200:
                content = response.body.decode(response.encoding, errors="replace")
        except Exception as e:
            log.warning(f"Failed to fetch robots.txt for {domain}: {e}")

        try:
            parser = Protego.parse(content)
        except Exception as e:
            log.warning(f"Failed to parse robots.txt for {domain}: {e}")
            parser = Protego.parse("")

        self._cache[domain] = parser
        return parser

    async def can_fetch(self, url: str, sid: str) -> bool:
        """Check if a URL can be fetched according to the domain's robots.txt.

        Handles:
        - User-agent specific rules (e.g., User-agent: SpinarakBot)
        - Wildcard user-agent rules (User-agent: *)
        - Allow/Disallow directives with wildcards (e.g., /*.pdf$)
        - Allow directives that override Disallow (e.g., Allow: /admin/public-docs/)

        Uses the wildcard user-agent (*) which matches standard robots.txt directives
        that apply to all bots. This is the conservative approach — if a URL is
        disallowed for all bots, we respect that.

        Args:
            url: The full URL to check
            sid: Session ID for fetching robots.txt if not yet cached

        Returns:
            True if the URL can be fetched, False otherwise
        """
        parser = await self._get_parser(url, sid)
        return parser.can_fetch(url, "*")

    async def get_crawl_delay(self, url: str, sid: str) -> Optional[float]:
        """Get the crawl delay for this crawler.

        Uses the wildcard user-agent (*) to get the general crawl delay
        that applies to all bots.

        Args:
            url: Any URL on the domain to check
            sid: Session ID for fetching robots.txt if not yet cached

        Returns:
            The crawl delay in seconds, or None if not specified
        """
        parser = await self._get_parser(url, sid)
        delay = parser.crawl_delay("*")
        return float(delay) if delay is not None else None

    async def get_request_rate(self, url: str, sid: str) -> Optional[tuple[int, int]]:
        """Get the request rate for this crawler.

        Uses the wildcard user-agent (*) to get the general request rate
        that applies to all bots.

        Args:
            url: Any URL on the domain to check
            sid: Session ID for fetching robots.txt if not yet cached

        Returns:
            A tuple of (requests, seconds) if specified, or None if not specified
        """
        parser = await self._get_parser(url, sid)
        rate = parser.request_rate("*")
        if rate is not None:
            return (rate.requests, rate.seconds)
        return None

    async def get_delay_directives(self, url: str, sid: str) -> tuple[Optional[float], Optional[tuple[int, int]]]:
        """Return both crawl-delay and request-rate in a single parser lookup.

        Args:
            url: Any URL on the domain to check
            sid: Session ID for fetching robots.txt if not yet cached

        Returns:
            A tuple of (crawl_delay, request_rate) where crawl_delay is in seconds
            or None, and request_rate is (requests, seconds) or None.
        """
        parser = await self._get_parser(url, sid)
        c_delay = parser.crawl_delay("*")
        rate = parser.request_rate("*")
        return (
            float(c_delay) if c_delay is not None else None,
            (rate.requests, rate.seconds) if rate is not None else None,
        )

    async def prefetch(self, urls: list[str], sid: str) -> None:
        """Pre-warm the robots.txt cache for a list of seed URLs concurrently.

        Callers are responsible for deduplicating URLs by domain before calling
        this method — passing multiple URLs for the same domain will trigger
        redundant fetches since no inflight deduplication exists here.

        Args:
            urls: Seed URLs whose domains should be pre-fetched (one per domain).
            sid: Session ID to use for the robots.txt fetch requests.
        """
        if not urls:
            return
        log.debug(f"Pre-fetching robots.txt for {len(urls)} domain(s)")
        async with create_task_group() as tg:
            for url in urls:
                tg.start_soon(self._get_parser, url, sid)

    def clear_cache(self, domain: Optional[str] = None) -> None:
        """Clear the robots.txt cache.

        Note: the ``sid`` parameter was removed — the cache is now keyed by
        domain only, so clearing a domain evicts all sessions at once.

        Args:
            domain: If specified, only clear cache for this domain.
                    If None, clears the entire cache.
        """
        if domain is None:
            self._cache.clear()
        else:
            self._cache.pop(domain, None)

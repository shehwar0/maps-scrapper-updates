import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
import requests
from bs4 import BeautifulSoup, FeatureNotFound

# Production boosters
try:
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    from fake_useragent import UserAgent
    _TENACITY_AVAILABLE = True
    _UA = UserAgent()
except Exception:
    _TENACITY_AVAILABLE = False
    _UA = None
    def retry(*a, **k): return lambda f: f  # no-op fallback
    def stop_after_attempt(n): return None
    def wait_exponential(*a, **k): return None
    def retry_if_exception_type(*a): return None

# Reduce noise when scraping sites with broken TLS (we intentionally allow verify=False)
try:  # pragma: no cover
    import warnings
    from urllib3.exceptions import InsecureRequestWarning

    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
except Exception:
    pass

try:
    from selectolax.parser import HTMLParser
except Exception:  # pragma: no cover
    HTMLParser = None

try:
    import trafilatura
except Exception:
    trafilatura = None

try:
    import orjson as _json

    def _loads_json(s: str):
        return _json.loads(s)
except Exception:  # pragma: no cover
    import json as _json

    def _loads_json(s: str):
        return _json.loads(s)


EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}", re.IGNORECASE)
# Common obfuscations: name (at) domain (dot) tld
OBFUSCATED_EMAIL_PATTERN = re.compile(
    r"([a-zA-Z0-9._%+-]{1,64})\s*(?:\(|\[)?\s*(?:at|\@)\s*(?:\)|\])?\s*"
    r"([a-zA-Z0-9.-]{1,253})\s*(?:\(|\[)?\s*(?:dot|\.)\s*(?:\)|\])?\s*"
    r"([a-zA-Z]{2,24})",
    re.IGNORECASE,
)
MAILTO_PATTERN = re.compile(r"mailto:([^\"'>\s?#]+)", re.IGNORECASE)
DATA_EMAIL_PATTERN = re.compile(
    r"(?:data-email|data-mail|data-contact|data-mailto)\s*=\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)
DATA_USER_DOMAIN_PATTERN = re.compile(
    r"data-user\s*=\s*['\"]([^'\"]{1,64})['\"][^>]{0,200}data-domain\s*=\s*['\"]([^'\"]{1,253})['\"]",
    re.IGNORECASE,
)
DATA_DOMAIN_USER_PATTERN = re.compile(
    r"data-domain\s*=\s*['\"]([^'\"]{1,253})['\"][^>]{0,200}data-user\s*=\s*['\"]([^'\"]{1,64})['\"]",
    re.IGNORECASE,
)
JS_EMAIL_JOIN_PATTERN = re.compile(
    r"['\"]([a-zA-Z0-9._%+-]{1,64})['\"]\s*\+\s*['\"]@['\"]\s*\+\s*['\"]([a-zA-Z0-9.-]{1,253}\.[a-zA-Z]{2,24})['\"]",
    re.IGNORECASE,
)

INVALID_EMAIL_DOMAINS = {
    "example.com",
    "test.com",
    "email.com",
    "domain.com",
    "yoursite.com",
    "website.com",
    "company.com",
    "business.com",
    "mail.com",
    "fake.com",
    "sample.com",
    "demo.com",
    "sentry.io",
    "wixpress.com",
    "sentry-next.wixpress.com",
}
INVALID_EMAIL_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "css", "js", "pdf"}
NO_REPLY_LOCAL_PARTS = {"noreply", "no-reply", "do-not-reply", "donotreply", "mailer-daemon"}

WHATSAPP_LINK_PATTERN = re.compile(r"(?:wa\.me/|phone=)(\+?\d{6,15})", re.IGNORECASE)
WHATSAPP_REF_PATTERN = re.compile(
    r"(?:https?:\\?/\\?/(?:wa\.me|api\.whatsapp\.com|chat\.whatsapp\.com)[^\"'\s<]*)|(?:whatsapp:\\\?/\\?/send\?[^\"'\s<]*)",
    re.IGNORECASE,
)
GENERIC_PHONE_PATTERN = re.compile(r"\+?\d[\d\s().-]{6,}\d")
DIGIT_PATTERN = re.compile(r"\d+")

# Prioritized internal pages and keywords for deep enrichment
# Expanded for VERY POWERFUL web scraping
PRIMARY_PATHS = [
    "", "/contact", "/contact-us", "/contactus", "/about", "/about-us", "/aboutus",
    "/team", "/reach-us", "/get-in-touch", "/connect", "/locations", "/branches",
]

DEFAULT_PATHS = [
    "", "/contact", "/contact-us", "/contactus", "/about", "/about-us", "/aboutus",
    "/team", "/reach-us", "/get-in-touch", "/connect", "/support", "/help",
    "/customer-service", "/locations", "/branches", "/stores", "/privacy", "/terms",
    "/legal", "/impressum", "/kontakt", "/contacto", "/info", "/services",
]

PRIORITY_LINK_KEYWORDS = (
    "contact", "about", "team", "support", "help", "email", "sales", "privacy",
    "terms", "legal", "impressum", "whatsapp", "location", "branch", "store",
    "service", "staff", "people", "directory", "find-us", "get-in-touch",
)

# Extra powerful patterns for business intel
DESCRIPTION_PATTERNS = [
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
    r'<p[^>]{0,100}class=["\'][^"\']*(?:about|intro|description|summary)[^"\']*["\'][^>]*>(.*?)</p>',
]

SERVICE_KEYWORDS = ["service", "services", "offer", "we provide", "our work", "solutions"]


@dataclass
class CrawledPage:
    url: str
    html: str


class WebsiteExtractor:
    """Fast website enrichment with bounded deep crawling.

    Backwards compatible: keep enrich() output keys stable.
    """

    def __init__(self, timeout: int = 12) -> None:
        self.timeout = timeout

        # Rotate realistic UAs (fake-useragent dramatically reduces blocks -> faster effective throughput)
        ua_str = _UA.random if _UA else (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        self._requests_session = requests.Session()
        self._requests_session.headers.update({"User-Agent": ua_str})

        self._httpx = httpx.Client(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(min(self.timeout, 14), connect=min(5, self.timeout)),
            headers={
                "User-Agent": ua_str,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=12, max_keepalive_connections=6),
            verify=False,
        )

        self._html_cache: Dict[str, str] = {}
        self._cache_fifo: List[str] = []
        self._max_cache_entries = 64
        self._host_denials: Dict[str, int] = {}
        self._blocked_hosts: Set[str] = set()
        self._host_block_threshold = 3

    def enrich(
        self,
        website_url: str,
        fallback_phone: str = "",
        max_pages: int = 18,  # Increased for very powerful web scraping
        max_total_time_sec: int = 35,
        priority_only: bool = False,
        use_playwright: bool = True,  # Powerful: use browser for JS-heavy sites
    ) -> Dict[str, Any]:
        """
        VERY POWERFUL web enrichment.
        Returns rich dict: email, whatsapp, all_emails, all_phones, socials, description,
        address, services, etc.
        Uses fast HTTP + deep Playwright when needed.
        """
        if not website_url:
            return self._empty_result(fallback_phone)

        normalized = self._normalize_url(website_url)
        if not normalized:
            return self._empty_result(fallback_phone)

        base_domain = self._get_base_domain(normalized)

        # Phase 1: Fast HTTP crawl (always)
        pages = self.crawl_pages(
            normalized,
            max_pages=max_pages,
            max_total_time_sec=max_total_time_sec,
            priority_only=priority_only,
        )

        # Phase 2: Powerful Playwright fallback / enhancement for key pages (if enabled and time left)
        if use_playwright and pages:
            try:
                pw_pages = self._playwright_crawl_powerful(normalized, max_pages=6, max_time_sec=18)
                pages.extend(pw_pages)
            except Exception:
                pass  # graceful

        if not pages:
            return self._empty_result(fallback_phone)

        # Aggregate from all pages (VERY DEEP POWERFUL)
        all_emails: List[str] = []
        all_phones: List[str] = []
        all_whatsapp: List[str] = []
        socials: Dict[str, str] = {}
        descriptions: List[str] = []
        addresses: List[str] = []
        services: List[str] = []
        structured_services = []
        hours = ""

        corpus = "\n\n".join(p.html for p in pages)

        for page in pages:
            html = page.html
            all_emails.extend(self._extract_emails(html))
            phones = self._extract_phones_deep(html)
            all_phones.extend(phones)
            all_whatsapp.extend(self._extract_whatsapp_numbers(html))

            # Use trafilatura for superior main content extraction if available (huge power boost for business info)
            clean_text = ""
            if trafilatura:
                try:
                    clean_text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
                except Exception:
                    clean_text = html
            else:
                clean_text = html

            desc = self._extract_description(clean_text) or self._extract_description(html)
            if desc:
                descriptions.append(desc)
            addr = self._extract_address(clean_text) or self._extract_address(html)
            if addr:
                addresses.append(addr)
            svcs = self._extract_services(clean_text) or self._extract_services(html)
            services.extend(svcs)

            h = self._extract_hours_deep(clean_text) or self._extract_hours_deep(html)
            if h:
                hours = h

            page_socials = self._extract_socials(html)
            for k, v in page_socials.items():
                if k not in socials:
                    socials[k] = v

            # Structured deep data
            struct = self._extract_structured_data(html)
            if struct.get("description"):
                descriptions.append(struct["description"])
            if struct.get("address"):
                addresses.append(struct["address"])
            if struct.get("services"):
                structured_services.extend(struct.get("services", []))
            if struct.get("phone"):
                all_phones.append(struct["phone"])
            if struct.get("email"):
                all_emails.append(struct["email"])
            if struct.get("hours"):
                hours = struct["hours"]

        # Dedup + rank powerfully
        emails = self._rank_emails(list(dict.fromkeys(all_emails)), base_domain)
        phones = list(dict.fromkeys([p for p in all_phones if len(p.replace('+','')) >= 8]))[:8]
        whatsapp = list(dict.fromkeys(all_whatsapp))[:5]
        services = list(dict.fromkeys(services + structured_services))[:10]

        best_email = emails[0] if emails else ""
        best_whatsapp = whatsapp[0] if whatsapp else self._normalize_phone(fallback_phone)

        return {
            "email": best_email,
            "whatsapp": best_whatsapp,
            "all_emails": "; ".join(emails[:8]),
            "all_phones": "; ".join(phones[:6]),
            "all_whatsapp": "; ".join(whatsapp),
            "socials": socials,
            "description": " | ".join([d for d in descriptions if d][:2]),
            "address_from_site": addresses[0] if addresses else "",
            "services": ", ".join(services),
            "pages_crawled": len(pages),
            "structured_data_found": bool(structured_services or any(addresses)),
            # Deep web fields
            "about_text": " | ".join([d for d in descriptions if d][:3]),
            "full_services": services,
            "hours": hours or "",
        }

    def _empty_result(self, fallback_phone: str) -> Dict[str, Any]:
        return {
            "email": "", "whatsapp": self._normalize_phone(fallback_phone),
            "all_emails": "", "all_phones": "", "all_whatsapp": "",
            "socials": {}, "description": "", "address_from_site": "", "services": "",
            "pages_crawled": 0,
        }

    def _playwright_crawl_powerful(self, base_url: str, max_pages: int = 6, max_time_sec: int = 18) -> List[CrawledPage]:
        """DEEP POWERFUL Playwright: stealth, resource block, JS render, extract hidden/dynamic content from business sites."""
        from playwright.sync_api import sync_playwright
        crawled: List[CrawledPage] = []
        start = time.time()

        contact_pages = ["/contact", "/contact-us", "/about", "/about-us", "/team", ""]

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"])
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()

                # Deep stealth from research
                page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                    window.chrome = { runtime: {} };
                """)

                # Block heavy for speed (research best practice)
                page.route("**/*", lambda route, req: route.abort() if req.resource_type in ["image", "font", "media"] else route.continue_())

                for path in contact_pages:
                    if time.time() - start > max_time_sec or len(crawled) >= max_pages:
                        break
                    url = urljoin(base_url + "/", path.lstrip("/"))
                    try:
                        page.goto(url, timeout=15000, wait_until="networkidle")
                        page.wait_for_timeout(1200)  # deep JS settle + human
                        # Simulate human scroll
                        page.evaluate("window.scrollBy(0, 300)")
                        html = page.content()
                        if html and len(html) > 800:
                            crawled.append(CrawledPage(url=url, html=html))
                    except Exception:
                        continue

                context.close()
                browser.close()
        except Exception:
            pass

        return crawled

    def _extract_all_phones(self, html: str) -> List[str]:
        """Extract all phone numbers (more aggressive)."""
        phones = []
        for m in GENERIC_PHONE_PATTERN.finditer(html):
            cleaned = re.sub(r"[^\d+]", "", m.group(0))
            if 8 <= len(cleaned.replace("+", "")) <= 15:
                phones.append(cleaned)
        return list(dict.fromkeys(phones))[:10]

    def _extract_socials(self, html: str) -> Dict[str, str]:
        socials = {}
        patterns = {
            "instagram": r'https?://(?:www\.)?instagram\.com/([a-zA-Z0-9_.]+)/?',
            "facebook": r'https?://(?:www\.)?facebook\.com/([a-zA-Z0-9./_-]+)/?',
            "linkedin": r'https?://(?:www\.)?linkedin\.com/(?:company|in)/([a-zA-Z0-9_-]+)/?',
            "twitter": r'https?://(?:www\.)?(?:twitter|x)\.com/([a-zA-Z0-9_]+)/?',
            "tiktok": r'https?://(?:www\.)?tiktok\.com/@([a-zA-Z0-9_.]+)/?',
            "youtube": r'https?://(?:www\.)?youtube\.com/(?:@|channel/|c/)([a-zA-Z0-9_-]+)/?',
        }
        for platform, pat in patterns.items():
            m = re.search(pat, html, re.I)
            if m:
                socials[platform] = m.group(0).split("?")[0]
        return socials

    def _extract_description(self, html: str) -> str:
        for pat in DESCRIPTION_PATTERNS:
            m = re.search(pat, html, re.I | re.S)
            if m:
                text = re.sub(r"<[^>]+>", " ", m.group(1)).strip()
                if len(text) > 30:
                    return text[:280]
        return ""

    def _extract_address(self, html: str) -> str:
        # Look for schema or common address blocks
        patterns = [
            r'"address":\s*\{[^}]*"streetAddress":\s*"([^"]+)"',
            r'<span[^>]*itemprop=["\']streetAddress["\'][^>]*>(.*?)</span>',
            r'(?:address|location|find us)[^<]{0,60}<[^>]+>([^<]{10,120})',
        ]
        for p in patterns:
            m = re.search(p, html, re.I | re.S)
            if m:
                return re.sub(r"<[^>]+>", "", m.group(1)).strip()[:160]
        return ""

    def _extract_services(self, html: str) -> List[str]:
        services = []
        lower = html.lower()
        for kw in SERVICE_KEYWORDS:
            if kw in lower:
                matches = re.findall(r'([A-Z][a-z]+(?:\s+[A-Z]?[a-z]+){1,4})', html)
                services.extend(matches[:5])
        # Deep: look for list items or headings with service-like
        for m in re.finditer(r'<(li|h[1-6])[^>]*>(.*?)</\1>', html, re.I | re.S):
            text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            if text and len(text) > 3 and len(text) < 100:
                services.append(text)
        return list(dict.fromkeys(services))[:10]

    def _extract_hours_deep(self, html: str) -> str:
        # Look for common hours patterns
        patterns = [
            r'(?:hours|opening|open)\s*[:\-]?\s*([^<]{10,200})',
            r'itemprop=["\']openingHours["\'][^>]*>([^<]+)<',
            r'<time[^>]*>([^<]+)</time>',
        ]
        for p in patterns:
            m = re.search(p, html, re.I | re.S)
            if m:
                return re.sub(r'\s+', ' ', m.group(1)).strip()[:150]
        return ""

    def _extract_structured_data(self, html: str) -> Dict[str, Any]:
        """Deep extraction from JSON-LD, schema.org for business data (very powerful)."""
        data = {"description": "", "address": "", "services": [], "phone": "", "email": "", "hours": ""}
        try:
            # JSON-LD
            for script in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
                try:
                    obj = _loads_json(script.strip())
                    if isinstance(obj, list):
                        obj = obj[0] if obj else {}
                    if obj.get("@type") in ["LocalBusiness", "Organization", "Corporation", "Restaurant", "Store"]:
                        data["description"] = obj.get("description", "") or data["description"]
                        addr = obj.get("address", {})
                        if isinstance(addr, dict):
                            data["address"] = addr.get("streetAddress", "") or data["address"]
                        data["phone"] = obj.get("telephone", "") or data["phone"]
                        data["email"] = obj.get("email", "") or data["email"]
                        if "openingHours" in obj:
                            data["hours"] = obj.get("openingHours", "")
                        if "offers" in obj or "hasOfferCatalog" in obj or "menu" in str(obj).lower():
                            data["services"].append("Offers / Menu available")
                except:
                    pass

            # Microdata or other
            if "itemprop" in html:
                for m in re.finditer(r'itemprop=["\']description["\'][^>]*>(.*?)<', html, re.I | re.S):
                    data["description"] = data["description"] or m.group(1).strip()[:300]
        except:
            pass
        return data

    def _extract_phones_deep(self, html: str) -> List[str]:
        """Very deep phone extraction with more patterns."""
        phones = []
        # tel: links, data, visible
        for m in re.finditer(r'tel:([+0-9\s\-()]+)', html, re.I):
            phones.append(re.sub(r'[^\d+]', '', m.group(1)))
        for m in re.finditer(r'["\'](?:phone|tel|contact)["\']\s*:\s*["\']([^"\']+)["\']', html, re.I):
            phones.append(re.sub(r'[^\d+]', '', m.group(1)))
        for m in GENERIC_PHONE_PATTERN.finditer(html):
            p = re.sub(r'[^\d+]', '', m.group(0))
            if 8 <= len(p.replace('+','')) <= 15:
                phones.append(p)
        return list(dict.fromkeys([p for p in phones if p]))[:8]

    def crawl_pages(
        self,
        website_url: str,
        max_pages: int = 10,
        max_bytes_per_page: int = 1_500_000,
        max_total_time_sec: int = 25,
        priority_only: bool = False,
    ) -> List[CrawledPage]:
        """Crawl a small, bounded set of internal pages and return HTML corpus."""
        base = self._normalize_url(website_url)
        if not base:
            return []

        to_visit: List[str] = []
        seen: Set[str] = set()
        crawled: List[CrawledPage] = []
        start_time = time.time()

        def _time_exceeded() -> bool:
            if max_total_time_sec is None:
                return False
            return (time.time() - start_time) > max_total_time_sec

        # Seed with common paths
        seed_paths = PRIMARY_PATHS if priority_only else DEFAULT_PATHS
        for p in seed_paths:
            to_visit.append(urljoin(base + "/", p.lstrip("/")))

        if priority_only:
            while to_visit and len(crawled) < max_pages:
                if _time_exceeded():
                    break
                url = (to_visit.pop(0) or "").strip()
                if not url:
                    continue
                norm_url = self._normalize_full_url(url)
                if not norm_url or norm_url in seen:
                    continue
                if not self._is_same_site(base, norm_url):
                    continue

                seen.add(norm_url)
                html = self._safe_get_html(norm_url, max_bytes=max_bytes_per_page)
                if not html:
                    continue
                crawled.append(CrawledPage(url=norm_url, html=html))

            return crawled

        # Also try robots/sitemap for hints (best-effort)
        sitemap_hint_urls: List[str] = []
        if _time_exceeded():
            return crawled

        robots = self._safe_get_text(urljoin(base + "/", "robots.txt"), max_bytes=200_000)
        if robots:
            for line in robots.splitlines():
                if line.lower().startswith("sitemap:"):
                    u = line.split(":", 1)[1].strip()
                    if u.startswith("http"):
                        sitemap_hint_urls.append(u)
        sitemap_hint_urls.append(urljoin(base + "/", "sitemap.xml"))

        for sm in sitemap_hint_urls[:2]:
            if _time_exceeded():
                return crawled
            sm_xml = self._safe_get_text(sm, max_bytes=600_000)
            if not sm_xml:
                continue
            # Cheap extraction: find URLs containing high-value keywords.
            for m in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm_xml, flags=re.I):
                ml = m.lower()
                if any(k in ml for k in PRIORITY_LINK_KEYWORDS):
                    to_visit.append(m.strip())

        # Powerful deep crawl
        crawled_count = 0
        while to_visit and len(crawled) < max_pages:
            if _time_exceeded():
                break
            url = (to_visit.pop(0) or "").strip()
            if not url:
                continue
            norm_url = self._normalize_full_url(url)
            if not norm_url or norm_url in seen:
                continue
            if not self._is_same_site(base, norm_url):
                continue

            seen.add(norm_url)
            html = self._safe_get_html(norm_url, max_bytes=max_bytes_per_page)
            if not html or len(html) < 400:
                continue

            crawled.append(CrawledPage(url=norm_url, html=html))
            crawled_count += 1

            # VERY POWERFUL: aggressive link discovery from every crawled page
            if len(crawled) < max_pages:
                new_links = self._discover_priority_links(html, base)
                # Also extract any other internal links that look useful
                extra = re.findall(r'href=["\'](/[^"\']+?)["\']', html, re.I)
                for ex in extra[:15]:
                    full = urljoin(base + "/", ex.lstrip("/"))
                    if full not in seen and full not in to_visit and self._is_same_site(base, full):
                        if any(k in full.lower() for k in PRIORITY_LINK_KEYWORDS + ("service", "location", "product")):
                            to_visit.append(full)

                for link in new_links:
                    if link not in to_visit:
                        to_visit.append(link)

        return crawled

    def _normalize_url(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        parsed = urlparse(url)
        if not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}"

    def _normalize_full_url(self, url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith(("http://", "https://")):
            u = "https://" + u.lstrip("/")
        parsed = urlparse(u)
        if not parsed.scheme or not parsed.netloc:
            return ""
        # Remove fragments; keep query.
        parsed = parsed._replace(fragment="")
        return parsed.geturl()

    def _is_same_site(self, base: str, url: str) -> bool:
        try:
            b = urlparse(base)
            u = urlparse(url)
            return (b.netloc or "").lower() == (u.netloc or "").lower()
        except Exception:
            return False

    def _cache_put(self, url: str, html: str) -> None:
        if url in self._html_cache:
            return
        self._html_cache[url] = html
        self._cache_fifo.append(url)
        if len(self._cache_fifo) > self._max_cache_entries:
            old = self._cache_fifo.pop(0)
            self._html_cache.pop(old, None)

    def _safe_get_text(self, url: str, max_bytes: int = 600_000) -> str:
        return self._safe_get_html(url, max_bytes=max_bytes)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type((httpx.RequestError, requests.RequestException)),
        reraise=True,
    )
    def _safe_get_html(self, url: str, max_bytes: int = 1_500_000) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        if url in self._html_cache:
            return self._html_cache[url]

        host = self._get_host(url)
        if host in self._blocked_hosts:
            return ""

        # httpx first (faster, HTTP/2)
        text = ""
        try:
            r = self._httpx.get(url)
            if r.status_code < 400:
                content = r.content or b""
                if max_bytes and len(content) > max_bytes:
                    content = content[:max_bytes]
                text = content.decode(r.encoding or "utf-8", errors="ignore")
                self._clear_host_denials(url)
            else:
                self._register_host_denial(url, r.status_code)
        except Exception:
            text = ""

        if not text:
            if host in self._blocked_hosts:
                return ""
            try:
                r2 = self._requests_session.get(url, timeout=self.timeout, verify=False, allow_redirects=True)
                if r2.status_code < 400:
                    raw = (r2.content or b"")
                    if max_bytes and len(raw) > max_bytes:
                        raw = raw[:max_bytes]
                    text = raw.decode(r2.encoding or "utf-8", errors="ignore")
                    self._clear_host_denials(url)
                else:
                    self._register_host_denial(url, r2.status_code)
            except requests.RequestException:
                text = ""

        if text:
            self._cache_put(url, text)
        return text

    def _get_host(self, url: str) -> str:
        try:
            return (urlparse(url).netloc or "").lower()
        except Exception:
            return ""

    def _register_host_denial(self, url: str, status_code: int) -> None:
        if status_code not in {401, 403, 429}:
            return

        host = self._get_host(url)
        if not host:
            return

        denied_count = self._host_denials.get(host, 0) + 1
        self._host_denials[host] = denied_count
        if denied_count >= self._host_block_threshold:
            self._blocked_hosts.add(host)

    def _clear_host_denials(self, url: str) -> None:
        host = self._get_host(url)
        if not host:
            return
        self._host_denials.pop(host, None)
        self._blocked_hosts.discard(host)

    def _discover_priority_links(self, html: str, base: str) -> List[str]:
        candidates: List[str] = []
        if not html:
            return candidates

        def consider(href: str, anchor_text: str = "") -> None:
            href = (href or "").strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
                return
            href_l = href.lower()
            text_l = (anchor_text or "").lower()
            # Deep: more keywords for business sites
            deep_keywords = PRIORITY_LINK_KEYWORDS + ("services", "menu", "products", "our-work", "portfolio", "locations", "branches", "hours", "open", "about-us", "team", "staff", "contact-us")
            if any(k in href_l for k in deep_keywords) or any(k in text_l for k in deep_keywords):
                u = urljoin(base + "/", href)
                u = self._normalize_full_url(u)
                if u and self._is_same_site(base, u):
                    candidates.append(u)

        if HTMLParser is not None:
            try:
                tree = HTMLParser(html)
                for a in tree.css("a"):
                    consider(a.attributes.get("href", ""), a.text())
            except Exception:
                pass

        if not candidates:
            try:
                soup = BeautifulSoup(html, "lxml")
            except FeatureNotFound:
                soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                consider(a.get("href", ""), a.get_text(" ", strip=True))

        # De-dupe preserving order, limit but higher for deep
        return list(dict.fromkeys(candidates))[:20]

    def _extract_emails(self, html: str) -> List[str]:
        if not html:
            return []
        raw = unescape(html)
        candidates: List[str] = []

        candidates.extend(EMAIL_PATTERN.findall(raw))

        for m in MAILTO_PATTERN.findall(raw):
            candidates.append(m)

        for m in DATA_EMAIL_PATTERN.findall(raw):
            candidates.append(m)

        for user, domain in DATA_USER_DOMAIN_PATTERN.findall(raw):
            candidates.append(f"{user}@{domain}")
        for domain, user in DATA_DOMAIN_USER_PATTERN.findall(raw):
            candidates.append(f"{user}@{domain}")

        for user, domain in JS_EMAIL_JOIN_PATTERN.findall(raw):
            candidates.append(f"{user}@{domain}")

        normalized = self._normalize_obfuscated_text(raw)
        candidates.extend(EMAIL_PATTERN.findall(normalized))

        # Also attempt to parse obfuscated emails (legacy pattern)
        for m in OBFUSCATED_EMAIL_PATTERN.findall(raw):
            try:
                local, domain, tld = m
                candidate = f"{local}@{domain}.{tld}"
                candidates.append(candidate)
            except Exception:
                continue

        # JSON-LD can include email fields
        for email in self._extract_emails_from_jsonld(raw):
            candidates.append(email)

        deduped: List[str] = []
        seen = set()
        for candidate in candidates:
            normalized_email = self._normalize_email_candidate(candidate)
            if not normalized_email:
                continue
            if not self._is_valid_email(normalized_email):
                continue
            if normalized_email in seen:
                continue
            seen.add(normalized_email)
            deduped.append(normalized_email)

        return deduped

    def _normalize_obfuscated_text(self, text: str) -> str:
        cleaned = text
        cleaned = re.sub(r"\s*(?:\(|\[|\{)?\s*at\s*(?:\)|\]|\})?\s*", "@", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*(?:\(|\[|\{)?\s*dot\s*(?:\)|\]|\})?\s*", ".", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*\(\s*at\s*\)\s*", "@", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*\[\s*at\s*\]\s*", "@", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*\(\s*dot\s*\)\s*", ".", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*\[\s*dot\s*\]\s*", ".", cleaned, flags=re.IGNORECASE)
        return cleaned

    def _normalize_email_candidate(self, value: str) -> str:
        if not value:
            return ""
        text = unescape(value)
        text = unquote(text)
        text = text.strip().strip("<>[](){}\"' ")
        if text.lower().startswith("mailto:"):
            text = text[7:]
        if "?" in text:
            text = text.split("?", 1)[0]
        text = text.strip().strip(".,;:")
        return text.lower()

    def _is_valid_email(self, email: str) -> bool:
        if not email or "@" not in email or email.count("@") != 1:
            return False
        local, domain = email.split("@", 1)
        if not local or not domain or "." not in domain:
            return False
        if len(local) > 64 or len(domain) > 255:
            return False
        if domain in INVALID_EMAIL_DOMAINS:
            return False
        tld = domain.rsplit(".", 1)[-1]
        if tld.lower() in INVALID_EMAIL_TLDS:
            return False
        return True

    def _get_base_domain(self, website_url: str) -> str:
        try:
            parsed = urlparse(website_url)
            host = (parsed.netloc or "").lower().strip()
            if ":" in host:
                host = host.split(":", 1)[0]
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    def _rank_emails(self, emails: List[str], base_domain: str) -> List[str]:
        if not emails:
            return []
        base_domain = (base_domain or "").lower()

        def score_email(email: str) -> int:
            score = 0
            local, domain = email.split("@", 1)
            if base_domain and (domain == base_domain or domain.endswith("." + base_domain)):
                score += 3
            if local in NO_REPLY_LOCAL_PARTS:
                score -= 2
            return score

        ranked = sorted(enumerate(emails), key=lambda item: (-score_email(item[1]), item[0]))
        return [email for _, email in ranked]

    def _extract_emails_from_jsonld(self, html: str) -> List[str]:
        emails: List[str] = []
        if not html:
            return emails
        try:
            # Cheaply slice scripts; avoid full DOM when possible
            for script in re.findall(
                r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                html,
                flags=re.I | re.S,
            ):
                s = (script or "").strip()
                if not s:
                    continue
                try:
                    data = _loads_json(s)
                except Exception:
                    continue
                for e in self._walk_json_for_key(data, "email"):
                    if isinstance(e, str):
                        for m in EMAIL_PATTERN.findall(e):
                            emails.append(m)
        except Exception:
            return emails

        return list(dict.fromkeys([e.lower() for e in emails if e]))

    def _walk_json_for_key(self, obj, key: str) -> Iterable:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key:
                    yield v
                yield from self._walk_json_for_key(v, key)
        elif isinstance(obj, list):
            for it in obj:
                yield from self._walk_json_for_key(it, key)

    def _extract_whatsapp_numbers(self, html: str) -> List[str]:
        try:
            soup = BeautifulSoup(html, "lxml")
        except FeatureNotFound:
            soup = BeautifulSoup(html, "html.parser")
        found: List[str] = []
        markers = ["wa.me", "api.whatsapp.com", "whatsapp://", "chat.whatsapp.com", "wa.link"]

        # Primary extraction from WhatsApp link formats including short links.
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if not href:
                continue
            lower_href = href.lower()
            if not any(marker in lower_href for marker in markers):
                continue

            link_numbers = self._extract_numbers_from_whatsapp_ref(href)
            for number in link_numbers:
                if number and number not in found:
                    found.append(number)

            # wa.link often redirects to wa.me or api.whatsapp.com with phone in query.
            if "wa.link" in lower_href and not link_numbers:
                resolved = self._resolve_short_whatsapp_link(href)
                if resolved:
                    for number in self._extract_numbers_from_whatsapp_ref(resolved):
                        if number and number not in found:
                            found.append(number)

        # Fallback: check raw HTML for WhatsApp markers and nearby phone candidates.
        lower_html = html.lower()
        if not found and any(marker in lower_html for marker in markers):
            for match in WHATSAPP_LINK_PATTERN.findall(html):
                number = self._normalize_phone(match)
                if number and number not in found:
                    found.append(number)

            if not found:
                for match in GENERIC_PHONE_PATTERN.findall(html):
                    number = self._normalize_phone(match)
                    if 8 <= len(number.replace("+", "")) <= 15 and number not in found:
                        found.append(number)

        # Extra fallback: many sites keep WhatsApp links in inline script strings.
        if not found:
            for script in soup.find_all("script"):
                script_text = script.get_text(" ", strip=True)
                if not script_text:
                    continue
                if not any(marker in script_text.lower() for marker in markers):
                    continue

                normalized_script = script_text.replace("\\/", "/")
                for ref in WHATSAPP_REF_PATTERN.findall(normalized_script):
                    for number in self._extract_numbers_from_whatsapp_ref(ref):
                        if number and number not in found:
                            found.append(number)

                if not found:
                    for match in WHATSAPP_LINK_PATTERN.findall(normalized_script):
                        number = self._normalize_phone(match)
                        if number and number not in found:
                            found.append(number)

        deduped: List[str] = []
        seen_canonical = set()
        for number in found:
            if not number:
                continue
            canonical = self._canonical_phone(number)
            if canonical and canonical not in seen_canonical:
                seen_canonical.add(canonical)
                deduped.append(number)
        return deduped

    def _extract_numbers_from_whatsapp_ref(self, href: str) -> List[str]:
        decoded = unquote((href or "").strip())
        if not decoded:
            return []

        numbers: List[str] = []
        seen_canonical = set()

        # Direct path style, e.g. wa.me/923001234567
        for match in WHATSAPP_LINK_PATTERN.findall(decoded):
            normalized = self._normalize_phone(match)
            canonical = self._canonical_phone(normalized)
            if normalized and canonical and canonical not in seen_canonical:
                seen_canonical.add(canonical)
                numbers.append(normalized)

        # Query parameter style, e.g. ?phone=923001234567
        try:
            parsed = urlparse(decoded)
            query_values = parse_qs(parsed.query)
            for key in ["phone", "phonenumber", "number"]:
                for value in query_values.get(key, []):
                    normalized = self._normalize_phone(value)
                    canonical = self._canonical_phone(normalized)
                    if normalized and canonical and canonical not in seen_canonical:
                        seen_canonical.add(canonical)
                        numbers.append(normalized)
        except Exception:
            pass

        # Last fallback for href containing mixed symbols.
        if not numbers:
            maybe = self._extract_digits(decoded)
            if maybe:
                numbers.append(maybe)

        return numbers

    def _resolve_short_whatsapp_link(self, href: str) -> str:
        url = (href or "").strip()
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = f"https://{url.lstrip('/')}"
        try:
            response = self._requests_session.get(url, timeout=8, allow_redirects=True, verify=False)
            return response.url or ""
        except requests.RequestException:
            return ""

    def _extract_digits(self, text: str) -> str:
        digits = "".join(DIGIT_PATTERN.findall(text))
        if len(digits) < 6:
            return ""
        return digits

    def _normalize_phone(self, phone: str) -> str:
        phone = (phone or "").strip()
        if not phone:
            return ""
        cleaned = phone.replace(" ", "").replace("-", "")
        if cleaned.startswith("+"):
            return "+" + "".join(DIGIT_PATTERN.findall(cleaned))
        digits = "".join(DIGIT_PATTERN.findall(cleaned))
        return digits

    def _canonical_phone(self, phone: str) -> str:
        return "".join(DIGIT_PATTERN.findall(phone or ""))


def crawl_site_pages(website_url: str, timeout: int = 12, max_pages: int = 8) -> List[Tuple[str, str]]:
    """Convenience helper for other modules.

    Returns list of (url, html) tuples.
    """
    extractor = WebsiteExtractor(timeout=timeout)
    pages = extractor.crawl_pages(website_url, max_pages=max_pages)
    return [(p.url, p.html) for p in pages]

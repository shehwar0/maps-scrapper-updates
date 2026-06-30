"""
Ultra Deep Business Scraper - Multi-Engine Parallel Extraction with Cross-Verification

Uses ALL available scrapers in parallel:
1. deep_scraper.py - Multi-source extraction (Maps + Website + Google Search)
2. enhanced_scraper_sync.py - Enhanced extraction
3. business_extractor.py - Website analysis utilities
4. email_extractor.py - Email/WhatsApp extraction
5. scraper.py - Basic Maps scraper

Cross-verifies results from multiple sources for maximum accuracy.
"""

import logging
import random
import re
import time
import concurrent.futures
from dataclasses import dataclass, field
from threading import Event, Lock
from typing import Callable, Dict, List, Optional, Set, Tuple, Any
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# Import all extraction utilities
from business_extractor import (
    BusinessData as BizData,
    WebsiteAnalyzer,
    analyze_website,
    validate_phone,
    validate_email,
    validate_whatsapp,
    deduplicate_leads,
    WHATSAPP_PATTERNS as BIZ_WA_PATTERNS,
    SOCIAL_PATTERNS,
    EMAIL_PATTERNS as BIZ_EMAIL_PATTERNS,
)

from email_extractor import WebsiteExtractor

from deep_scraper import (
    normalize_phone,
    extract_emails,
    extract_whatsapp,
    extract_social_handle,
    detect_chatbot,
    detect_analytics,
    detect_cms,
    clean_address,
    INSTAGRAM_PATTERNS,
    FACEBOOK_PATTERNS,
    TWITTER_PATTERNS,
    LINKEDIN_PATTERNS,
    TIKTOK_PATTERNS,
    YOUTUBE_PATTERNS,
    WHATSAPP_PATTERNS,
    EMAIL_PATTERNS,
    CONTACT_PAGES,
    CaptchaDetectedError,
)
from maps_city_coverage import build_citywide_queries
from url_filters import is_business_website, normalize_business_website

# Import history manager for deduplication
from scrape_history import get_history, ScrapeHistory

# Import other scrapers (Ultra must use all scripts)
try:
    from scraper import GoogleMapsScraper as BasicMapsScraper
except Exception:  # pragma: no cover
    BasicMapsScraper = None

try:
    from enhanced_scraper_sync import GoogleMapsScraper as EnhancedSyncMapsScraper
except Exception:  # pragma: no cover
    EnhancedSyncMapsScraper = None


# ============================================================================
# CONFIGURATION
# ============================================================================

MAX_RESULTS_CAP = 500
RESULT_SCAN_WINDOW = 320
CITYWIDE_QUERY_LIMIT = 30
MAP_STAGNANT_ROUNDS = 28
MAP_SCROLL_DELAY_MIN = 0.35
MAP_SCROLL_DELAY_MAX = 0.85
URL_DISCOVERY_MAX_SEC = 240
DISCOVERY_STABLE_ROUNDS_FOR_END = 4
REQUEST_TIMEOUT = 15
PARALLEL_WORKERS = 3
QUERY_RETRY_ATTEMPTS = 2
QUERY_RETRY_BASE_WAIT_MS = 2500
CAPTCHA_MANUAL_WAIT_MS = 180000
CAPTCHA_POLL_MS = 1500
CAPTCHA_MARKERS = (
    "unusual traffic",
    "detected unusual",
    "recaptcha",
    "verify you are human",
    "not a robot",
    "g-recaptcha",
    "our systems have detected unusual traffic",
    "sorry/index",
)
LISTING_TIME_BUDGET_SEC = 60
WEBSITE_ANALYSIS_BUDGET_SEC = 18
HEAVY_STEP_MIN_REMAINING_SEC = 9
GOOGLE_LOOKUP_MIN_REMAINING_SEC = 9
SOCIAL_VERIFY_MIN_REMAINING_SEC = 12

# Extended contact pages for ultra deep scan
ULTRA_CONTACT_PAGES = [
    "",
    "/contact",
    "/contact-us",
    "/contactus",
    "/about",
    "/about-us",
    "/aboutus",
    "/team",
    "/reach-us",
    "/get-in-touch",
    "/connect",
    "/social",
    "/follow-us",
    "/support",
    "/help",
    "/info",
    "/location",
    "/locations",
    "/branches",
    "/stores",
]

PHONE_REGEX = re.compile(r"(\+?\d[\d\s()\-\.]{6,}\d)")


def _get_base_domain(website_url: str) -> str:
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


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class UltraBusinessData:
    """Ultra comprehensive business data with source tracking."""
    # Basic Info
    name: str = ""
    phone: str = ""
    website: str = ""
    has_website: bool = False
    address: str = ""
    rating: float = 0.0
    review_count: int = 0
    business_hours: str = ""
    category: str = ""
    plus_code: str = ""
    google_maps_url: str = ""

    # Contact Info - with confidence scores
    emails: List[str] = field(default_factory=list)
    emails_verified: List[str] = field(default_factory=list)
    whatsapp_numbers: List[str] = field(default_factory=list)
    whatsapp_verified: List[str] = field(default_factory=list)
    additional_phones: List[str] = field(default_factory=list)

    # Social Media - with verification status
    instagram: str = ""
    instagram_verified: bool = False
    facebook: str = ""
    facebook_verified: bool = False
    twitter: str = ""
    linkedin: str = ""
    tiktok: str = ""
    youtube: str = ""

    # Marketing & Tech Intelligence
    has_chatbot: bool = False
    chatbot_type: str = ""
    has_google_analytics: bool = False
    has_meta_pixel: bool = False
    cms_platform: str = ""
    is_automated: bool = False

    # Metadata
    extraction_quality: str = "unknown"
    data_sources: List[str] = field(default_factory=list)
    verification_score: int = 0  # 0-100

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON/CSV export."""
        return {
            "name": self.name,
            "phone": self.phone,
            "email": self.emails[0] if self.emails else "",
            "all_emails": "; ".join(self.emails),
            "whatsapp": self.whatsapp_numbers[0] if self.whatsapp_numbers else "",
            "all_whatsapp": "; ".join(self.whatsapp_numbers),
            "website": self.website,
            "has_website": "Yes" if self.has_website else "No",
            "address": self.address,
            "rating": self.rating,
            "review_count": self.review_count,
            "category": self.category,
            "business_hours": self.business_hours,
            "instagram": self.instagram,
            "facebook": self.facebook,
            "twitter": self.twitter,
            "linkedin": self.linkedin,
            "tiktok": self.tiktok,
            "youtube": self.youtube,
            "has_chatbot": "Yes" if self.has_chatbot else "No",
            "chatbot_type": self.chatbot_type,
            "has_google_analytics": "Yes" if self.has_google_analytics else "No",
            "has_meta_pixel": "Yes" if self.has_meta_pixel else "No",
            "cms_platform": self.cms_platform,
            "is_automated": "Yes" if self.is_automated else "No",
            "quality_score": self.extraction_quality,
            "verification_score": self.verification_score,
            "data_sources": ", ".join(self.data_sources),
            "google_maps_url": self.google_maps_url,
        }

    def calculate_quality(self) -> str:
        """Calculate extraction quality score."""
        score = 0
        if self.name:
            score += 1
        if self.phone:
            score += 1
        if self.emails:
            score += 2
        if self.whatsapp_numbers:
            score += 2
        if self.website:
            score += 1
        if self.address:
            score += 1
        if self.instagram:
            score += 1
        if self.facebook:
            score += 1
        if self.has_chatbot or self.has_google_analytics:
            score += 1

        # Bonus for verification
        if len(self.data_sources) >= 3:
            score += 2

        if score >= 10:
            return "ultra"
        elif score >= 8:
            return "high"
        elif score >= 5:
            return "medium"
        else:
            return "low"

    def calculate_verification_score(self) -> int:
        """Calculate verification score based on cross-verified data."""
        score = 0
        
        # Each data source adds points
        score += len(self.data_sources) * 10
        
        # Verified fields add more points
        if self.emails_verified:
            score += 15
        if self.whatsapp_verified:
            score += 15
        if self.instagram_verified:
            score += 10
        if self.facebook_verified:
            score += 10
        
        # Completeness bonus
        if self.name and self.phone and self.address:
            score += 10
        if self.website and self.emails:
            score += 10
        
        return min(100, score)


# ============================================================================
# EXTRACTION ENGINE BASE
# ============================================================================

class ExtractionEngine:
    """Base class for extraction engines."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.log = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })

    def extract_from_html(self, html: str, url: str = "") -> Dict[str, Any]:
        """Extract all possible data from HTML."""
        raise NotImplementedError


class DeepExtractionEngine(ExtractionEngine):
    """Deep extraction using deep_scraper patterns."""
    
    def extract_from_html(self, html: str, url: str = "") -> Dict[str, Any]:
        base_domain = _get_base_domain(url)
        result = {
            "emails": extract_emails(html, base_domain),
            "whatsapp": extract_whatsapp(html),
            "instagram": extract_social_handle(html, INSTAGRAM_PATTERNS, "instagram"),
            "facebook": extract_social_handle(html, FACEBOOK_PATTERNS, "facebook"),
            "twitter": extract_social_handle(html, TWITTER_PATTERNS, "twitter"),
            "linkedin": extract_social_handle(html, LINKEDIN_PATTERNS, "linkedin"),
            "tiktok": extract_social_handle(html, TIKTOK_PATTERNS, "tiktok"),
            "youtube": extract_social_handle(html, YOUTUBE_PATTERNS, "youtube"),
            "chatbot": detect_chatbot(html),
            "analytics": detect_analytics(html),
            "cms": detect_cms(html),
            "source": "deep_engine",
        }
        return result


class BusinessExtractionEngine(ExtractionEngine):
    """Extraction using business_extractor patterns."""
    
    def extract_from_html(self, html: str, url: str = "") -> Dict[str, Any]:
        analyzer = WebsiteAnalyzer(html, url)
        
        result = {
            "emails": analyzer.extract_emails(),
            "whatsapp": analyzer.extract_whatsapp(),
            "socials": analyzer.extract_social_media(),
            "chatbot": analyzer.detect_chatbot(),
            "analytics": analyzer.detect_analytics(),
            "cms": analyzer.detect_cms(),
            "source": "business_engine",
        }
        return result


class EmailExtractionEngine(ExtractionEngine):
    """Extraction using email_extractor."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        super().__init__(logger)
        self.extractor = WebsiteExtractor()
    
    def extract_from_url(self, url: str, fallback_phone: str = "") -> Dict[str, Any]:
        enrichment = self.extractor.enrich(url, fallback_phone)
        return {
            "email": enrichment.get("email", ""),
            "whatsapp": enrichment.get("whatsapp", ""),
            "source": "email_engine",
        }


# ============================================================================
# CROSS VERIFIER
# ============================================================================

class CrossVerifier:
    """Cross-verifies data from multiple sources."""
    
    @staticmethod
    def merge_and_verify(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge results from multiple engines and verify.

        IMPORTANT: Verification is based on *distinct sources/engines*, not how many pages
        were scanned. This prevents false "verified" signals when the same engine runs
        across multiple pages of the same site.
        """
        merged: Dict[str, Any] = {
            "emails": [],
            "whatsapp": [],
            "instagram": "",
            "facebook": "",
            "twitter": "",
            "linkedin": "",
            "tiktok": "",
            "youtube": "",
            "has_chatbot": False,
            "chatbot_type": "",
            "has_google_analytics": False,
            "has_meta_pixel": False,
            "cms": "",
            "sources": [],
            "verification_count": {},
        }

        email_sources: Dict[str, Set[str]] = {}
        whatsapp_sources: Dict[str, Set[str]] = {}
        instagram_sources: Dict[str, Set[str]] = {}
        facebook_sources: Dict[str, Set[str]] = {}

        for result in results or []:
            source = (result.get("source") or "unknown").strip() or "unknown"
            merged["sources"].append(source)

            # Emails
            emails = result.get("emails", [])
            if isinstance(emails, str) and emails:
                emails = [emails]
            for email in emails or []:
                if not email:
                    continue
                email_sources.setdefault(email, set()).add(source)
                if email not in merged["emails"]:
                    merged["emails"].append(email)

            # WhatsApp
            whatsapp = result.get("whatsapp", [])
            if isinstance(whatsapp, str) and whatsapp:
                whatsapp = [whatsapp]
            for wa in whatsapp or []:
                if not wa:
                    continue
                whatsapp_sources.setdefault(wa, set()).add(source)
                if wa not in merged["whatsapp"]:
                    merged["whatsapp"].append(wa)

            # Instagram / Facebook
            ig = result.get("instagram", "") or result.get("socials", {}).get("instagram", "")
            if ig:
                instagram_sources.setdefault(ig, set()).add(source)
                if not merged["instagram"]:
                    merged["instagram"] = ig

            fb = result.get("facebook", "") or result.get("socials", {}).get("facebook", "")
            if fb:
                facebook_sources.setdefault(fb, set()).add(source)
                if not merged["facebook"]:
                    merged["facebook"] = fb

            # Other socials (take first non-empty)
            for social in ["twitter", "linkedin", "tiktok", "youtube"]:
                val = result.get(social, "") or result.get("socials", {}).get(social, "")
                if val and not merged[social]:
                    merged[social] = val

            # Chatbot
            chatbot = result.get("chatbot", (False, ""))
            if isinstance(chatbot, tuple):
                has_bot, bot_type = chatbot
            else:
                has_bot, bot_type = bool(chatbot), ""
            if has_bot:
                merged["has_chatbot"] = True
                if bot_type and not merged["chatbot_type"]:
                    merged["chatbot_type"] = bot_type

            # Analytics
            analytics = result.get("analytics", {})
            if isinstance(analytics, dict):
                if analytics.get("google_analytics"):
                    merged["has_google_analytics"] = True
                if analytics.get("meta_pixel"):
                    merged["has_meta_pixel"] = True

            # CMS
            cms = result.get("cms", "")
            if cms and not merged["cms"]:
                merged["cms"] = cms

        merged["verification_count"] = {
            "emails": {e: len(srcs) for e, srcs in email_sources.items()},
            "whatsapp": {w: len(srcs) for w, srcs in whatsapp_sources.items()},
            "instagram": {h: len(srcs) for h, srcs in instagram_sources.items()},
            "facebook": {h: len(srcs) for h, srcs in facebook_sources.items()},
        }

        merged["emails_verified"] = [e for e, srcs in email_sources.items() if len(srcs) >= 2]
        merged["whatsapp_verified"] = [w for w, srcs in whatsapp_sources.items() if len(srcs) >= 2]
        merged["instagram_verified"] = any(len(srcs) >= 2 for srcs in instagram_sources.values())
        merged["facebook_verified"] = any(len(srcs) >= 2 for srcs in facebook_sources.values())

        return merged


# ============================================================================
# ULTRA DEEP SCRAPER
# ============================================================================

class UltraDeepScraper:
    """
    Ultra Deep Multi-Engine Scraper with Cross-Verification.
    
    Uses ALL available extraction methods in parallel:
    1. Google Maps extraction
    2. Deep website analysis (multiple engines)
    3. Google Search cross-verification
    4. Social media profile verification
    """

    def __init__(
        self,
        max_results: int = 50,
        headless: bool = False,
        min_delay: float = 0.7,
        max_delay: float = 1.6,
        website_filter: str = "all",
        parallel_engines: bool = True,
        verify_socials: bool = True,
        skip_duplicates: bool = True,  # NEW: Skip previously scraped businesses
        logger: Optional[logging.Logger] = None,
        progress_callback: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> None:
        self.max_results = max(1, min(max_results, MAX_RESULTS_CAP))
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.website_filter = website_filter if website_filter in {"all", "with", "without"} else "all"
        self.parallel_engines = parallel_engines
        self.verify_socials = verify_socials
        self.skip_duplicates = skip_duplicates
        self.log = logger or logging.getLogger(__name__)
        self.progress_callback = progress_callback
        self._website_engine_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._google_verify_cache: Dict[str, Optional[Dict]] = {}
        
        # Initialize extraction engines
        self.deep_engine = DeepExtractionEngine(logger)
        self.biz_engine = BusinessExtractionEngine(logger)
        self.email_engine = EmailExtractionEngine(logger)
        self.verifier = CrossVerifier()
        
        # Initialize history manager for deduplication
        self.history = get_history(logger)
        
        # HTTP session
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })

        # Cross-check engines (run only as fallback to avoid slowing Ultra)
        self.basic_maps_engine = None
        self.enhanced_sync_engine = None
        try:
            if BasicMapsScraper is not None:
                self.basic_maps_engine = BasicMapsScraper(
                    max_results=1,
                    headless=self.headless,
                    min_delay=self.min_delay,
                    max_delay=self.max_delay,
                    website_filter="all",
                    logger=self.log,
                )
        except Exception as e:
            self.log.debug("BasicMapsScraper init failed: %s", e)

        try:
            if EnhancedSyncMapsScraper is not None:
                self.enhanced_sync_engine = EnhancedSyncMapsScraper(
                    max_results=1,
                    headless=self.headless,
                    min_delay=self.min_delay,
                    max_delay=self.max_delay,
                    website_filter="all",
                    logger=self.log,
                )
        except Exception as e:
            self.log.debug("EnhancedSyncMapsScraper init failed: %s", e)

    def scrape(
        self,
        keyword: str,
        location: str,
        stop_event: Optional[Event] = None,
    ) -> List[Dict[str, str]]:
        """Main scrape method with ultra deep extraction and deduplication."""
        stop_event = stop_event or Event()
        search_queries = build_citywide_queries(keyword, location, max_queries=CITYWIDE_QUERY_LIMIT)

        if not search_queries:
            return []

        duplicate_buffer = 0
        if self.skip_duplicates:
            duplicate_buffer = min(12, max(2, self.max_results // 12))
        target_urls = self.max_results + duplicate_buffer
        
        # Log history stats
        if self.skip_duplicates:
            stats = self.history.get_stats(keyword, location)
            self.log.info(f"📊 History: {stats.get('search_total', 0)} previously scraped for this search, {stats.get('global_total', 0)} total")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
            )
            
            try:
                page = context.new_page()

                if len(search_queries) > 1:
                    self.log.info("Using %d map zones for broader city coverage", len(search_queries))

                discovered: List[str] = []
                seen: Set[str] = set()
                per_query_target = max(10, (target_urls + len(search_queries) - 1) // len(search_queries))

                for query in search_queries:
                    if stop_event.is_set() or len(discovered) >= target_urls:
                        break

                    remaining = target_urls - len(discovered)
                    query_target = min(per_query_target, remaining)
                    place_urls = self._search_query_with_retries(page, query, stop_event, query_target)

                    for place_url in place_urls:
                        if place_url and place_url not in seen:
                            seen.add(place_url)
                            discovered.append(place_url)
                            if len(discovered) >= target_urls:
                                break

                if len(discovered) < target_urls and not stop_event.is_set():
                    remaining = target_urls - len(discovered)
                    fallback_urls = self._search_query_with_retries(
                        page,
                        search_queries[0],
                        stop_event,
                        remaining,
                    )
                    for place_url in fallback_urls:
                        if place_url and place_url not in seen:
                            seen.add(place_url)
                            discovered.append(place_url)
                            if len(discovered) >= target_urls:
                                break

                leads = self._ultra_extract_leads(context, discovered[:target_urls], keyword, location, stop_event)
                
                # Convert to dicts and save to history
                results = [lead.to_dict() for lead in leads]
                
                # Add new results to history
                if self.skip_duplicates and results:
                    self.history.add_batch_to_history(results, keyword, location)
                    self.log.info(f"💾 Saved {len(results)} new businesses to history")
                
                return results
            finally:
                context.close()
                browser.close()

    def _search_query_with_retries(self, page: Page, query: str, stop_event: Event, target_count: int) -> List[str]:
        last_error: Optional[Exception] = None

        for attempt in range(QUERY_RETRY_ATTEMPTS + 1):
            if stop_event.is_set():
                break

            try:
                self._open_and_search(page, query)
                return self._collect_place_urls(page, stop_event, target_count=target_count)
            except CaptchaDetectedError as exc:
                last_error = exc
                if attempt >= QUERY_RETRY_ATTEMPTS:
                    break

                cooldown_ms = QUERY_RETRY_BASE_WAIT_MS * (attempt + 1)
                self.log.warning(
                    "Captcha challenge for query '%s' (attempt %d/%d). Cooling down for %d ms and retrying.",
                    query,
                    attempt + 1,
                    QUERY_RETRY_ATTEMPTS + 1,
                    cooldown_ms,
                )
                page.wait_for_timeout(cooldown_ms)

                try:
                    page.goto("https://www.google.com/maps", timeout=45000)
                    page.wait_for_timeout(1000)
                    self._maybe_accept_consent(page)
                except Exception:
                    pass

        if isinstance(last_error, CaptchaDetectedError):
            raise last_error
        raise RuntimeError(f"Search failed for query: {query}")

    def _open_and_search(self, page: Page, query: str) -> None:
        """Navigate to Google Maps and search."""
        encoded_query = quote_plus(query)
        page.goto(f"https://www.google.com/maps/search/{encoded_query}", timeout=90000)
        page.wait_for_timeout(1500)
        self._maybe_accept_consent(page)
        self._raise_if_captcha(page)

        if self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=45000):
            self._human_delay()
            return

        search_input = self._find_search_input(page)
        if search_input:
            search_input.fill(query)
            self._human_delay()
            search_input.press("Enter")

        if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc", "h1.DUwDvf"], timeout_ms=45000):
            raise RuntimeError("Google Maps results did not load.")

        self._human_delay()
        self._raise_if_captcha(page)

    def _find_search_input(self, page: Page):
        """Find search input."""
        selectors = [
            "input#searchboxinput",
            "input[aria-label='Search Google Maps']",
            "input[aria-label*='Search']",
            "input[name='q']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    locator.wait_for(state="visible", timeout=6000)
                    return locator
            except Exception:
                continue
        return None

    def _wait_for_any(self, page: Page, selectors: List[str], timeout_ms: int) -> bool:
        """Wait for any selector."""
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            for selector in selectors:
                try:
                    if page.locator(selector).first.count() > 0:
                        return True
                except Exception:
                    continue
            page.wait_for_timeout(400)
        return False

    def _maybe_accept_consent(self, page: Page) -> None:
        """Accept consent dialog."""
        selectors = [
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Accept')",
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.count() > 0 and button.is_visible():
                    button.click(timeout=3000)
                    page.wait_for_timeout(1200)
                    return
            except Exception:
                continue

    def _is_end_of_results(self, page: Page) -> bool:
        try:
            try:
                content = (page.content() or "").lower()
            except Exception:
                content = ""
            markers = ("you've reached the end", "end of the list", "no more results", "reached the end")
            if any(m in content for m in markers):
                return True
            try:
                el = page.locator("div[role='status']").first
                if el.count() > 0 and ("end" in (el.inner_text(timeout=500) or "").lower()):
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _collect_place_urls(self, page: Page, stop_event: Event, target_count: Optional[int] = None) -> List[str]:
        """Robust collection with end detection."""
        discovered: List[str] = []
        seen: Set[str] = set()
        stagnant_rounds = 0
        max_stagnant_rounds = MAP_STAGNANT_ROUNDS + (6 if getattr(self, 'skip_duplicates', False) else 0)
        start_time = time.time()

        if target_count is None:
            duplicate_buffer = min(18, max(3, self.max_results // 8))
            target_urls = self.max_results + duplicate_buffer
        else:
            target_urls = max(1, target_count)

        current_url = page.url or ""
        if "/maps/place/" in current_url:
            return [current_url]

        feed = None
        try:
            feed = page.locator("div[role='feed']").first
            if feed.count() == 0:
                feed = None
        except Exception:
            feed = None

        while len(discovered) < target_urls and stagnant_rounds < max_stagnant_rounds and not stop_event.is_set():
            if (time.time() - start_time) > URL_DISCOVERY_MAX_SEC:
                self.log.warning("Discovery budget hit.")
                break

            before = len(discovered)

            try:
                hrefs = page.eval_on_selector_all(
                    "a.hfpxzc",
                    "els => els.map(el => el.getAttribute('href') || el.href || '').filter(Boolean)",
                )
            except Exception:
                hrefs = []

            newly = 0
            if hrefs:
                tail_start = max(0, len(hrefs) - RESULT_SCAN_WINDOW)
                for href in hrefs[tail_start:]:
                    if stop_event.is_set() or len(discovered) >= target_urls:
                        break
                    if href and href not in seen:
                        seen.add(href)
                        discovered.append(href)
                        newly += 1

            reached_end = self._is_end_of_results(page)

            if len(discovered) == before:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            if stagnant_rounds >= DISCOVERY_STABLE_ROUNDS_FOR_END and (reached_end or newly == 0):
                break

            try:
                if feed and feed.count() > 0:
                    feed.evaluate("(el) => el.scrollBy(0, Math.floor(el.scrollHeight * .9) + 150)")
                else:
                    page.mouse.wheel(0, 4800)
            except Exception:
                try:
                    page.evaluate("window.scrollBy(0, 4000)")
                except Exception:
                    pass

            page.wait_for_timeout(int(random.uniform(MAP_SCROLL_DELAY_MIN, MAP_SCROLL_DELAY_MAX) * 1000))
            self._raise_if_captcha(page)

        self.log.info("📍 Discovered %d place URLs total (target %d)", len(discovered), self.max_results)
        return discovered[:target_urls]

    def _ultra_extract_leads(
        self,
        context: BrowserContext,
        place_urls: List[str],
        keyword: str,
        location: str,
        stop_event: Event,
    ) -> List[UltraBusinessData]:
        """Ultra deep extraction with multi-engine cross-verification and deduplication."""
        leads: List[UltraBusinessData] = []
        skipped_duplicates = 0
        processed = 0

        for index, place_url in enumerate(place_urls, start=1):
            if stop_event.is_set():
                self.log.info("Stop requested.")
                break
            
            # Stop if we have enough NEW results
            if len(leads) >= self.max_results:
                break

            processed += 1
            self.log.info(
                "🔍 Ultra candidate %d (Collected: %d/%d, Skipped: %d duplicates)",
                processed,
                len(leads),
                self.max_results,
                skipped_duplicates,
            )

            page = context.new_page()
            try:
                lead = self._ultra_extract_single(page, place_url, keyword, location)
                if lead:
                    # Check for duplicates BEFORE applying other filters
                    if self.skip_duplicates:
                        lead_dict = lead.to_dict()
                        if self.history.is_duplicate(lead_dict, keyword, location):
                            skipped_duplicates += 1
                            self.log.info("⏭️ Skipping duplicate: %s", lead.name)
                            continue
                    
                    if self._passes_website_filter(lead.website):
                        lead.extraction_quality = lead.calculate_quality()
                        lead.verification_score = lead.calculate_verification_score()
                        leads.append(lead)
                        self.log.info("✓ NEW: %s (Quality: %s, Verified: %d%%)", 
                                     lead.name, lead.extraction_quality, lead.verification_score)
                        if self.progress_callback:
                            try:
                                self.progress_callback(lead.to_dict())
                            except Exception:
                                # Progress callbacks should never interrupt scraping.
                                pass
            except CaptchaDetectedError:
                raise
            except Exception as e:
                self.log.error("Failed: %s - %s", place_url, e)
            finally:
                page.close()

            self._human_delay(0.2, 0.5)

        return leads

    def _ultra_extract_single(
        self,
        page: Page,
        place_url: str,
        keyword: str,
        location: str,
    ) -> Optional[UltraBusinessData]:
        """Ultra deep extraction for a single business."""
        for attempt in range(2):
            try:
                start_time = time.time()
                page.goto(place_url, timeout=60000)
                page.wait_for_timeout(1800)
                self._raise_if_captcha(page)

                data = UltraBusinessData()
                data.google_maps_url = place_url
                data.data_sources.append("google_maps")

                # ===== PHASE 1: Google Maps Extraction =====
                data.name = self._safe_text(page, "h1.DUwDvf", "h1")
                data.phone = self._extract_phone(page)
                data.website = self._extract_website(page)
                data.has_website = bool(data.website)
                data.address = self._extract_address(page)
                data.rating, data.review_count = self._extract_rating(page)
                data.category = self._extract_category(page)
                data.business_hours = self._extract_hours(page)
                
                gmaps_socials = self._extract_social_from_gmaps(page)
                data.instagram = gmaps_socials.get("instagram", "")
                data.facebook = gmaps_socials.get("facebook", "")
                data.twitter = gmaps_socials.get("twitter", "")

                # ===== PHASE 1B: Fallback via other scrapers (use all scripts) =====
                # Only run when core fields are missing to avoid slowing Ultra.
                if self.basic_maps_engine and (not data.name or not data.phone or not data.website):
                    basic_page = None
                    try:
                        basic_page = page.context.new_page()
                        basic_lead = self.basic_maps_engine._extract_single_listing(basic_page, place_url)
                        if basic_lead:
                            data.data_sources.append("basic_scraper")
                            if not data.name:
                                data.name = basic_lead.get("name", "")
                            if not data.phone:
                                data.phone = basic_lead.get("phone", "")
                            if not data.website:
                                data.website = basic_lead.get("website", "")
                                data.has_website = bool((data.website or "").strip())
                            if not data.emails and basic_lead.get("email"):
                                data.emails = [basic_lead["email"]]
                            if not data.whatsapp_numbers and basic_lead.get("whatsapp"):
                                data.whatsapp_numbers = [basic_lead["whatsapp"]]
                    except Exception as e:
                        self.log.debug("Basic scraper fallback failed: %s", e)
                    finally:
                        try:
                            if basic_page:
                                basic_page.close()
                        except Exception:
                            pass

                if self.enhanced_sync_engine and (not data.address or not data.category or data.rating <= 0):
                    enh_page = None
                    try:
                        enh_page = page.context.new_page()
                        enh_data = self.enhanced_sync_engine._extract_full_listing(enh_page, place_url)
                        if enh_data:
                            data.data_sources.append("enhanced_sync_scraper")
                            if not data.name:
                                data.name = enh_data.name
                            if not data.phone:
                                data.phone = enh_data.phone
                            if not data.website:
                                data.website = enh_data.website
                                data.has_website = bool((data.website or "").strip())
                            if not data.address:
                                data.address = enh_data.address
                            if data.rating <= 0 and getattr(enh_data, "rating", 0):
                                data.rating = enh_data.rating
                                data.review_count = enh_data.review_count
                            if not data.category:
                                data.category = enh_data.category
                            if not data.business_hours:
                                data.business_hours = enh_data.business_hours
                            if not data.plus_code:
                                data.plus_code = enh_data.plus_code
                            # socials fallback
                            if not data.instagram and enh_data.instagram:
                                data.instagram = enh_data.instagram
                            if not data.facebook and enh_data.facebook:
                                data.facebook = enh_data.facebook
                            if not data.twitter and enh_data.twitter:
                                data.twitter = enh_data.twitter
                    except Exception as e:
                        self.log.debug("Enhanced sync scraper fallback failed: %s", e)
                    finally:
                        try:
                            if enh_page:
                                enh_page.close()
                        except Exception:
                            pass

                # ===== PHASE 2: Multi-Engine Website Analysis =====
                if data.website:
                    cache_key = self._website_cache_key(data.website)
                    engine_results = self._website_engine_cache.get(cache_key)
                    if engine_results is None:
                        remaining = LISTING_TIME_BUDGET_SEC - (time.time() - start_time)
                        if remaining >= HEAVY_STEP_MIN_REMAINING_SEC:
                            engine_results = self._multi_engine_website_analysis(
                                page,
                                data.website,
                                data.phone,
                                max_total_time_sec=min(WEBSITE_ANALYSIS_BUDGET_SEC, max(12, int(remaining))),
                            )
                            self._website_engine_cache[cache_key] = list(engine_results)
                    
                    if engine_results is not None:
                        # Cross-verify results
                        verified = self.verifier.merge_and_verify(engine_results)
                        data.data_sources.append("website_multi_engine")
                        
                        # Apply verified data
                        data.emails = verified.get("emails", [])
                        data.emails_verified = verified.get("emails_verified", [])
                        data.whatsapp_numbers = verified.get("whatsapp", [])
                        data.whatsapp_verified = verified.get("whatsapp_verified", [])
                        
                        if not data.instagram and verified.get("instagram"):
                            data.instagram = verified["instagram"]
                            data.instagram_verified = verified.get("instagram_verified", False)
                        
                        if not data.facebook and verified.get("facebook"):
                            data.facebook = verified["facebook"]
                            data.facebook_verified = verified.get("facebook_verified", False)
                        
                        for social in ["twitter", "linkedin", "tiktok", "youtube"]:
                            if verified.get(social):
                                setattr(data, social, verified[social])
                        
                        data.has_chatbot = verified.get("has_chatbot", False)
                        data.chatbot_type = verified.get("chatbot_type", "")
                        data.has_google_analytics = verified.get("has_google_analytics", False)
                        data.has_meta_pixel = verified.get("has_meta_pixel", False)
                        data.cms_platform = verified.get("cms", "")
                        data.is_automated = data.has_chatbot

                # ===== PHASE 3: Google Search Cross-Verification =====
                needs_google_lookup = (
                    data.name
                    and (
                        not data.instagram
                        or not data.facebook
                        or not data.whatsapp_numbers
                        or not data.emails
                    )
                )
                if needs_google_lookup:
                    remaining = LISTING_TIME_BUDGET_SEC - (time.time() - start_time)
                    if remaining < GOOGLE_LOOKUP_MIN_REMAINING_SEC:
                        needs_google_lookup = False

                if needs_google_lookup:
                    google_key = f"{data.name.strip().lower()}|{location.strip().lower()}"
                    if google_key not in self._google_verify_cache:
                        self._google_verify_cache[google_key] = self._google_search_verify(page, data.name, location)
                    google_data = self._google_verify_cache.get(google_key)
                    if google_data:
                        data.data_sources.append("google_search")
                        
                        if not data.instagram and google_data.get("instagram"):
                            data.instagram = google_data["instagram"]
                        if not data.facebook and google_data.get("facebook"):
                            data.facebook = google_data["facebook"]
                        if not data.whatsapp_numbers and google_data.get("whatsapp"):
                            data.whatsapp_numbers = [google_data["whatsapp"]]
                        if not data.emails and google_data.get("email"):
                            data.emails = [google_data["email"]]

                # ===== PHASE 4: Social Media Verification =====
                do_verify_socials = self.verify_socials
                if do_verify_socials:
                    remaining = LISTING_TIME_BUDGET_SEC - (time.time() - start_time)
                    if remaining < SOCIAL_VERIFY_MIN_REMAINING_SEC:
                        do_verify_socials = False

                if do_verify_socials:
                    if data.instagram and not data.instagram_verified:
                        if self._verify_instagram(page, data.instagram, data.name):
                            data.instagram_verified = True
                            data.data_sources.append("instagram_verified")
                    
                    if data.facebook and not data.facebook_verified:
                        if self._verify_facebook(page, data.facebook, data.name):
                            data.facebook_verified = True
                            data.data_sources.append("facebook_verified")

                # ===== PHASE 5: WhatsApp Fallback =====
                if not data.whatsapp_numbers and data.phone:
                    normalized = normalize_phone(data.phone)
                    if normalized:
                        data.whatsapp_numbers = [normalized]

                return data

            except CaptchaDetectedError:
                raise
            except Exception as exc:
                self.log.warning("Attempt %d failed: %s", attempt + 1, exc)
                self._human_delay(1.5, 2.5)

        return None

    def _multi_engine_website_analysis(
        self,
        page: Page,
        website_url: str,
        phone: str,
        max_total_time_sec: int = WEBSITE_ANALYSIS_BUDGET_SEC,
    ) -> List[Dict]:
        """Run multiple extraction engines on a website.

        Prefer fast HTTP crawling (email_extractor WebsiteExtractor) and only fall back to
        Playwright navigation if HTTP yields nothing (JS-heavy / blocked sites).
        """
        results: List[Dict[str, Any]] = []

        if not website_url.startswith(("http://", "https://")):
            website_url = f"https://{website_url}"

        base_url = website_url.rstrip("/")

        start_time = time.time()
        deadline = start_time + max_total_time_sec if max_total_time_sec else None
        combined_html = ""
        try:
            pages = self.email_engine.extractor.crawl_pages(
                base_url,
                max_pages=10,
                max_total_time_sec=max_total_time_sec,
            )
            if pages:
                combined_html = "\n\n".join(p.html for p in pages if p.html)
        except Exception as e:
            self.log.debug("HTTP crawl failed: %s", e)

        # Fallback: small Playwright sample if HTTP crawl didn't return anything.
        if not combined_html:
            all_html: List[Tuple[str, str]] = []
            for path in ULTRA_CONTACT_PAGES[:4]:
                if deadline and time.time() > deadline:
                    break
                try:
                    url = f"{base_url}{path}"
                    response = page.goto(url, timeout=12000, wait_until="domcontentloaded")
                    if response and response.status < 400:
                        page.wait_for_timeout(800)
                        html = page.content()
                        if html:
                            all_html.append((url, html))
                    if len(all_html) >= 2:
                        break
                except Exception:
                    continue

            if all_html:
                combined_html = "\n\n".join(h for _, h in all_html if h)

        if not combined_html:
            # Still return email engine output (URL-based) as a last attempt.
            try:
                email_result = self.email_engine.extract_from_url(base_url, phone)
                results.append(email_result)
            except Exception as e:
                self.log.debug("Email engine error: %s", e)
            return results

        # Run extraction engines on combined corpus (avoid per-page overcount)
        try:
            results.append(self.deep_engine.extract_from_html(combined_html, base_url))
        except Exception as e:
            self.log.debug("Deep engine error: %s", e)

        try:
            results.append(self.biz_engine.extract_from_html(combined_html, base_url))
        except Exception as e:
            self.log.debug("Business engine error: %s", e)

        try:
            email_result = self.email_engine.extract_from_url(base_url, phone)
            results.append(email_result)
        except Exception as e:
            self.log.debug("Email engine error: %s", e)

        return results

    def _website_cache_key(self, website_url: str) -> str:
        if not website_url:
            return ""
        normalized = website_url if website_url.startswith(("http://", "https://")) else f"https://{website_url}"
        parsed = urlparse(normalized)
        host = (parsed.netloc or parsed.path).lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host

    def _google_search_verify(self, page: Page, business_name: str, location: str) -> Optional[Dict]:
        """Search Google for additional verification."""
        try:
            search_query = f'"{business_name}" {location} instagram OR facebook OR whatsapp contact'
            encoded_query = quote_plus(search_query)
            
            page.goto(f"https://www.google.com/search?q={encoded_query}", timeout=15000)
            page.wait_for_timeout(1200)

            html = page.content()
            result = {}

            ig = extract_social_handle(html, INSTAGRAM_PATTERNS, "instagram")
            if ig:
                result["instagram"] = ig

            fb = extract_social_handle(html, FACEBOOK_PATTERNS, "facebook")
            if fb:
                result["facebook"] = fb

            wa_list = extract_whatsapp(html)
            if wa_list:
                result["whatsapp"] = wa_list[0]

            emails = extract_emails(html)
            if emails:
                result["email"] = emails[0]

            return result if result else None

        except Exception:
            return None

    def _verify_instagram(self, page: Page, instagram_url: str, business_name: str) -> bool:
        """Verify Instagram profile matches business."""
        try:
            page.goto(instagram_url, timeout=10000)
            page.wait_for_timeout(1500)
            
            html = page.content().lower()
            name_parts = business_name.lower().split()
            
            # Check if business name appears in profile
            matches = sum(1 for part in name_parts if len(part) > 2 and part in html)
            return matches >= len(name_parts) // 2
        except Exception:
            return False

    def _verify_facebook(self, page: Page, facebook_url: str, business_name: str) -> bool:
        """Verify Facebook page matches business."""
        try:
            page.goto(facebook_url, timeout=10000)
            page.wait_for_timeout(1500)
            
            html = page.content().lower()
            name_parts = business_name.lower().split()
            
            matches = sum(1 for part in name_parts if len(part) > 2 and part in html)
            return matches >= len(name_parts) // 2
        except Exception:
            return False

    def _passes_website_filter(self, website: str) -> bool:
        """Check website filter."""
        has_website = is_business_website(website)
        if self.website_filter == "with":
            return has_website
        if self.website_filter == "without":
            return not has_website
        return True

    def _safe_text(self, page: Page, selector: str, fallback: str = "") -> str:
        """Safely extract text."""
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                return locator.inner_text(timeout=4000).strip()
        except Exception:
            pass
        if fallback:
            try:
                locator = page.locator(fallback).first
                if locator.count() > 0:
                    return locator.inner_text(timeout=3000).strip()
            except Exception:
                pass
        return ""

    def _extract_phone(self, page: Page) -> str:
        """Extract phone."""
        selectors = [
            "button[data-item-id^='phone:tel:']",
            "a[data-item-id^='phone:tel:']",
            "button[aria-label*='Phone']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    text = locator.inner_text(timeout=3500).strip()
                    match = PHONE_REGEX.search(text)
                    if match:
                        return match.group(1).strip()
            except Exception:
                continue
        return ""

    def _extract_website(self, page: Page) -> str:
        """Extract website."""
        selectors = [
            "a[data-item-id='authority']",
            "a[aria-label*='Website']",
        ]
        for selector in selectors:
            try:
                anchor = page.locator(selector).first
                if anchor.count() > 0:
                    href = anchor.get_attribute("href") or ""
                    if href.startswith("http"):
                        return normalize_business_website(href)
            except Exception:
                continue
        return ""

    def _extract_address(self, page: Page) -> str:
        """Extract address."""
        selectors = [
            "button[data-item-id='address']",
            "button[aria-label*='Address']",
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    text = locator.inner_text(timeout=3000)
                    return clean_address(text)
            except Exception:
                continue
        return ""

    def _extract_rating(self, page: Page) -> Tuple[float, int]:
        """Extract rating."""
        rating, review_count = 0.0, 0
        try:
            rating_el = page.locator("span.ceNzKf, div.F7nice span[aria-hidden='true']").first
            if rating_el.count() > 0:
                text = rating_el.inner_text(timeout=3000)
                match = re.search(r"[\d.]+", text)
                if match:
                    rating = float(match.group())
        except Exception:
            pass
        try:
            review_el = page.locator("span.UY7F9").first
            if review_el.count() > 0:
                text = review_el.inner_text(timeout=3000)
                match = re.search(r"([\d,]+)", text)
                if match:
                    review_count = int(match.group(1).replace(",", ""))
        except Exception:
            pass
        return rating, review_count

    def _extract_category(self, page: Page) -> str:
        """Extract category."""
        try:
            el = page.locator("button.DkEaL, span.DkEaL").first
            if el.count() > 0:
                return el.inner_text(timeout=3000).strip()
        except Exception:
            pass
        return ""

    def _extract_hours(self, page: Page) -> str:
        """Extract hours."""
        try:
            el = page.locator("button[data-item-id*='oh']").first
            if el.count() > 0:
                text = el.inner_text(timeout=3000).strip()
                return re.sub(r"\s+", " ", text)
        except Exception:
            pass
        return ""

    def _extract_social_from_gmaps(self, page: Page) -> Dict[str, str]:
        """Extract socials from Maps."""
        socials = {}
        try:
            links = page.eval_on_selector_all(
                "a[href*='instagram.com'], a[href*='facebook.com'], a[href*='twitter.com'], a[href*='x.com']",
                "els => els.map(el => el.href)"
            )
            for link in links:
                if "instagram.com" in link.lower():
                    socials["instagram"] = link
                elif "facebook.com" in link.lower():
                    socials["facebook"] = link
                elif "twitter.com" in link.lower() or "x.com" in link.lower():
                    socials["twitter"] = link
        except Exception:
            pass
        return socials

    def _raise_if_captcha(self, page: Page) -> None:
        """Check for CAPTCHA."""
        if not self._is_captcha_present(page):
            return

        if not self.headless:
            deadline = time.time() + (CAPTCHA_MANUAL_WAIT_MS / 1000)
            self.log.warning("Captcha challenge detected. Waiting for manual solve in browser window.")

            while time.time() < deadline:
                page.wait_for_timeout(CAPTCHA_POLL_MS)
                if not self._is_captcha_present(page):
                    self.log.info("Captcha challenge cleared manually. Resuming scrape.")
                    return

            raise CaptchaDetectedError("Captcha challenge not cleared in time")

        raise CaptchaDetectedError("Captcha detected")

    def _is_captcha_present(self, page: Page) -> bool:
        try:
            content = page.content().lower()
        except Exception:
            return False
        return any(marker in content for marker in CAPTCHA_MARKERS)

    def _human_delay(self, minimum: Optional[float] = None, maximum: Optional[float] = None) -> None:
        """Human delay."""
        min_d = self.min_delay if minimum is None else minimum
        max_d = self.max_delay if maximum is None else maximum
        time.sleep(random.uniform(min_d, max_d))


# Alias for compatibility
GoogleMapsScraper = UltraDeepScraper

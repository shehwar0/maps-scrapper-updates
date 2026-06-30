"""
Enhanced Google Maps Scraper (Sync Version)
Extracts comprehensive business details with focus on ACCURACY and COMPLETENESS.
Uses sync Playwright API for Flask compatibility.
"""

import logging
import random
import re
import time
from threading import Event
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from business_extractor import BusinessData, analyze_website
from email_extractor import WebsiteExtractor
from maps_city_coverage import build_citywide_queries
from url_filters import is_business_website, normalize_business_website
from scraper_utils import apply_stealth, block_heavy_resources, robust_scroll_to_end, create_dry_run_leads, safe_launch_browser

try:
    from fake_useragent import UserAgent
    _UA = UserAgent()
except Exception:
    _UA = None

PHONE_REGEX = re.compile(r"(\+?\d[\d\s()\-]{6,}\d)")
MAX_RESULTS_CAP = 500
RESULT_SCAN_WINDOW = 320
CITYWIDE_QUERY_LIMIT = 30
MAP_STAGNANT_ROUNDS = 28
MAP_SCROLL_DELAY_MIN = 0.35
MAP_SCROLL_DELAY_MAX = 0.85
URL_DISCOVERY_MAX_SEC = 240
DISCOVERY_STABLE_ROUNDS_FOR_END = 4
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
LISTING_TIME_BUDGET_SEC = 45
WEBSITE_ANALYSIS_BUDGET_SEC = 14
HEAVY_STEP_MIN_REMAINING_SEC = 7

# Pages to check for contact info (in order of priority)
CONTACT_PAGES = ["", "/contact", "/contact-us", "/about", "/about-us", "/team"]


class CaptchaDetectedError(RuntimeError):
    pass


class GoogleMapsScraper:
    """Enhanced scraper that extracts comprehensive business intelligence."""
    
    def __init__(
        self,
        max_results: int = 50,
        headless: bool = False,
        min_delay: float = 0.7,
        max_delay: float = 1.6,
        website_filter: str = "all",
        dry_run: bool = False,
        logger: Optional[logging.Logger] = None,
        progress_callback: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> None:
        self.max_results = max(1, min(max_results, MAX_RESULTS_CAP))
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.website_filter = website_filter if website_filter in {"all", "with", "without"} else "all"
        self.dry_run = dry_run
        self.log = logger or logging.getLogger(__name__)
        self.progress_callback = progress_callback
        self._website_cache: Dict[str, Dict] = {}
    
    def scrape(
        self,
        keyword: str,
        location: str,
        stop_event: Optional[Event] = None,
    ) -> List[Dict[str, str]]:
        """Main scrape method."""
        stop_event = stop_event or Event()
        if self.dry_run:
            self.log.info("🧪 ENHANCED DRY-RUN (synthetic powerful test data)")
            return create_dry_run_leads(keyword, location, self.max_results)

        search_queries = build_citywide_queries(keyword, location, max_queries=CITYWIDE_QUERY_LIMIT)
        if not search_queries:
            return []
        
        with sync_playwright() as p:
            browser, context = safe_launch_browser(p, headless=self.headless, logger=self.log)
            page = context.new_page()
            apply_stealth(page)
            block_heavy_resources(page)
            
            try:
                if len(search_queries) > 1:
                    self.log.info("Using %d map zones for broader city coverage", len(search_queries))

                discovered: List[str] = []
                seen: Set[str] = set()
                target_urls = self.max_results
                per_query_target = max(8, (target_urls + len(search_queries) - 1) // len(search_queries))

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

                leads = self._collect_lead_details(context, discovered[:target_urls], stop_event)
                return [lead.to_dict() for lead in leads]
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
        encoded_query = quote_plus(query)
        try:
            page.goto(f"https://www.google.com/maps/search/{encoded_query}", timeout=70000, wait_until="domcontentloaded")
        except Exception:
            page.goto("https://www.google.com/maps", timeout=25000)
        page.wait_for_timeout(1000)
        self._maybe_accept_consent(page)
        self._raise_if_captcha(page)
        
        if self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=36000):
            page.wait_for_timeout(550)
            return
        
        search_input = self._find_search_input(page)
        if search_input:
            try:
                search_input.fill(query)
                self._human_delay(0.15, 0.4)
                search_input.press("Enter")
            except Exception:
                pass
        
        if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc", "h1.DUwDvf"], timeout_ms=38000):
            try:
                page.reload(timeout=15000)
            except Exception:
                pass
            if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=15000):
                raise RuntimeError("Google Maps results failed to load.")
        
        self._human_delay(0.25, 0.55)
        self._raise_if_captcha(page)
    
    def _find_search_input(self, page: Page):
        """Find the search input element."""
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
        deadline = time.time() + (timeout_ms / 1000)
        poll = 280
        while time.time() < deadline:
            for selector in selectors:
                try:
                    if page.locator(selector).first.count() > 0:
                        return True
                except Exception:
                    continue
            page.wait_for_timeout(poll)
            poll = min(600, int(poll * 1.08))
        return False
    
    def _maybe_accept_consent(self, page: Page) -> None:
        """Accept cookie consent if present."""
        selectors = [
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Accept')",
            "button[aria-label='Accept all']",
            "form button[type='submit']",
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
            end_markers = ("you've reached the end", "end of the list", "no more results", "reached the end", "no other results")
            if any(m in content for m in end_markers):
                return True
            for sel in ["div[role='status']"]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        txt = (el.inner_text(timeout=600) or "").lower()
                        if "end" in txt or "no more" in txt:
                            return True
                except Exception:
                    pass
            return False
        except Exception:
            return False

    def _collect_place_urls(self, page: Page, stop_event: Event, target_count: Optional[int] = None) -> List[str]:
        """Robust URL collection (end detection + time budget)."""
        discovered: List[str] = []
        seen: Set[str] = set()
        stagnant_rounds = 0
        max_stagnant_rounds = MAP_STAGNANT_ROUNDS
        target_urls = max(1, target_count or self.max_results)
        start_time = time.time()

        current_url = page.url or ""
        if "/maps/place/" in current_url:
            return [current_url][:target_urls]

        feed = None
        try:
            feed = page.locator("div[role='feed']").first
            if feed.count() == 0:
                feed = None
        except Exception:
            feed = None

        while len(discovered) < target_urls and stagnant_rounds < max_stagnant_rounds and not stop_event.is_set():
            if (time.time() - start_time) > URL_DISCOVERY_MAX_SEC:
                self.log.warning("Discovery budget exhausted.")
                break

            before = len(discovered)
            hrefs: List[str] = []
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
            else:
                try:
                    links = page.locator("a.hfpxzc")
                    count = links.count()
                    start_idx = max(0, count - RESULT_SCAN_WINDOW)
                    for idx in range(start_idx, count):
                        if stop_event.is_set() or len(discovered) >= target_urls:
                            break
                        try:
                            href = links.nth(idx).get_attribute("href", timeout=1200) or ""
                            if href and href not in seen:
                                seen.add(href)
                                discovered.append(href)
                                newly += 1
                        except PlaywrightTimeoutError:
                            continue
                except Exception:
                    pass

            reached_end = self._is_end_of_results(page)

            if len(discovered) == before:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            if stagnant_rounds >= DISCOVERY_STABLE_ROUNDS_FOR_END and (reached_end or newly == 0):
                break

            try:
                if feed and feed.count() > 0:
                    feed.evaluate("(el) => el.scrollBy(0, Math.floor(el.scrollHeight*0.9) + 160)")
                else:
                    page.mouse.wheel(0, 4800)
            except Exception:
                try:
                    page.evaluate("window.scrollBy(0, 4200)")
                except Exception:
                    pass

            page.wait_for_timeout(int(random.uniform(MAP_SCROLL_DELAY_MIN, MAP_SCROLL_DELAY_MAX) * 1000))
            self._raise_if_captcha(page)

        self.log.info("Discovered %s place urls", len(discovered))
        return discovered[:target_urls]
    
    def _collect_lead_details(
        self, 
        context: BrowserContext, 
        place_urls: List[str], 
        stop_event: Event
    ) -> List[BusinessData]:
        """Extract detailed business data from each listing."""
        leads: List[BusinessData] = []
        
        for index, place_url in enumerate(place_urls, start=1):
            if stop_event.is_set():
                self.log.info("Stop requested. Ending scrape early.")
                break
            
            self.log.info("Processing %s/%s", index, len(place_urls))
            
            # Create a new page for each listing to avoid state issues
            # Use reusable page for speed (create once)
            if not hasattr(self, "_detail_page") or self._detail_page is None:
                self._detail_page = context.new_page()
            page = self._detail_page
            try:
                lead = self._extract_full_listing(page, place_url)
                if lead:
                    if self._passes_website_filter(lead.website):
                        lead.extraction_quality = lead.calculate_quality()
                        leads.append(lead)
                        self.log.info("Successfully extracted: %s (Total: %d)", lead.name, len(leads))
                        if self.progress_callback:
                            try:
                                self.progress_callback(lead.to_dict())
                            except Exception:
                                pass
            except Exception as e:
                self.log.error("Failed to extract %s: %s", place_url, e)
                try:
                    page.goto("https://www.google.com/maps", timeout=12000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            self._human_delay(0.15, 0.4)
        
        # cleanup reusable page (context will close anyway)
        if hasattr(self, "_detail_page") and self._detail_page:
            try:
                self._detail_page.close()
            except Exception:
                pass
            self._detail_page = None
        
        return leads
    
    def _passes_website_filter(self, website: str) -> bool:
        """Check if listing passes website filter."""
        has_website = is_business_website(website)
        if self.website_filter == "with":
            return has_website
        if self.website_filter == "without":
            return not has_website
        return True
    
    def _extract_full_listing(self, page: Page, place_url: str) -> Optional[BusinessData]:
        """Extract comprehensive data from a single Google Maps listing."""
        for attempt in range(2):
            try:
                start_time = time.time()
                page.goto(place_url, timeout=38000)
                page.wait_for_timeout(800)
                self._raise_if_captcha(page)
                
                # Create business data object
                data = BusinessData()
                data.google_maps_url = place_url
                
                # Extract all Google Maps data
                data.name = self._safe_text(page, "h1.DUwDvf", fallback_selector="h1")
                data.phone = self._extract_phone(page)
                data.website = self._extract_website(page)
                data.has_website = bool(data.website)
                data.address = self._extract_address(page)
                data.rating, data.review_count = self._extract_rating(page)
                data.category = self._extract_category(page)
                data.business_hours = self._extract_hours(page)
                data.plus_code = self._extract_plus_code(page)
                
                # Extract social links from Google Maps (if shown)
                gmaps_socials = self._extract_social_from_gmaps(page)
                data.instagram = gmaps_socials.get("instagram", "")
                data.facebook = gmaps_socials.get("facebook", "")
                data.twitter = gmaps_socials.get("twitter", "")
                
                # If has website, extract additional data
                if data.website:
                    cache_key = self._website_cache_key(data.website)
                    website_data = self._website_cache.get(cache_key)
                    if website_data is None:
                        remaining = LISTING_TIME_BUDGET_SEC - (time.time() - start_time)
                        if remaining >= HEAVY_STEP_MIN_REMAINING_SEC:
                            website_data = self._analyze_website(
                                page,
                                data.website,
                                max_total_time_sec=min(WEBSITE_ANALYSIS_BUDGET_SEC, max(8, int(remaining))),
                            )
                            self._website_cache[cache_key] = dict(website_data)

                    if website_data is not None:
                        # Merge website data
                        data.emails = website_data.get("emails", [])
                        data.whatsapp_numbers = website_data.get("whatsapp_numbers", [])
                        
                        socials = website_data.get("socials", {})
                        if not data.instagram:
                            data.instagram = socials.get("instagram", "")
                        if not data.facebook:
                            data.facebook = socials.get("facebook", "")
                        if not data.twitter:
                            data.twitter = socials.get("twitter", "")
                        data.linkedin = socials.get("linkedin", "")
                        data.tiktok = socials.get("tiktok", "")
                        data.youtube = socials.get("youtube", "")
                        
                        data.has_chatbot = website_data.get("has_chatbot", False)
                        data.chatbot_type = website_data.get("chatbot_type", "")
                        data.has_google_analytics = website_data.get("has_google_analytics", False)
                        data.has_meta_pixel = website_data.get("has_meta_pixel", False)
                        data.has_other_analytics = website_data.get("other_analytics", [])
                        data.cms_platform = website_data.get("cms_platform", "")
                        data.is_automated = data.has_chatbot
                    elif data.phone:
                        normalized = self._normalize_phone(data.phone)
                        if normalized:
                            data.whatsapp_numbers = [normalized]
                else:
                    # Use phone as WhatsApp fallback
                    if data.phone:
                        normalized = self._normalize_phone(data.phone)
                        if normalized:
                            data.whatsapp_numbers = [normalized]
                
                return data
                
            except CaptchaDetectedError:
                raise
            except Exception as exc:
                self.log.warning("Failed listing attempt %s for %s: %s", attempt + 1, place_url, exc)
                self._human_delay(1.2, 2.2)
        
        return None
    
    def _safe_text(self, page: Page, selector: str, fallback_selector: str = "") -> str:
        """Safely extract text from element."""
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                value = locator.inner_text(timeout=4000).strip()
                if value:
                    return value
        except Exception:
            pass
        
        if fallback_selector:
            try:
                locator = page.locator(fallback_selector).first
                if locator.count() > 0:
                    value = locator.inner_text(timeout=3000).strip()
                    if value:
                        return value
            except Exception:
                pass
        
        return ""
    
    def _extract_phone(self, page: Page) -> str:
        """Extract phone number from Google Maps listing."""
        selectors = [
            "button[data-item-id^='phone:tel:']",
            "button[aria-label*='Phone']",
            "button[aria-label*='phone']",
            "a[data-item-id^='phone:tel:']",
        ]
        
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                text = self._clean_phone_text(locator.inner_text(timeout=3500))
                if text:
                    return text
            except Exception:
                continue
        
        return ""
    
    def _clean_phone_text(self, value: str) -> str:
        """Clean and normalize phone text."""
        raw = (value or "").strip()
        if not raw:
            return ""
        match = PHONE_REGEX.search(raw)
        if match:
            return match.group(1).strip()
        cleaned = re.sub(r"[^0-9+()\-\s.]", "", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned
    
    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone to digits only."""
        if not phone:
            return ""
        digits = re.sub(r"[^\d+]", "", phone)
        return digits if len(digits) >= 8 else ""
    
    def _extract_website(self, page: Page) -> str:
        """Extract website URL from Google Maps listing."""
        selectors = [
            "a[data-item-id='authority']",
            "a[aria-label*='Website']",
            "a[aria-label*='website']",
        ]
        
        for selector in selectors:
            try:
                anchor = page.locator(selector).first
                if anchor.count() == 0:
                    continue
                href = anchor.get_attribute("href") or ""
                if href and href.startswith("http"):
                    return normalize_business_website(href)
            except Exception:
                continue
        
        return ""
    
    def _extract_address(self, page: Page) -> str:
        """Extract business address from Google Maps."""
        selectors = [
            "button[data-item-id='address']",
            "button[aria-label*='Address']",
            "button[aria-label*='address']",
        ]
        
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    text = locator.inner_text(timeout=3000).strip()
                    if text:
                        return text
            except Exception:
                continue
        
        return ""
    
    def _extract_rating(self, page: Page) -> tuple:
        """Extract rating and review count."""
        rating = 0.0
        review_count = 0
        
        try:
            # Try to find rating span
            rating_el = page.locator("span.ceNzKf, div.F7nice span[aria-hidden='true']").first
            if rating_el.count() > 0:
                rating_text = rating_el.inner_text(timeout=3000)
                match = re.search(r"[\d.]+", rating_text)
                if match:
                    rating = float(match.group())
        except Exception:
            pass
        
        try:
            # Try to find review count
            review_el = page.locator("span.UY7F9, button[jsaction*='review'] span").first
            if review_el.count() > 0:
                review_text = review_el.inner_text(timeout=3000)
                review_match = re.search(r"[\d,]+", review_text.replace(",", ""))
                if review_match:
                    review_count = int(review_match.group())
        except Exception:
            pass
        
        return rating, review_count
    
    def _extract_category(self, page: Page) -> str:
        """Extract business category."""
        try:
            # Category is often shown as a button below the name
            category_el = page.locator("button.DkEaL, span.DkEaL").first
            if category_el.count() > 0:
                return category_el.inner_text(timeout=3000).strip()
        except Exception:
            pass
        return ""
    
    def _extract_hours(self, page: Page) -> str:
        """Extract business hours."""
        try:
            hours_button = page.locator("button[data-item-id*='oh'], button[aria-label*='hour']").first
            if hours_button.count() > 0:
                return hours_button.inner_text(timeout=3000).strip()
        except Exception:
            pass
        return ""
    
    def _extract_plus_code(self, page: Page) -> str:
        """Extract Plus Code (location code)."""
        try:
            plus_el = page.locator("button[data-item-id='oloc']").first
            if plus_el.count() > 0:
                return plus_el.inner_text(timeout=3000).strip()
        except Exception:
            pass
        return ""
    
    def _extract_social_from_gmaps(self, page: Page) -> Dict[str, str]:
        """Extract social links shown directly on Google Maps."""
        socials = {}
        
        try:
            # Look for social media links in the info section
            links = page.eval_on_selector_all(
                "a[href*='instagram.com'], a[href*='facebook.com'], a[href*='twitter.com'], a[href*='x.com']",
                "els => els.map(el => el.href)"
            )
            
            for link in links:
                if "instagram.com" in link:
                    socials["instagram"] = link
                elif "facebook.com" in link:
                    socials["facebook"] = link
                elif "twitter.com" in link or "x.com" in link:
                    socials["twitter"] = link
        except Exception:
            pass
        
        return socials

    def _website_cache_key(self, website_url: str) -> str:
        if not website_url:
            return ""
        normalized = website_url if website_url.startswith(("http://", "https://")) else f"https://{website_url}"
        parsed = urlparse(normalized)
        host = (parsed.netloc or parsed.path).lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host
    
    def _analyze_website(self, page: Page, website_url: str, max_total_time_sec: int = WEBSITE_ANALYSIS_BUDGET_SEC) -> Dict:
        """Analyze a business website for contact info + tech stack.

        Prefer fast HTTP crawling (email_extractor WebsiteExtractor) and only fall back
        to Playwright navigation when HTTP yields nothing.
        """
        combined_data = {
            "emails": [],
            "whatsapp_numbers": [],
            "socials": {},
            "has_chatbot": False,
            "chatbot_type": "",
            "has_google_analytics": False,
            "has_meta_pixel": False,
            "other_analytics": [],
            "cms_platform": "",
        }

        if not website_url.startswith(("http://", "https://")):
            website_url = f"https://{website_url}"

        base_url = website_url.rstrip("/")

        start_time = time.time()
        deadline = start_time + max_total_time_sec if max_total_time_sec else None

        # ---- Phase 1: HTTP crawl (fast) ----
        try:
            crawler = WebsiteExtractor(timeout=9)
            pages = crawler.crawl_pages(
                base_url,
                max_pages=5,
                max_total_time_sec=max_total_time_sec,
                priority_only=True,
            )
            if pages:
                corpus = "\n\n".join(p.html for p in pages if p.html)
                page_data = analyze_website(corpus, base_url)

                combined_data["emails"] = list(dict.fromkeys(page_data.get("emails", [])))
                combined_data["whatsapp_numbers"] = list(dict.fromkeys(page_data.get("whatsapp_numbers", [])))
                combined_data["socials"] = page_data.get("socials", {}) or {}
                combined_data["has_chatbot"] = bool(page_data.get("has_chatbot"))
                combined_data["chatbot_type"] = page_data.get("chatbot_type", "") or ""
                combined_data["has_google_analytics"] = bool(page_data.get("has_google_analytics"))
                combined_data["has_meta_pixel"] = bool(page_data.get("has_meta_pixel"))
                combined_data["other_analytics"] = list(dict.fromkeys(page_data.get("other_analytics", []) or []))
                combined_data["cms_platform"] = page_data.get("cms_platform", "") or ""
                if combined_data["emails"]:
                    return combined_data
        except Exception as e:
            self.log.debug("HTTP crawl failed: %s", e)

        # ---- Phase 2: Playwright fallback ----
        for path in CONTACT_PAGES:
            if deadline and time.time() > deadline:
                break
            try:
                url = f"{base_url}{path}"
                response = page.goto(url, timeout=15000, wait_until="domcontentloaded")

                if not response or response.status >= 400:
                    continue

                page.wait_for_timeout(1000)
                html = page.content()
                page_data = analyze_website(html, url)

                for email in page_data.get("emails", []):
                    if email not in combined_data["emails"]:
                        combined_data["emails"].append(email)

                for wa in page_data.get("whatsapp_numbers", []):
                    if wa not in combined_data["whatsapp_numbers"]:
                        combined_data["whatsapp_numbers"].append(wa)

                for platform, link in page_data.get("socials", {}).items():
                    if platform not in combined_data["socials"]:
                        combined_data["socials"][platform] = link

                if page_data.get("has_chatbot"):
                    combined_data["has_chatbot"] = True
                    combined_data["chatbot_type"] = page_data.get("chatbot_type", "")

                if page_data.get("has_google_analytics"):
                    combined_data["has_google_analytics"] = True

                if page_data.get("has_meta_pixel"):
                    combined_data["has_meta_pixel"] = True

                for tool in page_data.get("other_analytics", []):
                    if tool not in combined_data["other_analytics"]:
                        combined_data["other_analytics"].append(tool)

                if page_data.get("cms_platform") and not combined_data["cms_platform"]:
                    combined_data["cms_platform"] = page_data["cms_platform"]

                if combined_data["emails"] and combined_data["whatsapp_numbers"]:
                    break

            except Exception as e:
                self.log.debug("Error analyzing %s: %s", url, e)
                continue

        return combined_data
    
    def _raise_if_captcha(self, page: Page) -> None:
        """Check for CAPTCHA and raise error if detected."""
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

        raise CaptchaDetectedError("Captcha or anti-bot challenge detected on Google Maps")

    def _is_captcha_present(self, page: Page) -> bool:
        try:
            content = page.content().lower()
        except Exception:
            return False
        return any(marker in content for marker in CAPTCHA_MARKERS)
    
    def _human_delay(self, minimum: Optional[float] = None, maximum: Optional[float] = None) -> None:
        """Add human-like delay."""
        min_d = self.min_delay if minimum is None else minimum
        max_d = self.max_delay if maximum is None else maximum
        time.sleep(random.uniform(min_d, max_d))

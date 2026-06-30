import logging
import random
import re
import time
from threading import Event
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from email_extractor import WebsiteExtractor
from maps_city_coverage import build_citywide_queries
from url_filters import is_business_website, normalize_business_website
from scraper_utils import apply_stealth, block_heavy_resources, create_dry_run_leads, robust_scroll_to_end, safe_launch_browser

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
WEBSITE_ENRICH_BUDGET_SEC = 18
HEAVY_STEP_MIN_REMAINING_SEC = 8


class CaptchaDetectedError(RuntimeError):
    pass


class GoogleMapsScraper:
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
        self.website_extractor = WebsiteExtractor()
        self.progress_callback = progress_callback
        self._enrichment_cache: Dict[str, Dict[str, str]] = {}

    def scrape(
        self,
        keyword: str,
        location: str,
        stop_event: Optional[Event] = None,
    ) -> List[Dict[str, str]]:
        stop_event = stop_event or Event()
        if self.dry_run:
            self.log.info("🧪 BASIC DRY-RUN")
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

                leads = self._collect_lead_details(page, discovered[:target_urls], stop_event)
                return leads
            finally:
                context.close()
                browser.close()

    def _search_query_with_retries(self, page, query: str, stop_event: Event, target_count: int) -> List[str]:
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

    def _open_and_search(self, page, query: str) -> None:
        encoded_query = quote_plus(query)
        try:
            page.goto(f"https://www.google.com/maps/search/{encoded_query}", timeout=70000)
        except Exception:
            page.goto("https://www.google.com/maps", timeout=25000)
        page.wait_for_timeout(900)
        self._maybe_accept_consent(page)
        self._raise_if_captcha(page)

        if self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=34000):
            page.wait_for_timeout(500)
            return

        search_input = self._find_search_input(page)
        if search_input:
            try:
                search_input.fill(query)
                self._human_delay(0.2, 0.5)
                search_input.press("Enter")
            except Exception:
                pass

        if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc", "h1.DUwDvf"], timeout_ms=36000):
            try:
                page.reload(timeout=12000)
            except Exception:
                pass
            if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=12000):
                raise RuntimeError("Google Maps results did not load.")

        self._human_delay(0.25, 0.55)
        self._raise_if_captcha(page)

    def _find_search_input(self, page):
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

    def _wait_for_any(self, page, selectors: List[str], timeout_ms: int) -> bool:
        deadline = time.time() + (timeout_ms / 1000)
        poll = 260
        while time.time() < deadline:
            for selector in selectors:
                try:
                    if page.locator(selector).first.count() > 0:
                        return True
                except Exception:
                    continue
            page.wait_for_timeout(poll)
            poll = min(580, int(poll * 1.08))
        return False

    def _maybe_accept_consent(self, page) -> None:
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

    def _is_end_of_results(self, page) -> bool:
        try:
            try:
                content = (page.content() or "").lower()
            except Exception:
                content = ""
            end_markers = ("you've reached the end", "end of the list", "no more results", "reached the end")
            if any(m in content for m in end_markers):
                return True
            try:
                el = page.locator("div[role='status']").first
                if el.count() > 0:
                    txt = (el.inner_text(timeout=500) or "").lower()
                    if "end" in txt or "no more" in txt:
                        return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def _collect_place_urls(self, page, stop_event: Event, target_count: Optional[int] = None) -> List[str]:
        discovered: List[str] = []
        seen: Set[str] = set()
        stagnant_rounds = 0
        max_stagnant_rounds = MAP_STAGNANT_ROUNDS
        target_urls = max(1, target_count or self.max_results)
        start_time = time.time()

        if "/maps/place/" in (page.url or ""):
            return [page.url][:target_urls]

        apply_stealth(page)
        block_heavy_resources(page)
        final_c, _ = robust_scroll_to_end(page, stop_event, target_urls, max_scrolls=65, logger=self.log)

        discovered = []
        seen = set()
        try:
            for h in page.eval_on_selector_all("a.hfpxzc", "els => els.map(e => e.getAttribute('href')||e.href||'').filter(Boolean)"):
                if h and h not in seen:
                    seen.add(h)
                    discovered.append(h)
                    if len(discovered) >= target_urls:
                        break
        except Exception:
            pass
        self.log.info("Discovered %s place urls (robust)", len(discovered))
        return discovered[:target_urls]

    def _collect_lead_details(self, page, place_urls: List[str], stop_event: Event) -> List[Dict[str, str]]:
        leads: List[Dict[str, str]] = []

        for index, place_url in enumerate(place_urls, start=1):
            if stop_event.is_set():
                self.log.info("Stop requested. Ending scrape early.")
                break

            self.log.info("Processing %s/%s", index, len(place_urls))
            lead = self._extract_single_listing(page, place_url)
            if not lead:
                continue

            if not self._passes_website_filter(lead.get("website", "")):
                continue

            leads.append(lead)
            if self.progress_callback:
                try:
                    self.progress_callback(dict(lead))
                except Exception:
                    pass
            self._human_delay(0.12, 0.35)

        return leads

    def _passes_website_filter(self, website: str) -> bool:
        has_website = is_business_website(website)
        if self.website_filter == "with":
            return has_website
        if self.website_filter == "without":
            return not has_website
        return True

    def _extract_single_listing(self, page, place_url: str) -> Optional[Dict[str, str]]:
        for attempt in range(2):
            try:
                start_time = time.time()
                page.goto(place_url, timeout=38000)
                page.wait_for_timeout(700)
                self._raise_if_captcha(page)

                name = self._safe_text(page, "h1.DUwDvf", fallback_selector="h1")
                phone = self._extract_phone(page)
                website = self._extract_website(page)

                if website:
                    cache_key = self._website_cache_key(website)
                    enrichment = self._enrichment_cache.get(cache_key)
                    if enrichment is None:
                        remaining = LISTING_TIME_BUDGET_SEC - (time.time() - start_time)
                        if remaining < HEAVY_STEP_MIN_REMAINING_SEC:
                            enrichment = {
                                "email": "",
                                "whatsapp": self.website_extractor._normalize_phone(phone),
                            }
                        else:
                            enrichment = self.website_extractor.enrich(
                                website,
                                fallback_phone=phone,
                                max_pages=5,
                                max_total_time_sec=min(WEBSITE_ENRICH_BUDGET_SEC, max(6, int(remaining))),
                                priority_only=True,
                            )
                        self._enrichment_cache[cache_key] = dict(enrichment)
                else:
                    enrichment = {
                        "email": "",
                        "whatsapp": self.website_extractor._normalize_phone(phone),
                    }

                return {
                    "name": name,
                    "phone": phone,
                    "email": enrichment.get("email", ""),
                    "website": website,
                    "whatsapp": enrichment.get("whatsapp", ""),
                    "google_maps_url": place_url,
                    "has_website": "Yes" if is_business_website(website) else "No",
                }
            except CaptchaDetectedError:
                raise
            except Exception as exc:
                self.log.warning("Failed listing attempt %s for %s: %s", attempt + 1, place_url, exc)
                self._human_delay(1.2, 2.2)

        return None

    def _safe_text(self, page, selector: str, fallback_selector: str = "") -> str:
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

    def _website_cache_key(self, website_url: str) -> str:
        if not website_url:
            return ""
        normalized = website_url if website_url.startswith(("http://", "https://")) else f"https://{website_url}"
        parsed = urlparse(normalized)
        host = (parsed.netloc or parsed.path).lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host

    def _extract_phone(self, page) -> str:
        selectors = [
            "button[data-item-id^='phone:tel:']",
            "button[aria-label*='Phone']",
            "button[aria-label*='phone']",
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
        raw = (value or "").strip()
        if not raw:
            return ""
        match = PHONE_REGEX.search(raw)
        if match:
            return match.group(1).strip()
        # Remove obvious non-phone symbols while preserving useful separators.
        cleaned = re.sub(r"[^0-9+()\-\s.]", "", raw)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _extract_website(self, page) -> str:
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

    def _raise_if_captcha(self, page) -> None:
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

    def _is_captcha_present(self, page) -> bool:
        try:
            content = page.content().lower()
        except Exception:
            return False
        return any(marker in content for marker in CAPTCHA_MARKERS)

    def _human_delay(self, minimum: Optional[float] = None, maximum: Optional[float] = None) -> None:
        min_d = self.min_delay if minimum is None else minimum
        max_d = self.max_delay if maximum is None else maximum
        time.sleep(random.uniform(min_d, max_d))

"""
Powerful shared utilities for Google Maps scraping.
Includes stealth, resource blocking, hybrid card extraction,
robust scrolling, dry-run simulation, and checkpointing.

Designed for maximum efficiency, reliability and power when scraping 100-500 results.
"""

import json
import os
import random
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Set, Tuple

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# Production UA rotation booster (reduces blocks, increases effective speed)
try:
    from fake_useragent import UserAgent
    _UA = UserAgent()
except Exception:
    _UA = None

# Common modern Google Maps selectors (updated from research + live patterns 2025-2026)
CARD_NAME_SELECTORS = [
    "div.fontHeadlineSmall",
    "div.qBF1Pd",
    ".hfpxzc + div > div > div > span",
    "a.hfpxzc",
]
CARD_RATING_SELECTORS = ["span.MW4etd", "span[aria-hidden='true']", ".ceNzKf"]
CARD_REVIEWS_SELECTORS = ["span.UY7F9", "span[aria-label*='review']", "button[jsaction*='review'] span"]
CARD_ADDRESS_SELECTORS = [".W4Efsd:nth-child(2)", ".W4Efsd", "div.W4Efsd > span"]
CARD_CATEGORY_SELECTORS = ["div.W4Efsd > span:not(:has(> span))", ".DkEaL"]

PHONE_CARD_HINT = re.compile(r"(\+?\d[\d\s()\-]{6,}\d)")  # sometimes visible

CHECKPOINT_DIR = Path(__file__).parent.parent / "output" / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

@dataclass
class CardData:
    """Data pre-extracted from a results card (no full page load needed)."""
    url: str = ""
    name: str = ""
    rating: float = 0.0
    review_count: int = 0
    address: str = ""
    category: str = ""
    price_level: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def apply_stealth(page: Page) -> None:
    """Apply anti-detection patches. Makes headless much less detectable."""
    try:
        page.add_init_script("""
            () => {
                // Hide webdriver
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                // Fake plugins
                Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                // Languages
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                // Hardware concurrency
                Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
                // WebGL vendor spoof
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(p) {
                    if (p === 37445) return 'Intel Inc.';
                    if (p === 37446) return 'Intel(R) UHD Graphics';
                    return getParameter.call(this, p);
                };
                // Permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({ state: 'prompt' }) :
                        originalQuery(parameters)
                );
            }
        """)
    except Exception:
        pass


def block_heavy_resources(page: Page) -> None:
    """Block images, fonts, css, media. Massive speed + stealth win."""
    def _route(route, request):
        rtype = request.resource_type
        if rtype in ("image", "font", "stylesheet", "media", "other"):
            # Allow some critical images if needed, but for Maps mostly safe to abort
            if "maps" not in request.url and "google" not in request.url:
                route.abort()
                return
        route.continue_()

    try:
        page.route("**/*", _route)
    except Exception:
        pass


def robust_scroll_to_end(
    page: Page,
    stop_event: Event,
    target_count: int,
    max_scrolls: int = 80,
    logger=None,
) -> Tuple[int, bool]:
    """
    Best-in-class scrolling:
    - Stops as soon as we have enough
    - Tracks real progress (count + scroll)
    - Strong end signals
    - Human-like but efficient
    Returns (final_count, reached_logical_end)
    """
    feed = page.locator("div[role='feed']").first
    last_count = 0
    stagnant = 0
    reached_end = False
    start = time.time()

    for scroll_idx in range(max_scrolls):
        if stop_event.is_set():
            break
        if time.time() - start > 180:  # safety for any scroll phase
            break

        try:
            count = page.locator("a.hfpxzc").count()
        except Exception:
            count = last_count

        if logger and (scroll_idx % 5 == 0 or count > last_count + 5):
            logger.info(f"Scroll progress: {count}/{target_count} items")

        if count >= target_count:
            return count, False

        if count <= last_count:
            stagnant += 1
        else:
            stagnant = 0
            last_count = count

        # End detection (Google often shows this)
        try:
            content = (page.content() or "").lower()
            if any(x in content for x in ("you've reached the end", "end of the list", "no more results", "results have been limited")):
                reached_end = True
                break
        except Exception:
            pass

        if stagnant >= 4:
            reached_end = True
            break

        # Scroll action - efficient + human
        try:
            if feed.count() > 0:
                feed.evaluate("(el) => el.scrollTop = el.scrollHeight")
            else:
                page.evaluate("window.scrollBy(0, 4000)")
            page.mouse.wheel(0, random.randint(1500, 3800))
            if random.random() < 0.25:
                page.mouse.move(random.randint(150, 800), random.randint(200, 600))
        except Exception:
            pass

        # Smart pause: shorter when making progress
        pause = random.uniform(450, 950) if count > last_count else random.uniform(750, 1400)
        page.wait_for_timeout(int(pause))

    final = page.locator("a.hfpxzc").count() if 'count' not in locals() else count
    return final, reached_end


def extract_card_data(page: Page, href: str) -> CardData:
    """
    Given a place href (or current context), try to pull rich info from the
    visible card in the feed. Falls back gracefully.
    This is the key to high performance: many fields without full navigation.
    """
    card = CardData(url=href)
    try:
        # Find the anchor, then walk to parent card
        anchor = page.locator(f"a.hfpxzc[href*='{href.split('/place/')[-1][:40]}']").first
        if anchor.count() == 0:
            anchor = page.locator("a.hfpxzc").filter(has_text="").first  # loose
        parent = anchor.locator("xpath=ancestor::div[contains(@jsaction,'mouseover') or @role='article' or contains(@class,'Nv2PK')][1]").first
        if parent.count() == 0:
            parent = anchor.locator("xpath=..")

        # Name
        for sel in CARD_NAME_SELECTORS:
            try:
                el = parent.locator(sel).first
                if el.count() > 0:
                    t = el.inner_text(timeout=800).strip()
                    if t:
                        card.name = t
                        break
            except Exception:
                continue

        # Rating
        for sel in CARD_RATING_SELECTORS:
            try:
                el = parent.locator(sel).first
                if el.count() > 0:
                    val = el.inner_text(timeout=600) or el.get_attribute("aria-label") or ""
                    m = re.search(r"[\d.]+", val)
                    if m:
                        card.rating = float(m.group())
                        break
            except Exception:
                continue

        # Reviews
        for sel in CARD_REVIEWS_SELECTORS:
            try:
                el = parent.locator(sel).first
                if el.count() > 0:
                    val = el.inner_text(timeout=600) or ""
                    m = re.search(r"([\d,]+)", val.replace(",", ""))
                    if m:
                        card.review_count = int(m.group(1))
                        break
            except Exception:
                continue

        # Address snippet
        for sel in CARD_ADDRESS_SELECTORS:
            try:
                el = parent.locator(sel).first
                if el.count() > 0:
                    t = el.inner_text(timeout=600).strip()
                    if t and len(t) > 5:
                        card.address = t
                        break
            except Exception:
                continue

        # Category
        for sel in CARD_CATEGORY_SELECTORS:
            try:
                el = parent.locator(sel).first
                if el.count() > 0:
                    t = el.inner_text(timeout=600).strip()
                    if t and not re.match(r"^\d", t):
                        card.category = t
                        break
            except Exception:
                continue

    except Exception:
        pass

    return card


def get_card_data_batch(page: Page, urls: List[str]) -> Dict[str, CardData]:
    """Batch extract from currently visible cards. Very fast."""
    out: Dict[str, CardData] = {}
    try:
        anchors = page.locator("a.hfpxzc")
        n = min(anchors.count(), len(urls) + 30)
        for i in range(n):
            try:
                href = anchors.nth(i).get_attribute("href") or ""
                if not href:
                    continue
                cd = extract_card_data(page, href)
                if cd.name or cd.rating:
                    out[href] = cd
            except Exception:
                continue
    except Exception:
        pass
    return out


def create_dry_run_leads(keyword: str, location: str, max_results: int) -> List[Dict[str, str]]:
    """Generate realistic synthetic leads. No browser, no network. Perfect for dry tests and UI validation."""
    import random as _r
    first_names = ["Alex", "Jordan", "Sam", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Reese"]
    business_types = keyword.title().split()[:2] or ["Local Business"]
    base = f"{_r.choice(business_types)}"

    leads = []
    for i in range(max_results):
        name = f"{_r.choice(first_names)} {base} #{i+1}"
        phone = f"+1 ({_r.randint(200,999)}) {_r.randint(200,999)}-{1000+i}"
        website = f"https://www.{base.lower().replace(' ','')}{i}.example.com" if _r.random() > 0.35 else ""
        email = f"contact@{base.lower().replace(' ','')}{i}.example.com" if website else ""
        wa = phone if _r.random() > 0.5 else ""
        leads.append({
            "name": name,
            "phone": phone,
            "email": email,
            "website": website,
            "whatsapp": wa,
            "google_maps_url": f"https://www.google.com/maps/place/{name.replace(' ','+')}",
            "has_website": "Yes" if website else "No",
            "address": f"{100+i} Main St, {location}",
            "rating": round(_r.uniform(3.2, 4.9), 1),
            "review_count": _r.randint(5, 480),
            "category": keyword.title(),
            "business_hours": "9:00 AM - 6:00 PM",
            "instagram": f"https://instagram.com/{base.lower()}{i}" if _r.random() > 0.6 else "",
            "facebook": "",
            "twitter": "",
            "linkedin": "",
            "tiktok": "",
            "youtube": "",
            "has_chatbot": "No",
            "chatbot_type": "",
            "has_google_analytics": "Yes" if _r.random() > 0.7 else "No",
            "has_meta_pixel": "No",
            "cms_platform": _r.choice(["", "wordpress", "wix", "shopify"]),
            "is_automated": "No",
            "quality_score": _r.choice(["high", "medium", "high"]),
            "data_sources": "dry_run,maps,web_powerful",
            "all_emails": email,
            "all_whatsapp": wa,
            "web_description": f"Leading {keyword} provider in the area offering premium services.",
            "web_services": "Consultation, Delivery, Support, Custom Solutions",
            "web_address": f"{100+i} Main St, {location}",
            "all_phones_from_web": phone,
            "web_about": f"Established provider of {keyword} with focus on quality and customer satisfaction in {location}.",
            "web_hours": "Mon-Fri 9AM-6PM, Sat 10AM-4PM",
        })
    return leads


def save_checkpoint(keyword: str, location: str, discovered: List[str], partial_leads: List[Dict], progress: int) -> str:
    """Save resumable checkpoint."""
    safe = re.sub(r"[^a-z0-9_]", "_", f"{keyword}_{location}".lower())[:60]
    path = CHECKPOINT_DIR / f"{safe}_{int(time.time())}.json"
    data = {
        "keyword": keyword,
        "location": location,
        "discovered_urls": discovered,
        "partial_leads": partial_leads,
        "progress": progress,
        "ts": time.time(),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return str(path)
    except Exception:
        return ""


def load_latest_checkpoint(keyword: str, location: str) -> Optional[Dict]:
    """Load most recent checkpoint for same search if exists."""
    safe_prefix = re.sub(r"[^a-z0-9_]", "_", f"{keyword}_{location}".lower())[:60]
    candidates = sorted(CHECKPOINT_DIR.glob(f"{safe_prefix}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    try:
        with open(candidates[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def apply_card_to_lead(lead: Dict[str, str], card: CardData) -> None:
    """Merge card pre-extracted data into lead dict (non-destructive)."""
    if not card:
        return
    if card.name and not lead.get("name"):
        lead["name"] = card.name
    if card.rating and not lead.get("rating"):
        lead["rating"] = card.rating
    if card.review_count and not lead.get("review_count"):
        lead["review_count"] = card.review_count
    if card.address and not lead.get("address"):
        lead["address"] = card.address
    if card.category and not lead.get("category"):
        lead["category"] = card.category
    if not lead.get("google_maps_url"):
        lead["google_maps_url"] = card.url


def safe_launch_browser(p, headless: bool = True, logger=None):
    """Power wrapper: stealth args, clear actionable errors, detects common env/driver issues.
    Returns (browser, context) or raises helpful RuntimeError.
    """
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--window-size=1366,900",
    ]
    # Rotate UA for production (huge boost to success rate on Maps + web)
    ua = _UA.random if _UA else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    try:
        browser = p.chromium.launch(headless=headless, args=args)
        context = browser.new_context(
            user_agent=ua,
            viewport={"width": 1366, "height": 900},
        )
        return browser, context
    except Exception as exc:
        err_str = str(exc)
        if "cli.js" in err_str or "playwright" in err_str.lower() or "driver" in err_str.lower():
            hint = " (Common cause: conflicting system node-playwright. Run in clean venv or `python -m playwright install chromium`)"
        else:
            hint = ""
        msg = (
            f"Playwright Chromium launch failed: {exc}{hint}\n\n"
            "Fixes:\n"
            "  python -m playwright install chromium\n"
            "  sudo apt install -y xvfb (for headless Linux)\n\n"
            "Powerful workaround: pass dry_run=True (instant synthetic results, full data shape) or card_only=True (real feed cards only).\n"
            "The scraper is designed so dry/card paths never hang and are extremely fast for 100+ results."
        )
        if logger:
            logger.error(msg)
        raise RuntimeError(msg) from exc

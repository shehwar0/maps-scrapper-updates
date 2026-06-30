"""
Deep Business Scraper - Multi-Source Intelligence Extraction
Extracts comprehensive business data from:
1. Google Maps (primary source)
2. Business website (contact pages, about pages, etc.)
3. Google Search (cross-verification and additional info)
4. Social media profiles (Instagram, Facebook, etc.)

Focus on ACCURACY, COMPLETENESS, and CROSS-VERIFICATION.
"""

import logging
import random
import re
import time
from html import unescape
from dataclasses import dataclass, field
from threading import Event
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, BrowserContext
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from email_extractor import WebsiteExtractor
from maps_city_coverage import build_citywide_queries
from url_filters import is_business_website, normalize_business_website
from scraper_utils import (
    apply_stealth, block_heavy_resources, robust_scroll_to_end,
    extract_card_data, get_card_data_batch, CardData, apply_card_to_lead,
    create_dry_run_leads, save_checkpoint, load_latest_checkpoint, safe_launch_browser
)

# Production UA rotation (if available)
try:
    from fake_useragent import UserAgent
    _UA = UserAgent()
except Exception:
    _UA = None

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except Exception:
    TQDM_AVAILABLE = False
    tqdm = None


# ============================================================================
# CONFIGURATION
# ============================================================================

MAX_RESULTS_CAP = 500
RESULT_SCAN_WINDOW = 320
CITYWIDE_QUERY_LIMIT = 30
MAP_STAGNANT_ROUNDS = 28
MAP_SCROLL_DELAY_MIN = 0.25
MAP_SCROLL_DELAY_MAX = 0.65  # tuned for best balance of speed + reliability for 100+ results
# Overall time budget for discovering URLs (prevents hang on hard queries)
URL_DISCOVERY_MAX_SEC = 240  # 4 minutes max for collecting place links
LISTING_MAX_SEC_PER_ITEM = 55  # hard safety per business detail page + enrich
DISCOVERY_STABLE_ROUNDS_FOR_END = 4  # if no new after this many + end markers => stop
REQUEST_TIMEOUT = 15
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
LISTING_TIME_BUDGET_SEC = 55
WEBSITE_ANALYSIS_BUDGET_SEC = 16
HEAVY_STEP_MIN_REMAINING_SEC = 8
GOOGLE_LOOKUP_MIN_REMAINING_SEC = 8

# Pages to check for contact info on websites
CONTACT_PAGES = [
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
]

# Social media pages to check
SOCIAL_PAGES = [
    "/social",
    "/follow-us",
    "/connect",
]


# ============================================================================
# REGEX PATTERNS
# ============================================================================

PHONE_REGEX = re.compile(r"(\+?\d[\d\s()\-\.]{6,}\d)")

# WhatsApp patterns - comprehensive
WHATSAPP_PATTERNS = [
    re.compile(r"(?:https?://)?wa\.me/(\+?\d{10,15})", re.I),
    re.compile(r"(?:https?://)?api\.whatsapp\.com/send\?phone=(\+?\d{10,15})", re.I),
    re.compile(r"(?:https?://)?wa\.me/(\d{10,15})", re.I),
    re.compile(r"(?:https?://)?api\.whatsapp\.com/send\?phone=(\d{10,15})", re.I),
    re.compile(r"whatsapp://send\?phone=(\+?\d{10,15})", re.I),
    re.compile(r"href=[\"'](?:https?://)?wa\.me/(\+?\d{10,15})[\"']", re.I),
    re.compile(r"href=[\"'](?:https?://)?wa\.me/(\d{10,15})[\"']", re.I),
    re.compile(r"data-phone[=\"':]+[\"']?(\+?\d{10,15})", re.I),
    re.compile(r"data-whatsapp[=\"':]+[\"']?(\+?\d{10,15})", re.I),
    re.compile(r"whatsapp[\"']?\s*[:=]\s*[\"']?(\+?\d{10,15})", re.I),
]

# Instagram patterns - fixed to properly extract usernames
INSTAGRAM_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9_\.]{1,30})/?(?:\?|$|#|\"|\s|<)", re.I),
    re.compile(r"href=[\"'](?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9_\.]{1,30})/?[\"']", re.I),
    re.compile(r"instagram\.com/([a-zA-Z0-9_\.]{1,30})(?:/|\?|$|#|\"|\s)", re.I),
]

# Facebook patterns
FACEBOOK_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?facebook\.com/([a-zA-Z0-9\.]{1,50})/?(?:\?|$|#|\"|\s|<)", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?fb\.com/([a-zA-Z0-9\.]{1,50})/?", re.I),
    re.compile(r"href=[\"'](?:https?://)?(?:www\.)?facebook\.com/([a-zA-Z0-9\.]{1,50})/?[\"']", re.I),
]

# Twitter/X patterns
TWITTER_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?twitter\.com/([a-zA-Z0-9_]{1,15})/?(?:\?|$|#|\"|\s|<)", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?x\.com/([a-zA-Z0-9_]{1,15})/?", re.I),
]

# LinkedIn patterns
LINKEDIN_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/company/([a-zA-Z0-9_-]+)/?", re.I),
    re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/([a-zA-Z0-9_-]+)/?", re.I),
]

# TikTok patterns
TIKTOK_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@([a-zA-Z0-9_\.]+)/?", re.I),
]

# YouTube patterns
YOUTUBE_PATTERNS = [
    re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/(?:@|channel/|c/|user/)?([a-zA-Z0-9_-]+)/?", re.I),
]

# Email patterns
EMAIL_PATTERNS = [
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}", re.I),
    re.compile(r"mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,})", re.I),
]
MAILTO_PATTERN = re.compile(r"mailto:([^\"'>\s?#]+)", re.I)
DATA_EMAIL_PATTERN = re.compile(
    r"(?:data-email|data-mail|data-contact|data-mailto)\s*=\s*['\"]([^'\"]+)['\"]",
    re.I,
)
DATA_USER_DOMAIN_PATTERN = re.compile(
    r"data-user\s*=\s*['\"]([^'\"]{1,64})['\"][^>]{0,200}data-domain\s*=\s*['\"]([^'\"]{1,253})['\"]",
    re.I,
)
DATA_DOMAIN_USER_PATTERN = re.compile(
    r"data-domain\s*=\s*['\"]([^'\"]{1,253})['\"][^>]{0,200}data-user\s*=\s*['\"]([^'\"]{1,64})['\"]",
    re.I,
)
JS_EMAIL_JOIN_PATTERN = re.compile(
    r"['\"]([a-zA-Z0-9._%+-]{1,64})['\"]\s*\+\s*['\"]@['\"]\s*\+\s*['\"]([a-zA-Z0-9.-]{1,253}\.[a-zA-Z]{2,24})['\"]",
    re.I,
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

# Invalid social handles (generic pages)
INVALID_SOCIAL_HANDLES = {
    "share", "sharer", "intent", "dialog", "login", "signup", "home",
    "p", "explore", "accounts", "oauth", "help", "settings", "search",
    "hashtag", "i", "direct", "stories", "reels", "live", "tv",
    "pages", "groups", "events", "marketplace", "gaming", "watch",
    "profile.php", "plugins", "sharer.php", "share.php", "tr",
    "photo.php", "video.php", "reel", "about", "photos", "videos",
}

# Chatbot/automation markers
CHATBOT_MARKERS = [
    "tidio", "intercom", "drift", "crisp", "livechat", "zendesk", "freshchat",
    "hubspot", "tawk.to", "olark", "smartsupp", "chatra", "jivochat",
    "whatsapp-widget", "click-to-chat", "wa-automate", "wati.io",
    "messenger.com/t/", "m.me/", "getbutton.io",
]

# Analytics markers
ANALYTICS_MARKERS = {
    "google_analytics": ["google-analytics.com", "gtag", "ga.js", "analytics.js", "G-", "UA-", "GTM-"],
    "meta_pixel": ["facebook.com/tr", "fbevents.js", "fbq(", "Meta Pixel", "connect.facebook.net"],
}

# CMS markers
CMS_MARKERS = {
    "wordpress": ["wp-content", "wp-includes", "wordpress"],
    "wix": ["wix.com", "wixstatic.com", "_wix"],
    "squarespace": ["squarespace.com", "sqsp.net"],
    "shopify": ["shopify.com", "cdn.shopify"],
    "webflow": ["webflow.com", "webflow.io"],
    "godaddy": ["godaddy.com", "secureserver.net"],
    "weebly": ["weebly.com"],
}


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class BusinessData:
    """Comprehensive business data structure."""
    # Basic Info (from Google Maps)
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

    # Contact Info (extracted from website)
    emails: List[str] = field(default_factory=list)
    whatsapp_numbers: List[str] = field(default_factory=list)
    additional_phones: List[str] = field(default_factory=list)

    # DEEP powerful web data
    web_description: str = ""
    web_address: str = ""
    web_services: str = ""
    pages_crawled_on_web: int = 0
    structured_data_on_web: bool = False
    web_about: str = ""
    web_hours: str = ""

    # Social Media
    instagram: str = ""
    facebook: str = ""
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
            "google_maps_url": self.google_maps_url,
            # Powerful web extra
            "web_description": self.web_description,
            "web_services": self.web_services,
            "web_address": self.web_address,
            "pages_crawled_on_web": self.pages_crawled_on_web,
            "structured_data_on_web": "Yes" if self.structured_data_on_web else "No",
            "web_about": self.web_about,
            "web_hours": self.web_hours,
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
        if self.instagram or self.facebook:
            score += 1
        if self.has_chatbot or self.has_google_analytics:
            score += 1

        if score >= 8:
            return "high"
        elif score >= 5:
            return "medium"
        else:
            return "low"


# ============================================================================
# EXTRACTION UTILITIES
# ============================================================================

def normalize_phone(phone: str, default_country: str = "92") -> str:
    """Normalize phone number to international format."""
    if not phone:
        return ""
    
    # Remove all non-digit characters except +
    cleaned = re.sub(r"[^\d+]", "", phone)
    
    # Remove + for processing
    has_plus = cleaned.startswith("+")
    digits = cleaned.replace("+", "")
    
    if len(digits) < 8:
        return ""
    
    # Pakistan specific handling
    if digits.startswith("03") and len(digits) == 11:
        # Convert 03XX to 923XX
        digits = default_country + digits[1:]
    elif digits.startswith("3") and len(digits) == 10:
        # Convert 3XX to 923XX
        digits = default_country + digits
    elif digits.startswith("0") and len(digits) == 11:
        # Generic: remove leading 0 and add country code
        digits = default_country + digits[1:]
    
    # Return with + prefix for international format
    if has_plus or len(digits) > 10:
        return "+" + digits
    return digits


def extract_emails(html: str, base_domain: str = "") -> List[str]:
    """Extract unique valid emails from HTML with light prioritization."""
    if not html:
        return []

    raw = unescape(html)
    candidates: List[str] = []

    for pattern in EMAIL_PATTERNS:
        for match in pattern.finditer(raw):
            candidates.append(match.group(1) if match.groups() else match.group(0))

    for match in MAILTO_PATTERN.findall(raw):
        candidates.append(match)

    for match in DATA_EMAIL_PATTERN.findall(raw):
        candidates.append(match)

    for user, domain in DATA_USER_DOMAIN_PATTERN.findall(raw):
        candidates.append(f"{user}@{domain}")
    for domain, user in DATA_DOMAIN_USER_PATTERN.findall(raw):
        candidates.append(f"{user}@{domain}")

    for user, domain in JS_EMAIL_JOIN_PATTERN.findall(raw):
        candidates.append(f"{user}@{domain}")

    normalized = _normalize_obfuscated_text(raw)
    for match in EMAIL_PATTERNS[0].finditer(normalized):
        candidates.append(match.group(0))

    emails: List[str] = []
    seen = set()
    for candidate in candidates:
        email = _normalize_email_candidate(candidate)
        if not email or not is_valid_email(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)

    return _rank_emails(emails, base_domain)[:15]


def is_valid_email(email: str) -> bool:
    """Check if email is valid and not generic."""
    if not email or "@" not in email:
        return False
    
    # Check format
    parts = email.split("@")
    if len(parts) != 2:
        return False
    
    local, domain = parts
    if not local or not domain or "." not in domain:
        return False
    
    # Filter fake/invalid domains
    if domain in INVALID_EMAIL_DOMAINS:
        return False

    tld = domain.rsplit(".", 1)[-1]
    if tld.lower() in INVALID_EMAIL_TLDS:
        return False
    
    return True


def _normalize_obfuscated_text(text: str) -> str:
    cleaned = text
    cleaned = re.sub(r"\s*(?:\(|\[|\{)?\s*at\s*(?:\)|\]|\})?\s*", "@", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*(?:\(|\[|\{)?\s*dot\s*(?:\)|\]|\})?\s*", ".", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*\(\s*at\s*\)\s*", "@", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*\[\s*at\s*\]\s*", "@", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*\(\s*dot\s*\)\s*", ".", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*\[\s*dot\s*\]\s*", ".", cleaned, flags=re.I)
    return cleaned


def _normalize_email_candidate(value: str) -> str:
    if not value:
        return ""
    text = unescape(value)
    text = text.strip().strip("<>[](){}\"' ")
    if text.lower().startswith("mailto:"):
        text = text[7:]
    if "?" in text:
        text = text.split("?", 1)[0]
    text = text.strip().strip(".,;:")
    return text.lower()


def _rank_emails(emails: List[str], base_domain: str) -> List[str]:
    if not emails:
        return []
    base_domain = (base_domain or "").lower()

    def score_email(email: str) -> int:
        local, domain = email.split("@", 1)
        score = 0
        if base_domain and (domain == base_domain or domain.endswith("." + base_domain)):
            score += 3
        if local in NO_REPLY_LOCAL_PARTS:
            score -= 2
        return score

    ranked = sorted(enumerate(emails), key=lambda item: (-score_email(item[1]), item[0]))
    return [email for _, email in ranked]


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


def extract_whatsapp(html: str) -> List[str]:
    """Extract WhatsApp numbers from HTML."""
    numbers = []
    seen = set()
    
    for pattern in WHATSAPP_PATTERNS:
        for match in pattern.finditer(html):
            raw_num = match.group(1) if match.groups() else match.group(0)
            normalized = normalize_phone(raw_num)
            
            if normalized and normalized not in seen:
                seen.add(normalized)
                numbers.append(normalized)
    
    # Also check for WhatsApp widget markers with phone numbers nearby
    html_lower = html.lower()
    wa_markers = ["wa.me", "api.whatsapp.com", "wa.link", "whatsapp://", "whatsapp-widget"]
    
    if any(m in html_lower for m in wa_markers) and not numbers:
        # Find phone numbers near WhatsApp mentions
        for match in PHONE_REGEX.finditer(html):
            normalized = normalize_phone(match.group(1))
            if normalized and len(normalized.replace("+", "")) >= 10 and normalized not in seen:
                seen.add(normalized)
                numbers.append(normalized)
                if len(numbers) >= 3:
                    break
    
    return numbers[:5]


def extract_social_handle(html: str, patterns: List[re.Pattern], platform: str) -> str:
    """Extract social media handle/URL from HTML."""
    for pattern in patterns:
        for match in pattern.finditer(html):
            handle = match.group(1) if match.groups() else match.group(0)
            handle = handle.strip().rstrip("/")
            
            if handle and handle.lower() not in INVALID_SOCIAL_HANDLES:
                # Return full URL
                if platform == "instagram":
                    return f"https://www.instagram.com/{handle}"
                elif platform == "facebook":
                    return f"https://www.facebook.com/{handle}"
                elif platform == "twitter":
                    return f"https://twitter.com/{handle}"
                elif platform == "linkedin":
                    return f"https://www.linkedin.com/company/{handle}"
                elif platform == "tiktok":
                    return f"https://www.tiktok.com/@{handle}"
                elif platform == "youtube":
                    return f"https://www.youtube.com/{handle}"
    return ""


def detect_chatbot(html: str) -> Tuple[bool, str]:
    """Detect chatbot/automation on website."""
    html_lower = html.lower()
    for marker in CHATBOT_MARKERS:
        if marker in html_lower:
            return True, marker
    return False, ""


def detect_analytics(html: str) -> Dict[str, bool]:
    """Detect analytics tools."""
    html_lower = html.lower()
    result = {"google_analytics": False, "meta_pixel": False}
    
    for tool, markers in ANALYTICS_MARKERS.items():
        for marker in markers:
            if marker.lower() in html_lower:
                result[tool] = True
                break
    
    return result


def detect_cms(html: str) -> str:
    """Detect CMS platform."""
    html_lower = html.lower()
    for cms, markers in CMS_MARKERS.items():
        for marker in markers:
            if marker.lower() in html_lower:
                return cms
    return ""


def clean_address(address: str) -> str:
    """Clean and format address string."""
    if not address:
        return ""
    
    # Remove excessive whitespace and newlines
    cleaned = re.sub(r"\s+", " ", address)
    cleaned = cleaned.strip()
    
    # Remove leading/trailing punctuation
    cleaned = cleaned.strip(",;.")
    
    return cleaned


# ============================================================================
# CAPTCHA ERROR
# ============================================================================

class CaptchaDetectedError(RuntimeError):
    pass


# ============================================================================
# MAIN SCRAPER CLASS
# ============================================================================

class DeepBusinessScraper:
    """
    Multi-source business intelligence scraper.
    
    Extraction flow:
    1. Search Google Maps for businesses
    2. For each business:
       a. Extract data from Google Maps listing
       b. Visit business website (if available)
       c. Search Google for additional info
       d. Cross-verify and consolidate data
    """

    def __init__(
        self,
        max_results: int = 50,
        headless: bool = False,
        min_delay: float = 0.7,
        max_delay: float = 1.6,
        website_filter: str = "all",
        deep_search: bool = True,
        skip_duplicates: bool = True,
        dry_run: bool = False,
        use_checkpoints: bool = True,
        card_only: bool = False,
        web_max_pages: int = 16,
        web_timeout_sec: int = 30,
        logger: Optional[logging.Logger] = None,
        progress_callback: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> None:
        self.max_results = max(1, min(max_results, MAX_RESULTS_CAP))
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.website_filter = website_filter if website_filter in {"all", "with", "without"} else "all"
        self.deep_search = deep_search
        self.skip_duplicates = skip_duplicates
        self.dry_run = dry_run
        self.use_checkpoints = use_checkpoints
        self.card_only = card_only
        self.log = logger or logging.getLogger(__name__)
        self.progress_callback = progress_callback
        self._website_cache: Dict[str, Dict] = {}
        self._google_cache: Dict[str, Optional[Dict]] = {}
        self._card_cache: Dict[str, CardData] = {}

        # Production config for web depth (configurable for prod tuning)
        self.web_max_pages = web_max_pages
        self.web_timeout_sec = web_timeout_sec
        
        # Initialize history manager for deduplication
        try:
            from scrape_history import get_history
            self.history = get_history(logger)
        except ImportError:
            self.history = None
            self.skip_duplicates = False
        
        # HTTP session for additional requests
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })

    def scrape(
        self,
        keyword: str,
        location: str,
        stop_event: Optional[Event] = None,
    ) -> List[Dict[str, str]]:
        """Main scrape method. Now with dry_run, hybrid card extraction, stealth, checkpoints."""
        stop_event = stop_event or Event()

        if self.dry_run:
            self.log.info("🧪 DRY RUN MODE - generating synthetic data (no browser)")
            leads = create_dry_run_leads(keyword, location, self.max_results)
            if self.progress_callback:
                for l in leads:
                    try:
                        self.progress_callback(l)
                    except Exception:
                        pass
            return leads

        search_queries = build_citywide_queries(keyword, location, max_queries=CITYWIDE_QUERY_LIMIT)
        if not search_queries:
            return []

        duplicate_buffer = min(18, max(3, self.max_results // 7))
        if self.skip_duplicates:
            duplicate_buffer = max(duplicate_buffer, 8)
        target_urls = self.max_results + duplicate_buffer

        # Resume from checkpoint if available
        discovered: List[str] = []
        seen: Set[str] = set()
        checkpoint = None
        if self.use_checkpoints:
            checkpoint = load_latest_checkpoint(keyword, location)
            if checkpoint and checkpoint.get("keyword") == keyword and checkpoint.get("location") == location:
                discovered = checkpoint.get("discovered_urls", [])[:target_urls]
                seen = set(discovered)
                self.log.info("♻️ Resuming from checkpoint with %d urls", len(discovered))

        if self.skip_duplicates and self.history:
            stats = self.history.get_stats(keyword, location)
            self.log.info(f"📊 History: {stats.get('search_total', 0)} previously scraped")

        with sync_playwright() as p:
            browser, context = safe_launch_browser(p, headless=self.headless, logger=self.log)
            page = context.new_page()
            apply_stealth(page)
            block_heavy_resources(page)

            try:
                if len(search_queries) > 1:
                    self.log.info("Using %d map zones for broader city coverage", len(search_queries))

                per_query_target = max(10, (target_urls + len(search_queries) - 1) // len(search_queries))

                for query in search_queries:
                    if stop_event.is_set() or len(discovered) >= target_urls:
                        break
                    remaining = target_urls - len(discovered)
                    qtarget = min(per_query_target, remaining)
                    place_urls = self._search_query_with_retries(page, query, stop_event, qtarget)
                    for u in place_urls:
                        if u and u not in seen:
                            seen.add(u)
                            discovered.append(u)
                            if len(discovered) >= target_urls:
                                break

                if len(discovered) < target_urls and not stop_event.is_set():
                    rem = target_urls - len(discovered)
                    fb = self._search_query_with_retries(page, search_queries[0], stop_event, rem)
                    for u in fb:
                        if u and u not in seen:
                            seen.add(u)
                            discovered.append(u)
                            if len(discovered) >= target_urls:
                                break

                # Powerful hybrid: batch extract card data first (no full visits)
                self._card_cache = get_card_data_batch(page, discovered)
                self.log.info("🃏 Pre-extracted rich card data for %d listings (huge perf win)", len(self._card_cache))

                if self.card_only:
                    # Ultra fast power mode: pure card data, no place visits or website crawls at all
                    self.log.info("⚡ CARD_ONLY fast mode - skipping all place visits and web enrichment")
                    results = []
                    for url in discovered[:target_urls]:
                        cd = self._card_cache.get(url) or CardData(url=url)
                        lead = {
                            "name": cd.name or "Unknown Business",
                            "phone": "",
                            "email": "",
                            "website": "",
                            "whatsapp": "",
                            "google_maps_url": url,
                            "has_website": "No",
                            "address": cd.address,
                            "rating": cd.rating,
                            "review_count": cd.review_count,
                            "category": cd.category or keyword,
                            "business_hours": "",
                            "instagram": "",
                            "facebook": "",
                            "twitter": "",
                            "linkedin": "",
                            "tiktok": "",
                            "youtube": "",
                            "has_chatbot": "No",
                            "chatbot_type": "",
                            "has_google_analytics": "No",
                            "has_meta_pixel": "No",
                            "cms_platform": "",
                            "is_automated": "No",
                            "quality_score": "card-only",
                            "data_sources": "maps_cards",
                            "all_emails": "",
                            "all_whatsapp": "",
                        }
                        results.append(lead)
                        if self.progress_callback:
                            try:
                                self.progress_callback(lead)
                            except Exception:
                                pass
                    if self.skip_duplicates and self.history and results:
                        self.history.add_batch_to_history(results, keyword, location)
                    if self.use_checkpoints:
                        save_checkpoint(keyword, location, discovered, results, len(results))
                    return results

                leads = self._collect_lead_details(context, discovered[:target_urls], keyword, location, stop_event)

                results = [lead.to_dict() for lead in leads]

                if self.skip_duplicates and self.history and results:
                    self.history.add_batch_to_history(results, keyword, location)
                    self.log.info(f"💾 Saved {len(results)} to history")

                if self.use_checkpoints and len(discovered) > 5:
                    save_checkpoint(keyword, location, discovered, results, len(results))

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
        """Navigate + search with multiple strategies and stronger waits."""
        encoded_query = quote_plus(query)
        try:
            page.goto(f"https://www.google.com/maps/search/{encoded_query}", timeout=75000, wait_until="domcontentloaded")
        except Exception:
            page.goto("https://www.google.com/maps", timeout=30000)
            self._human_delay(0.4, 0.9)

        page.wait_for_timeout(1100)
        self._maybe_accept_consent(page)
        self._raise_if_captcha(page)

        # Stronger initial wait: wait for feed or results
        if self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=38000):
            # Give a bit for first batch render
            page.wait_for_timeout(650)
            self._human_delay(0.2, 0.5)
            return

        # Fallback to classic search box
        search_input = self._find_search_input(page)
        if search_input:
            try:
                search_input.fill("")
                search_input.fill(query)
                self._human_delay(0.2, 0.5)
                search_input.press("Enter")
            except Exception:
                pass

        if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc", "h1.DUwDvf"], timeout_ms=42000):
            # Last attempt: try direct place search variation or reload
            try:
                page.reload(timeout=20000)
                page.wait_for_timeout(900)
            except Exception:
                pass
            if not self._wait_for_any(page, ["div[role='feed']", "a.hfpxzc"], timeout_ms=18000):
                raise RuntimeError("Google Maps results did not load (UI change / slow net / captcha).")

        self._human_delay(0.3, 0.7)
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
        """Wait for any selector with progressive polling."""
        deadline = time.time() + (timeout_ms / 1000)
        poll = 280
        while time.time() < deadline:
            for selector in selectors:
                try:
                    loc = page.locator(selector).first
                    if loc.count() > 0:
                        # Verify visible-ish
                        try:
                            if loc.is_visible():
                                return True
                        except Exception:
                            return True
                except Exception:
                    continue
            page.wait_for_timeout(poll)
            poll = min(620, int(poll * 1.1))
        return False

    def _maybe_accept_consent(self, page: Page) -> None:
        """Accept cookie consent if present."""
        selectors = [
            "button:has-text('Accept all')",
            "button:has-text('I agree')",
            "button:has-text('Accept')",
            "button[aria-label='Accept all']",
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
        """Detect Google Maps 'end of list' state to stop scrolling reliably."""
        try:
            # Quick content scan for common end markers (Google localized variations exist)
            try:
                content = (page.content() or "").lower()
            except Exception:
                content = ""
            end_markers = (
                "you've reached the end",
                "end of the list",
                "no more results",
                "all results shown",
                "reached the end",
                "no other results",
            )
            if any(m in content for m in end_markers):
                return True

            # Status / info banners
            for sel in [
                "div[role='status']",
                "[aria-label*='end']",
                "div.n7lv7yjy",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        txt = (el.inner_text(timeout=600) or "").lower()
                        if any(m in txt for m in ("end", "no more", "limited", "results")):
                            return True
                except Exception:
                    continue

            # If feed height stopped growing significantly for a while, secondary signal
            # (caller tracks stable rounds)
            return False
        except Exception:
            return False

    def _collect_place_urls(self, page: Page, stop_event: Event, target_count: Optional[int] = None) -> List[str]:
        """Delegates to ultra-robust scroller from scraper_utils (stealth + smart end detect + human timing)."""
        if target_count is None:
            duplicate_buffer = min(20, max(3, self.max_results // 7))
            if self.skip_duplicates:
                duplicate_buffer = max(duplicate_buffer, 8)
            target_urls = self.max_results + duplicate_buffer
        else:
            target_urls = max(1, target_count)

        if "/maps/place/" in (page.url or ""):
            return [page.url][:target_urls]

        apply_stealth(page)
        block_heavy_resources(page)

        final_count, reached_end = robust_scroll_to_end(
            page, stop_event, target_urls, max_scrolls=75, logger=self.log
        )

        # Collect the URLs after robust scroll
        discovered: List[str] = []
        seen: Set[str] = set()
        try:
            hrefs = page.eval_on_selector_all(
                "a.hfpxzc",
                "els => els.map(el => el.getAttribute('href') || el.href || '').filter(Boolean)",
            )
            for h in hrefs:
                if h and h not in seen:
                    seen.add(h)
                    discovered.append(h)
                    if len(discovered) >= target_urls:
                        break
        except Exception:
            pass

        self.log.info("📍 Discovered %d place URLs (robust, end_detected=%s)", len(discovered), reached_end)
        return discovered[:target_urls]

    def _collect_lead_details(
        self,
        context: BrowserContext,
        place_urls: List[str],
        keyword: str,
        location: str,
        stop_event: Event,
    ) -> List[BusinessData]:
        """Hybrid: use pre-extracted card data + visit only when needed. Reuses page. Extremely efficient."""
        leads: List[BusinessData] = []
        skipped_duplicates = 0
        processed = 0
        detail_page = None

        try:
            detail_page = context.new_page()
            apply_stealth(detail_page)
            block_heavy_resources(detail_page)
        except Exception:
            detail_page = None

        iterator = tqdm(place_urls, desc="Deep scraping leads", unit="lead") if TQDM_AVAILABLE else place_urls
        for index, place_url in enumerate(iterator, start=1):
            if stop_event.is_set() or len(leads) >= self.max_results:
                break

            processed += 1
            if not TQDM_AVAILABLE:
                self.log.info("🔍 %d/%d (new:%d skip:%d)", processed, len(place_urls), len(leads), skipped_duplicates)

            page_to_use = detail_page or context.new_page()
            try:
                lead = self._extract_full_listing(page_to_use, place_url, keyword, location, max_time=LISTING_MAX_SEC_PER_ITEM)

                # Merge any card data we pre-extracted (huge win - name/rating/address often complete)
                card = self._card_cache.get(place_url)
                if card and lead:
                    apply_card_to_lead(lead.to_dict(), card)  # will merge into lead attrs below if needed
                    if not lead.name and card.name:
                        lead.name = card.name
                    if not lead.address and card.address:
                        lead.address = card.address
                    if lead.rating == 0 and card.rating:
                        lead.rating = card.rating
                    if lead.review_count == 0 and card.review_count:
                        lead.review_count = card.review_count
                    if not lead.category and card.category:
                        lead.category = card.category

                if lead:
                    if self.skip_duplicates and self.history:
                        if self.history.is_duplicate(lead.to_dict(), keyword, location):
                            skipped_duplicates += 1
                            continue
                    if self._passes_website_filter(lead.website):
                        lead.extraction_quality = lead.calculate_quality()
                        leads.append(lead)
                        if self.progress_callback:
                            try:
                                self.progress_callback(lead.to_dict())
                            except Exception:
                                pass
            except CaptchaDetectedError:
                raise
            except Exception as e:
                self.log.warning("Lead extract error %s: %s", place_url[:60], str(e)[:80])
                if detail_page:
                    try:
                        detail_page.goto("https://www.google.com/maps", timeout=10000)
                    except Exception:
                        pass
            finally:
                if page_to_use is not detail_page:
                    try:
                        page_to_use.close()
                    except Exception:
                        pass

            self._human_delay(0.1, 0.35)

        if detail_page:
            try:
                detail_page.close()
            except Exception:
                pass

        self.log.info("📊 %d collected, %d dups skipped (hybrid card+page)", len(leads), skipped_duplicates)

        # POWER BOOST: Parallel deep web enrichment for leads with websites (speeds up 100+ runs significantly)
        leads_with_web = [l for l in leads if getattr(l, 'website', None)]
        if leads_with_web:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            max_workers = min(6, len(leads_with_web))
            self.log.info(f"🚀 Parallel deep web enrichment for {len(leads_with_web)} sites (workers={max_workers})")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_lead = {executor.submit(self._deep_analyze_website, None, lead.website, getattr(self, 'web_timeout_sec', 30)): lead for lead in leads_with_web if lead.website}
                for future in as_completed(future_to_lead):
                    lead = future_to_lead[future]
                    try:
                        web_data = future.result()
                        if web_data:
                            lead.emails = web_data.get("emails", []) or getattr(lead, 'emails', [])
                            lead.whatsapp_numbers = web_data.get("whatsapp_numbers", []) or getattr(lead, 'whatsapp_numbers', [])
                            for k in ["web_description", "web_services", "web_about", "web_hours", "web_address"]:
                                if web_data.get(k) and not getattr(lead, k, None):
                                    setattr(lead, k, web_data[k])
                    except Exception as e:
                        self.log.warning(f"Parallel web enrich failed: {e}")

        return leads

    def _passes_website_filter(self, website: str) -> bool:
        """Check if listing passes website filter."""
        has_website = is_business_website(website)
        if self.website_filter == "with":
            return has_website
        if self.website_filter == "without":
            return not has_website
        return True

    def _extract_full_listing(
        self,
        page: Page,
        place_url: str,
        keyword: str,
        location: str,
        max_time: float = LISTING_MAX_SEC_PER_ITEM,
    ) -> Optional[BusinessData]:
        """Extract data from single listing. Respects max_time budget."""
        start_time = time.time()

        def _budget_ok(extra: float = 0) -> bool:
            return (time.time() - start_time) < (max_time - extra)

        for attempt in range(2):
            try:
                if attempt > 0:
                    start_time = time.time()  # reset for retry budget
                page.goto(place_url, timeout=42000)
                page.wait_for_timeout(900)
                self._raise_if_captcha(page)

                data = BusinessData()
                data.google_maps_url = place_url
                data.data_sources.append("google_maps")

                # ===== STEP 1: Extract from Google Maps =====
                data.name = self._safe_text(page, "h1.DUwDvf", fallback_selector="h1")
                data.phone = self._extract_phone(page)
                data.website = self._extract_website(page)
                data.has_website = bool(data.website)
                data.address = self._extract_address(page)
                data.rating, data.review_count = self._extract_rating(page)
                data.category = self._extract_category(page)
                data.business_hours = self._extract_hours(page)
                
                # Social from Maps
                gmaps_socials = self._extract_social_from_gmaps(page)
                data.instagram = gmaps_socials.get("instagram", "")
                data.facebook = gmaps_socials.get("facebook", "")
                data.twitter = gmaps_socials.get("twitter", "")

                # ===== STEP 2: Analyze website if available (respect budget) =====
                if data.website and _budget_ok(12):
                    cache_key = self._website_cache_key(data.website)
                    website_data = self._website_cache.get(cache_key)
                    if website_data is None:
                        remaining = max(6, max_time - (time.time() - start_time))
                        if remaining >= HEAVY_STEP_MIN_REMAINING_SEC:
                            try:
                                website_data = self._deep_analyze_website(
                                    page,
                                    data.website,
                                    max_total_time_sec=min(WEBSITE_ANALYSIS_BUDGET_SEC, int(remaining)),
                                )
                                self._website_cache[cache_key] = dict(website_data)
                            except Exception as we:
                                self.log.warning("Web analysis error for %s: %s", data.website, we)
                                website_data = {}
                    if website_data:
                        data.data_sources.append("website")
                        data.emails = website_data.get("emails", []) or []
                        data.whatsapp_numbers = website_data.get("whatsapp_numbers", []) or []
                        
                        socials = website_data.get("socials", {})
                        if not data.instagram and socials.get("instagram"):
                            data.instagram = socials["instagram"]
                        if not data.facebook and socials.get("facebook"):
                            data.facebook = socials["facebook"]
                        if not data.twitter and socials.get("twitter"):
                            data.twitter = socials["twitter"]
                        if socials.get("linkedin"):
                            data.linkedin = socials["linkedin"]
                        if socials.get("tiktok"):
                            data.tiktok = socials["tiktok"]
                        if socials.get("youtube"):
                            data.youtube = socials["youtube"]
                        
                        data.has_chatbot = website_data.get("has_chatbot", False)
                        data.chatbot_type = website_data.get("chatbot_type", "")
                        data.has_google_analytics = website_data.get("has_google_analytics", False)
                        data.has_meta_pixel = website_data.get("has_meta_pixel", False)
                        data.cms_platform = website_data.get("cms_platform", "")
                        data.is_automated = data.has_chatbot

                        # Store DEEP powerful web data
                        data.web_description = website_data.get("web_description", "")
                        data.web_services = website_data.get("web_services", "")
                        data.web_address = website_data.get("web_address", "")
                        data.pages_crawled_on_web = website_data.get("pages_crawled", 0)
                        data.structured_data_on_web = website_data.get("structured_data_found", False)
                        data.web_about = website_data.get("web_about", "")
                        data.web_hours = website_data.get("web_hours", "")
                        if website_data.get("all_phones_from_web"):
                            data.additional_phones = [p for p in website_data["all_phones_from_web"].split("; ") if p][:5]

                # ===== STEP 3: Google search for additional info (only if time) =====
                needs_google_lookup = (
                    self.deep_search
                    and data.name
                    and _budget_ok(8)
                    and (
                        not data.instagram
                        or not data.facebook
                        or not data.whatsapp_numbers
                        or not data.emails
                    )
                )
                if needs_google_lookup:
                    remaining = max_time - (time.time() - start_time)
                    if remaining < GOOGLE_LOOKUP_MIN_REMAINING_SEC:
                        needs_google_lookup = False

                if needs_google_lookup:
                    google_key = f"{data.name.strip().lower()}|{location.strip().lower()}"
                    if google_key not in self._google_cache:
                        self._google_cache[google_key] = self._search_google_for_business(page, data.name, location)
                    google_data = self._google_cache.get(google_key)
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

                # ===== STEP 4: phone fallback =====
                if not data.whatsapp_numbers and data.phone:
                    normalized = normalize_phone(data.phone)
                    if normalized:
                        data.whatsapp_numbers = [normalized]

                return data

            except CaptchaDetectedError:
                raise
            except Exception as exc:
                self.log.warning("Attempt %d failed for %s: %s", attempt + 1, place_url, exc)
                if _budget_ok(3):
                    self._human_delay(0.8, 1.8)
                else:
                    break

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
        """Extract phone number from Google Maps."""
        selectors = [
            "button[data-item-id^='phone:tel:']",
            "a[data-item-id^='phone:tel:']",
            "button[aria-label*='Phone']",
            "button[aria-label*='phone']",
        ]

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() == 0:
                    continue
                text = locator.inner_text(timeout=3500).strip()
                match = PHONE_REGEX.search(text)
                if match:
                    return match.group(1).strip()
            except Exception:
                continue

        return ""

    def _extract_website(self, page: Page) -> str:
        """Extract website URL from Google Maps."""
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
        """Extract and clean business address."""
        selectors = [
            "button[data-item-id='address']",
            "button[aria-label*='Address']",
            "button[aria-label*='address']",
            "div[data-item-id='address']",
        ]

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() > 0:
                    text = locator.inner_text(timeout=3000)
                    cleaned = clean_address(text)
                    if cleaned:
                        return cleaned
            except Exception:
                continue

        return ""

    def _extract_rating(self, page: Page) -> Tuple[float, int]:
        """Extract rating and review count."""
        rating = 0.0
        review_count = 0

        try:
            # Rating
            rating_el = page.locator("span.ceNzKf, div.F7nice span[aria-hidden='true']").first
            if rating_el.count() > 0:
                rating_text = rating_el.inner_text(timeout=3000)
                match = re.search(r"[\d.]+", rating_text)
                if match:
                    rating = float(match.group())
        except Exception:
            pass

        try:
            # Review count - try multiple selectors
            review_selectors = [
                "span.UY7F9",
                "button[jsaction*='review'] span",
                "span[aria-label*='review']",
            ]
            for sel in review_selectors:
                review_el = page.locator(sel).first
                if review_el.count() > 0:
                    review_text = review_el.inner_text(timeout=3000)
                    # Extract number, handle commas
                    review_match = re.search(r"([\d,]+)", review_text)
                    if review_match:
                        review_count = int(review_match.group(1).replace(",", ""))
                        break
        except Exception:
            pass

        return rating, review_count

    def _extract_category(self, page: Page) -> str:
        """Extract business category."""
        try:
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
                text = hours_button.inner_text(timeout=3000).strip()
                # Clean up the text
                text = re.sub(r"\s+", " ", text)
                return text
        except Exception:
            pass
        return ""

    def _extract_social_from_gmaps(self, page: Page) -> Dict[str, str]:
        """Extract social links shown on Google Maps."""
        socials = {}

        try:
            links = page.eval_on_selector_all(
                "a[href*='instagram.com'], a[href*='facebook.com'], a[href*='twitter.com'], a[href*='x.com']",
                "els => els.map(el => el.href)"
            )

            for link in links:
                link_lower = link.lower()
                if "instagram.com" in link_lower:
                    socials["instagram"] = link
                elif "facebook.com" in link_lower:
                    socials["facebook"] = link
                elif "twitter.com" in link_lower or "x.com" in link_lower:
                    socials["twitter"] = link
        except Exception:
            pass

        return socials

    def _deep_analyze_website(self, page: Page, website_url: str, max_total_time_sec: int = WEBSITE_ANALYSIS_BUDGET_SEC) -> Dict:
        """EXTREMELY POWERFUL website analysis.
        Uses the upgraded WebsiteExtractor that does aggressive HTTP + Playwright rendering.
        Respects instance prod config for depth.
        """
        if not website_url.startswith(("http://", "https://")):
            website_url = f"https://{website_url}"

        try:
            crawler = WebsiteExtractor(timeout=min(14, REQUEST_TIMEOUT))
            data = crawler.enrich(
                website_url,
                max_pages=self.web_max_pages,
                max_total_time_sec=max_total_time_sec or self.web_timeout_sec,
                priority_only=False,
                use_playwright=True,
            )

            # Map to existing + DEEP web intel
            result = {
                "emails": [e for e in (data.get("all_emails", "") or "").split("; ") if e][:5],
                "whatsapp_numbers": [w for w in (data.get("all_whatsapp", "") or "").split("; ") if w][:5],
                "socials": data.get("socials", {}),
                "has_chatbot": False,
                "chatbot_type": "",
                "has_google_analytics": False,
                "has_meta_pixel": False,
                "cms_platform": "",
                # Very deep powerful web data
                "web_description": data.get("description", ""),
                "web_address": data.get("address_from_site", ""),
                "web_services": data.get("services", ""),
                "all_phones_from_web": data.get("all_phones", ""),
                "pages_crawled": data.get("pages_crawled", 0),
                "structured_data_found": data.get("structured_data_found", False),
                "web_about": data.get("about_text", ""),
                "web_hours": data.get("hours", ""),
            }

            return result
        except Exception as e:
            self.log.debug("Powerful web analysis failed: %s", e)
            return {"emails": [], "whatsapp_numbers": [], "socials": {}}

    def _website_cache_key(self, website_url: str) -> str:
        if not website_url:
            return ""
        normalized = website_url if website_url.startswith(("http://", "https://")) else f"https://{website_url}"
        parsed = urlparse(normalized)
        host = (parsed.netloc or parsed.path).lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host

    def _search_google_for_business(self, page: Page, business_name: str, location: str) -> Optional[Dict]:
        """
        Search Google for additional business information.
        Cross-verifies and finds missing social media links.
        """
        try:
            # Create search query
            search_query = f"{business_name} {location} instagram contact"
            encoded_query = quote_plus(search_query)
            
            page.goto(f"https://www.google.com/search?q={encoded_query}", timeout=20000)
            page.wait_for_timeout(1500)

            html = page.content()
            result = {}

            # Look for Instagram
            ig = extract_social_handle(html, INSTAGRAM_PATTERNS, "instagram")
            if ig:
                result["instagram"] = ig

            # Look for Facebook
            fb = extract_social_handle(html, FACEBOOK_PATTERNS, "facebook")
            if fb:
                result["facebook"] = fb

            # Look for WhatsApp
            wa_list = extract_whatsapp(html)
            if wa_list:
                result["whatsapp"] = wa_list[0]

            # Look for email
            emails = extract_emails(html)
            if emails:
                result["email"] = emails[0]

            return result if result else None

        except Exception as e:
            self.log.debug("Google search failed: %s", e)
            return None

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

        raise CaptchaDetectedError("Captcha or anti-bot challenge detected")

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


# Alias for backwards compatibility
GoogleMapsScraper = DeepBusinessScraper

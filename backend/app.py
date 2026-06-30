import csv
import concurrent.futures
import logging
import os
import re
import threading
from pathlib import Path
from datetime import datetime
from threading import Event, Thread
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
import requests
from url_filters import is_business_website

# Production: load .env for easy config
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass  # optional, env vars still work directly

# Import all scrapers for different modes
# Ultra Deep - uses ALL engines in parallel with cross-verification
try:
    from ultra_scraper import UltraDeepScraper
except ImportError:
    UltraDeepScraper = None

# Deep - multi-source (Maps + Website + Google Search)
try:
    from deep_scraper import DeepBusinessScraper
except ImportError:
    DeepBusinessScraper = None

# Enhanced - comprehensive extraction
try:
    from enhanced_scraper_sync import GoogleMapsScraper as EnhancedScraper
except ImportError:
    try:
        from enhanced_scraper import GoogleMapsScraper as EnhancedScraper
    except ImportError:
        EnhancedScraper = None

# Basic - fast Maps-only extraction
try:
    from scraper import GoogleMapsScraper as BasicScraper, CaptchaDetectedError
except ImportError:
    BasicScraper = None
    class CaptchaDetectedError(RuntimeError):
        pass

# Import history manager for deduplication
try:
    from scrape_history import get_history, ScrapeHistory
    scrape_history = get_history()
except ImportError:
    scrape_history = None

try:
    from email_extractor import WebsiteExtractor
except ImportError:
    WebsiteExtractor = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("lead-scraper")

SCRAPE_STATE = {
    "running": False,
    "status": "idle",
    "message": "Ready",
    "results": [],
    "csv_path": "",
    "keyword": "",
    "location": "",
}
STOP_EVENT = Event()

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "lead-scraper-location-assistant/1.0"
LOCATION_SUGGEST_TIMEOUT = 8
LOCATION_SUGGEST_MAX = 10
EMAIL_ENRICHMENT_MAX_TARGETS = int(os.getenv("EMAIL_ENRICHMENT_MAX_TARGETS", "160"))
EMAIL_ENRICHMENT_WORKERS = int(os.getenv("EMAIL_ENRICHMENT_WORKERS", "6"))
WEB_MAX_PAGES = int(os.getenv("WEB_MAX_PAGES", "16"))
WEB_TIMEOUT_SEC = int(os.getenv("WEB_TIMEOUT_SEC", "30"))

COUNTRY_ALIASES = {
    "usa": "United States",
    "us": "United States",
    "uk": "United Kingdom",
    "uae": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "sa": "Saudi Arabia",
}


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/modes")
def get_available_modes() -> Dict:
    """Return available extraction modes based on installed scrapers."""
    modes = []
    
    if UltraDeepScraper:
        modes.append({
            "value": "ultra",
            "label": "🚀 Ultra Deep (ALL engines + Cross-verification)",
            "description": "Uses ALL extraction engines in parallel with cross-verification. Highest accuracy, slowest speed."
        })
    
    if DeepBusinessScraper:
        modes.append({
            "value": "deep",
            "label": "🔍 Deep (Maps + Website + Google Search)",
            "description": "Multi-source extraction with Google Search cross-verification. Finds more Instagram/Facebook/WhatsApp."
        })
    
    if EnhancedScraper:
        modes.append({
            "value": "enhanced",
            "label": "⚙️ Enhanced (Maps + Website analysis)",
            "description": "Google Maps + comprehensive website analysis. Good balance of speed and data quality."
        })
    
    if BasicScraper:
        modes.append({
            "value": "basic",
            "label": "⚡ Basic (Fastest, Maps only)",
            "description": "Fast Maps-only extraction. Gets name, phone, address, rating, website from Google Maps only."
        })
    
    return jsonify({"modes": modes})


@app.get("/status")
def status() -> Dict:
    return jsonify(
        {
            "running": SCRAPE_STATE["running"],
            "status": SCRAPE_STATE["status"],
            "message": SCRAPE_STATE["message"],
            "count": len(SCRAPE_STATE["results"]),
            "results": SCRAPE_STATE["results"],
        }
    )


@app.post("/stop")
def stop_scrape() -> Dict:
    if SCRAPE_STATE["running"]:
        STOP_EVENT.set()
        SCRAPE_STATE["status"] = "stopping"
        SCRAPE_STATE["message"] = "Stop requested. Finishing current step..."
        return jsonify(
            {
                "ok": True,
                "message": "Stop signal sent",
                "count": len(SCRAPE_STATE["results"]),
                "results": SCRAPE_STATE["results"],
            }
        )
    return jsonify({"ok": False, "message": "No active scrape job"}), 400


# ============================================================================
# HISTORY MANAGEMENT ENDPOINTS
# ============================================================================

@app.get("/history/stats")
def get_history_stats() -> Dict:
    """Get history statistics for a search query."""
    if not scrape_history:
        return jsonify({"error": "History not available"}), 500
    
    keyword = request.args.get("keyword", "")
    location = request.args.get("location", "")
    
    stats = scrape_history.get_stats(keyword, location)
    return jsonify(stats)


@app.post("/history/clear")
def clear_history() -> Dict:
    """Clear history for a search query or all history."""
    if not scrape_history:
        return jsonify({"error": "History not available"}), 500
    
    payload = request.get_json(silent=True) or {}
    keyword = (payload.get("keyword") or "").strip()
    location = (payload.get("location") or "").strip()
    clear_all = payload.get("clear_all", False)
    
    if clear_all:
        count = scrape_history.clear_all_history()
        return jsonify({"ok": True, "message": f"Cleared all history ({count} businesses)", "cleared": count})
    elif keyword and location:
        count = scrape_history.clear_search_history(keyword, location)
        return jsonify({"ok": True, "message": f"Cleared history for '{keyword}' in '{location}' ({count} businesses)", "cleared": count})
    else:
        return jsonify({"error": "Provide keyword and location, or set clear_all=true"}), 400


@app.get("/history/previous")
def get_previous_scraped() -> Dict:
    """Get list of previously scraped businesses for a search."""
    if not scrape_history:
        return jsonify({"error": "History not available"}), 500
    
    keyword = request.args.get("keyword", "")
    location = request.args.get("location", "")
    limit = int(request.args.get("limit", 100))
    
    if not keyword or not location:
        return jsonify({"error": "keyword and location are required"}), 400
    
    previous = scrape_history.get_previously_scraped(keyword, location, limit)
    return jsonify({"count": len(previous), "businesses": previous})


@app.get("/history/output-files")
def get_output_history_files() -> Dict:
    """List CSV output files that can be used as selectable history sources."""
    files = _list_output_history_files()
    return jsonify({"count": len(files), "files": files})


@app.get("/location/suggest")
def suggest_locations() -> Dict:
    raw_query = (request.args.get("q") or "").strip()
    if not raw_query:
        return jsonify({"count": 0, "suggestions": []})

    try:
        requested_limit = int(request.args.get("limit", LOCATION_SUGGEST_MAX))
    except (TypeError, ValueError):
        requested_limit = LOCATION_SUGGEST_MAX

    limit = max(1, min(requested_limit, LOCATION_SUGGEST_MAX))
    suggestions = _fetch_location_suggestions(raw_query, limit=limit)
    return jsonify({"count": len(suggestions), "suggestions": suggestions})


@app.post("/scrape")
def scrape() -> Dict:
    if SCRAPE_STATE["running"]:
        return jsonify({"error": "A scrape is already running"}), 409

    payload = request.get_json(silent=True) or {}
    keyword = (payload.get("keyword") or "").strip()
    location = (payload.get("location") or "").strip()
    website_filter = (payload.get("website_filter") or "all").strip().lower()
    extraction_mode = (payload.get("extraction_mode") or "deep").strip().lower()
    
    if website_filter not in {"all", "with", "without"}:
        website_filter = "all"

    # Backward compatibility with older frontend payloads.
    if "only_with_website" in payload and bool(payload.get("only_with_website")):
        website_filter = "with"

    # Additional options
    deep_search = bool(payload.get("deep_search", True))
    verify_socials = bool(payload.get("verify_socials", True))
    skip_duplicates = bool(payload.get("skip_duplicates", True))
    dry_run = bool(payload.get("dry_run", False))  # Powerful dry-run for tests / safe validation
    card_only = bool(payload.get("card_only", False))  # Ultra-fast: maps cards only, no website visits
    selected_history_files = _normalize_history_file_selection(payload.get("selected_history_files"))

    try:
        requested_max_results = int(payload.get("max_results", 50))
    except (TypeError, ValueError):
        requested_max_results = 50
    max_results = max(1, min(requested_max_results, 500))
    headless = bool(payload.get("headless", False))

    if not keyword or not location:
        return jsonify({"error": "keyword and location are required"}), 400

    # Mode descriptions for status message
    mode_descriptions = {
        "basic": "Basic (Maps only)",
        "enhanced": "Enhanced (Maps + Website)",
        "deep": "Deep (Maps + Website + Google Search)",
        "ultra": "Ultra Deep (ALL engines + Cross-verification)",
    }
    mode_desc = mode_descriptions.get(extraction_mode, extraction_mode)
    exclusion_business_ids: Set[str] = set()
    excluded_by_history = 0

    if skip_duplicates and scrape_history:
        exclusion_business_ids.update(scrape_history.get_existing_business_ids(keyword, location))

    if selected_history_files and scrape_history:
        selected_paths = [Path(os.path.join(OUTPUT_DIR, name)) for name in selected_history_files]
        imported_count, selected_ids = scrape_history.import_output_files_to_history(selected_paths)
        exclusion_business_ids.update(selected_ids)
        log.info(
            "Using %d selected output files for exclusion (%d IDs, %d newly imported)",
            len(selected_history_files),
            len(selected_ids),
            imported_count,
        )
        mode_desc += f" + selected-files({len(selected_history_files)})"
    elif selected_history_files:
        log.warning("Selected history files were provided, but history manager is unavailable")
    
    # Add deduplication info
    if skip_duplicates and scrape_history:
        stats = scrape_history.get_stats(keyword, location)
        prev_count = stats.get("search_total", 0)
        if prev_count > 0:
            mode_desc += f" (skipping {prev_count} already scraped)"

    SCRAPE_STATE["keyword"] = keyword
    SCRAPE_STATE["location"] = location
    SCRAPE_STATE["running"] = True
    SCRAPE_STATE["status"] = "running"
    SCRAPE_STATE["message"] = f"🔍 {mode_desc} scraping for '{keyword}' in '{location}' (background)"
    SCRAPE_STATE["results"] = []
    SCRAPE_STATE["csv_path"] = ""
    STOP_EVENT.clear()

    # Launch scrape in background thread so HTTP request returns immediately.
    # Frontend relies on /status polling for live updates + final results.
    def _run_scrape_job():
        partial_results: List[Dict[str, str]] = []
        local_excluded = 0

        def report_progress(lead: Dict[str, str]) -> None:
            if not isinstance(lead, dict):
                return
            lead_copy = dict(lead)
            if exclusion_business_ids and scrape_history:
                business_id = scrape_history.get_business_id(lead_copy)
                if business_id and business_id in exclusion_business_ids:
                    return
            lead_copy["whatsapp_wa_me_links"] = _build_whatsapp_wa_me_links(lead_copy)
            partial_results.append(lead_copy)
            SCRAPE_STATE["results"] = partial_results.copy()
            if STOP_EVENT.is_set():
                SCRAPE_STATE["message"] = f"Stopping... {len(partial_results)} leads collected so far"
            else:
                SCRAPE_STATE["message"] = f"Running... {len(partial_results)} leads collected"

        scraper = None
        results: List[Dict[str, str]] = []
        try:
            # Choose scraper based on extraction mode
            if extraction_mode == "ultra":
                if UltraDeepScraper:
                    log.info("Using ULTRA DEEP scraper (all engines + cross-verification)")
                    scraper = UltraDeepScraper(
                        max_results=max_results,
                        headless=headless,
                        website_filter=website_filter,
                        verify_socials=verify_socials,
                        skip_duplicates=skip_duplicates,
                        logger=log,
                        progress_callback=report_progress,
                    )
                else:
                    log.warning("UltraDeepScraper not available, falling back to Deep")
            if extraction_mode == "deep" and scraper is None:
                if DeepBusinessScraper:
                    log.info("Using DEEP scraper (Maps + Website + Google Search)")
                    scraper = DeepBusinessScraper(
                        max_results=max_results,
                        headless=headless,
                        website_filter=website_filter,
                        deep_search=deep_search,
                        skip_duplicates=skip_duplicates,
                        dry_run=dry_run,
                        use_checkpoints=True,
                        card_only=card_only,
                        web_max_pages=WEB_MAX_PAGES,
                        web_timeout_sec=WEB_TIMEOUT_SEC,
                        logger=log,
                        progress_callback=report_progress,
                    )
                else:
                    log.warning("DeepBusinessScraper not available, falling back to Enhanced")
            if extraction_mode == "enhanced" and scraper is None:
                if EnhancedScraper:
                    log.info("Using ENHANCED scraper (Maps + Website analysis)")
                    scraper = EnhancedScraper(
                        max_results=max_results,
                        headless=headless,
                        website_filter=website_filter,
                        dry_run=dry_run,
                        logger=log,
                        progress_callback=report_progress,
                    )
                else:
                    log.warning("EnhancedScraper not available, falling back to Basic")
            if extraction_mode == "basic" or scraper is None:
                if BasicScraper:
                    log.info("Using BASIC scraper (Maps only)")
                    scraper = BasicScraper(
                        max_results=max_results,
                        headless=headless,
                        website_filter=website_filter,
                        dry_run=dry_run,
                        logger=log,
                        progress_callback=report_progress,
                    )
                else:
                    SCRAPE_STATE["status"] = "error"
                    SCRAPE_STATE["message"] = "No scraper implementation available"
                    return

            if scraper is None:
                SCRAPE_STATE["status"] = "error"
                SCRAPE_STATE["message"] = "Failed to initialize scraper"
                return

            raw_results = scraper.scrape(keyword=keyword, location=location, stop_event=STOP_EVENT)

            if exclusion_business_ids and scrape_history:
                filtered = []
                for lead in raw_results or []:
                    if not isinstance(lead, dict):
                        filtered.append(lead)
                        continue
                    bid = scrape_history.get_business_id(lead)
                    if bid and bid in exclusion_business_ids:
                        local_excluded += 1
                        continue
                    filtered.append(lead)
                raw_results = filtered
                if local_excluded:
                    log.info("Excluded %d leads using history/selected files", local_excluded)

            results = _enrich_missing_emails(raw_results or [])

            for lead in results:
                if isinstance(lead, dict):
                    lead["whatsapp_wa_me_links"] = _build_whatsapp_wa_me_links(lead)

            if scrape_history and results:
                scrape_history.add_batch_to_history(results, keyword, location)

            SCRAPE_STATE["results"] = results
            log.info("Scraping completed. Found %d results", len(results))

            csv_path = _write_csv(keyword, location, results)
            SCRAPE_STATE["csv_path"] = csv_path
            log.info("CSV file written to: %s", csv_path)

            if STOP_EVENT.is_set():
                SCRAPE_STATE["status"] = "stopped"
                SCRAPE_STATE["message"] = f"Scrape stopped. {len(results)} leads collected"
            else:
                msg = f"Completed. {len(results)} leads collected"
                if local_excluded > 0:
                    msg += f" ({local_excluded} skipped from history/selected files)"
                SCRAPE_STATE["status"] = "completed"
                SCRAPE_STATE["message"] = msg
        except CaptchaDetectedError as exc:
            log.error("Captcha detected: %s", exc)
            SCRAPE_STATE["status"] = "captcha"
            SCRAPE_STATE["message"] = "Captcha detected. Run non-headless and solve manually."
        except Exception as exc:
            if _looks_like_captcha_error(exc):
                log.error("Captcha-like challenge: %s", exc)
                SCRAPE_STATE["status"] = "captcha"
                SCRAPE_STATE["message"] = "Captcha challenge. Run non-headless to solve if shown."
            else:
                log.exception("Scrape job failed: %s", exc)
                SCRAPE_STATE["status"] = "error"
                SCRAPE_STATE["message"] = f"Scrape failed: {exc}"
        finally:
            SCRAPE_STATE["running"] = False

    # Start background job (daemon so it doesn't prevent shutdown)
    job_thread = Thread(target=_run_scrape_job, name="scrape-job", daemon=True)
    job_thread.start()

    # Return immediately so client doesn't block on long-running scrape
    return jsonify(
        {
            "status": "started",
            "message": f"Scrape started in background for '{keyword}' in '{location}'. Use /status to monitor.",
            "count": 0,
            "results": [],
        }
    )


@app.get("/download")
def download_csv():
    csv_path = SCRAPE_STATE.get("csv_path")
    if not csv_path or not os.path.exists(csv_path):
        results = SCRAPE_STATE.get("results") or []
        if results:
            try:
                csv_path = _write_csv(
                    SCRAPE_STATE.get("keyword", ""),
                    SCRAPE_STATE.get("location", ""),
                    results,
                )
                SCRAPE_STATE["csv_path"] = csv_path
            except Exception as exc:
                log.error("Failed to create CSV for download: %s", exc)
        if not csv_path or not os.path.exists(csv_path):
            return jsonify({"error": "No CSV file available. Run scraping first."}), 404
    return send_file(csv_path, as_attachment=True)


def _looks_like_captcha_error(exc: Exception) -> bool:
    message = (str(exc) or "").lower()
    markers = (
        "captcha",
        "recaptcha",
        "unusual traffic",
        "anti-bot",
        "verify you are human",
        "not a robot",
        "challenge",
    )
    return any(marker in message for marker in markers)


def _write_csv(keyword: str, location: str, leads: List[Dict[str, str]]) -> str:
    safe_keyword = _sanitize_token(keyword)
    safe_location = _sanitize_token(location)
    base_filename = f"leads_{safe_keyword}_{safe_location}"
    path = _build_unique_output_path(base_filename)
    
    log.info("Writing CSV to: %s", path)
    log.info("Number of leads to write: %d", len(leads))

    # Enhanced CSV with all extracted fields + very powerful web data
    fieldnames = [
        "Name", "Phone", "Email", "All Emails", "WhatsApp", "All WhatsApp",
        "All Phones from Web",
        "WhatsApp wa.me Links",
        "Website", "Has Website", "Address", "Rating", "Reviews",
        "Category", "Business Hours",
        "Instagram", "Facebook", "Twitter", "LinkedIn", "TikTok", "YouTube",
        "Has Chatbot", "Chatbot Type", "Has Google Analytics", "Has Meta Pixel",
        "CMS Platform", "Is Automated", "Quality Score", "Verification Score",
        "Data Sources", "Google Maps URL",
        "Web Description", "Web Services", "Web Address", "Pages Crawled on Web", "Structured Data on Web", "Web About", "Web Hours"
    ]

    # Use pandas for powerful data handling when available (faster, more features, Excel support)
    if PANDAS_AVAILABLE and len(leads) > 0:
        try:
            df = pd.DataFrame([
                {
                    "Name": lead.get("name", ""),
                    "Phone": lead.get("phone", ""),
                    "Email": lead.get("email", ""),
                    "All Emails": lead.get("all_emails", ""),
                    "WhatsApp": lead.get("whatsapp", ""),
                    "All WhatsApp": lead.get("all_whatsapp", ""),
                    "WhatsApp wa.me Links": _build_whatsapp_wa_me_links(lead),
                    "Website": lead.get("website", ""),
                    "Has Website": lead.get("has_website", ""),
                    "Address": lead.get("address", ""),
                    "Rating": lead.get("rating", ""),
                    "Reviews": lead.get("review_count", ""),
                    "Category": lead.get("category", ""),
                    "Business Hours": lead.get("business_hours", ""),
                    "Instagram": lead.get("instagram", ""),
                    "Facebook": lead.get("facebook", ""),
                    "Twitter": lead.get("twitter", ""),
                    "LinkedIn": lead.get("linkedin", ""),
                    "TikTok": lead.get("tiktok", ""),
                    "YouTube": lead.get("youtube", ""),
                    "Has Chatbot": lead.get("has_chatbot", ""),
                    "Chatbot Type": lead.get("chatbot_type", ""),
                    "Has Google Analytics": lead.get("has_google_analytics", ""),
                    "Has Meta Pixel": lead.get("has_meta_pixel", ""),
                    "CMS Platform": lead.get("cms_platform", ""),
                    "Is Automated": lead.get("is_automated", ""),
                    "Quality Score": lead.get("quality_score", ""),
                    "Verification Score": lead.get("verification_score", ""),
                    "Data Sources": lead.get("data_sources", ""),
                    "Google Maps URL": lead.get("google_maps_url", ""),
                    "Web Description": lead.get("web_description", ""),
                    "Web Services": lead.get("web_services", ""),
                    "Web Address": lead.get("web_address", ""),
                    "All Phones from Web": lead.get("all_phones", "") or lead.get("all_phones_from_web", ""),
                    "Pages Crawled on Web": lead.get("pages_crawled_on_web", ""),
                    "Structured Data on Web": lead.get("structured_data_on_web", ""),
                    "Web About": lead.get("web_about", ""),
                    "Web Hours": lead.get("web_hours", ""),
                }
                for lead in leads
            ])
            df.to_csv(path, index=False)
            # Bonus: also save Excel for power users
            excel_path = path.replace(".csv", ".xlsx")
            try:
                df.to_excel(excel_path, index=False, engine="openpyxl" if "openpyxl" in str(pd.__version__) else None)
                log.info("Also wrote powerful Excel: %s", excel_path)
            except Exception:
                pass  # openpyxl may not be installed
            log.info("Powerful pandas CSV written: %s", path)
            return path
        except Exception as e:
            log.warning("Pandas export failed, falling back to csv: %s", e)
    
    # Standard CSV fallback
    try:
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for lead in leads:
                writer.writerow(
                    {
                        "Name": lead.get("name", ""),
                        "Phone": lead.get("phone", ""),
                        "Email": lead.get("email", ""),
                        "All Emails": lead.get("all_emails", ""),
                        "WhatsApp": lead.get("whatsapp", ""),
                        "All WhatsApp": lead.get("all_whatsapp", ""),
                        "WhatsApp wa.me Links": _build_whatsapp_wa_me_links(lead),
                        "Website": lead.get("website", ""),
                        "Has Website": lead.get("has_website", ""),
                        "Address": lead.get("address", ""),
                        "Rating": lead.get("rating", ""),
                        "Reviews": lead.get("review_count", ""),
                        "Category": lead.get("category", ""),
                        "Business Hours": lead.get("business_hours", ""),
                        "Instagram": lead.get("instagram", ""),
                        "Facebook": lead.get("facebook", ""),
                        "Twitter": lead.get("twitter", ""),
                        "LinkedIn": lead.get("linkedin", ""),
                        "TikTok": lead.get("tiktok", ""),
                        "YouTube": lead.get("youtube", ""),
                        "Has Chatbot": lead.get("has_chatbot", ""),
                        "Chatbot Type": lead.get("chatbot_type", ""),
                        "Has Google Analytics": lead.get("has_google_analytics", ""),
                        "Has Meta Pixel": lead.get("has_meta_pixel", ""),
                        "CMS Platform": lead.get("cms_platform", ""),
                        "Is Automated": lead.get("is_automated", ""),
                        "Quality Score": lead.get("quality_score", ""),
                        "Verification Score": lead.get("verification_score", ""),
                        "Data Sources": lead.get("data_sources", ""),
                        "Google Maps URL": lead.get("google_maps_url", ""),
                        "Web Description": lead.get("web_description", ""),
                        "Web Services": lead.get("web_services", ""),
                        "Web Address": lead.get("web_address", ""),
                        "All Phones from Web": lead.get("all_phones", "") or lead.get("all_phones_from_web", ""),
                        "Pages Crawled on Web": lead.get("pages_crawled_on_web", ""),
                        "Structured Data on Web": lead.get("structured_data_on_web", ""),
                        "Web About": lead.get("web_about", ""),
                        "Web Hours": lead.get("web_hours", ""),
                    }
                )
        log.info("CSV file successfully written: %s", path)
    except Exception as e:
        log.error("Failed to write CSV file: %s", e)
        raise

    return path


def _build_unique_output_path(base_filename: str) -> str:
    filename = os.path.basename((base_filename or "").strip())
    stem, ext = os.path.splitext(filename)

    if not stem:
        stem = datetime.now().strftime("%Y%m%d%H%M%S")
    if ext.lower() != ".csv":
        ext = ".csv"

    candidate = os.path.join(OUTPUT_DIR, f"{stem}{ext}")
    if not os.path.exists(candidate):
        return candidate

    suffix = 1
    while True:
        candidate = os.path.join(OUTPUT_DIR, f"{stem}_{suffix}{ext}")
        if not os.path.exists(candidate):
            return candidate
        suffix += 1


def _list_output_history_files() -> List[Dict[str, str]]:
    files: List[Dict[str, str]] = []

    try:
        for entry in os.scandir(OUTPUT_DIR):
            if not entry.is_file() or not entry.name.lower().endswith(".csv"):
                continue

            stat = entry.stat()
            modified = datetime.fromtimestamp(stat.st_mtime)
            files.append(
                {
                    "name": entry.name,
                    "size_bytes": stat.st_size,
                    "rows": _count_csv_rows(entry.path),
                    "modified": modified.strftime("%Y-%m-%d %H:%M:%S"),
                    "modified_ts": stat.st_mtime,
                }
            )
    except Exception as exc:
        log.warning("Failed listing output history files: %s", exc)
        return []

    files.sort(key=lambda item: item.get("modified_ts", 0), reverse=True)
    for item in files:
        item.pop("modified_ts", None)
    return files


def _count_csv_rows(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", newline="") as file:
            row_count = sum(1 for _ in csv.reader(file))
            return max(0, row_count - 1)
    except Exception:
        return 0


def _normalize_history_file_selection(raw_files: Optional[object]) -> List[str]:
    if not isinstance(raw_files, list):
        return []

    selected: List[str] = []
    seen: Set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, str):
            continue

        name = os.path.basename(raw.strip())
        if not name or not name.lower().endswith(".csv"):
            continue

        full_path = os.path.join(OUTPUT_DIR, name)
        if not os.path.isfile(full_path):
            continue

        if name in seen:
            continue
        seen.add(name)
        selected.append(name)

    return selected


def _build_whatsapp_wa_me_links(lead: Dict[str, str]) -> str:
    """Build wa.me links from extracted WhatsApp numbers."""
    raw_values = [
        str(lead.get("all_whatsapp", "") or ""),
        str(lead.get("whatsapp", "") or ""),
    ]

    numbers: List[str] = []
    seen = set()

    for raw in raw_values:
        if not raw:
            continue

        candidates = re.findall(r"\+?\d[\d\s()\-.]{6,}\d", raw)
        if not candidates:
            candidates = [raw]

        for candidate in candidates:
            digits = re.sub(r"\D", "", candidate)
            if len(digits) < 8:
                continue
            if digits in seen:
                continue
            seen.add(digits)
            numbers.append(digits)

    return "; ".join(f"https://wa.me/{number}" for number in numbers)


def _fetch_location_suggestions(query: str, limit: int = LOCATION_SUGGEST_MAX) -> List[Dict[str, str]]:
    expanded_queries = _expand_location_query(query)
    headers = {
        "User-Agent": NOMINATIM_USER_AGENT,
        "Accept": "application/json",
    }

    seen = set()
    suggestions: List[Dict[str, str]] = []

    for q in expanded_queries:
        if len(suggestions) >= limit:
            break

        params = {
            "q": q,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": max(limit * 2, 10),
            "dedupe": 1,
        }

        try:
            response = requests.get(
                NOMINATIM_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=LOCATION_SUGGEST_TIMEOUT,
            )
            if response.status_code >= 400:
                continue
            payload = response.json()
        except Exception as exc:
            log.debug("Location suggestion lookup failed for '%s': %s", q, exc)
            continue

        if not isinstance(payload, list):
            continue

        for item in payload:
            suggestion = _normalize_location_suggestion(item)
            if not suggestion:
                continue

            key = (suggestion.get("value") or "").lower()
            if not key or key in seen:
                continue

            seen.add(key)
            suggestions.append(suggestion)
            if len(suggestions) >= limit:
                break

    return suggestions


def _expand_location_query(query: str) -> List[str]:
    cleaned = re.sub(r"\s+", " ", (query or "").strip())
    if not cleaned:
        return []

    expanded: List[str] = [cleaned]
    lowered = cleaned.lower()
    for alias, full_name in COUNTRY_ALIASES.items():
        alias_pattern = re.compile(rf"\b{re.escape(alias)}\b", re.I)
        if alias_pattern.search(lowered):
            expanded.append(alias_pattern.sub(full_name, cleaned))

    deduped: List[str] = []
    seen = set()
    for value in expanded:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _normalize_location_suggestion(item: object) -> Optional[Dict[str, str]]:
    if not isinstance(item, dict):
        return None

    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    display_name = str(item.get("display_name") or "").strip()
    if not display_name:
        return None

    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or ""
    )
    state = address.get("state") or address.get("region") or ""
    country = address.get("country") or ""
    country_code = str(address.get("country_code") or "").upper()

    pieces = [part for part in [city, state, country] if part]
    if not pieces:
        value = display_name
    else:
        value = ", ".join(pieces)

    label = value
    if country_code:
        label = f"{value} ({country_code})"

    return {
        "label": label,
        "value": value,
        "display_name": display_name,
        "city": city,
        "state": state,
        "country": country,
        "country_code": country_code,
        "type": str(item.get("type") or ""),
    }


def _website_host_key(website: str) -> str:
    raw = (website or "").strip()
    if not raw:
        return ""
    normalized = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
    parsed = urlparse(normalized)
    host = (parsed.netloc or parsed.path).lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _enrich_missing_emails(results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not results or WebsiteExtractor is None:
        return results

    website_targets: Dict[str, Dict[str, object]] = {}
    target_count = 0

    for idx, lead in enumerate(results):
        if not isinstance(lead, dict):
            continue

        existing_email = str(lead.get("email", "") or "").strip()
        website = str(lead.get("website", "") or "").strip()
        if existing_email or not is_business_website(website):
            continue

        host_key = _website_host_key(website)
        if not host_key:
            continue

        target_count += 1
        if target_count > EMAIL_ENRICHMENT_MAX_TARGETS:
            break

        bucket = website_targets.setdefault(
            host_key,
            {
                "website": website,
                "phone": str(lead.get("phone", "") or "").strip(),
                "indexes": [],
            },
        )
        bucket["indexes"].append(idx)

    if not website_targets:
        return results

    log.info("Backfilling missing emails for %d website host(s)", len(website_targets))

    host_results: Dict[str, Dict[str, str]] = {}

    def _enrich_host(host_key: str, data: Dict[str, object]) -> Optional[tuple]:
        website = str(data.get("website") or "")
        fallback_phone = str(data.get("phone") or "")
        if not website:
            return None

        try:
            extractor = WebsiteExtractor(timeout=10)
            enriched = extractor.enrich(
                website,
                fallback_phone=fallback_phone,
                max_pages=12,
                max_total_time_sec=18,
                priority_only=False,
                use_playwright=True,
            )
            return host_key, {
                "email": str(enriched.get("email") or "").strip(),
                "whatsapp": str(enriched.get("whatsapp") or "").strip(),
                "all_emails": str(enriched.get("all_emails") or "").strip(),
                "all_phones": str(enriched.get("all_phones") or "").strip(),
                "socials": enriched.get("socials") or {},
                "web_description": str(enriched.get("description") or "").strip(),
            }
        except Exception as exc:
            log.debug("Powerful web backfill failed for %s: %s", website, exc)
            return host_key, {"email": "", "whatsapp": ""}

    max_workers = max(1, min(EMAIL_ENRICHMENT_WORKERS, len(website_targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_enrich_host, host_key, data): host_key
            for host_key, data in website_targets.items()
        }
        for future in concurrent.futures.as_completed(future_map):
            item = future.result()
            if not item:
                continue
            host_key, enriched = item
            host_results[host_key] = enriched

    updated = 0
    for host_key, data in website_targets.items():
        enriched = host_results.get(host_key) or {}
        email = str(enriched.get("email") or "").strip()
        whatsapp = str(enriched.get("whatsapp") or "").strip()

        for idx in data.get("indexes", []):
            lead = results[idx]
            if not isinstance(lead, dict):
                continue

            if email and not str(lead.get("email") or "").strip():
                lead["email"] = email
                existing_all = str(lead.get("all_emails") or "").strip()
                lead["all_emails"] = f"{existing_all}; {email}".strip("; ") if existing_all else email
                updated += 1

            if whatsapp and not str(lead.get("whatsapp") or "").strip():
                lead["whatsapp"] = whatsapp
                existing_all_wa = str(lead.get("all_whatsapp") or "").strip()
                lead["all_whatsapp"] = f"{existing_all_wa}; {whatsapp}".strip("; ") if existing_all_wa else whatsapp

    if updated:
        log.info("Email backfill updated %d lead(s)", updated)

    return results


def _sanitize_token(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"[^a-z0-9_]+", "", value)
    return value or datetime.now().strftime("%Y%m%d%H%M%S")


if __name__ == "__main__":
    # Disable debug mode to prevent auto-reload during long scraping sessions
    host = os.getenv("APP_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("APP_PORT", "5001"))
    except ValueError:
        port = 5001

    log.info("Starting server on http://%s:%s", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)

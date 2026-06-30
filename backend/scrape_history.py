"""
Scrape History Manager - Tracks previously scraped businesses to ensure fresh results.

Features:
1. Stores all scraped businesses with their identifiers
2. Checks new results against history to skip duplicates
3. Supports keyword+location specific history
4. Allows clearing history per search or globally
"""

import json
import os
import hashlib
import csv
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple
from pathlib import Path
import logging

try:
    from rapidfuzz import fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    fuzz = None

# History file location
HISTORY_DIR = Path(__file__).parent.parent / "output" / "history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)

GLOBAL_HISTORY_FILE = HISTORY_DIR / "global_history.json"
SEARCH_HISTORY_DIR = HISTORY_DIR / "searches"
SEARCH_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


class ScrapeHistory:
    """Manages scrape history to prevent duplicate results."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.log = logger or logging.getLogger(__name__)
        self._global_history: Dict[str, Dict] = {}
        self._search_histories: Dict[str, Set[str]] = {}
        self._load_global_history()
    
    def _load_global_history(self) -> None:
        """Load global history from file."""
        if GLOBAL_HISTORY_FILE.exists():
            try:
                with open(GLOBAL_HISTORY_FILE, "r", encoding="utf-8") as f:
                    self._global_history = json.load(f)
                self.log.info(f"Loaded {len(self._global_history)} businesses from history")
            except Exception as e:
                self.log.error(f"Failed to load history: {e}")
                self._global_history = {}
        else:
            self._global_history = {}
    
    def _save_global_history(self) -> None:
        """Save global history to file."""
        try:
            with open(GLOBAL_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self._global_history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log.error(f"Failed to save history: {e}")
    
    def _get_search_key(self, keyword: str, location: str) -> str:
        """Generate a unique key for a search query."""
        combined = f"{keyword.lower().strip()}_{location.lower().strip()}"
        return hashlib.md5(combined.encode()).hexdigest()[:12]
    
    def _get_business_id(self, business: Dict) -> str:
        """Generate a unique ID for a business."""
        # Use multiple fields to create a unique identifier
        name = (business.get("name") or "").lower().strip()
        phone = (business.get("phone") or "").replace(" ", "").replace("-", "")
        address = (business.get("address") or "").lower().strip()[:50]
        
        # Primary ID based on name + phone (most reliable)
        if name and phone:
            combined = f"{name}_{phone}"
        # Fallback to name + address
        elif name and address:
            combined = f"{name}_{address}"
        # Last resort: just name
        elif name:
            combined = name
        else:
            # Use Google Maps URL if available
            maps_url = business.get("google_maps_url", "")
            if maps_url:
                combined = maps_url
            else:
                return ""
        
        return hashlib.md5(combined.encode()).hexdigest()

    def get_business_id(self, business: Dict) -> str:
        """Public helper to generate business ID from a business dictionary."""
        return self._get_business_id(business)

    def get_existing_business_ids(self, keyword: str = "", location: str = "") -> Set[str]:
        """Return a snapshot of existing IDs currently in history."""
        ids = set(self._global_history.keys())
        if keyword and location:
            search_key = self._get_search_key(keyword, location)
            ids.update(self._load_search_history(search_key))
        return ids

    def import_output_files_to_history(self, file_paths: List[Path]) -> Tuple[int, Set[str]]:
        """Import businesses from CSV output files into global history.

        Returns:
            A tuple of (newly_imported_count, all_ids_found_in_files)
        """
        imported = 0
        file_business_ids: Set[str] = set()

        for file_path in file_paths:
            if not file_path.exists() or not file_path.is_file():
                continue

            try:
                with open(file_path, "r", encoding="utf-8", newline="") as file:
                    reader = csv.DictReader(file)
                    for row in reader:
                        business = {
                            "name": (row.get("Name") or row.get("name") or "").strip(),
                            "phone": (row.get("Phone") or row.get("phone") or "").strip(),
                            "address": (row.get("Address") or row.get("address") or "").strip(),
                            "google_maps_url": (row.get("Google Maps URL") or row.get("google_maps_url") or "").strip(),
                        }

                        business_id = self._get_business_id(business)
                        if not business_id:
                            continue

                        file_business_ids.add(business_id)
                        if business_id in self._global_history:
                            continue

                        self._global_history[business_id] = {
                            "name": business.get("name", ""),
                            "phone": business.get("phone", ""),
                            "first_scraped": datetime.now().isoformat(),
                            "keyword": "history_import",
                            "location": file_path.name,
                        }
                        imported += 1
            except Exception as e:
                self.log.error(f"Failed to import history file {file_path.name}: {e}")

        if imported > 0:
            self._save_global_history()
            self.log.info(f"Imported {imported} new businesses from selected output files")

        return imported, file_business_ids
    
    def _get_search_history_file(self, search_key: str) -> Path:
        """Get the history file path for a specific search."""
        return SEARCH_HISTORY_DIR / f"{search_key}.json"
    
    def _load_search_history(self, search_key: str) -> Set[str]:
        """Load history for a specific search query."""
        if search_key in self._search_histories:
            return self._search_histories[search_key]
        
        history_file = self._get_search_history_file(search_key)
        if history_file.exists():
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._search_histories[search_key] = set(data.get("business_ids", []))
            except Exception as e:
                self.log.error(f"Failed to load search history: {e}")
                self._search_histories[search_key] = set()
        else:
            self._search_histories[search_key] = set()
        
        return self._search_histories[search_key]
    
    def _save_search_history(self, search_key: str, keyword: str, location: str) -> None:
        """Save history for a specific search query."""
        history_file = self._get_search_history_file(search_key)
        try:
            data = {
                "keyword": keyword,
                "location": location,
                "last_updated": datetime.now().isoformat(),
                "total_scraped": len(self._search_histories.get(search_key, set())),
                "business_ids": list(self._search_histories.get(search_key, set()))
            }
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log.error(f"Failed to save search history: {e}")
    
    def is_duplicate(self, business: Dict, keyword: str = "", location: str = "") -> bool:
        """Check if a business has already been scraped.
        Uses rapidfuzz for powerful fuzzy deduplication when available.
        """
        business_id = self._get_business_id(business)
        if not business_id:
            return False
        
        # Exact global match
        if business_id in self._global_history:
            return True
        
        # Search specific exact
        if keyword and location:
            search_key = self._get_search_key(keyword, location)
            search_history = self._load_search_history(search_key)
            if business_id in search_history:
                return True
        
        # Advanced fuzzy dedup (very powerful for catching near-duplicates)
        if RAPIDFUZZ_AVAILABLE:
            name = str(business.get("name") or "").lower().strip()
            phone = str(business.get("phone") or "").strip()
            address = str(business.get("address") or "").lower().strip()[:80]
            
            for ex_id, ex in list(self._global_history.items())[-300:]:
                ex_name = str(ex.get("name") or "").lower().strip()
                ex_phone = str(ex.get("phone") or "").strip()
                ex_addr = str(ex.get("address") or "").lower().strip()[:80]
                
                if phone and phone == ex_phone:
                    return True
                if name and ex_name:
                    name_score = fuzz.ratio(name, ex_name)
                    addr_score = fuzz.ratio(address, ex_addr) if address and ex_addr else 0
                    if name_score >= 93 or (name_score >= 87 and addr_score >= 78):
                        return True
        return False
    
    def add_to_history(self, business: Dict, keyword: str = "", location: str = "") -> None:
        """Add a business to the history."""
        business_id = self._get_business_id(business)
        if not business_id:
            return
        
        # Add to global history with metadata
        self._global_history[business_id] = {
            "name": business.get("name", ""),
            "phone": business.get("phone", ""),
            "first_scraped": datetime.now().isoformat(),
            "keyword": keyword,
            "location": location,
        }
        
        # Add to search-specific history
        if keyword and location:
            search_key = self._get_search_key(keyword, location)
            if search_key not in self._search_histories:
                self._load_search_history(search_key)
            self._search_histories[search_key].add(business_id)
    
    def add_batch_to_history(self, businesses: List[Dict], keyword: str, location: str) -> None:
        """Add multiple businesses to history and save."""
        for business in businesses:
            self.add_to_history(business, keyword, location)
        
        # Save both histories
        self._save_global_history()
        if keyword and location:
            search_key = self._get_search_key(keyword, location)
            self._save_search_history(search_key, keyword, location)
        
        self.log.info(f"Added {len(businesses)} businesses to history")
    
    def filter_new_businesses(self, businesses: List[Dict], keyword: str, location: str) -> List[Dict]:
        """Filter out businesses that have already been scraped."""
        new_businesses = []
        duplicates = 0
        
        for business in businesses:
            if not self.is_duplicate(business, keyword, location):
                new_businesses.append(business)
            else:
                duplicates += 1
        
        if duplicates > 0:
            self.log.info(f"Filtered out {duplicates} duplicate businesses, {len(new_businesses)} new")
        
        return new_businesses
    
    def get_stats(self, keyword: str = "", location: str = "") -> Dict:
        """Get history statistics."""
        stats = {
            "global_total": len(self._global_history),
        }
        
        if keyword and location:
            search_key = self._get_search_key(keyword, location)
            search_history = self._load_search_history(search_key)
            stats["search_total"] = len(search_history)
            stats["search_key"] = search_key
        
        return stats
    
    def clear_search_history(self, keyword: str, location: str) -> int:
        """Clear history for a specific search query."""
        search_key = self._get_search_key(keyword, location)
        history_file = self._get_search_history_file(search_key)
        
        count = len(self._search_histories.get(search_key, set()))
        
        # Remove from memory
        if search_key in self._search_histories:
            del self._search_histories[search_key]
        
        # Remove file
        if history_file.exists():
            history_file.unlink()
        
        self.log.info(f"Cleared {count} businesses from search history: {keyword} in {location}")
        return count
    
    def clear_all_history(self) -> int:
        """Clear all history (global and search-specific)."""
        count = len(self._global_history)
        
        # Clear global
        self._global_history = {}
        self._save_global_history()
        
        # Clear all search histories
        self._search_histories = {}
        for f in SEARCH_HISTORY_DIR.glob("*.json"):
            f.unlink()
        
        self.log.info(f"Cleared all history: {count} businesses")
        return count
    
    def get_previously_scraped(self, keyword: str, location: str, limit: int = 100) -> List[Dict]:
        """Get list of previously scraped businesses for a search."""
        search_key = self._get_search_key(keyword, location)
        search_history = self._load_search_history(search_key)
        
        results = []
        for business_id in list(search_history)[:limit]:
            if business_id in self._global_history:
                results.append(self._global_history[business_id])
        
        return results


# Singleton instance
_history_instance: Optional[ScrapeHistory] = None


def get_history(logger: Optional[logging.Logger] = None) -> ScrapeHistory:
    """Get the singleton history instance."""
    global _history_instance
    if _history_instance is None:
        _history_instance = ScrapeHistory(logger)
    return _history_instance


def reset_history_instance() -> None:
    """Reset the singleton instance (useful for testing)."""
    global _history_instance
    _history_instance = None

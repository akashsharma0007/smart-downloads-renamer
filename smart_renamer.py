#!/usr/bin/env python3
"""
Smart Downloads Renamer
=======================
Monitors your Downloads folder and renames files to human-readable names.

How it works:
  1. Reads the source URL from the file's OS metadata (macOS xattr / Windows Zone.Identifier)
  2. Extracts meaningful context (site name, path segment, search query)
  3. Renames the file with a clean name + DDMMYYYY date suffix

Usage:
  python3 smart_renamer.py              # Suggest mode (show proposed names, don't rename)
  python3 smart_renamer.py --auto       # Auto mode (rename files automatically)
  python3 smart_renamer.py --scan       # One-time scan of Downloads folder
  python3 smart_renamer.py --watch      # Watch mode: continuously monitor for new files
  python3 smart_renamer.py --test       # Run on files inside this project folder (for testing)

Requirements: Python 3.7+ (no third-party packages needed)
"""

import os
import re
import sys
import json
import time
import shutil
import hashlib
import logging
import platform
import subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, unquote, parse_qs

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).parent / "config.json"

DEFAULT_CONFIG = {
    "downloads_folder": str(Path.home() / "Downloads"),
    "auto_rename": False,          # False = suggest only, True = rename automatically
    "watch_interval_seconds": 5,   # Polling interval when in --watch mode
    "log_file": str(Path(__file__).parent / "rename_log.json"),
    "supported_extensions": [
        ".jpg", ".jpeg", ".png", ".gif", ".webp",   # images
        ".pdf", ".docx", ".doc", ".txt", ".xlsx",   # documents
        ".zip", ".csv", ".mp4", ".mov"              # other common types
    ],
    "generic_name_patterns": [
        r"^image[-_]?\d+$",
        r"^img[-_]?\d+$",
        r"^photo[-_]?\d+$",
        r"^download[-_]?\d*$",
        r"^file[-_]?\d*$",
        r"^document[-_]?\d*$",
        r"^unnamed$",
        r"^untitled[-_]?\d*$",
        r"^[a-f0-9]{8,}$",        # hex hash names
        r"^\d{8,}$"               # pure number names
    ]
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Merge with defaults (so new keys are picked up)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
    else:
        cfg = DEFAULT_CONFIG.copy()
    # Expand ~ to the actual home directory (needed on Windows)
    cfg["downloads_folder"] = str(Path(cfg["downloads_folder"]).expanduser())
    if not Path(cfg["log_file"]).is_absolute():
        cfg["log_file"] = str(Path(__file__).parent / cfg["log_file"])
    return cfg


# ─── URL / Metadata Extraction ────────────────────────────────────────────────

SEARCH_ENGINE_HOSTS = {
    "google.com", "www.google.com",
    "bing.com", "www.bing.com",
    "duckduckgo.com", "www.duckduckgo.com",
    "yahoo.com", "search.yahoo.com",
    "yandex.com", "www.yandex.com",
}

SEARCH_QUERY_PARAMS = ["q", "query", "search_query", "text", "p"]

# Path segments that are "infrastructure" words and don't add meaningful context
SKIP_PATH_SEGMENTS = {
    "download", "downloads", "file", "files", "static", "assets",
    "media", "upload", "uploads", "tmp", "temp", "public",
    "api", "v1", "v2", "v3", "terminal", "portal", "app",
    "content", "data", "storage", "resources", "resource",
    "attachments", "docs", "get", "fetch",
    "serve", "output", "exports", "export",
    "user", "users", "lms", "d2l", "dropbox",
    "folder", "folders", "inbox", "sent", "shared",
    "web", "home", "index", "main", "default", "page",
}


def get_source_url_macos(filepath: Path) -> str | None:
    """Read the download source URL from macOS extended attributes."""
    try:
        import plistlib
        result = subprocess.run(
            ["xattr", "-p", "com.apple.metadata:kMDItemWhereFroms", str(filepath)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None
        # The output is hex-encoded plist; decode it
        hex_data = result.stdout.strip()
        raw = bytes.fromhex(hex_data)
        urls = plistlib.loads(raw)
        if isinstance(urls, list) and urls:
            return urls[0]  # First entry is the direct download URL
    except Exception:
        pass

    # Alternative: try mdls
    try:
        result = subprocess.run(
            ["mdls", "-name", "kMDItemWhereFroms", str(filepath)],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            match = re.search(r'"(https?://[^"]+)"', result.stdout)
            if match:
                return match.group(1)
    except Exception:
        pass

    return None


def get_source_url_windows(filepath: Path) -> str | None:
    """Read the download source URL from Windows Zone.Identifier ADS."""
    zone_path = str(filepath) + ":Zone.Identifier"
    try:
        with open(zone_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        match = re.search(r"ReferrerUrl=(.+)", content)
        if match:
            return match.group(1).strip()
        match = re.search(r"HostUrl=(.+)", content)
        if match:
            url = match.group(1).strip()
            if url and url != "about:internet":
                return url
    except Exception:
        pass
    return None


def get_source_url_chrome(filepath: Path) -> str | None:
    """
    Look up a file's download URL from Chrome's history database.
    Chrome stores every download with its full source URL — works even when
    Zone.Identifier is missing (e.g. web app downloads from Volante portal).
    """
    import sqlite3, shutil, tempfile

    chrome_history_paths = [
        # Windows — Chrome
        Path.home() / "AppData/Local/Google/Chrome/User Data/Default/History",
        # Windows — Edge
        Path.home() / "AppData/Local/Microsoft/Edge/User Data/Default/History",
        # macOS — Chrome
        Path.home() / "Library/Application Support/Google/Chrome/Default/History",
    ]

    filename = filepath.name

    for history_path in chrome_history_paths:
        if not history_path.exists():
            continue
        # Chrome locks the DB while open — copy to a temp file first
        try:
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(history_path), tmp_path)

            conn = sqlite3.connect(tmp_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Try downloads_url_chains first (has the real download URL)
            cur.execute("""
                SELECT duc.url, d.tab_url, d.referrer
                FROM downloads d
                JOIN downloads_url_chains duc ON d.id = duc.id
                WHERE d.target_path LIKE ?
                ORDER BY d.start_time DESC
                LIMIT 1
            """, (f"%{filename}%",))
            row = cur.fetchone()

            if not row:
                # Fallback to tab_url from downloads table
                cur.execute("""
                    SELECT tab_url, referrer
                    FROM downloads
                    WHERE target_path LIKE ?
                    ORDER BY start_time DESC
                    LIMIT 1
                """, (f"%{filename}%",))
                row = cur.fetchone()

            conn.close()
            Path(tmp_path).unlink(missing_ok=True)

            if row:
                # Prefer the direct download URL, fall back to tab URL
                url = row[0] if row[0] else (row[1] if len(row) > 1 else None)
                if url and url.startswith("http"):
                    return url

        except Exception:
            pass

    return None


def get_source_url(filepath: Path) -> str | None:
    """
    Get the source URL of a downloaded file.
    Tries Zone.Identifier first, then Chrome/Edge history as fallback.
    """
    system = platform.system()

    # Try OS-level metadata first (fastest)
    if system == "Darwin":
        url = get_source_url_macos(filepath)
    elif system == "Windows":
        url = get_source_url_windows(filepath)
    else:
        url = None

    # Fallback: look up Chrome/Edge download history
    if not url:
        url = get_source_url_chrome(filepath)

    return url


# ─── Name Extraction Logic ────────────────────────────────────────────────────

def extract_search_query(url: str) -> str | None:
    """Extract a search query from a search engine URL."""
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().lstrip("www.")
        if any(se in host for se in ["google", "bing", "duckduckgo", "yahoo", "yandex"]):
            params = parse_qs(parsed.query)
            for param in SEARCH_QUERY_PARAMS:
                if param in params:
                    return params[param][0]
    except Exception:
        pass
    return None


def extract_context_from_url(url: str) -> tuple[str | None, str | None]:
    """
    Returns (url_filename_stem, best_context_segment) from a URL.
    e.g. "volantecloud.com/providencehealth/terminal/payroll.pdf"
         → ("payroll", "providencehealth")

    Skips infrastructure path segments (terminal, api, static, etc.)
    and prefers the most organisation/project-like segment.
    """
    if not url:
        return None, None

    try:
        parsed = urlparse(url)
        path_parts = [unquote(p).strip() for p in parsed.path.split("/") if p.strip()]

        # ── URL filename stem ─────────────────────────────────────────────────
        # Return the RAW stem (hyphens/underscores preserved) so callers can
        # run generic-name checks on it before pretty-printing.
        url_filename = None
        if path_parts:
            last = path_parts[-1]
            stem = re.sub(r"\.[a-zA-Z0-9]+$", "", last).strip()
            if stem and not re.match(r"^\d{5,}$", stem):  # skip pure long numbers
                url_filename = stem  # keep original separators for generic check

        # ── Best context segment ──────────────────────────────────────────────
        # Walk path from second-to-last backwards, skip infrastructure words
        context = None
        for part in reversed(path_parts[:-1]):
            clean = part.replace("-", " ").replace("_", " ").strip().lower()
            # Skip: too short, pure digits, version strings, infrastructure words,
            #       or ID-like tokens (e.g. "in_001", "ch_abc123", "uuid-style")
            orig_lower = part.strip().lower()   # unchanged for pattern matching
            if (len(clean) < 3
                    or re.match(r"^v?\d+(\.\d+)*$", clean)
                    or re.match(r"^[a-z]{1,4}[-_]\w+$", orig_lower)  # short-prefix IDs
                    or re.match(r"^[a-f0-9-]{20,}$", orig_lower)      # UUID / hash
                    or clean in SKIP_PATH_SEGMENTS):
                continue
            context = part.replace("-", " ").replace("_", " ").strip()
            break

        # ── Fallback: use domain name (brand, not subdomain) ─────────────────
        if not context:
            host = parsed.netloc.lower()
            domain_parts = host.split(".")
            # Skip generic subdomains to get to the brand name (SLD)
            generic_subdomains = {
                "www", "cdn", "static", "img", "dl", "web", "mail",
                "app", "api", "m", "images", "image", "media", "assets",
                "files", "download", "uploads", "s3", "storage",
            }
            # Find first non-generic, non-TLD part from left
            tlds = {"com", "org", "net", "edu", "gov", "io", "co", "ca",
                    "uk", "au", "de", "fr", "in", "sg", "enligne", "gc"}
            for part in domain_parts:
                if part and part not in generic_subdomains and part not in tlds and len(part) >= 3:
                    context = part
                    break

        return url_filename, context

    except Exception:
        return None, None


def title_case_smart(name: str) -> str:
    """
    Applies smart title casing. Handles names like 'brad pitt' → 'Brad Pitt',
    'providencehealth' → 'Providencehealth', 'payroll' → 'Payroll'.
    Preserves ALL-CAPS words (acronyms).
    """
    words = name.replace("-", " ").replace("_", " ").split()
    result = []
    for word in words:
        if word.isupper() and len(word) > 1:
            result.append(word)  # preserve acronyms
        else:
            result.append(word.capitalize())
    return " ".join(result)


def is_generic_name(name: str, patterns: list[str]) -> bool:
    """Check if a filename stem is generic/meaningless."""
    stem = Path(name).stem.lower().strip()
    for pattern in patterns:
        if re.match(pattern, stem, re.IGNORECASE):
            return True
    return False


def strip_chrome_duplicate_suffix(name: str) -> str:
    """Remove Chrome's duplicate number suffix: 'file (3)' → 'file'"""
    return re.sub(r"\s*\(\d+\)\s*$", "", name).strip()


def build_new_filename(
    original_path: Path,
    source_url: str | None,
    download_date: datetime,
    generic_patterns: list[str]
) -> str | None:
    """
    Core logic: produce a new filename (with extension) or None if no rename needed.
    """
    ext = original_path.suffix.lower()
    stem = strip_chrome_duplicate_suffix(original_path.stem)
    date_str = download_date.strftime("%d%m%Y")

    # Already has a date suffix → skip
    if re.search(r"-\d{8}(-\d+)?$", stem):
        return None

    search_query = None
    file_context = None
    site_context = None

    if source_url:
        search_query = extract_search_query(source_url)
        if not search_query:
            file_context, site_context = extract_context_from_url(source_url)

    if search_query:
        # ── Case 1: Came from a search engine → use the search query ──────────
        clean_query = title_case_smart(search_query)
        new_stem = f"{clean_query}-{date_str}"

    elif is_generic_name(stem, generic_patterns):
        # ── Case 2: Generic filename (image-1, download3, etc.) ───────────────
        # file_context is the raw URL filename stem; check if it's also generic
        url_file_generic = (
            file_context is None
            or is_generic_name(file_context + ext, generic_patterns)
        )
        if not url_file_generic:
            # URL filename is meaningful (e.g. "q4-summary") — use it
            clean_file = title_case_smart(
                file_context.replace("+", " ").replace("-", " ").replace("_", " ")
            )
            if site_context:
                clean_site = title_case_smart(site_context)
                new_stem = f"{clean_file}-{clean_site}-{date_str}"
            else:
                new_stem = f"{clean_file}-{date_str}"
        elif site_context:
            # URL filename is also generic — just use path context
            clean_context = title_case_smart(site_context)
            new_stem = f"{clean_context}-{date_str}"
        else:
            # No URL info — can't improve the name meaningfully
            return None

    else:
        # ── Case 3: Meaningful original name — always keep it, just add date ──
        # Never replace a good descriptive name with a URL page name.
        # Only the URL has a richer filename (e.g. from a direct file link),
        # AND the original stem looks like a plain/single-word name, do we swap.
        original_is_rich = " " in stem or len(stem.split("-")) > 2 or len(stem) > 20
        url_file_is_better = (
            not original_is_rich
            and file_context
            and file_context.lower() not in SKIP_PATH_SEGMENTS
            and not is_generic_name(file_context + ext, generic_patterns)
            and file_context.lower().replace("-", "").replace("_", "") !=
                stem.lower().replace("-", "").replace("_", "")
        )
        base_stem = file_context if url_file_is_better else stem
        clean_stem = title_case_smart(
            base_stem.replace("+", " ").replace("-", " ").replace("_", " ")
        )
        if site_context and site_context.lower() not in clean_stem.lower():
            clean_site = title_case_smart(site_context)
            new_stem = f"{clean_stem}-{clean_site}-{date_str}"
        else:
            new_stem = f"{clean_stem}-{date_str}"

    # Sanitize filename (remove chars not allowed in filenames)
    new_stem = re.sub(r'[<>:"/\\|?*]', "", new_stem).strip()

    return new_stem + ext


def safe_new_path(dest_folder: Path, new_filename: str) -> Path:
    """Return a non-colliding path, appending -1, -2 etc. if needed."""
    candidate = dest_folder / new_filename
    if not candidate.exists():
        return candidate
    stem = Path(new_filename).stem
    ext = Path(new_filename).suffix
    counter = 1
    while True:
        candidate = dest_folder / f"{stem}-{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


# ─── Rename Log ───────────────────────────────────────────────────────────────

LOG_RETENTION_DAYS = 7


def get_daily_log_path(log_path: str) -> Path:
    """Return today's log file path, e.g. rename_log_2026-03-28.json"""
    base = Path(log_path)
    today = datetime.now().strftime("%Y-%m-%d")
    return base.parent / f"{base.stem}_{today}{base.suffix}"


def purge_old_logs(log_path: str):
    """Delete log files older than LOG_RETENTION_DAYS."""
    base = Path(log_path)
    cutoff = datetime.now().timestamp() - (LOG_RETENTION_DAYS * 86400)
    for f in base.parent.glob(f"{base.stem}_????-??-??{base.suffix}"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logging.info(f"  🗑  Removed old log: {f.name}")
        except Exception:
            pass


def load_log(log_path: str) -> list:
    p = get_daily_log_path(log_path)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return []


def redact_url(url: str) -> str:
    """
    Redact sensitive tokens from URLs before saving to log.
    Removes: UUIDs, session IDs, auth tokens, query parameters.
    Keeps: domain and path structure only.
    e.g. https://site.com/org/terminal/file?id=abc-123-uuid
      →  https://site.com/org/terminal/file?[redacted]
    """
    if not url or not url.startswith("http"):
        return url
    try:
        parsed = urlparse(url)
        # Redact all query string parameters
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean += "?[redacted]"
        # Redact UUID-like path segments
        clean = re.sub(
            r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            "/[id]", clean, flags=re.IGNORECASE
        )
        # Redact long hex/alphanumeric tokens in path
        clean = re.sub(r"/[A-Za-z0-9_\-]{32,}", "/[token]", clean)
        return clean
    except Exception:
        return "[url redacted]"


def append_log(log_path: str, entry: dict):
    purge_old_logs(log_path)
    daily_path = get_daily_log_path(log_path)
    # Redact sensitive info from URL before logging
    if "source_url" in entry:
        entry["source_url"] = redact_url(entry["source_url"])
    entries = load_log(log_path)
    entries.append(entry)
    with open(daily_path, "w") as f:
        json.dump(entries, f, indent=2)


# ─── Core Processing ──────────────────────────────────────────────────────────

def process_file(filepath: Path, cfg: dict, auto_rename: bool, debug: bool = False) -> dict | None:
    """
    Process a single file. Returns a result dict or None if nothing to do.
    """
    if not filepath.is_file():
        return None

    ext = filepath.suffix.lower()

    # Look up source URL first — files with no extension (e.g. Volante downloads)
    # are still valid if Chrome recorded a URL for them
    source_url = get_source_url(filepath)

    if ext not in cfg["supported_extensions"]:
        # Allow extensionless files only if we found a URL in browser history
        if not (ext == "" and source_url):
            if debug:
                print(f"  SKIP  {filepath.name}  (extension '{ext}' not supported)")
            return None

    if debug:
        print(f"  FILE  {filepath.name}")
        print(f"        URL metadata: {source_url or '(none found)'}")

    download_date = datetime.fromtimestamp(filepath.stat().st_mtime)
    new_filename = build_new_filename(filepath, source_url, download_date, cfg["generic_name_patterns"])

    if not new_filename:
        if debug:
            print(f"        → no rename needed (name already good or no URL context)")
        return None

    new_path = safe_new_path(filepath.parent, new_filename)

    result = {
        "original": filepath.name,
        "suggested": new_filename,
        "source_url": source_url or "unknown",
        "date": download_date.strftime("%Y-%m-%d %H:%M"),
        "status": "pending"
    }

    if auto_rename:
        try:
            filepath.rename(new_path)
            result["status"] = "renamed"
            logging.info(f"  ✓  {filepath.name}  →  {new_filename}")
        except Exception as e:
            result["status"] = f"error: {e}"
            logging.error(f"  ✗  Failed to rename {filepath.name}: {e}")
    else:
        logging.info(f"  →  {filepath.name}  ⟶  {new_filename}")
        result["status"] = "suggested"

    return result


def scan_folder(folder: Path, cfg: dict, auto_rename: bool, debug: bool = False) -> list[dict]:
    """Scan a folder and process all matching files."""
    results = []
    files = sorted(folder.iterdir(), key=lambda f: f.stat().st_mtime if f.is_file() else 0)
    for f in files:
        result = process_file(f, cfg, auto_rename, debug=debug)
        if result:
            results.append(result)
    return results


def watch_folder(folder: Path, cfg: dict, auto_rename: bool):
    """Poll the Downloads folder every N seconds for new files."""
    interval = cfg.get("watch_interval_seconds", 5)
    seen = set(f.name for f in folder.iterdir() if f.is_file())
    logging.info(f"Watching {folder} (every {interval}s)… Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(interval)
            current = set(f.name for f in folder.iterdir() if f.is_file())
            new_files = current - seen
            for name in new_files:
                filepath = folder / name
                # Wait briefly so the file is fully written
                time.sleep(1)
                result = process_file(filepath, cfg, auto_rename)
                if result:
                    append_log(cfg["log_file"], result)
            seen = current
    except KeyboardInterrupt:
        logging.info("Stopped watching.")


# ─── Pretty Output ────────────────────────────────────────────────────────────

def print_results(results: list[dict], auto_rename: bool):
    if not results:
        print("\n  No files need renaming.\n")
        return

    mode = "RENAMED" if auto_rename else "SUGGESTED RENAMES"
    print(f"\n{'─'*60}")
    print(f"  {mode} ({len(results)} file(s))")
    print(f"{'─'*60}")
    for r in results:
        arrow = "✓" if r["status"] == "renamed" else "→"
        print(f"  {arrow}  {r['original']}")
        print(f"       ⟶  {r['suggested']}")
        if r["source_url"] != "unknown":
            print(f"       URL: {r['source_url'][:80]}{'…' if len(r['source_url']) > 80 else ''}")
        print()
    print(f"{'─'*60}\n")


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s"
    )

    cfg = load_config()
    args = set(sys.argv[1:])

    auto_rename = "--auto" in args or cfg.get("auto_rename", False)
    watch_mode  = "--watch" in args
    test_mode   = "--test" in args
    debug_mode  = "--debug" in args

    # Manual rename: python smart_renamer.py --rename "filename.pdf" --url "https://..."
    if "--rename" in args:
        arg_list = sys.argv[1:]
        try:
            fname = arg_list[arg_list.index("--rename") + 1]
            url   = arg_list[arg_list.index("--url") + 1] if "--url" in arg_list else None
        except (IndexError, ValueError):
            print("Usage: python smart_renamer.py --rename \"filename.pdf\" --url \"https://...\"")
            sys.exit(1)

        folder = Path(cfg["downloads_folder"])
        filepath = folder / fname
        if not filepath.exists():
            print(f"File not found: {filepath}")
            sys.exit(1)

        download_date = datetime.fromtimestamp(filepath.stat().st_mtime)
        new_filename = build_new_filename(filepath, url, download_date, cfg["generic_name_patterns"])

        if not new_filename:
            print(f"Could not generate a better name for: {fname}")
            sys.exit(0)

        new_path = safe_new_path(folder, new_filename)
        print(f"\n  {fname}  →  {new_filename}")

        confirm = input("  Rename? [y/N]: ").strip().lower()
        if confirm == "y":
            filepath.rename(new_path)
            print(f"  ✓ Renamed successfully.")
            append_log(cfg["log_file"], {
                "original": fname, "suggested": new_filename,
                "source_url": url or "manual", "status": "renamed",
                "date": download_date.strftime("%Y-%m-%d %H:%M")
            })
        else:
            print("  Skipped.")
        sys.exit(0)

    if test_mode:
        # Run on the project folder itself (for testing with sample files)
        folder = Path(__file__).parent
        print(f"\nTest mode: scanning {folder}")
    else:
        folder = Path(cfg["downloads_folder"])

    if not folder.exists():
        print(f"Folder not found: {folder}")
        print("Edit config.json to set the correct downloads_folder path.")
        sys.exit(1)

    if watch_mode:
        watch_folder(folder, cfg, auto_rename)
    else:
        print(f"\nScanning: {folder}")
        print(f"Mode:     {'AUTO RENAME' if auto_rename else 'SUGGEST ONLY (pass --auto to rename)'}")
        if debug_mode:
            print(f"\nDEBUG: scanning every file...\n")
        results = scan_folder(folder, cfg, auto_rename, debug=debug_mode)
        print_results(results, auto_rename)

        if results:
            # Save log
            for r in results:
                append_log(cfg["log_file"], r)
            daily_log = get_daily_log_path(cfg["log_file"])
            print(f"  Log saved to: {daily_log.name}\n")


if __name__ == "__main__":
    main()

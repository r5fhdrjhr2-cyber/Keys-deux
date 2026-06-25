"""
PoliteClient — rate-limited, session-persistent, stealth-patched Playwright client.

ALL browser access in keys_records goes through this module.
No adapter may navigate or fetch except by calling PoliteClient methods.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib import robotparser
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext,
    Page,
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class CircuitOpenError(Exception):
    """Raised when the circuit breaker for a domain is open."""


class BlockedError(Exception):
    """Raised when the server returns a blocking status (429, 503, etc.)."""


class CaptchaError(Exception):
    """Raised when a CAPTCHA challenge is detected."""


class ManualRetrievalRequired(Exception):
    """Raised when an adapter cannot automate retrieval and human intervention is needed."""

    def __init__(self, source: str, reason: str, contact: str, url: Optional[str] = None):
        self.source = source
        self.reason = reason
        self.contact = contact
        self.url = url
        super().__init__(f"ManualRetrievalRequired: {source} — {reason}")


# ---------------------------------------------------------------------------
# Stealth init script
# ---------------------------------------------------------------------------

_STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
if (!window.chrome) window.chrome = {runtime: {}};
const _getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
  if (p === 37445) return 'Intel Inc.';
  if (p === 37446) return 'Intel(R) Iris(TM) Plus Graphics OpenGL Engine';
  return _getParam.call(this, p);
};
"""


# ---------------------------------------------------------------------------
# Per-domain state
# ---------------------------------------------------------------------------

def _find_chromium_executable() -> Optional[str]:
    """
    Locate an installed Chromium under PLAYWRIGHT_BROWSERS_PATH.

    Playwright's bundled launch expects one exact build revision; managed
    environments often ship a different revision at a fixed path. When the
    default launch can't find its pinned build, we fall back to whatever
    chromium build is actually present so retrieval still runs.
    """
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if not root or not os.path.isdir(root):
        return None
    candidates = []
    for entry in sorted(os.listdir(root)):
        if not entry.startswith("chromium"):
            continue
        for rel in ("chrome-linux/chrome", "chrome-linux/headless_shell",
                    "chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
            exe = os.path.join(root, entry, rel)
            if os.path.exists(exe):
                candidates.append(exe)
    # Prefer a full chrome build over a headless shell.
    candidates.sort(key=lambda p: ("headless_shell" in p, p))
    return candidates[0] if candidates else None


@dataclass
class DomainState:
    request_times: deque = field(default_factory=lambda: deque(maxlen=60))
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_open_until: float = 0.0
    action_count: int = 0
    next_long_rest_at: int = field(default_factory=lambda: random.randint(8, 12))
    robots_checked: bool = False
    robots_allowed: bool = True
    robots_restricted: bool = False
    last_request_at: float = 0.0


# ---------------------------------------------------------------------------
# PoliteClient
# ---------------------------------------------------------------------------

class PoliteClient:
    """
    Context manager that owns one Playwright browser and one BrowserContext per domain.
    Enforces rate limits, circuit breakers, stealth patching, and audit logging.
    """

    def __init__(self, config: dict):
        self.config = config
        pacing = config.get("pacing", {})
        self.nav_delay_min: float = pacing.get("nav_delay_min", 4.0)
        self.nav_delay_max: float = pacing.get("nav_delay_max", 9.0)
        self.dwell_min: float = pacing.get("dwell_min", 2.0)
        self.dwell_max: float = pacing.get("dwell_max", 5.0)
        self.key_delay_min: float = pacing.get("key_delay_min", 0.08)
        self.key_delay_max: float = pacing.get("key_delay_max", 0.22)
        self.rate_cap: int = pacing.get("rate_cap_per_minute", 6)
        self.long_rest_min: float = pacing.get("long_rest_min", 20.0)
        self.long_rest_max: float = pacing.get("long_rest_max", 45.0)
        self.long_rest_every_min: int = pacing.get("long_rest_every_min", 8)
        self.long_rest_every_max: int = pacing.get("long_rest_every_max", 12)

        backoff = config.get("backoff", {})
        self.backoff_initial: float = backoff.get("initial_seconds", 120.0)
        self.backoff_multiplier: float = backoff.get("multiplier", 2.0)
        self.backoff_max: float = backoff.get("max_seconds", 480.0)
        self.circuit_break_after: int = backoff.get("circuit_break_after", 3)

        cache_cfg = config.get("cache", {})
        self.cache_root = Path(cache_cfg.get("root", "./cache")).resolve()
        self.freshness_days: int = cache_cfg.get("freshness_days", 7)

        sessions_cfg = config.get("sessions", {})
        self.sessions_root = Path(sessions_cfg.get("root", "./sessions")).resolve()

        robots_cfg = config.get("robots", {})
        self.restricted_domains: list = robots_cfg.get("restricted_domains", [])
        self.restricted_multiplier: float = robots_cfg.get("restricted_pacing_multiplier", 2.0)

        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.sessions_root.mkdir(parents=True, exist_ok=True)

        self._audit_path = self.cache_root / "audit.jsonl"

        self._domain_states: dict = {}
        self._contexts: dict = {}
        self._pages: dict = {}

        self._playwright = None
        self._browser = None
        self._xvfb_proc = None
        self._user_agent: Optional[str] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "PoliteClient":
        self._playwright = sync_playwright().start()

        headless_fallback = True
        # Build a launch-kwargs helper so the executable_path fallback applies
        # to every launch attempt below.
        exe = _find_chromium_executable()

        def _launch(**kw):
            if exe:
                kw.setdefault("executable_path", exe)
            return self._playwright.chromium.launch(**kw)

        if shutil.which("Xvfb") is not None:
            try:
                self._xvfb_proc = subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                os.environ["DISPLAY"] = ":99"
                time.sleep(0.5)
                self._browser = _launch(
                    headless=False,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                logger.info("Launched headful Chromium under Xvfb :99")
                headless_fallback = False
            except Exception as exc:
                logger.warning("Xvfb launch failed (%s), falling back to headless", exc)
                if self._xvfb_proc:
                    self._xvfb_proc.terminate()
                    self._xvfb_proc = None

        if headless_fallback:
            self._browser = _launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            logger.info("Launched headless Chromium%s",
                        f" ({exe})" if exe else " (no Xvfb found)")

        # Read the actual User-Agent from a temp context
        tmp_ctx = self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        tmp_page = tmp_ctx.new_page()
        self._user_agent = tmp_page.evaluate("navigator.userAgent")
        tmp_page.close()
        tmp_ctx.close()
        logger.info("Detected User-Agent: %s", self._user_agent)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Save all sessions
        for domain, ctx in self._contexts.items():
            self._save_session(domain, ctx)

        for ctx in self._contexts.values():
            try:
                ctx.close()
            except Exception:
                pass

        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

        if self._xvfb_proc:
            try:
                self._xvfb_proc.terminate()
            except Exception:
                pass

        return False

    # ------------------------------------------------------------------
    # Domain / context management
    # ------------------------------------------------------------------

    def _domain_key(self, url: str) -> str:
        return urlparse(url).netloc.lower()

    def _safe_domain(self, domain: str) -> str:
        return re.sub(r"[^a-z0-9]", "_", domain)

    def _session_path(self, domain: str) -> Path:
        return self.sessions_root / f"{self._safe_domain(domain)}.json"

    def _get_state(self, domain: str) -> DomainState:
        if domain not in self._domain_states:
            state = DomainState()
            state.robots_restricted = any(r in domain for r in self.restricted_domains)
            self._domain_states[domain] = state
        return self._domain_states[domain]

    def _get_context(self, domain: str) -> BrowserContext:
        if domain not in self._contexts:
            session_file = self._session_path(domain)
            kwargs: dict = dict(
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York",
                user_agent=self._user_agent,
            )
            if session_file.exists():
                try:
                    with open(session_file) as fh:
                        storage = json.load(fh)
                    kwargs["storage_state"] = storage
                    logger.debug("Loaded session for %s", domain)
                except Exception as exc:
                    logger.warning("Failed to load session for %s: %s", domain, exc)

            ctx = self._browser.new_context(**kwargs)
            ctx.add_init_script(_STEALTH_SCRIPT)
            self._contexts[domain] = ctx
        return self._contexts[domain]

    def _save_session(self, domain: str, ctx: BrowserContext):
        session_file = self._session_path(domain)
        try:
            state = ctx.storage_state()
            session_file.parent.mkdir(parents=True, exist_ok=True)
            with open(session_file, "w") as fh:
                json.dump(state, fh)
            logger.debug("Saved session for %s", domain)
        except Exception as exc:
            logger.warning("Failed to save session for %s: %s", domain, exc)

    def save_domain_session(self, domain: str):
        """Explicitly save session for a domain (e.g., after accepting a disclaimer)."""
        if domain in self._contexts:
            self._save_session(domain, self._contexts[domain])

    def get_page(self, url: str = "") -> Page:
        """Get current page for a domain (for use by adapters after navigate())."""
        if url:
            domain = self._domain_key(url)
        elif self._pages:
            domain = list(self._pages.keys())[-1]
        else:
            raise RuntimeError("No pages open")
        if domain not in self._pages or self._pages[domain].is_closed():
            ctx = self._get_context(domain)
            self._pages[domain] = ctx.new_page()
        return self._pages[domain]

    # ------------------------------------------------------------------
    # Robots.txt
    # ------------------------------------------------------------------

    def _check_robots(self, domain: str, url: str):
        state = self._get_state(domain)
        if state.robots_checked:
            return
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
            state.robots_allowed = rp.can_fetch("*", url)
            if not state.robots_allowed:
                logger.warning("robots.txt disallows access to %s", url)
        except Exception as exc:
            logger.debug("Could not parse robots.txt for %s: %s", domain, exc)
            state.robots_allowed = True
        state.robots_checked = True

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _prune_request_times(self, state: DomainState):
        now = time.monotonic()
        cutoff = now - 60.0
        times = [t for t in state.request_times if t > cutoff]
        state.request_times.clear()
        for t in times:
            state.request_times.append(t)

    def _wait_for_rate_cap(self, state: DomainState, restricted: bool):
        while True:
            self._prune_request_times(state)
            if len(state.request_times) < self.rate_cap:
                break
            oldest = state.request_times[0]
            wait = (oldest + 60.0) - time.monotonic() + random.uniform(0.5, 2.0)
            if wait > 0:
                logger.info("Rate cap reached; sleeping %.1fs", wait)
                time.sleep(wait)

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _check_circuit(self, domain: str):
        state = self._get_state(domain)
        if not state.circuit_open:
            return
        now = time.monotonic()
        if now >= state.circuit_open_until:
            state.circuit_open = False
            state.consecutive_failures = 0
            logger.info("Circuit breaker for %s reset", domain)
        else:
            remaining = state.circuit_open_until - now
            raise CircuitOpenError(
                f"Circuit open for {domain}; retry in {remaining:.0f}s"
            )

    def _record_failure(self, domain: str, reason: str):
        state = self._get_state(domain)
        state.consecutive_failures += 1
        logger.warning(
            "Failure #%d for %s: %s", state.consecutive_failures, domain, reason
        )
        if state.consecutive_failures >= self.circuit_break_after:
            failures = state.consecutive_failures
            backoff = min(
                self.backoff_initial
                * (self.backoff_multiplier ** (failures - self.circuit_break_after)),
                self.backoff_max,
            )
            jitter = random.uniform(0.0, backoff * 0.2)
            total = backoff + jitter
            state.circuit_open = True
            state.circuit_open_until = time.monotonic() + total
            logger.warning("Circuit breaker OPEN for %s (%.0fs)", domain, total)

    def _record_success(self, domain: str):
        state = self._get_state(domain)
        state.consecutive_failures = 0
        state.action_count += 1
        state.request_times.append(time.monotonic())
        state.last_request_at = time.monotonic()

    # ------------------------------------------------------------------
    # CAPTCHA detection
    # ------------------------------------------------------------------

    def _detect_captcha(self, page: Page) -> bool:
        try:
            selectors = [
                "iframe[src*='recaptcha']",
                "iframe[src*='hcaptcha']",
                ".g-recaptcha",
                "#captcha",
                "div[class*='captcha']",
            ]
            for sel in selectors:
                if page.query_selector(sel):
                    return True
            title = page.title().lower()
            for kw in ("captcha", "robot", "verify you are human"):
                if kw in title:
                    return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _audit(self, entry: dict):
        try:
            with open(self._audit_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.debug("Audit write failed: %s", exc)

    # ------------------------------------------------------------------
    # navigate()
    # ------------------------------------------------------------------

    def navigate(self, url: str, wait_until: str = "domcontentloaded") -> Page:
        domain = self._domain_key(url)
        state = self._get_state(domain)

        self._check_circuit(domain)
        self._check_robots(domain, url)
        self._wait_for_rate_cap(state, state.robots_restricted)

        delay_used = 0.0
        if state.action_count > 0:
            base_delay = random.uniform(self.nav_delay_min, self.nav_delay_max)
            if state.robots_restricted:
                base_delay *= self.restricted_multiplier
                logger.info(
                    "Restricted domain %s: pacing multiplier %.1f applied",
                    domain,
                    self.restricted_multiplier,
                )
            delay_used = base_delay
            logger.debug("Nav delay %.2fs for %s", delay_used, domain)
            time.sleep(delay_used)

        if state.action_count > 0 and state.action_count >= state.next_long_rest_at:
            rest = random.uniform(self.long_rest_min, self.long_rest_max)
            state.next_long_rest_at = state.action_count + random.randint(
                self.long_rest_every_min, self.long_rest_every_max
            )
            logger.info("Long rest %.0fs (action_count=%d)", rest, state.action_count)
            time.sleep(rest)

        ctx = self._get_context(domain)

        if domain not in self._pages or self._pages[domain].is_closed():
            self._pages[domain] = ctx.new_page()
        page = self._pages[domain]

        resp = None
        status = None
        try:
            resp = page.goto(url, wait_until=wait_until, timeout=60000)
            if resp:
                status = resp.status
        except PlaywrightTimeoutError as exc:
            self._record_failure(domain, f"timeout: {exc}")
            self._audit(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "domain": domain,
                    "action": "navigate",
                    "url": url,
                    "status": None,
                    "delay_used": delay_used,
                    "cache_hit": False,
                    "robots_restricted": state.robots_restricted,
                    "error": "timeout",
                }
            )
            raise

        if status in (429, 503):
            self._record_failure(domain, f"HTTP {status}")
            self._audit(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "domain": domain,
                    "action": "navigate",
                    "url": url,
                    "status": status,
                    "delay_used": delay_used,
                    "cache_hit": False,
                    "robots_restricted": state.robots_restricted,
                    "error": f"blocked_{status}",
                }
            )
            raise BlockedError(f"HTTP {status} from {domain}")

        if self._detect_captcha(page):
            self._record_failure(domain, "captcha detected")
            self._audit(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "domain": domain,
                    "action": "navigate",
                    "url": url,
                    "status": status,
                    "delay_used": delay_used,
                    "cache_hit": False,
                    "robots_restricted": state.robots_restricted,
                    "error": "captcha",
                }
            )
            raise CaptchaError(f"CAPTCHA detected on {url}")

        self._record_success(domain)

        self._audit(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "domain": domain,
                "action": "navigate",
                "url": url,
                "status": status,
                "delay_used": delay_used,
                "cache_hit": False,
                "robots_restricted": state.robots_restricted,
            }
        )

        dwell = random.uniform(self.dwell_min, self.dwell_max)
        time.sleep(dwell)

        return page

    # ------------------------------------------------------------------
    # type_text() and click()
    # ------------------------------------------------------------------

    def type_text(self, locator, text: str):
        """Type text character by character with human-like delays."""
        for char in text:
            locator.press_sequentially(char)
            time.sleep(random.uniform(self.key_delay_min, self.key_delay_max))

    def click(self, locator):
        """Click with a pre-click pause."""
        time.sleep(random.uniform(0.3, 1.2))
        locator.click()

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _safe_cache_key(self, key: str) -> str:
        return re.sub(r"[^a-z0-9]", "_", key.lower().strip())

    def save_snapshot(self, source: str, query: str, content: str, ext: str = "html") -> Path:
        date_iso = datetime.now(timezone.utc).date().isoformat()
        safe_q = self._safe_cache_key(query)
        dest = self.cache_root / source / date_iso / f"{safe_q}.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        logger.debug("Snapshot saved: %s", dest)
        return dest

    def load_snapshot(self, source: str, query: str, ext: str = "html") -> Optional[str]:
        safe_q = self._safe_cache_key(query)
        for day_offset in range(self.freshness_days):
            check_date = (
                datetime.now(timezone.utc).date() - timedelta(days=day_offset)
            ).isoformat()
            path = self.cache_root / source / check_date / f"{safe_q}.{ext}"
            if path.exists():
                logger.debug("Cache hit: %s", path)
                return path.read_text(encoding="utf-8")
        return None

    def download_file(self, url: str, dest_path: Path) -> Path:
        """Download a file using the Playwright API request context for the domain."""
        domain = self._domain_key(url)
        ctx = self._get_context(domain)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = ctx.request.get(url)
            dest_path.write_bytes(response.body())
            logger.debug("Downloaded %s -> %s", url, dest_path)
        except Exception as exc:
            logger.warning("download_file failed for %s: %s", url, exc)
            raise
        return dest_path

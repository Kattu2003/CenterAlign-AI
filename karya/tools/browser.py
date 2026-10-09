"""Playwright browser session and the browser_* tools (plan §11.6).

One BrowserContext per task session. `context.route("**/*", gateway.on_route)`
is registered before any navigation, so every request the page makes passes
through the gateway. After every page-changing action the tool waits for the
page, takes a ref-labelled text snapshot (snapshot.js) and a screenshot for
evidence. The model sees text, never pixels (DECISIONS.md D2).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin, urlsplit

from pydantic import Field

from karya.connectors.models import normalize_origin
from karya.runtime.models import Observation
from karya.security.injection import scan_snapshot
from karya.tools.base import Tool, ToolArgs

if TYPE_CHECKING:
    from karya.tools.base import ToolSession

SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text(encoding="utf-8")
PAGE_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 15_000
GET_RETRY_BACKOFF_S = (1.0, 3.0)
BLOCKING_EVENTS = ("denied", "blocked_host", "read_only_block", "duplicate_prevented")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return cleaned or "download.bin"


def parse_window(value: str) -> tuple[int, int, int, int] | None:
    """KARYA_BROWSER_WINDOW '640,0,1280,1040' -> (x, y, width, height); empty or malformed -> None."""
    try:
        x, y, width, height = (int(part) for part in value.split(","))
    except ValueError:
        return None
    return (x, y, width, height) if width > 0 and height > 0 else None


class BrowserSession:
    def __init__(self, tools: "ToolSession") -> None:
        self.tools = tools
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.page: Any = None
        self._downloads: list[Any] = []
        self.last_status: int | None = None
        self.nav_count = 0
        self.last_snapshot = ""

    # --- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> None:
        if self.page is not None:
            return
        from playwright.sync_api import sync_playwright

        settings = self.tools.settings
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {"headless": settings.headless}
        if settings.chromium_path:
            launch["executable_path"] = settings.chromium_path
        context: dict[str, Any] = {"accept_downloads": True, "viewport": {"width": 1280, "height": 900}}
        window = None if settings.headless else parse_window(settings.browser_window)
        if window:
            # Demo recordings: every run's window opens at the same place and size, and the page fills it.
            x, y, width, height = window
            launch["args"] = [f"--window-position={x},{y}", f"--window-size={width},{height}"]
            context = {"accept_downloads": True, "no_viewport": True}
        self._browser = self._pw.chromium.launch(**launch)
        self._context = self._browser.new_context(**context)
        self._context.set_default_timeout(PAGE_TIMEOUT_MS)
        # Before any navigation: every request of this context goes through the gateway (D3).
        self._context.route("**/*", self.tools.gateway.on_route)
        self.page = self._context.new_page()
        # Only append in the handler: blocking calls inside Playwright event handlers deadlock.
        self.page.on("download", self._on_download)
        self.page.on("response", self._on_response)

    def _on_download(self, download: Any) -> None:
        self._downloads.append(download)

    def _on_response(self, response: Any) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == self.page.main_frame:
                self.last_status = response.status
                self.nav_count += 1
        except Exception:  # noqa: BLE001 - a frame may be gone already
            pass

    def close(self) -> None:
        for closer in (lambda: self._context and self._context.close(), lambda: self._browser and self._browser.close(), lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._context = self.page = None

    # --- helpers -------------------------------------------------------------------------------------

    def has_page(self) -> bool:
        return self.page is not None and self.page.url not in ("", "about:blank")

    def settle(self) -> list[str]:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            return []
        except Exception:  # noqa: BLE001 - PlaywrightTimeoutError
            return ["The page was still loading after 10 s; the snapshot may be incomplete."]

    def save_downloads(self, wait_ms: int = 0) -> list[str]:
        waited = 0
        while not self._downloads and waited < wait_ms:
            self.page.wait_for_timeout(100)
            waited += 100
        saved = []
        for download in self._downloads:
            name = _safe_filename(download.suggested_filename)
            target = self.tools.dir("downloads") / name
            download.save_as(str(target))
            saved.append(f"downloads/{name}")
        self._downloads.clear()
        return saved

    def snapshot_text(self) -> str:
        result = self.page.evaluate(SNAPSHOT_JS, 50)
        text = f"URL: {result['url']}\nTITLE: {result['title']}\n---\n{result['text']}"
        self.last_snapshot = text
        return text

    def screenshot(self) -> str | None:
        screens = self.tools.dir(self.tools.screens_dir)
        name = f"{self.tools.step:03d}.png"
        suffix = 2
        while (screens / name).exists():
            name = f"{self.tools.step:03d}-{suffix}.png"
            suffix += 1
        try:
            self.page.screenshot(path=str(screens / name), full_page=True)
        except Exception:  # noqa: BLE001
            return None
        return f"{self.tools.screens_dir}/{name}"

    def locate(self, ref: str) -> Any | None:
        if not re.fullmatch(r"e\d+", ref or ""):
            return None
        locator = self.page.locator(f'[data-kref="{ref}"]')
        return locator if locator.count() > 0 else None

    def observe(
        self,
        action: str,
        notes: list[str] | None = None,
        downloads: list[str] | None = None,
        retries: int = 0,
        screenshot: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> Observation:
        """Build the observation for the current page (after an action)."""
        notes = list(notes or [])
        downloads = list(downloads or [])
        events = self.tools.gateway.drain_events()
        url = self.page.url
        try:
            text = self.snapshot_text()
        except Exception as exc:  # noqa: BLE001
            text = f"URL: {url}\n---\n(could not read the page: {exc})"
        shot = self.screenshot() if screenshot else None
        status = self.last_status
        connector = self.tools.company.connector_for_origin(normalize_origin(url)) if url.startswith("http") else None
        path = urlsplit(url).path or url
        signal = connector.auth.logged_out_signal if connector and connector.auth else None
        if signal and path.startswith(signal.path_prefix) and action != "browser_login":
            self.tools.logged_out.add(connector.id)
            notes.append(
                f"You appear to be logged out of {connector.id}. Call browser_login('{connector.id}'), "
                "then redo the form — your previous input was not submitted."
            )
        flags = [f.as_flag(f"{connector.id if connector else 'web'}:{path}") for f in scan_snapshot(text)]
        for flag in flags:
            notes.append(f"⚠ Possible {flag['type'].replace('_', ' ')} on this page: '{flag['excerpt'][:200]}'")
        if retries:
            notes.append(f"Retried {retries}x after HTTP 5xx / timeout.")
        limit = self.tools.settings.snapshot_chars
        content = text if len(text) <= limit else text[:limit] + "\n…truncated; use browser_find to search the rest of this page."
        alerts = [line.strip()[len("[alert] "):] for line in text.splitlines() if line.strip().startswith("[alert] ")]
        committed = any(e.type == "write_outcome" and e.status == "committed" for e in events)
        blocked = any(e.type in BLOCKING_EVENTS for e in events)
        ok = committed or (not blocked and (status is None or status < 400))
        bits = [f"{path}", f"HTTP {status}" if status else ""]
        if alerts:
            bits.append(alerts[0][:120])
        bits += [f"[{e.type}{':' + e.status if e.status else ''}]" for e in events]
        if downloads:
            bits.append("downloaded " + ", ".join(downloads))
        artifacts = ([shot] if shot else []) + downloads
        return Observation(
            ok=ok,
            summary=" · ".join(b for b in bits if b),
            content=content,
            untrusted=True,
            source=f"{connector.id}:{path}" if connector else url,
            artifacts=artifacts,
            policy_events=events,
            flags=flags,
            meta={"url": url, "status": status, "retries": retries, "notes": notes, **(meta or {})},
        )

    def no_page(self) -> Observation:
        return Observation(ok=False, summary="no page is open", error="No page is open yet. Use browser_open or browser_login first.")

    def stale(self, ref: str) -> Observation:
        obs = self.observe("stale", notes=[f"ref {ref} is stale (or not on this page); use the refs below."], screenshot=False)
        return obs.model_copy(update={"ok": False, "summary": f"ref {ref} is stale; fresh snapshot provided"})


# --- tools ---------------------------------------------------------------------------------------------

BROWSER_ROLES = frozenset({"worker", "verifier"})


class OpenArgs(ToolArgs):
    url: str = Field(description="Absolute URL of a company app page (see connectors), or a path relative to the current page")


class BrowserOpen(Tool):
    name = "browser_open"
    description = "Open a URL (GET) in the browser. Returns a text snapshot with element refs and saves a screenshot. Failed page loads (HTTP 5xx, timeouts) are retried automatically."
    Args = OpenArgs
    roles = BROWSER_ROLES

    def run(self, args: OpenArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        browser.start()
        target = args.url.strip()
        if not re.match(r"^https?://", target):
            if not browser.has_page():
                return Observation(ok=False, summary="relative URL with no open page", error="Use a full URL: a connector's base_url (see SYSTEMS in the company context) plus a path.")
            target = urljoin(browser.page.url, target)
        retries, notes, downloads = 0, [], []
        for attempt in range(3):
            browser.last_status = None
            try:
                browser.page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001 - Playwright errors carry the reason in the message
                message = str(exc)
                if "Download is starting" in message:
                    downloads = browser.save_downloads(wait_ms=3000)
                    break
                if "ERR_BLOCKED_BY_CLIENT" in message:
                    events = session.gateway.drain_events()
                    reason = events[0].message if events else "Blocked by the gateway."
                    return Observation(ok=False, summary=f"blocked: {target}", error=reason, policy_events=events)
                if attempt < 2:
                    retries += 1
                    time.sleep(GET_RETRY_BACKOFF_S[attempt])
                    continue
                return Observation(ok=False, summary=f"could not open {target}", error=message.splitlines()[0][:300], meta={"retries": retries})
            if browser.last_status is not None and browser.last_status >= 500 and attempt < 2:
                retries += 1
                notes.append(f"HTTP {browser.last_status} on attempt {attempt + 1}")
                time.sleep(GET_RETRY_BACKOFF_S[attempt])
                continue
            break
        notes += browser.settle() if not downloads else []
        return browser.observe("browser_open", notes, downloads, retries)


class SnapshotArgs(ToolArgs):
    pass


class BrowserSnapshot(Tool):
    name = "browser_snapshot"
    description = "Take a fresh text snapshot (and screenshot) of the current page."
    Args = SnapshotArgs
    roles = BROWSER_ROLES

    def run(self, args: SnapshotArgs, session: "ToolSession") -> Observation:
        if not session.browser.has_page():
            return session.browser.no_page()
        return session.browser.observe("browser_snapshot")


class RefArgs(ToolArgs):
    ref: str = Field(description='Element ref from the latest snapshot, e.g. "e12"')


class BrowserClick(Tool):
    name = "browser_click"
    description = "Click an element (link, button) by its ref. Waits for the page, then returns a fresh snapshot. Clicking an attachment link downloads the file into downloads/."
    Args = RefArgs
    roles = BROWSER_ROLES

    def run(self, args: RefArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        if not browser.has_page():
            return browser.no_page()
        locator = browser.locate(args.ref)
        if locator is None:
            return browser.stale(args.ref)
        is_link = bool(locator.evaluate("el => el.tagName === 'A'"))
        navigations_before = browser.nav_count
        browser.last_status = None
        notes: list[str] = []
        try:
            # Never auto-retried: a click may submit a form (write).
            locator.click(timeout=NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            if "Timeout" not in str(exc):
                return Observation(ok=False, summary=f"click {args.ref} failed", error=str(exc).splitlines()[0][:300])
            # A slow server: the click may still have submitted. Report what the page and the
            # gateway saw (write outcomes included) instead of a bare failure.
            notes.append("The click timed out waiting for the page; check the result below before doing anything else.")
        notes += browser.settle()
        downloads = browser.save_downloads(wait_ms=2500 if is_link and browser.nav_count == navigations_before else 0)
        return browser.observe("browser_click", notes, downloads)


class FillArgs(ToolArgs):
    ref: str = Field(description="Ref of a textbox, date, number or textarea")
    value: str = Field(description="The full value to enter (replaces the current value). Dates: YYYY-MM-DD.")


class BrowserFill(Tool):
    name = "browser_fill"
    description = "Type a value into an input or textarea (replacing its content). Password fields are refused: credentials are entered only by browser_login."
    Args = FillArgs
    roles = BROWSER_ROLES

    def run(self, args: FillArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        if not browser.has_page():
            return browser.no_page()
        locator = browser.locate(args.ref)
        if locator is None:
            return browser.stale(args.ref)
        kind = str(locator.evaluate("el => (el.tagName + ':' + (el.getAttribute('type') || '')).toLowerCase()"))
        if kind.endswith(":password"):
            return Observation(ok=False, summary=f"refused to fill password field {args.ref}", error="Credentials are entered only by browser_login(connector_id).")
        if kind.startswith("select"):
            return Observation(ok=False, summary=f"{args.ref} is a select", error="Use browser_select with the option's visible label.")
        try:
            locator.fill(args.value, timeout=5000)
            current = locator.input_value(timeout=2000)
        except Exception as exc:  # noqa: BLE001
            return Observation(ok=False, summary=f"fill {args.ref} failed", error=str(exc).splitlines()[0][:300])
        obs = browser.observe("browser_fill", screenshot=False)
        note = f"Filled {args.ref}; the field now contains {current!r}."
        return obs.model_copy(update={"summary": note, "meta": {**obs.meta, "notes": [note] + obs.meta.get("notes", [])}})


class SelectArgs(ToolArgs):
    ref: str = Field(description="Ref of a select element")
    option_label: str = Field(description="The visible label of the option to choose")


class BrowserSelect(Tool):
    name = "browser_select"
    description = "Choose an option in a select element by its visible label."
    Args = SelectArgs
    roles = BROWSER_ROLES

    def run(self, args: SelectArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        if not browser.has_page():
            return browser.no_page()
        locator = browser.locate(args.ref)
        if locator is None:
            return browser.stale(args.ref)
        pairs: list[list[str]] = locator.evaluate("el => el.options ? Array.from(el.options).map(o => [o.text.trim(), o.value]) : []")
        options = [label for label, _ in pairs]
        wanted = args.option_label.strip()
        match = next((o for o in options if o == wanted), None) or next((o for o in options if o.casefold() == wanted.casefold()), None)
        if match is None:  # models sometimes pass the option's value (e.g. V001) instead of its label
            match = next((label for label, value in pairs if value and value.casefold() == wanted.casefold()), None)
        if match is None:
            return Observation(ok=False, summary=f"no option {wanted!r} in {args.ref}", error=f"Options are: {options}")
        try:
            locator.select_option(label=match, timeout=5000)
        except Exception as exc:  # noqa: BLE001
            return Observation(ok=False, summary=f"select on {args.ref} failed", error=str(exc).splitlines()[0][:300])
        obs = browser.observe("browser_select", screenshot=False)
        note = f"Selected {match!r} in {args.ref}."
        return obs.model_copy(update={"summary": note, "meta": {**obs.meta, "notes": [note] + obs.meta.get("notes", [])}})


class PressArgs(ToolArgs):
    key: Literal["Enter", "Tab", "Escape"] = Field(description="Key to press in the focused element")


class BrowserPress(Tool):
    name = "browser_press"
    description = "Press Enter, Tab or Escape in the focused element (Enter may submit a form)."
    Args = PressArgs
    roles = BROWSER_ROLES

    def run(self, args: PressArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        if not browser.has_page():
            return browser.no_page()
        browser.last_status = None
        browser.page.keyboard.press(args.key)
        notes = browser.settle()
        return browser.observe("browser_press", notes, browser.save_downloads())


class FindArgs(ToolArgs):
    text: str = Field(description="Text to look for in the current page (case-insensitive)")


class BrowserFind(Tool):
    name = "browser_find"
    description = "Return the lines of the current page snapshot that contain some text (useful on long pages)."
    Args = FindArgs
    roles = BROWSER_ROLES

    def run(self, args: FindArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        if not browser.has_page():
            return browser.no_page()
        text = browser.last_snapshot or browser.snapshot_text()
        needle = args.text.casefold()
        lines = [line for line in text.splitlines() if needle in line.casefold()]
        summary = f"{len(lines)} line(s) contain {args.text!r}"
        return Observation(ok=True, summary=summary, content="\n".join(lines[:40]) or "(no matches)", untrusted=True, source=f"page:{browser.page.url}")


class BackArgs(ToolArgs):
    pass


class BrowserBack(Tool):
    name = "browser_back"
    description = "Go back one page in the browser history."
    Args = BackArgs
    roles = BROWSER_ROLES

    def run(self, args: BackArgs, session: "ToolSession") -> Observation:
        browser = session.browser
        if not browser.has_page():
            return browser.no_page()
        browser.last_status = None
        try:
            browser.page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            return Observation(ok=False, summary="back failed", error=str(exc).splitlines()[0][:300])
        return browser.observe("browser_back")


class LoginArgs(ToolArgs):
    connector_id: str = Field(description="Which company app to log in to, e.g. erp or mail")


class BrowserLogin(Tool):
    name = "browser_login"
    description = "Log in to a company web app with the AI employee's own service account. Credentials are filled by the tool; you never see them. Returns success or failure and a snapshot."
    Args = LoginArgs
    roles = BROWSER_ROLES

    def run(self, args: LoginArgs, session: "ToolSession") -> Observation:
        connector = session.company.connectors.get(args.connector_id)
        if connector is None or connector.kind != "web_app" or connector.auth is None:
            web = [c.id for c in session.company.web_connectors() if c.auth]
            return Observation(ok=False, summary=f"cannot log in to {args.connector_id!r}", error=f"Connectors with a login: {web}")
        try:
            username, password = session.secrets.login_credentials(connector)
        except Exception as exc:  # noqa: BLE001 - MissingSecret
            return Observation(ok=False, summary=f"credentials for {connector.id} are not configured", error=str(exc))
        browser = session.browser
        browser.start()
        auth = connector.auth
        login_url = (connector.base_url or "").rstrip("/") + auth.login_path
        current = browser.page.url if browser.has_page() else ""
        # Already on this app's login page (e.g. after a timeout)? Stay, to keep its ?next= target.
        on_login_page = current.startswith(login_url)
        retries = 0
        if not on_login_page:
            for attempt in range(3):  # the login page is a GET: safe to retry
                browser.last_status = None
                try:
                    browser.page.goto(login_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                    if browser.last_status is None or browser.last_status < 500:
                        break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        return Observation(ok=False, summary=f"login page of {connector.id} unreachable", error=str(exc).splitlines()[0][:300])
                if attempt < 2:
                    retries += 1
                    time.sleep(GET_RETRY_BACKOFF_S[attempt])
        page = browser.page
        user_field = page.locator(f'form input[name="{auth.form_fields["username"]}"]')
        pass_field = page.locator(f'form input[name="{auth.form_fields["password"]}"]')
        if user_field.count() == 0 or pass_field.count() == 0:
            return Observation(ok=False, summary=f"no login form on {page.url}", error="The login form fields were not found.")
        browser.last_status = None
        user_field.first.fill(username)
        pass_field.first.fill(password)
        pass_field.first.press("Enter")  # submits the form whatever the button is called today
        notes = browser.settle()
        path = urlsplit(page.url).path
        signal = auth.logged_out_signal
        success = not (signal and path.startswith(signal.path_prefix))
        relogin = connector.id in session.logged_out
        session.logged_out.discard(connector.id)
        obs = browser.observe("browser_login", notes, retries=retries, meta={"login": connector.id, "relogin": relogin})
        summary = f"Logged in to {connector.id} as the service account" if success else f"Login to {connector.id} FAILED"
        return obs.model_copy(update={"ok": success and obs.ok, "summary": f"{summary} · now at {path}"})

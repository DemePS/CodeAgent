"""screenshot_page: open a page in a headless browser and show Claude the screenshot."""

import os
import sys

from .. import state
from ..common import ToolError, resolve, truncate
from ..config import MAX_IMAGE_BYTES, MAX_SCREENSHOT_TILES
from ..tools.documents import image_block
from ..tools.network import check_host, confirm_network

# No background traffic (updates, sync, safe-browsing lists): only the page's own requests go out,
# which matters behind a firewall and keeps the browser quiet.
BROWSER_ARGS = ["--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--no-first-run", "--no-default-browser-check", "--disable-domain-reliability"]


def launch_browser(pw):
    """Start a headless browser, trying each option in turn. Returns (browser, label).

    Order: AGENT_BROWSER_PATH, Playwright's own Chromium (`playwright install chromium`), then
    the Microsoft Edge or Google Chrome already installed on the machine -- so screenshots work
    even where Playwright's browser download is blocked (e.g. behind a corporate proxy).
    """
    from playwright.sync_api import Error as PlaywrightError

    attempts = []
    if os.environ.get("AGENT_BROWSER_PATH"):
        attempts.append((f"AGENT_BROWSER_PATH ({os.environ['AGENT_BROWSER_PATH']})",
                         {"executable_path": os.environ["AGENT_BROWSER_PATH"]}))
    attempts += [("Playwright's Chromium", {}), ("Microsoft Edge", {"channel": "msedge"}),
                 ("Google Chrome", {"channel": "chrome"})]
    errors = []
    for label, options in attempts:
        try:
            return pw.chromium.launch(headless=True, args=BROWSER_ARGS, **options), label
        except PlaywrightError as e:
            first = next((line.strip() for line in str(e).splitlines() if line.strip()), "failed")
            errors.append(f"{label}: {first[:300]}")
    raise ToolError(
        "No browser could be started. Tried:\n  " + "\n  ".join(errors) + "\n"
        "Fix one of them: run `playwright install chromium` in the agent's environment "
        "(`uv run playwright install chromium`), install Microsoft Edge or Google Chrome, or set "
        "AGENT_BROWSER_PATH to a Chrome/Chromium/Edge executable. `coding-agent --check-browser` "
        "tests the setup."
    )


def check_browser() -> str:
    """--check-browser: report which browser screenshot_page will use, or why none works."""
    lines = [f"Python: {sys.executable}"]
    try:
        from playwright._repo_version import version
        from playwright.sync_api import sync_playwright
    except ImportError:
        lines.append("Playwright: NOT installed in this environment.\n"
                     "  Fix: uv sync --extra browser   (or: pip install playwright)")
        return "\n".join(lines)
    lines.append(f"Playwright: {version}")
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        lines.append(f"PLAYWRIGHT_BROWSERS_PATH: {os.environ['PLAYWRIGHT_BROWSERS_PATH']}")
    try:
        with sync_playwright() as pw:
            browser, label = launch_browser(pw)
            try:
                page = browser.new_page()
                page.set_content("<h1>ok</h1>")
                size = len(page.screenshot())
            finally:
                browser.close()
        lines.append(f"Browser: {label} (version {browser.version}) -- screenshot OK ({size:,} bytes)")
    except ToolError as e:
        lines.append(f"Browser: FAILED\n{e}")
    except Exception as e:  # the Playwright driver itself failed to start
        lines.append(f"Browser: FAILED -- {type(e).__name__}: {e}")
    return "\n".join(lines)


def tool_screenshot_page(url: str, width: int = 1280, height: int = 800, full_page: bool = False,
                         selector: str | None = None, dark_mode: bool = False, wait_ms: int = 500,
                         include_text: bool = False) -> list:
    from urllib.parse import unquote, urlsplit
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ToolError("Playwright is not installed. Tell the user to run: pip install playwright "
                        "(or: uv add --dev playwright), then: playwright install chromium")

    width, height = max(320, min(int(width), 2560)), max(320, min(int(height), 2000))
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        note = check_host(parts.hostname or "")
        if not note:  # a public site: the URL leaves the machine, so the user decides
            confirm_network("Open web page", [f"url: {url}", "a headless browser loads it and takes a screenshot"])
    elif parts.scheme in ("", "file"):
        page_file = resolve(parts.path if parts.scheme == "file" else url)
        if not page_file.is_file():
            raise ToolError(f"File not found: {url}")
        url = page_file.as_uri()
    else:
        raise ToolError("Use an http(s) URL or a workspace HTML file.")

    host_ok: dict[str, bool] = {}

    def allowed(request_url: str) -> bool:
        """Every request the page makes: no metadata addresses, no local files outside the workspace."""
        u = urlsplit(request_url)
        if u.scheme == "file":
            try:
                resolve(unquote(u.path))
                return True
            except ToolError:
                return False
        if u.scheme in ("http", "https", "ws", "wss"):
            host = u.hostname or ""
            if host not in host_ok:
                try:
                    check_host(host)
                    host_ok[host] = True
                except ToolError:
                    host_ok[host] = False
            return host_ok[host]
        return True  # data:, blob: and the like stay inside the page

    state.ui.status(f"[browser] {url} ({width}x{height}{', dark' if dark_mode else ''}"
                    f"{', full page' if full_page else ''}{', ' + selector if selector else ''})")
    console, failed, blocked = [], [], []
    try:
        with sync_playwright() as pw:
            browser, browser_label = launch_browser(pw)
            try:
                context = browser.new_context(viewport={"width": width, "height": height},
                                              color_scheme="dark" if dark_mode else "light",
                                              accept_downloads=False, service_workers="block")
                page = context.new_page()

                def route(r):
                    if allowed(r.request.url):
                        r.continue_()
                    else:
                        blocked.append(r.request.url)
                        r.abort()
                context.route("**/*", route)
                page.on("console", lambda m: m.type in ("error", "warning") and console.append(f"{m.type}: {m.text}"))
                page.on("pageerror", lambda e: console.append(f"uncaught: {e}"))
                page.on("requestfailed", lambda r: failed.append(f"{r.url} ({r.failure})"))
                page.on("response", lambda r: r.status >= 400 and failed.append(f"{r.url} (HTTP {r.status})"))
                response = page.goto(url, wait_until="load", timeout=30_000)
                page.wait_for_timeout(max(0, min(int(wait_ms), 15_000)))

                shots = []
                if selector:
                    element = page.query_selector(selector)
                    if element is None:
                        raise ToolError(f"No element matches {selector!r} on the page.")
                    shots.append(element.screenshot(type="png"))
                elif full_page:
                    total = page.evaluate("document.documentElement.scrollHeight")
                    for i in range(min(MAX_SCREENSHOT_TILES, -(-total // height))):
                        clip = {"x": 0, "y": i * height, "width": width, "height": min(height, total - i * height)}
                        shots.append(page.screenshot(type="png", full_page=True, clip=clip))
                else:
                    shots.append(page.screenshot(type="png"))
                title = page.title()
                text = page.inner_text("body") if include_text else ""
                total_height = page.evaluate("document.documentElement.scrollHeight")
            finally:
                browser.close()
    except PlaywrightError as e:
        text = str(e)
        message = next((line.strip() for line in text.splitlines() if line.strip()), "unknown error")[:500]
        if "ERR_CONNECTION_REFUSED" in text:
            message = f"Nothing is listening at {url}. Ask the user to start the dev server."
        elif "ERR_NAME_NOT_RESOLVED" in text:
            message = f"Cannot resolve the host in {url}."
        elif "Timeout" in text:
            message = f"The page did not finish loading within 30 s: {url}"
        raise ToolError(f"Browser error: {message}")
    except (OSError, NotImplementedError) as e:  # the Playwright driver could not start
        raise ToolError(f"Playwright could not start ({type(e).__name__}: {e}). Ask the user to run "
                        "`coding-agent --check-browser` and share the output.")

    status = response.status if response else "n/a"
    lines = [f"Page: {title!r} -- {url} (HTTP {status}), viewport {width}x{height}"
             f"{', dark mode' if dark_mode else ''}, page height {total_height}px"]
    if full_page and -(-total_height // height) > len(shots):
        lines.append(f"(showing the first {len(shots)} screens of {-(-total_height // height)})")
    lines.append("Console errors/warnings:\n  " + ("\n  ".join(console[:30]) if console else "(none)"))
    lines.append("Failed requests:\n  " + ("\n  ".join(failed[:30]) if failed else "(none)"))
    if blocked:
        lines.append("Blocked by the agent (metadata address or file outside the workspace):\n  " + "\n  ".join(blocked[:10]))
    if include_text:
        lines.append("Visible text:\n" + truncate(text)[:20_000])
    content: list = [{"type": "text", "text": "\n".join(lines)}]
    for i, shot in enumerate(shots):
        if len(shot) > MAX_IMAGE_BYTES:
            content.append({"type": "text", "text": f"(screenshot {i + 1} skipped: too large; use a smaller viewport)"})
            continue
        if len(shots) > 1:
            content.append({"type": "text", "text": f"Screen {i + 1} (from y={i * height}px):"})
        content.append(image_block(shot, "image/png"))
    state.ui.status(f"[browser] {browser_label}: {len(shots)} screenshot(s), {len(console)} console message(s), "
                    f"{len(failed)} failed request(s)")
    return content



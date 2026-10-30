"""Conservative, local FRITZ!Box browser fallback for settings without an API.

The browser starts on the first UI tool call. Playwright's context ignores HTTPS
certificate errors because local FRITZ!Box installations commonly use a
self-signed certificate; this setting applies only to the configured router.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from mcp.types import ToolAnnotations

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright


class UIError(ValueError):
    """An error safe to return through an MCP tool."""


_SENSITIVE_FIELD = re.compile(
    r"password|passwort|secret|token|schlüssel|schluessel|\bsid\b|\bpin\b|\bpsk\b|\bkey\b|key$",
    re.IGNORECASE,
)
_TWO_FACTOR = re.compile(
    r"two.factor|2fa|verification code|bestätigungscode|authenticator|einmalcode",
    re.IGNORECASE,
)
_LOGIN_BUTTON = re.compile(
    r"^(?:log\s*in|sign\s*in|anmelden|login|weiter)$", re.IGNORECASE
)
_LOGIN_FAILURE = re.compile(
    r"anmeldung fehlgeschlagen|login failed|sign.in failed|zugangsberechtigung",
    re.IGNORECASE,
)
_LOGIN_PROMPT = re.compile(
    r"willkommen bei (?:ihrer|your) fritz|kennwort vergessen|forgot password|loading router login",
    re.IGNORECASE,
)
_AUTH_NAV = re.compile(
    r"^(?:internet|heimnetz|home network|system|übersicht|overview|abmelden|log\s*out)$",
    re.IGNORECASE,
)
_AUTH_MENU = re.compile(
    r"internet|heimnetz|home network|system|übersicht|overview", re.IGNORECASE
)
_VISIBLE_SNAPSHOT = """start => {
  const secret = /password|passwort|secret|token|schlüssel|schluessel|\\bsid\\b|\\bpin\\b|\\bpsk\\b|(?:^|[-_])key(?:$|[-_])/i;
  const hidden = el => {
    if (!el || !el.isConnected || el.closest('[hidden], [aria-hidden="true"], script, style, template')) return true;
    const style = getComputedStyle(el);
    return style.display === 'none' || style.visibility === 'hidden' || el.getClientRects().length === 0;
  };
  const sensitive = el => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (secret.test([n.id, n.className, n.getAttribute('name'), n.getAttribute('data-testid')].join(' '))) return true;
    }
    return false;
  };
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 180);
  const label = el => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/);
    const byIds = ids.map(id => document.getElementById(id)?.textContent || '').join(' ');
    return clean(el.getAttribute('aria-label') || byIds ||
      [...(el.labels || [])].map(x => x.textContent).join(' ') ||
      el.getAttribute('placeholder') || el.getAttribute('title') ||
      (el.matches('button, a') ? el.textContent : ''));
  };
  const text = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode() && text.length < 350) {
    const node = walker.currentNode;
    const el = node.parentElement;
    if (hidden(el) || sensitive(el)) continue;
    const value = clean(node.textContent);
    if (value && !secret.test(value)) text.push(value);
  }
  const controls = [];
  for (const old of document.querySelectorAll('[data-fritz-mcp-ref]')) old.removeAttribute('data-fritz-mcp-ref');
  const selector = 'a, button, input, select, textarea, [role="button"], [role="checkbox"], [role="radio"], [role="switch"], [role="link"], [role="combobox"]';
  for (const el of document.querySelectorAll(selector)) {
    if (controls.length >= 150) break;
    if (hidden(el) || sensitive(el) || el.matches('input[type="hidden"], input[type="password"]')) continue;
    let role = el.getAttribute('role') || ({A:'link', BUTTON:'button', SELECT:'combobox', TEXTAREA:'textbox'}[el.tagName]);
    if (!role && el.tagName === 'INPUT') {
      role = ({checkbox:'checkbox', radio:'radio', button:'button', submit:'button', reset:'button', search:'searchbox'}[el.type] || 'textbox');
    }
    if (!role) continue;
    const name = label(el);
    if (secret.test(name)) continue;
    const ref = `c${start + controls.length}`;
    el.setAttribute('data-fritz-mcp-ref', ref);
    const item = {ref, role, name};
    if (['textbox','searchbox'].includes(role)) item.value = clean(el.value);
    if (['checkbox','radio','switch'].includes(role)) item.checked = !!el.checked || el.getAttribute('aria-checked') === 'true';
    if (role === 'combobox' && el.tagName === 'SELECT') {
      item.options = [...el.options].slice(0, 30).map(o => clean(o.label)).filter(o => o && !secret.test(o));
    }
    controls.push(item);
  }
  return {text: text.join(' ').slice(0, 10000), controls};
}"""


def _validated_url(configured: str, host: str) -> str:
    parsed = urlsplit(configured)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.hostname.lower().rstrip(".") != host.lower().rstrip(".")
    ):
        raise UIError(
            "FRITZ_UI_URL must be the configured router origin without credentials, path, query, or fragment."
        )
    try:
        port = parsed.port
    except ValueError:
        raise UIError("FRITZ_UI_URL has an invalid port.") from None
    return f"{parsed.scheme}://{parsed.hostname}{f':{port}' if port else ''}"


def _configured_url() -> str:
    host = os.getenv("FRITZ_HOST", "fritz.box").strip()
    if not host or any(c in host for c in "/?#@"):
        raise UIError("FRITZ_HOST must be a router hostname or IP address.")
    configured = os.getenv("FRITZ_UI_URL", f"https://{host}")
    return _validated_url(configured, host)


def _password() -> str | None:
    direct = os.getenv("FRITZ_PASSWORD")
    if direct:
        return direct
    filename = os.getenv("FRITZ_PASSWORD_FILE")
    if filename:
        try:
            return Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError:
            raise UIError("Could not read FRITZ_PASSWORD_FILE.") from None
    return None


class BrowserUI:
    """Single-page browser session; all public operations run under one lock."""

    def __init__(self, url: str | None = None) -> None:
        self.origin = (
            _configured_url()
            if url is None
            else _validated_url(url, os.getenv("FRITZ_HOST", "fritz.box").strip())
        )
        parsed = urlsplit(self.origin)
        self._origin_parts = (
            parsed.scheme,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._redactions: set[str] = set()
        self._refs: set[str] = set()
        self._next_ref = 0

    def _same_origin(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            ) == self._origin_parts
        except ValueError:
            return False

    def _same_websocket_origin(self, url: str) -> bool:
        try:
            parsed = urlsplit(url)
            scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme)
            return (
                scheme,
                parsed.hostname,
                parsed.port or (443 if scheme == "https" else 80),
            ) == self._origin_parts
        except ValueError:
            return False

    async def _route(self, route: object) -> None:
        if self._same_origin(route.request.url):
            await route.continue_()
        else:
            await route.abort()

    async def _route_web_socket(self, route: object) -> None:
        if self._same_websocket_origin(route.url):
            await route.connect_to_server()
        else:
            await route.close(code=1008)

    async def _ensure_page(self) -> Page:
        if self._page is not None and not self._page.is_closed():
            return self._page
        try:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise UIError(
                    "UI tools require the optional Playwright package and Chromium browser."
                ) from None
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
            self._context = await self._browser.new_context(
                ignore_https_errors=True, service_workers="block"
            )
            await self._context.route("**/*", self._route)
            await self._context.route_web_socket("**/*", self._route_web_socket)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(10_000)
            await self._page.goto(self.origin + "/", wait_until="domcontentloaded")
            self._assert_origin()
            await self._login_if_needed()
            return self._page
        except UIError:
            await self._close_unlocked()
            raise
        except Exception:  # noqa: BLE001 - Playwright errors may contain credentials or session URLs.
            await self._close_unlocked()
            raise UIError(
                "Could not open the router UI. Check router availability and Playwright Chromium installation."
            ) from None

    def _assert_origin(self) -> None:
        if self._page is None or not self._same_origin(self._page.url):
            raise UIError(
                "The browser left the configured router origin; the UI session was stopped."
            )

    async def _login_if_needed(self) -> None:
        assert self._page is not None
        await self._page.wait_for_load_state("load")
        password_input = self._page.locator('input[type="password"]:visible')
        # FRITZ pages may render the form after DOMContentLoaded and even after load.
        # A short bounded wait avoids treating a loading login shell as authenticated.
        if not await password_input.count():
            try:
                await password_input.first.wait_for(state="visible", timeout=2_000)
            except Exception:  # noqa: BLE001, S110 - A settings page need not have a password field.
                pass
        body = (await self._page.locator("body").inner_text())[:2500]
        if not await password_input.count() and (
            _LOGIN_PROMPT.search(body) or _LOGIN_FAILURE.search(body)
        ):
            try:
                await password_input.first.wait_for(state="visible", timeout=5_000)
            except Exception:  # noqa: BLE001 - Do not leak the login page URL.
                raise UIError("Router UI login page did not become ready.") from None
        if await password_input.count() == 0:
            return
        form = password_input.first.locator("xpath=ancestor::form")
        button = form.get_by_role("button", name=_LOGIN_BUTTON).first
        if not await form.count() or not await button.count():
            if _LOGIN_PROMPT.search(body):
                raise UIError(
                    "Router UI login form is not supported by this browser fallback."
                )
            # A password field on a settings page is not a login form.
            return
        password = _password()
        if not password:
            raise UIError(
                "Router login is required. Configure FRITZ_PASSWORD or FRITZ_PASSWORD_FILE."
            )
        username = os.getenv("FRITZ_USERNAME", "")
        if username:
            username_select = self._page.locator(
                'select#uiViewUser:visible, select[name*="user" i]:visible'
            ).first
            if await username_select.count():
                await username_select.select_option(label=username)
            else:
                username_input = self._page.locator(
                    'input#uiViewUser:visible, input[name*="user" i]:visible, input[autocomplete="username"]:visible'
                ).first
                if await username_input.count():
                    await username_input.fill(username)
                elif not await self._page.locator("select#uiViewUser").count():
                    raise UIError(
                        "Router login username field was not recognized; complete login locally."
                    )
        await password_input.first.fill(password)
        self._redactions.add(password)
        await button.click()
        deadline = asyncio.get_running_loop().time() + 10
        try:
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.25)
                self._assert_origin()
                body = (await self._page.locator("body").inner_text())[:2500]
                if _TWO_FACTOR.search(body):
                    raise UIError(
                        "Router login requires additional user verification in a local browser."
                    )
                if not await self._page.locator(
                    'form:has(input[type="password"]):visible'
                ).count():
                    navigation = await self._page.get_by_role(
                        "link", name=_AUTH_NAV
                    ).count()
                    navigation += await self._page.get_by_role(
                        "button", name=_AUTH_NAV
                    ).count()
                    navigation += await self._page.get_by_role(
                        "menuitem", name=_AUTH_MENU
                    ).count()
                    query = parse_qs(urlsplit(self._page.url).query)
                    sid = query.get("sid", [""])[0]
                    valid_sid = (
                        bool(re.fullmatch(r"[0-9a-fA-F]{16}", sid)) and sid != "0" * 16
                    )
                    if navigation or valid_sid:
                        return
        except UIError:
            raise
        except Exception:  # noqa: BLE001 - The message must not include a login URL.
            raise UIError(
                "Router login did not complete. Check credentials or complete any required user verification locally."
            ) from None
        if _LOGIN_FAILURE.search(body):
            raise UIError(
                "Router UI login failed. Check UI credentials or access permissions."
            )
        raise UIError(
            "Router login did not complete; no authenticated navigation appeared."
        )

    def _redact(self, value: str) -> str:
        for secret in sorted(self._redactions, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[redacted]")
        # Remove common token and session forms, including values rendered as text.
        value = re.sub(
            r"(?i)\b(sid|session(?:id)?|token|password|passwort|secret|psk|pin)\s*[:=]\s*\S+",
            r"\1=[redacted]",
            value,
        )
        value = re.sub(r"\b[0-9a-fA-F]{24,}\b", "[redacted]", value)
        value = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[redacted]", value)
        return value

    async def _snapshot_unlocked(self) -> dict:
        assert self._page is not None
        self._assert_origin()
        data = await self._page.evaluate(_VISIBLE_SNAPSHOT, self._next_ref)
        if _LOGIN_FAILURE.search(data["text"]) or _LOGIN_PROMPT.search(data["text"]):
            raise UIError(
                "Router UI is still on a login page; authentication did not complete."
            )
        self._next_ref += len(data["controls"])
        # Never expose a query string: FRITZ!Box URLs may contain a session ID.
        path = urlsplit(self._page.url).path or "/"
        controls = []
        remaining = 7_000
        for item in data["controls"]:
            safe = {
                "ref": item["ref"],
                "role": item["role"],
                "name": self._redact(item["name"]),
            }
            if "checked" in item:
                safe["checked"] = item["checked"]
            if "value" in item:
                safe["value"] = self._redact(item["value"])
            if "options" in item:
                safe["options"] = [self._redact(option) for option in item["options"]]
            size = len(str(safe))
            if size > remaining:
                break
            remaining -= size
            controls.append(safe)
        self._refs = {item["ref"] for item in controls}
        return {
            "path": self._redact(path)[:500],
            "text": self._redact(data["text"])[:8000],
            "controls": controls,
        }

    async def open(self, path: str = "/") -> dict:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "?" in path
            or "#" in path
            or "\\" in path
        ):
            raise UIError(
                "Path must be a router-relative path without query or fragment."
            )
        async with self._lock:
            try:
                initial = self._page is None or self._page.is_closed()
                page = await self._ensure_page()
                query = parse_qs(urlsplit(page.url).query)
                sid = query.get("sid", [""])[0]
                suffix = (
                    f"?sid={sid}"
                    if re.fullmatch(r"[0-9a-fA-F]{16}", sid) and sid != "0" * 16
                    else ""
                )
                target = self.origin + path + suffix
                navigate = (not initial and target != page.url) or (
                    initial and path != "/"
                )
                if navigate:
                    await page.goto(target, wait_until="domcontentloaded")
                self._assert_origin()
                if navigate:
                    await self._login_if_needed()
                return await self._snapshot_unlocked()
            except UIError:
                if self._page is not None and not self._same_origin(self._page.url):
                    await self._close_unlocked()
                raise
            except Exception:  # noqa: BLE001 - Never expose raw Playwright errors through MCP.
                if self._page is not None and not self._same_origin(self._page.url):
                    await self._close_unlocked()
                raise UIError("Could not open the requested router page.") from None

    async def snapshot(self) -> dict:
        async with self._lock:
            try:
                await self._ensure_page()
                return await self._snapshot_unlocked()
            except UIError:
                if self._page is not None and not self._same_origin(self._page.url):
                    await self._close_unlocked()
                raise
            except Exception:  # noqa: BLE001 - Never expose raw Playwright errors through MCP.
                if self._page is not None and not self._same_origin(self._page.url):
                    await self._close_unlocked()
                raise UIError("Could not read the router page.") from None

    async def _act(
        self,
        operation: str,
        locator_for_page,
        value=None,
        label: str | None = None,
        ref: str | None = None,
    ) -> dict:
        async with self._lock:
            try:
                page = await self._ensure_page()
                if ref is not None and ref not in self._refs:
                    raise UIError("Control reference is stale. Take a new UI snapshot.")
                locator = locator_for_page(page)
                if label and not await locator.count():
                    role = {
                        "fill": "textbox",
                        "select": "combobox",
                        "checked": "checkbox",
                    }.get(operation)
                    if role:
                        locator = page.get_by_role(role, name=label, exact=True)
                if label and not await locator.count() and operation == "fill":
                    locator = page.get_by_placeholder(label, exact=True)
                if operation == "click":
                    await locator.click()
                elif operation == "fill":
                    if label and _SENSITIVE_FIELD.search(label):
                        self._redactions.add(value)
                    await locator.fill(value)
                elif operation == "select":
                    await locator.select_option(label=value)
                elif operation == "checked":
                    await locator.set_checked(value)
                self._assert_origin()
                return await self._snapshot_unlocked()
            except UIError:
                if self._page is not None and not self._same_origin(self._page.url):
                    await self._close_unlocked()
                raise
            except Exception:  # noqa: BLE001 - Never expose raw Playwright errors through MCP.
                if self._page is not None and not self._same_origin(self._page.url):
                    await self._close_unlocked()
                raise UIError(
                    f"Router UI {operation} failed. Check the control name and current snapshot."
                ) from None

    async def click(self, role: str, name: str, exact: bool = True) -> dict:
        if role not in {
            "button",
            "link",
            "checkbox",
            "radio",
            "switch",
            "tab",
            "menuitem",
        }:
            raise UIError("Unsupported clickable role.")
        if not name:
            raise UIError("Control name is required.")
        return await self._act(
            "click", lambda page: page.get_by_role(role, name=name, exact=exact)
        )

    async def fill(self, label: str, value: str) -> dict:
        if not label:
            raise UIError("Field label is required.")
        return await self._act(
            "fill", lambda page: page.get_by_label(label, exact=True), value, label
        )

    async def select(self, label: str, value: str) -> dict:
        if not label:
            raise UIError("Field label is required.")
        return await self._act(
            "select", lambda page: page.get_by_label(label, exact=True), value, label
        )

    async def set_checked(self, label: str, checked: bool) -> dict:
        if not label:
            raise UIError("Field label is required.")
        return await self._act(
            "checked", lambda page: page.get_by_label(label, exact=True), checked, label
        )

    async def control(self, ref: str, action: str, value: str | None = None) -> dict:
        """Act on a control reference from the most recent snapshot."""
        if not re.fullmatch(r"c[0-9]+", ref):
            raise UIError(
                "Invalid control reference. Use a ref from the latest UI snapshot."
            )
        if action not in {"click", "fill", "select", "check", "uncheck"}:
            raise UIError("Unsupported control action.")
        if action in {"fill", "select"} and value is None:
            raise UIError("This control action requires a value.")
        operation = "checked" if action in {"check", "uncheck"} else action
        argument = (action == "check") if operation == "checked" else value
        return await self._act(
            operation,
            lambda page: page.locator(f'[data-fritz-mcp-ref="{ref}"]'),
            argument,
            ref=ref,
        )

    async def _close_unlocked(self) -> None:
        for resource in (self._context, self._browser, self._playwright):
            if resource is None:
                continue
            try:
                if resource is self._playwright:
                    await resource.stop()
                else:
                    await resource.close()
            except Exception:  # noqa: BLE001, S110 - Continue releasing remaining browser resources.
                # Cleanup errors are intentionally discarded; no request data is logged.
                pass
        self._context = None
        self._browser = None
        self._playwright = None
        self._page = None
        self._redactions.clear()
        self._refs.clear()

    async def close(self) -> str:
        async with self._lock:
            try:
                await self._close_unlocked()
            except Exception:  # noqa: BLE001 - Never expose raw Playwright errors through MCP.
                raise UIError(
                    "Could not close the router UI session cleanly."
                ) from None
            return "Router UI session closed."


def register_ui_tools(mcp, ui: BrowserUI | None = None) -> BrowserUI:
    """Register browser fallback tools on a FastMCP v1 server."""
    browser_ui = ui or BrowserUI()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ui_open(path: str = "/") -> dict:
        """Open a router-relative UI path and return a redacted page snapshot."""
        return await browser_ui.open(path)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ui_snapshot() -> dict:
        """Read visible router text and controls; excludes secret field values."""
        return await browser_ui.snapshot()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def ui_click(role: str, name: str, exact: bool = True) -> dict:
        """Click one named router control. This can change settings or submit forms."""
        return await browser_ui.click(role, name, exact)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def ui_fill(label: str, value: str) -> dict:
        """Fill a labelled field; secret-labelled values are redacted from snapshots."""
        return await browser_ui.fill(label, value)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def ui_select(label: str, value: str) -> dict:
        """Select a value in a labelled router dropdown; may change settings."""
        return await browser_ui.select(label, value)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def ui_set_checked(label: str, checked: bool) -> dict:
        """Set a labelled checkbox or radio state; may change settings."""
        return await browser_ui.set_checked(label, checked)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def ui_control(ref: str, action: str, value: str | None = None) -> dict:
        """Act on a ref from the latest snapshot: click, fill, select, check, uncheck."""
        return await browser_ui.control(ref, action, value)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def ui_close() -> str:
        """Close the browser session and clear its in-memory redactions."""
        return await browser_ui.close()

    return browser_ui

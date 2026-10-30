"""Offline safety and interpretation checks for the coverage inventory."""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_coverage.py"
_SPEC = importlib.util.spec_from_file_location("audit_coverage", _SCRIPT)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("/?lp=net&sid=0123456789abcdef", "net"),
        ("/index.lua?page=dns&sid=0123456789abcdef", "dns"),
        ("https://router.local/?lp=wifi&sid=abc", "wifi"),
        ("https://other.local/?lp=wifi", None),
        ("/logout?lp=wifi", None),
        ("/?lp=wifi&action=save", None),
        ("/?lp=wifi#apply", None),
        ("/?lp=wifi&lp=lan", None),
    ],
)
def test_route_id_removes_session_and_rejects_unsafe_urls(href, expected):
    assert audit.route_id(href, "https://router.local") == expected


@pytest.mark.parametrize(
    "label", ["Abmelden", "Factory reset", "Diagnose starten", "Apply", "Setup wizard"]
)
def test_action_like_navigation_is_excluded(label):
    assert not audit.navigation_allowed(
        label, "/?lp=some_page&sid=private", "https://router.local"
    )


@pytest.mark.parametrize(
    "route",
    [
        "reset",
        "rebootNow",
        "factoryReset",
        "diagnosticStart",
        "wizOv",
        "ssoPass",
        "importConfig",
        "applySettings",
    ],
)
def test_action_route_is_excluded_even_with_innocent_label(route):
    assert not audit.navigation_allowed(
        "More details",
        f"/?lp={route}&sid=0123456789abcdef",
        "https://router.local",
    )


def test_unverified_route_is_excluded():
    assert not audit.navigation_allowed(
        "More details", "/?lp=unknownPage&sid=0123456789abcdef", "https://router.local"
    )
    assert not audit.navigation_allowed(
        "DNS Server",
        "/?lp=dnsServer&sid=0123456789abcdef",
        "https://router.local",
    )


@pytest.mark.parametrize(
    "label",
    [
        "WiFi key: shortpass",
        "Password=letmein",
        "Router SID: abc123",
        "192.0.2.1",
        "Device 00:11:22:33:44:55",
    ],
)
def test_safe_label_drops_inline_values_and_identifiers(label):
    assert audit.safe_label(label) is None


def test_control_summary_never_exposes_values_or_table_device_rows():
    base = {
        "tag": "input",
        "type": "text",
        "role": "",
        "visible": True,
        "disabled": False,
        "matched": True,
        "in_form": True,
        "in_table": False,
        "ancestor": "",
    }
    rows = [
        {**base, "name": "DNS Server", "value": "192.0.2.4"},
        {
            **base,
            "name": "Laptop 00:11:22:33:44:55",
            "in_table": True,
            "value": "private",
        },
        {
            **base,
            "name": "Verschlüsselte DNS-Auflösung",
            "ancestor": "schluessel_container",
            "matched": False,
        },
        {**base, "name": "Router password", "type": "password", "value": "secret"},
        {**base, "name": "Certificate", "type": "file", "matched": False},
    ]
    result = audit.summarize_controls(rows)
    assert result["counts"]["setting_control"] == 1
    assert result["counts"]["table_or_device_row"] == 1
    assert result["counts"]["possible_sensitive_keyword"] == 1
    assert result["counts"]["secret"] == 1
    assert result["counts"]["file_upload"] == 1
    assert [x["name"] for x in result["settings"]] == ["DNS Server", "Certificate"]
    assert "192.0.2.4" not in repr(result)
    assert "00:11:22:33:44:55" not in repr(result)
    assert "'value': 'secret'" not in repr(result)


@pytest.mark.asyncio
async def test_dom_inventory_counts_open_shadow_controls_without_values():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """<nav><button>Navigation</button></nav><h1>Settings</h1><form><label for='dns'>DNS server</label>
                <input id='dns' value='192.0.2.8' data-fritz-mcp-ref='c1'></form>
                <div id='host'></div><script>
                const root = document.querySelector('#host').attachShadow({mode:'open'});
                root.innerHTML = '<label for="inner">Inner setting</label><input id="inner" value="private">';
                </script>"""
            )
            data = await page.evaluate(audit._DOM_INVENTORY)
            result = audit.summarize_controls(data["controls"])
            assert result["open_shadow_controls"] == 1
            assert result["open_shadow_unmatched"] == 1
            assert result["visible_matched"] == 1
            assert "192.0.2.8" not in repr(result)
            assert "private" not in repr(result)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_navigation_uses_allowlisted_page_handler_without_clicking_actions():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("""<script>window.clicked = [];</script>
              <a href='/?lp=reset' onclick="event.preventDefault();clicked.push('reset')">Details</a>
              <button role='menuitem' aria-haspopup='true' aria-expanded='false'
                onclick="clicked.push('action')">System</button>
              <form><a href='/?lp=sitemap' onclick="event.preventDefault();clicked.push('form')">Help</a></form>
              <a href='/?lp=sitemap' onclick="event.preventDefault();clicked.push('sitemap')">Help and info</a>
            """)
            assert not await audit.click_navigation(
                page, "reset", "https://router.local"
            )
            assert await audit.click_navigation(page, "sitemap", "https://router.local")
            assert await page.evaluate("window.clicked") == ["sitemap"]
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_navigation_expands_only_verified_group_then_trusted_clicks_leaf():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("""<script>window.clicked = [];</script>
              <button role='menuitem' aria-haspopup='true' aria-expanded='false'
                onclick="clicked.push('unsafe');document.querySelector('#leaf').hidden=false">Other group</button>
              <button role='menuitem' aria-haspopup='true' aria-expanded='false'
                onclick="clicked.push('system');document.querySelector('#leaf').hidden=false">systemSystemchevron_up</button>
              <a id='leaf' hidden href='/?lp=mSave' onclick="event.preventDefault();clicked.push('leaf')">Backup</a>
            """)
            assert await audit.click_navigation(page, "mSave", "https://router.local")
            assert await page.evaluate("window.clicked") == ["system", "leaf"]
        finally:
            await browser.close()

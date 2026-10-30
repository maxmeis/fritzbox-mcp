"""Read-only inventory of reachable FRITZ!Box UI controls and advertised TR-064 actions.

This is evidence of discovery, not a write-capability or settings-parity test.
Only allowlisted page anchors and verified navigation groups are followed. No
settings controls, form submissions, or configuration API actions are invoked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter, deque
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from fritzbox_mcp.router import RouterClient
from fritzbox_mcp.ui import BrowserUI, UIError

_ROUTE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_EXCLUDED_ROUTES = {"ssoPass", "ssoSet", "authSet"}
_SAFE_MENU_GROUPS = {
    "globeInternetchevron_up",
    "phoneTelefoniechevron_up",
    "monitor_stackHeimnetzchevron_up",
    "wifiWLANchevron_up",
    "smarthomeSmart Homechevron_up",
    "tv_playDVB-Cchevron_up",
    "diagnoseDiagnosechevron_up",
    "systemSystemchevron_up",
}
_KNOWN_PAGES = {
    "overview",
    "mNetMoni",
    "mCred",
    "mobile",
    "filter",
    "shares",
    "mfSet",
    "docsis",
    "calls",
    "tam",
    "tBook",
    "tAlarm",
    "mFax",
    "callHan",
    "telDevices",
    "myNum",
    "dect",
    "meshNet",
    "net",
    "usbDev",
    "mSrv",
    "boxname",
    "wSet",
    "chan",
    "wSecu",
    "wTime",
    "wGuest",
    "sh_dev",
    "sh_organize",
    "sh_control",
    "dvbTv",
    "funcCheck",
    "secCheck",
    "log",
    "eMon",
    "mPush",
    "mUser",
    "mfn",
    "regi",
    "mSave",
    "mUp",
    "sitemap",
    "liveTv",
}
_ACTION_ROUTE = re.compile(
    r"reset|reboot|restart|factory|logout|diagnos.*start|import|apply|delete|"
    r"upload|flash|firmware.*start|^wiz",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"password|passwort|secret|token|\bsid\b|\bpin\b|\bpsk\b|(?:^|[-_ ])key(?:$|[-_ ])",
    re.IGNORECASE,
)
_POSSIBLE_SECRET = re.compile(r"schlüssel|schluessel", re.IGNORECASE)
_DANGEROUS = re.compile(
    r"logout|log\s*out|abmelden|reboot|neustart|reset|werkseinstellung|factory|"
    r"diagnos.*start|diagnostic.*start|assistent|wizard|einricht|setup|start|aktivier|activate|"
    r"speichern|save|übernehmen|apply|löschen|delete|firmware\s*update|update\s*start",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(
    r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}|\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b[0-9a-f]{16,}\b|[?&](?:sid|token|key)=|\b(?:sid|token|password|passwort)\s*[:=]",
    re.IGNORECASE,
)


def safe_label(value: str) -> str | None:
    """Keep short UI labels only; never output device identifiers or values."""
    label = re.sub(r"\s+", " ", value).strip()
    if (
        not label
        or len(label) > 100
        or _IDENTIFIER.search(label)
        or re.search(r"[:=]\s*\S+", label)
    ):
        return None
    return label


def route_id(href: str, origin: str) -> str | None:
    """Accept simple router page navigation, discarding the session-bearing URL."""
    target = urlsplit(urljoin(origin + "/", href))
    base = urlsplit(origin)
    if (target.scheme, target.hostname, target.port) != (
        base.scheme,
        base.hostname,
        base.port,
    ):
        return None
    if target.path not in ("/", "/index.lua", "/index.html") or target.fragment:
        return None
    query = parse_qs(target.query, keep_blank_values=True)
    if set(query) - {"sid", "lp", "page"}:
        return None
    routes = query.get("lp", []) + query.get("page", [])
    if len(routes) != 1 or not _ROUTE.fullmatch(routes[0]):
        return None
    return routes[0]


def navigation_allowed(label: str, href: str, origin: str) -> bool:
    rid = route_id(href, origin)
    return bool(
        safe_label(label)
        and not _DANGEROUS.search(label)
        and rid
        and rid not in _EXCLUDED_ROUTES
        and not _ACTION_ROUTE.search(rid)
        and rid in _KNOWN_PAGES
    )


async def click_navigation(page, route: str, origin: str) -> bool:
    """Use the router's navigation handler so its private session is retained."""
    if route not in _KNOWN_PAGES:
        return False

    async def visible_anchor():
        for anchor in await page.locator("a[href]").element_handles():
            href = await anchor.get_attribute("href") or ""
            if route_id(href, origin) != route:
                continue
            label = safe_label(await anchor.text_content() or "")
            if not label or not navigation_allowed(label, href, origin):
                continue
            if await anchor.evaluate(
                "el => !!el.closest('form,table,[role=table],[role=grid]')"
            ):
                continue
            if await anchor.is_visible():
                return anchor
        return None

    anchor = await visible_anchor()
    if anchor is None:
        for button in await page.locator(
            'button[role="menuitem"][aria-haspopup="true"][aria-expanded="false"]'
        ).element_handles():
            label = safe_label(
                await button.get_attribute("aria-label")
                or await button.text_content()
                or ""
            )
            if label not in _SAFE_MENU_GROUPS or not await button.is_visible():
                continue
            await button.click(timeout=2000)
            anchor = await visible_anchor()
            if anchor is not None:
                break
    if anchor is not None:
        await anchor.click(timeout=5000)
        return True
    return False


async def settle_page(page) -> None:
    await page.wait_for_load_state("load", timeout=15000)
    with suppress(Exception):  # Polling statistics can prevent network idle.
        await page.wait_for_load_state("networkidle", timeout=4000)
    await asyncio.sleep(1)


_DOM_INVENTORY = r"""() => {
  const all = selector => {
    const roots = [document];
    for (let i=0; i<roots.length; i++) {
      for (const el of roots[i].querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
    }
    return roots.flatMap(root => [...root.querySelectorAll(selector)]);
  };
  const visible = el => {
    if (el.closest('[hidden],[aria-hidden="true"],script,style,template')) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && !!el.getClientRects().length;
  };
  const name = el => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/);
    return (el.getAttribute('aria-label') || ids.map(id => document.getElementById(id)?.textContent || '').join(' ') ||
      [...(el.labels || [])].map(x => x.textContent).join(' ') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || (el.matches('button,[role="button"]') ? el.textContent : '') || '').replace(/\s+/g,' ').trim().slice(0,120);
  };
  const selector = 'input,select,textarea,button,[role="button"],[role="checkbox"],[role="radio"],[role="switch"],[role="combobox"],[role="slider"],[role="spinbutton"],[role="textbox"]';
  const controls = all(selector).map(el => ({
    tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
    role: el.getAttribute('role') || '', name: name(el), visible: visible(el),
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
    matched: !!el.getAttribute('data-fritz-mcp-ref'), shadow_dom: el.getRootNode() !== document,
    in_navigation: !!el.closest('nav,aside,header,[role="navigation"]'),
    in_form: !!el.closest('form,fieldset'), in_table: !!el.closest('table,[role="table"],[role="grid"]'),
    ancestor: (() => { let n=el, a=[]; while(n && n !== document.body) { a.push([n.id,n.className,n.getAttribute('name'),n.getAttribute('data-testid')].join(' ')); n=n.parentElement; } return a.join(' ').slice(0,500); })()
  }));
  const anchors = all('a[href]')
    .filter(el => !el.closest('form,table,[role="table"],[role="grid"]'))
    .map(el => ({label: name(el) || (el.textContent || '').replace(/\s+/g,' ').trim().slice(0,100), href: el.href,
      visible: visible(el),
      navigation: !!el.closest('nav,aside,header,[role="navigation"],[role="menu"],[role="tab"],.tabs,.tab-menu') || el.getAttribute('role') === 'menuitem' || el.getAttribute('role') === 'tab',
      download: el.hasAttribute('download')}));
  const download_links = all('a[download],a[href$=".csv"],a[href$=".pdf"],a[href$=".zip"],a[href$=".xml"]')
    .filter(visible).length;
  const heading = all('h1,h2').some(visible);
  const forms = all('form').filter(visible).length;
  const content_controls = controls.filter(el => el.visible && !el.in_navigation).length;
  const content = document.querySelector('main,[role="main"],#main,#content,#page-content');
  const content_text = !!content && visible(content) && (content.innerText || '').trim().length > 20;
  return {title: (document.querySelector('h1')?.textContent || document.title || '').replace(/\s+/g,' ').trim().slice(0,100), controls, anchors, download_links,
    render_evidence: {form_count: forms, heading_present: heading, content_text_present: content_text, non_navigation_control_count: content_controls}};
}"""


def classify_control(item: dict) -> str:
    if item["type"] == "hidden" or not item["visible"]:
        return "hidden"
    if item["type"] == "password" or _SECRET.search(
        item["name"] + " " + item["ancestor"]
    ):
        return "secret"
    if _POSSIBLE_SECRET.search(item["name"] + " " + item["ancestor"]):
        return "possible_sensitive_keyword"
    if item["in_table"]:
        return "table_or_device_row"
    if item["type"] == "file":
        return "file_upload"
    if (
        item["tag"] == "button"
        or item["role"] == "button"
        or item["type"] in {"button", "submit", "reset"}
    ):
        return "action_button"
    if item["role"] in {"slider", "spinbutton"}:
        return "custom_widget"
    return (
        "setting_control"
        if item["in_form"] or item["tag"] in {"input", "select", "textarea"}
        else "custom_widget"
    )


def summarize_controls(raw: list[dict]) -> dict:
    counts = Counter()
    settings = []
    for item in raw:
        category = classify_control(item)
        counts[category] += 1
        if category in {"setting_control", "custom_widget", "file_upload"}:
            label = safe_label(item["name"])
            if not label:
                counts["unlabeled_or_identifier_name"] += 1
            settings.append(
                {
                    "name": label,
                    "tag": item["tag"],
                    "type": item["type"],
                    "role": item["role"],
                    "disabled": item["disabled"],
                    "snapshot_matched": item["matched"],
                    "category": category,
                }
            )
    return {
        "counts": dict(sorted(counts.items())),
        "settings": settings,
        "visible_matched": sum(bool(x["matched"]) for x in raw if x["visible"]),
        "visible_unmatched": sum(not x["matched"] for x in raw if x["visible"]),
        "open_shadow_controls": sum(bool(x.get("shadow_dom")) for x in raw),
        "open_shadow_unmatched": sum(
            bool(x.get("shadow_dom")) and x["visible"] and not x["matched"] for x in raw
        ),
    }


async def run_audit(max_pages: int, section: str | None, include_api: bool) -> dict:
    ui = BrowserUI()
    report = {
        "generated_utc": datetime.now(UTC).isoformat(),
        "method": "BrowserUI authenticated snapshots plus read-only DOM inventory; allowlisted page anchors only (router navigation handlers retain its private session); RouterClient.schema() metadata only",
        "interpretation": "Discovery evidence only. A matched control does not prove that changing or saving a setting works.",
        "limits": [
            "Snapshot is capped at 150 controls and a 7000-character control budget.",
            "Sensitive controls, device rows, and route exclusions are not write-tested.",
            "Only verified navigation groups and allowlisted page links are clicked; no settings forms or configuration actions are executed.",
        ],
        "pages": [],
        "navigation": [],
        "errors": [],
    }
    if include_api:
        client = RouterClient()
        try:
            report["tr064_advertised_actions"] = client.schema()
        except Exception:  # noqa: BLE001 - library exceptions may contain endpoints or credentials
            report["errors"].append(
                {"stage": "tr064_schema", "reason": "Schema retrieval failed"}
            )
        finally:
            client.close()

    queue = deque([("overview", None, "Overview")])
    seen = set()
    try:
        await (
            ui.snapshot()
        )  # Authenticate through BrowserUI before inspecting its page.
        sid = parse_qs(urlsplit(ui._page.url).query).get("sid", [""])[0]
        if re.fullmatch(r"[0-9a-fA-F]{16}", sid) and sid != "0" * 16:
            queue.append(
                ("sitemap", f"{ui.origin}/?lp=sitemap&sid={sid}", "Hilfe und Info")
            )
        while queue and len(seen) < max_pages:
            key, href, group = queue.popleft()
            if key in seen:
                continue
            seen.add(key)
            try:
                if href:
                    page = ui._page
                    assert page is not None and ui._same_origin(href)
                    if not await click_navigation(page, key, ui.origin):
                        if not await click_navigation(page, "sitemap", ui.origin):
                            raise RuntimeError("Safe navigation anchor unavailable")
                        await settle_page(page)
                        if not await click_navigation(page, key, ui.origin):
                            raise RuntimeError("Safe navigation anchor unavailable")
                    ui._assert_origin()
                page = ui._page
                assert page is not None
                await settle_page(page)
                await ui.snapshot()
                page = ui._page
                assert page is not None
                data = await page.evaluate(_DOM_INVENTORY)
                title = safe_label(data["title"])
                if title and _SECRET.search(title):
                    title = None
                report["pages"].append(
                    {
                        "route": key,
                        "group": group,
                        "title": title,
                        "download_links": data["download_links"],
                        "render_evidence": data["render_evidence"],
                        **summarize_controls(data["controls"]),
                        "status": "suspected_headers_only"
                        if not data["render_evidence"]["heading_present"]
                        and not data["render_evidence"]["form_count"]
                        and not data["render_evidence"]["non_navigation_control_count"]
                        else "visited",
                    }
                )
                for anchor in data["anchors"]:
                    label = safe_label(anchor["label"])
                    href = anchor["href"]
                    rid = route_id(href, ui.origin)
                    if not rid:
                        continue
                    if not label or _SECRET.search(label):
                        report["navigation"].append(
                            {
                                "route": rid,
                                "label": None,
                                "status": "excluded_sensitive_label",
                            }
                        )
                        continue
                    if anchor["download"]:
                        report["navigation"].append(
                            {
                                "route": rid,
                                "label": label,
                                "status": "excluded_download",
                            }
                        )
                    elif rid not in _KNOWN_PAGES and not _ACTION_ROUTE.search(rid):
                        report["navigation"].append(
                            {
                                "route": rid,
                                "label": label,
                                "status": "discovered_not_visited",
                            }
                        )
                    elif not navigation_allowed(label, href, ui.origin):
                        report["navigation"].append(
                            {
                                "route": rid,
                                "label": label,
                                "status": "excluded_action_like",
                            }
                        )
                    elif (
                        section is None
                        or section.casefold() in (label + " " + rid).casefold()
                    ):
                        report["navigation"].append(
                            {
                                "route": rid,
                                "label": label,
                                "status": "queued",
                                "link_visible": anchor["visible"],
                            }
                        )
                        if rid not in seen:
                            queue.append((rid, href, label))
            except Exception as error:  # noqa: BLE001 - suppress raw session URLs and continue
                report["pages"].append(
                    {"route": key, "group": group, "status": "error"}
                )
                report["errors"].append(
                    {
                        "stage": "page",
                        "route": key,
                        "reason": "Navigation or inventory failed",
                        "error_type": type(error).__name__,
                        "safe_reason": str(error)
                        if isinstance(error, UIError)
                        else "Inventory step failed",
                    }
                )
        report["remaining_queued"] = len(queue)
        report["max_pages_reached"] = bool(queue and len(seen) >= max_pages)
    finally:
        await ui.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("audit/coverage-inventory.json")
    )
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--section", help="Filter discovered navigation route or label")
    parser.add_argument("--no-api", action="store_true")
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    result = asyncio.run(run_audit(args.max_pages, args.section, not args.no_api))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Visited {sum(p['status'] == 'visited' for p in result['pages'])} pages; "
        f"{len(result['errors'])} errors; inventory written to {args.output}"
    )


if __name__ == "__main__":
    main()

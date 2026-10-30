"""Build a standalone, evidence-based FRITZ!Box coverage report.

The input is the read-only inventory created by ``audit_coverage.py``. The
result describes route and control discovery; it does not infer functional
parity or write capability from controls that happen to be visible.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORDINARY = {"setting_control", "custom_widget"}
SENSITIVE = {"secret", "possible_sensitive_keyword"}


def safe_json(value: Any) -> str:
    """Serialize data safely for an inline script element."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def device_label(inventory: dict[str, Any]) -> str:
    """Return only device details explicitly supplied by this inventory."""
    metadata = inventory.get("metadata") or {}
    device = inventory.get("device") or metadata.get("device") or metadata
    if isinstance(device, str):
        return device.strip() or "Configured FRITZ!Box"
    if isinstance(device, dict):
        parts = [
            str(device[key]).strip()
            for key in ("model", "firmware")
            if device.get(key) and str(device[key]).strip()
        ]
        return " · ".join(parts) if parts else "Configured FRITZ!Box"
    return "Configured FRITZ!Box"


def summarize(inventory: dict[str, Any]) -> dict[str, Any]:
    pages = inventory.get("pages") or []
    navigation = inventory.get("navigation") or []
    advertised = inventory.get("tr064_advertised_actions") or {}
    discovered: dict[str, str] = {}
    for item in navigation:
        route = item.get("route")
        status = item.get("status")
        if not route or status not in {
            "queued",
            "discovered_not_visited",
            "visited",
            "error",
        }:
            continue
        previous = discovered.get(route)
        # Duplicate anchors are common. Keep the furthest useful state seen.
        priority = {"queued": 0, "discovered_not_visited": 1, "error": 2, "visited": 3}
        if previous is None or priority[status] > priority[previous]:
            discovered[route] = status
    page_status = {p.get("route"): p.get("status") for p in pages if p.get("route")}
    for route, status in page_status.items():
        observed = status if status in {"visited", "error"} else "render_unverified"
        if route not in discovered:
            discovered[route] = observed
        elif status == "visited":
            discovered[route] = "visited"
        elif status == "error" and discovered[route] != "visited":
            discovered[route] = "error"
        elif observed == "render_unverified" and discovered[route] != "visited":
            discovered[route] = observed

    route_counts = Counter(discovered.values())
    ordinary = Counter()
    sensitive = Counter()
    files = Counter()
    page_rows: list[dict[str, Any]] = []
    categories = Counter()
    for page in pages:
        controls = page.get("settings") or []
        ordinary_items = [x for x in controls if x.get("category") in ORDINARY]
        file_items = [x for x in controls if x.get("category") == "file_upload"]
        counts = page.get("counts") or {}
        # Sensitive controls are deliberately not listed individually.
        sensitive_count = sum(as_int(counts.get(c)) for c in SENSITIVE)
        matched = sum(x.get("snapshot_matched") is True for x in ordinary_items)
        unknown_match = sum(
            x.get("snapshot_matched") not in (True, False) for x in ordinary_items
        )
        ordinary.update(
            inspected=len(ordinary_items),
            matched=matched,
            unmatched=len(ordinary_items) - matched - unknown_match,
            unknown=unknown_match,
        )
        files.update(
            inspected=len(file_items),
            matched=sum(x.get("snapshot_matched") is True for x in file_items),
        )
        sensitive.update(excluded=sensitive_count)
        categories.update(counts)
        page_rows.append(
            {
                "route": page.get("route", ""),
                "group": page.get("group", ""),
                "title": page.get("title", ""),
                "status": page.get("status", "unknown"),
                "ordinary_inspected": len(ordinary_items),
                "ordinary_matched": matched,
                "ordinary_unmatched": len(ordinary_items) - matched - unknown_match,
                "ordinary_unknown": unknown_match,
                "file_controls": len(file_items),
                "file_snapshot_matched": sum(
                    x.get("snapshot_matched") is True for x in file_items
                ),
                "sensitive_excluded": sensitive_count,
                "visible_matched": as_int(page.get("visible_matched")),
                "visible_unmatched": as_int(page.get("visible_unmatched")),
                "download_links": as_int(page.get("download_links")),
            }
        )

    service_count = len(advertised) if isinstance(advertised, dict) else 0
    action_count = (
        sum(len(actions) for actions in advertised.values())
        if isinstance(advertised, dict)
        else 0
    )
    visited = sum(p.get("status") == "visited" for p in pages)
    errors = sum(p.get("status") == "error" for p in pages)
    excluded_action_routes = len(
        {
            x.get("route")
            for x in navigation
            if x.get("route") and x.get("status") == "excluded_action_like"
        }
    )
    excluded_sensitive_routes = len(
        {
            x.get("route")
            for x in navigation
            if x.get("route")
            and x.get("status") in {"excluded_sensitive_label", "excluded_download"}
        }
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "inventory_generated_utc": inventory.get("generated_utc"),
        "device": device_label(inventory),
        "routes": {
            "discovered_distinct": len(discovered),
            "visited_pages": visited,
            "page_errors": errors,
            "not_inspected": route_counts.get("queued", 0)
            + route_counts.get("discovered_not_visited", 0)
            + route_counts.get("render_unverified", 0),
            "excluded_action_like_distinct": excluded_action_routes,
            "excluded_sensitive_or_download_distinct": excluded_sensitive_routes,
            "state_counts": dict(route_counts),
        },
        "controls": {
            "ordinary_controls_measured": ordinary["inspected"] > 0,
            "ordinary_inspected": ordinary["inspected"],
            "ordinary_snapshot_matched": ordinary["matched"],
            "ordinary_unmatched": ordinary["unmatched"],
            "ordinary_match_state_unknown": ordinary["unknown"],
            "file_controls_separate": files["inspected"],
            "file_snapshot_matched": files["matched"],
            "sensitive_excluded": sensitive["excluded"],
            "all_category_counts": dict(categories),
        },
        "api_metadata": {
            "services": service_count,
            "service_action_pairs": action_count,
            "generic_call_action_catalog_reachability_is_not_test_proof": True,
        },
        "tools": {
            "total": 26,
            "api": 18,
            "api_generic": 3,
            "api_dedicated": 15,
            "ui": 8,
            "is_sample_metadata": True,
        },
        "live_evidence": inventory.get("live_evidence")
        or ["Current inventory discovery only; no live workflow evidence supplied."],
        "scope_limits": [
            "Control discovery is not end-to-end settings coverage.",
            "Navigation errors and uninspected routes are audit limitations, not proof that those features are unsupported.",
            "Overall meaningful functionality percentage is unknown. API schema entries and generic call_action reachability do not prove test coverage.",
            "Current UI surface has no upload/download management, dialogs/popups/frames, keyboard or drag primitive. File controls are reported separately.",
            "Sensitive controls are excluded from control-match rates. Broad German 'Schlüssel' filtering can hide DNS-over-TLS controls.",
            "Snapshots cap at 150 controls and 7,000 characters; selects cap at 30 options. Some shadow controls may remain unexposed.",
        ],
        "page_rows": page_rows,
        "inventory_limits": inventory.get("limits", []),
        "inventory_method": inventory.get("method", ""),
        "inventory_interpretation": inventory.get("interpretation", ""),
        "inventory_errors": inventory.get("errors", []),
    }


def build_html(summary: dict[str, Any]) -> str:
    data = safe_json(summary)
    generated = html.escape(
        str(summary.get("inventory_generated_utc") or "not recorded")
    )
    template = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Router coverage report</title><style>
:root{{--foreground:var(--foreground,CanvasText);--muted:var(--muted-foreground,var(--muted,GrayText));--card:var(--card,Canvas);--border:var(--border,GrayText);--primary:var(--primary,#2563eb);--chart-1:var(--chart-1,#22a07b);--chart-2:var(--chart-2,#e7a33e);--chart-3:var(--chart-3,#d66d65);--font-sans:var(--font-sans,Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif)}
*{{box-sizing:border-box}}body{{margin:0;background:transparent;color:var(--foreground,CanvasText);font:14px/1.45 var(--font-sans,Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif)}}main{{max-width:1120px;margin:0 auto;padding:0}}h1{{font-size:21px;letter-spacing:-.025em;margin:0 0 4px}}h2{{font-size:15px;margin:0 0 12px}}h3{{font-size:13px;margin:0}}p{{margin:0}}.muted{{color:var(--muted-foreground,var(--muted,GrayText))}}.head{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin:0 2px 14px}}.head p{{font-size:12px}}.badge{{display:inline-flex;align-items:center;border:1px solid var(--border,GrayText);border-radius:999px;padding:4px 9px;color:var(--muted-foreground,var(--muted,GrayText));font-size:11px;white-space:nowrap}}.grid{{display:grid;gap:10px}}.metrics{{grid-template-columns:repeat(4,minmax(0,1fr));margin-bottom:10px}}.card{{background:var(--card,Canvas);border:1px solid var(--border,GrayText);border-radius:12px;padding:13px 14px;min-width:0}}.metric .label{{color:var(--muted-foreground,var(--muted,GrayText));font-size:11px}}.metric strong{{display:block;font-size:23px;line-height:1.2;margin:4px 0 2px;letter-spacing:-.03em}}.metric small{{font-size:11px;color:var(--muted-foreground,var(--muted,GrayText))}}.two{{grid-template-columns:1fr 1fr;margin-bottom:10px}}.chart{{height:12px;display:flex;border-radius:99px;overflow:hidden;background:var(--secondary,ButtonFace);margin:12px 0 7px}}.chart span{{height:100%;min-width:0}}.legend{{display:flex;flex-wrap:wrap;gap:7px 14px;color:var(--muted-foreground,var(--muted,GrayText));font-size:11px}}.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}}.summaryline{{font-size:12px;color:var(--muted-foreground,var(--muted,GrayText))}}.warning{{border-color:var(--warning,Highlight);background:color-mix(in srgb,var(--card,Canvas),var(--warning-surface,Canvas) 35%)}}.warning strong{{font-size:13px}}.live-list,.limits{{display:grid;gap:6px;font-size:12px;color:var(--muted-foreground,var(--muted,GrayText))}}.live-list div:before{{content:"✓";color:var(--chart-1,#22a07b);font-weight:700;margin-right:7px}}.limits div:before{{content:"•";color:var(--chart-2,#e7a33e);font-weight:700;margin-right:7px}}.tag{{border:1px solid var(--border,GrayText);border-radius:7px;padding:3px 7px;font-size:10px;color:var(--muted-foreground,var(--muted,GrayText))}}.api-row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}}.api-row strong{{font-size:14px}}.toolbar{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}}input,select,button{{font:inherit;color:var(--foreground,CanvasText);background:var(--card,Canvas);border:1px solid var(--border,GrayText);border-radius:8px;padding:7px 9px}}input{{min-width:220px;flex:1}}button{{cursor:pointer}}button:hover{{border-color:var(--primary,#2563eb)}}.table-wrap{{overflow:auto;border:1px solid var(--border,GrayText);border-radius:10px}}table{{border-collapse:collapse;width:100%;min-width:760px;font-size:12px}}th,td{{padding:8px 9px;text-align:left;border-bottom:1px solid var(--border,GrayText);white-space:nowrap}}th{{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted-foreground,var(--muted,GrayText));background:color-mix(in srgb,var(--card,Canvas),var(--secondary,ButtonFace) 40%)}}tbody tr:last-child td{{border-bottom:0}}.count{{font-variant-numeric:tabular-nums}}.state{{border-radius:999px;padding:2px 7px;font-size:10px;background:var(--success-surface,ButtonFace);color:var(--success,CanvasText)}}.state.error{{background:var(--destructive-surface,ButtonFace);color:var(--destructive,CanvasText)}}.state.unknown{{background:var(--secondary,ButtonFace);color:var(--muted-foreground,var(--muted,GrayText))}}.pager{{display:flex;align-items:center;justify-content:space-between;margin-top:9px;font-size:11px;color:var(--muted-foreground,var(--muted,GrayText))}}.footer{{font-size:10px;color:var(--muted-foreground,var(--muted,GrayText));margin:10px 2px}}a{{color:var(--primary,#2563eb)}}@media(max-width:700px){{main{{padding:0}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}.two{{grid-template-columns:1fr}}.head{{display:block}}.badge{{margin-top:8px}}.metric strong{{font-size:20px}}}}
</style></head><body><main>
<p class="summaryline" style="margin-bottom:12px"><span id="device"></span> · read-only audit</p>
<section class="grid metrics" id="metrics"></section>
<section class="grid two"><article class="card"><h2>What the inventory establishes</h2><div id="routeChart"></div><div class="api-row" id="sampleMetadata"></div><p class="summaryline" style="margin-top:9px">Navigation errors and uninspected routes are audit limitations, not proof of unsupported functionality.</p><p class="summaryline" style="margin-top:9px">API metadata catalog: <strong id="apiCounts"></strong>. Catalog reachability through generic call_action is not test proof.</p><p class="summaryline" style="margin-top:9px">Project check results vary by run; run project checks locally.</p></article>
<article class="card warning"><strong>Control discovery is not end-to-end settings coverage.</strong><p class="summaryline" style="margin-top:5px">Overall meaningful functionality percentage: <b>unknown</b>. A visible or snapshot-matched control does not prove that a setting can be saved or read back.</p><div class="live-list" id="liveEvidence" style="margin-top:10px"></div><p class="summaryline" style="margin-top:9px">This audit performs no live writes; no settings are changed.</p></article></section>
<section class="grid two"><article class="card"><h2>Ordinary setting controls</h2><div id="controlChart"></div><p class="summaryline">Denominator: inspected setting/custom controls only. File controls and sensitive controls are separate; action buttons and navigation are excluded.</p></article><article class="card"><h2>Known discovery limits</h2><div class="limits"><div>No upload/download management, dialogs, popups, frames, keyboard or drag primitive.</div><div>Secret filtering may hide DNS-over-TLS controls via broad German “Schlüssel” matching.</div><div>Snapshot limits: 150 controls, 7,000 characters, and 30 options per select; some shadow controls may remain unexposed.</div></div></article></section>
<section class="card"><div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:11px"><div><h2 style="margin-bottom:3px">Inspected pages</h2><p class="summaryline">Matched means a DOM control had a snapshot reference. It is not an interaction or save result.</p></div><a href="coverage-inventory.json" class="badge">Source inventory ↗</a></div><div class="toolbar"><input id="search" type="search" placeholder="Search page or route"><select id="stateFilter"><option value="">All page states</option><option value="visited">Visited</option><option value="error">Error</option><option value="other">Other</option></select><select id="missingFilter"><option value="">Any control match state</option><option value="missing">Has unmatched ordinary controls</option><option value="none">No unmatched ordinary controls</option></select><select id="pageSize"><option selected>10</option><option>20</option><option>50</option><option>All</option></select></div><div class="table-wrap"><table><thead><tr><th>Area / route</th><th>State</th><th>Ordinary controls matched / inspected</th><th>Unmatched</th><th>File controls†</th><th>Sensitive excluded</th></tr></thead><tbody id="rows"></tbody></table></div><div class="pager"><span id="pageInfo"></span><span><button id="prev">Previous</button> <button id="next">Next</button></span></div><p class="footer">† File controls remain unsupported UI workflows even when snapshot matched. Sensitive controls are excluded, not counted as functional failures. Counts are DOM observations, not unique settings or tested workflows.</p></section>
<footer class="footer">Audit scope: route/control discovery and advertised API schema metadata. Functional parity requires a frozen model/firmware/account workflow matrix across read, set, add, delete, import/export, confirmation, and read-back; manual steps must be marked. Physical confirmation may require the user.</footer>
</main><script>const D=__REPORT_DATA__;
const $=s=>document.querySelector(s), fmt=n=>Number(n||0).toLocaleString();
$('#device').textContent=D.device;
$('#sampleMetadata').innerHTML=`<span class="tag">${{fmt(D.tools.total)}} tools · sample metadata</span><span class="tag">${{fmt(D.tools.api)}} API · ${{fmt(D.tools.ui)}} UI</span>`;
$('#apiCounts').textContent=`${{fmt(D.api_metadata.services)}} services and ${{fmt(D.api_metadata.service_action_pairs)}} service/action pairs`;
$('#liveEvidence').innerHTML=(D.live_evidence.length?D.live_evidence:['Current inventory discovery only; no live workflow evidence supplied.']).map(x=>`<div>${{esc(x)}}</div>`).join('');
const metric=(label,value,note)=>`<article class="card metric"><div class="label">${{label}}</div><strong>${{value}}</strong><small>${{note}}</small></article>`;
$('#metrics').innerHTML=[metric('Distinct discovered routes',fmt(D.routes.discovered_distinct),`${{fmt(D.routes.visited_pages)}} visited pages · ${{fmt(D.routes.page_errors)}} errors`),metric('Ordinary controls inspected',D.controls.ordinary_controls_measured?fmt(D.controls.ordinary_inspected):'Not measured','setting controls + custom widgets'),metric('Snapshot matched',D.controls.ordinary_controls_measured?fmt(D.controls.ordinary_snapshot_matched):'Not measured',D.controls.ordinary_controls_measured?`${{fmt(D.controls.ordinary_unmatched)}} unmatched · ${{fmt(D.controls.ordinary_match_state_unknown)}} unknown`:'no eligible control denominator'),metric('Verified live write workflows','0','no settings changed')].join('');
function bar(el,items,total){{const colors=['var(--chart-1,#22a07b)','var(--chart-2,#e7a33e)','var(--chart-3,#d66d65)'];const val=items.reduce((a,x)=>a+x.n,0);el.innerHTML=val?`<div class="chart" role="img" aria-label="${{items.map(x=>x.label+': '+x.n).join(', ')}}">${{items.map((x,i)=>`<span style="width:${{100*x.n/val}}%;background:${{colors[i]}}"></span>`).join('')}}</div><div class="legend">${{items.map((x,i)=>`<span><i class="dot" style="background:${{colors[i]}}"></i>${{x.label}} <b>${{fmt(x.n)}}</b></span>`).join('')}}</div>`:`<p class="summaryline">Not measured — no eligible controls were captured.</p>`}}
bar($('#routeChart'),[{{label:'Visited',n:D.routes.visited_pages}},{{label:'Not inspected',n:D.routes.not_inspected}},{{label:'Errors',n:D.routes.page_errors}}],D.routes.discovered_distinct);
bar($('#controlChart'),[{{label:'Snapshot matched',n:D.controls.ordinary_snapshot_matched}},{{label:'Unmatched',n:D.controls.ordinary_unmatched}},{{label:'Match state unknown',n:D.controls.ordinary_match_state_unknown}}],D.controls.ordinary_inspected);
let page=0,filtered=[];function render(){{const q=$('#search').value.toLocaleLowerCase(),state=$('#stateFilter').value,missing=$('#missingFilter').value;filtered=D.page_rows.filter(x=>`${{x.group}} ${{x.route}} ${{x.title}}`.toLocaleLowerCase().includes(q)&&(!state||(state==='other'?!['visited','error'].includes(x.status):x.status===state))&&(!missing||(missing==='missing'?x.ordinary_unmatched>0:x.ordinary_unmatched===0)));const size=$('#pageSize').value==='All'?Math.max(1,filtered.length):Number($('#pageSize').value);const pages=Math.max(1,Math.ceil(filtered.length/size));page=Math.min(page,pages-1);const subset=filtered.slice(page*size,(page+1)*size);$('#rows').innerHTML=subset.map(x=>`<tr><td><b>${{esc(x.group||x.route)}}</b><br><span class="muted">${{esc(x.route)}}</span></td><td><span class="state ${{x.status==='error'?'error':x.status==='visited'?'':'unknown'}}">${{esc(x.status)}}</span></td><td class="count">${{x.ordinary_inspected?`${{fmt(x.ordinary_matched)}} / ${{fmt(x.ordinary_inspected)}}`: 'Not measured'}}${{x.ordinary_unknown?` <span class="muted">(+${{fmt(x.ordinary_unknown)}} ?)</span>`:''}}</td><td class="count">${{x.ordinary_inspected?fmt(x.ordinary_unmatched):'—'}}</td><td class="count">${{fmt(x.file_controls)}}${{x.file_snapshot_matched?` <span class="muted">(${{fmt(x.file_snapshot_matched)}} matched)</span>`:''}}</td><td class="count">${{fmt(x.sensitive_excluded)}}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">No pages match these filters.</td></tr>';$('#pageInfo').textContent=filtered.length?`${{page*size+1}}–${{Math.min((page+1)*size,filtered.length)}} of ${{filtered.length}} pages`:'0 pages';$('#prev').disabled=page===0;$('#next').disabled=page>=pages-1}}
function esc(s){{const e=document.createElement('span');e.textContent=s??'';return e.innerHTML}}
['search','stateFilter','missingFilter','pageSize'].forEach(id=>$('#'+id).addEventListener('input',()=>{{page=0;render()}}));$('#prev').onclick=()=>{{page--;render()}};$('#next').onclick=()=>{{page++;render()}};render();</script></body></html>"""
    template = template.replace("{{", "{").replace("}}", "}")
    return template.replace("__REPORT_DATA__", data).replace("__GENERATED__", generated)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "route",
        "group",
        "title",
        "status",
        "ordinary_inspected",
        "ordinary_matched",
        "ordinary_unmatched",
        "ordinary_unknown",
        "file_controls",
        "file_snapshot_matched",
        "sensitive_excluded",
        "visible_matched",
        "visible_unmatched",
        "download_links",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory", type=Path, default=ROOT / "audit/coverage-inventory.json"
    )
    parser.add_argument("--html", type=Path, default=ROOT / "audit/coverage.html")
    parser.add_argument("--csv", type=Path, default=ROOT / "audit/coverage-matrix.csv")
    parser.add_argument(
        "--summary", type=Path, default=ROOT / "audit/coverage-summary.json"
    )
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    summary = summarize(inventory)
    for path in (args.html, args.csv, args.summary):
        path.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(build_html(summary), encoding="utf-8")
    write_csv(args.csv, summary["page_rows"])
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {args.html}, {args.csv}, and {args.summary} from {args.inventory}")


if __name__ == "__main__":
    main()

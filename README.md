# FRITZ!Box MCP

A local MCP server for FRITZ!Box routers. It uses
[`fritzconnection`](https://github.com/kbr/fritzconnection) for TR-064 and
optionally Playwright for general browser interaction with the router UI.

## Capabilities and limits

- Discover advertised TR-064 services, actions, and argument types.
- Read router information, network devices, Wi-Fi settings, and DNS server
  information.
- Rename a device or change its internet access using dedicated tools.
- Enable or disable a Wi-Fi band.
- Start, check, or stop TR-064 second-factor authentication.
- Call other actions advertised by the router, including actions that may
  change configuration.
- Open the web UI, inspect controls, fill fields, select options, and submit
  forms.

The server has no separate read-only mode. Some tools can change router
settings, and the MCP client chooses when to call them. Use an account with
only the permissions you need and review requested operations before allowing
them.

The documented TR-064 interfaces support DNS reads, but do not provide the
setters needed to change all DNS configuration. Browser tools are general
controls, not a firmware-specific DNS configuration workflow. DNS changes
through the UI and complete parity with every router setting have not been
verified. Available controls and behavior vary by model, firmware, account,
and UI layout.

## Requirements and setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Clone the public
repository and install the optional browser dependencies:

```sh
git clone https://github.com/maxmeis/fritzbox-mcp.git
cd fritzbox-mcp
uv sync --frozen --extra ui --group dev
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" uv run --extra ui playwright install chromium
```

For API tools only, omit `--extra ui` and the Chromium installation.

In the router, enable **Allow access for applications** under the home network
settings and use a FRITZ!Box account with the permissions you need. The
computer running the server must be able to reach the router on its local
network or VPN.

The server reads configuration from its process environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FRITZ_HOST` | `fritz.box` | Router hostname or IP address |
| `FRITZ_USERNAME` | library default | FRITZ!Box account name |
| `FRITZ_PASSWORD_FILE` | unset | Path to a UTF-8 password file |
| `FRITZ_PASSWORD` | unset | Alternative to the password file; set only one |
| `FRITZ_TIMEOUT` | `10` | API request timeout in seconds |
| `FRITZ_TLS` | `true` | Use TLS for TR-064 |
| `FRITZ_UI_URL` | `https://` + host | Web UI origin, including a custom port if needed |
| `PLAYWRIGHT_BROWSERS_PATH` | Playwright default | Browser path; use the absolute `.browsers` path from the install command for UI tools |

Keep a password file private (for example, `chmod 600 /path/to/password`). The
server does not automatically load `.env`; configure variables in the MCP
client or export them before launching it.

## Connect an MCP client

Copy `mcp.json.example` to `mcp.json` and edit the absolute paths for your
checkout and private password file. The example uses `uv` from `PATH`; if your
MCP client has a restricted `PATH`, set `command` to the absolute path of your
`uv` executable. The local `mcp.json` is ignored by git because it can contain
private paths or credentials. Add its server entry to your client's MCP
configuration if the client does not read this file directly.

The command starts a stdio MCP process and waits for an MCP client. You can
check the command without starting a session:

```sh
uv run --frozen --extra ui fritzbox-mcp --help
```

Start with `capabilities`, `router_info`, and `list_devices`. Example requests:

- “Rename the device with MAC address … to office-printer.”
- “Block internet access for 192.168.178.25.”
- “Open the router UI and inspect the configured DNS servers.”

## Operational and security notes

The API connection and browser are lazy. Listing tools requires no router
credentials or browser installation. API calls are serialized and mutations
are not automatically retried. Browser requests are restricted to the
configured router origin. `ui_close` closes its in-memory browser session, and
MCP shutdown also releases the browser and router connection.

The upstream TR-064 library and browser permit the router's self-signed TLS
certificate; TLS does not verify the router's identity. Keep this connection
on a trusted local network or VPN. Known credential fields and session-bearing
URLs are redacted from results, but arbitrary router responses may still
contain sensitive data. Device lists, network settings, and UI text are sent
to the MCP client.

## Validation

To check the MCP protocol with the
[MCP Inspector CLI](https://github.com/modelcontextprotocol/inspector/blob/main/clients/cli/README.md),
run from the project directory:

```sh
pnpm dlx @modelcontextprotocol/inspector --cli --config mcp.json --server fritzbox --method tools/list
```

Listing tools requires no credentials. For authenticated read checks, configure
`FRITZ_USERNAME` and `FRITZ_PASSWORD_FILE` in the launching environment or the
server entry's environment:

```sh
pnpm dlx @modelcontextprotocol/inspector --cli --config mcp.json --server fritzbox --method tools/call --tool-name router_info
pnpm dlx @modelcontextprotocol/inspector --cli --config mcp.json --server fritzbox --method tools/call --tool-name get_wan_dns
```

Run the automated tests and lint checks with:

```sh
uv run --frozen --extra ui --group dev pytest -q
uv run --frozen --group dev ruff check src tests
```

Tests use fake router responses and local browser fixtures. They do not change
a router. Read-only checks have been run on a FRITZ!Box 6660 Cable running
FRITZ!OS 8.25, including API authentication, device discovery, DNS reads, and
browser navigation to DNS settings. No live settings changes were made. This
is not a guarantee of compatibility with other models or firmware versions.

## Functionality coverage audit

Audit outputs are generated locally and are not included in the repository.
With credentials for a router, the inventory command below performs
read-only discovery and writes device-specific data under `audit/`. The
directory is ignored by git. Build the HTML report and its CSV route matrix
from that inventory with the second command:

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" uv run --extra ui python scripts/audit_coverage.py --max-pages 180
uv run python scripts/build_coverage_report.py
```

The report measures route and control discovery, not end-to-end settings
coverage. A visible control or advertised API action does not establish that a
setting can be changed, saved, and read back. The target for full coverage is
every applicable workflow on a frozen model/firmware/account configuration,
including import/export and confirmation steps. Workflows requiring a
physical button or user confirmation must be marked as requiring human
interaction. Current tests and control counts are not an overall functionality
percentage.

## Interface references

- [Official interface index](https://fritz.com/en/pages/interfaces)
- [Hosts: names and network devices](https://fritz.support/resources/TR-064_Hosts.pdf)
- [HostFilter: device internet access](https://fritz.support/resources/TR-064_Host_Filter.pdf)
- [LAN host configuration](https://fritz.support/resources/TR-064_LAN_Host_Config_Management.pdf)
- [Web session authentication and UI interface limitations](https://fritz.support/resources/HTTP_Session-ID_EN.pdf)

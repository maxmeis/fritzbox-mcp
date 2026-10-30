# FRITZ!Box MCP

A local MCP server for FRITZ!Box routers. It uses
[`fritzconnection`](https://github.com/kbr/fritzconnection) for TR-064 and
optionally Playwright for general browser interaction with the router UI.

## Launch announcement

Just published [fritzbox-mcp](https://github.com/maxmeis/fritzbox-mcp): FRITZ!Box
tools for your AI assistant.

Discover and rename devices, control Wi-Fi and device internet access, and
inspect DNS settings—all through MCP, using TR-064 and a browser fallback for
advanced settings.

63 tests passing. Public source. Early release, with full UI coverage still in
progress.

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

Follow these steps on macOS or Linux. The server runs on Python 3.11 or newer;
the commands below use Python 3.12 managed by [uv](https://docs.astral.sh/uv/).
You will also need Git and a computer that can reach the FRITZ!Box on the local
network or over VPN.

1. **Enable TR-064 and choose an account.** In the FRITZ!Box interface, enable
   **Allow access for applications** in the home network settings. Create or
   choose a FRITZ!Box user with **FRITZ!Box settings** permissions. Menu names
   and locations vary between firmware versions. Use only the permissions you
   need; some server tools can make changes.

2. **Clone the project and install its runtime.** Install uv using its
   [official installation guide](https://docs.astral.sh/uv/getting-started/installation/),
   then run:

```sh
git clone https://github.com/maxmeis/fritzbox-mcp.git
cd fritzbox-mcp
uv python install 3.12
uv sync --python 3.12 --frozen --extra ui --no-dev
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" uv run --no-dev --extra ui playwright install chromium
```

This installs the MCP server and browser tools without developer dependencies.
On Linux, if Chromium reports missing system libraries, install them with
`PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" uv run --no-dev --extra ui playwright install --with-deps chromium`.
Playwright's [browser installation guide](https://playwright.dev/python/docs/browsers)
describes supported OS packages; `--with-deps` may require administrator access.
For API tools only, omit `--extra ui` from every setup command and remove both
`"--extra"` and `"ui"` from the client configuration's `args`. Skip Chromium
installation and omit `PLAYWRIGHT_BROWSERS_PATH` from the client environment.

3. **Create a private password file.** This prompts without displaying the
   password, creates the file only if it does not already exist, and sets
   owner-only permissions on macOS/Linux. Run it from the project directory:

```sh
uv run --no-dev --frozen --extra ui python - <<'PY'
from getpass import getpass
import os
from pathlib import Path

directory = Path.home() / ".config" / "fritzbox-mcp"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory, 0o700)
path = directory / "password"
password = getpass("FRITZ!Box password: ")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as password_file:
    password_file.write(password)
print(f"Created {path}")
PY
```

   Keep the printed path for the client configuration. If the script says the
   file already exists, it leaves it untouched; choose a different path or
   handle that file deliberately. Do not put the password in a command, config
   file, or shell history.

4. **Set router and client paths.** Copy `mcp.json.example` to `mcp.json`, which
   is ignored by git, then replace every `/absolute/path/...` placeholder with
   an absolute path on your computer. For example, `pwd` prints the project
   path, `command -v uv` prints the uv executable path, and the password script
   printed its file path. The browser path is the project path followed by
   `/.browsers`. Set `FRITZ_HOST` to `fritz.box` or the router's IP address,
   `FRITZ_USERNAME` to your FRITZ!Box account name, and `FRITZ_UI_URL` to the
   router UI origin on the same host (for example `https://fritz.box`). If
   using an IP or custom port, make `FRITZ_UI_URL` match that host and port.

```sh
cp mcp.json.example mcp.json
```

5. **Connect your MCP client.** Add the `fritzbox` server entry from `mcp.json`
   to the client's MCP configuration, following that client's documentation,
   then restart or reload the client. The server uses stdio: it waits for MCP
   messages on standard input and output, so a process that appears to wait
   when launched by itself is expected. Keep standard output reserved for MCP
   communication.

6. **Check access with read-only tools first.** Ask the client to call
   `router_info`, `list_devices`, and `get_wan_dns`. These verify router
   authentication, device discovery, and DNS reads without changing settings.
   Review any later request that may change router configuration.

For UI tools, Chromium must be installed in the project `.browsers` directory
and the MCP environment must use that same absolute path. The project-local
browser installation keeps this setup independent of Playwright's default
browser cache.

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

The server does not automatically load `.env`; configure variables in the MCP
client or export them before launching it. You can check the CLI without
starting an MCP session:

```sh
uv run --no-dev --frozen --extra ui fritzbox-mcp --help
```

The optional [MCP Inspector CLI](https://github.com/modelcontextprotocol/inspector/blob/main/clients/cli/README.md)
can list tools or call read-only tools without a desktop MCP client. It requires
Node.js 22.19+ and pnpm:

```sh
pnpm dlx @modelcontextprotocol/inspector --cli --config mcp.json --server fritzbox --method tools/list
pnpm dlx @modelcontextprotocol/inspector --cli --config mcp.json --server fritzbox --method tools/call --tool-name router_info
pnpm dlx @modelcontextprotocol/inspector --cli --config mcp.json --server fritzbox --method tools/call --tool-name get_wan_dns
```

Tool listing does not contact the router. The two calls above require working
credentials in the server entry or its launching environment.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Client reports `uv` or `fritzbox-mcp` not found | Use the absolute uv path in `mcp.json`; keep the `uv run` arguments and project directory correct. |
| `fritz.box` does not resolve | Set `FRITZ_HOST` to the router's local IP and set `FRITZ_UI_URL` to the matching origin, such as `https://192.168.178.1`. |
| TR-064 connection or service errors | Enable **Allow access for applications**, confirm the computer can reach the router, and check that the router exposes the requested service. |
| Authentication fails or actions are unavailable | Check `FRITZ_USERNAME`, the password file path and contents, and the FRITZ!Box user's permissions. |
| UI tools cannot find Chromium | Install Chromium with `PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" uv run --no-dev --extra ui playwright install chromium`; use that exact absolute `.browsers` path in the client environment. |
| Chromium reports missing libraries on Linux | Run Playwright's `install --with-deps chromium` command from setup, or install the OS packages listed in the Playwright browser guide. |
| Works in a terminal, not in the MCP client | GUI clients may not inherit your shell environment. Put required variables in the server entry's `env`; `.env` files are not loaded automatically. |
| Server process stays open without visible output | This is expected for a stdio server waiting for its MCP client. Check the MCP client's logs and connection status. |

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

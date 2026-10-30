import pytest

from fritzbox_mcp.ui import BrowserUI, UIError, _configured_url


@pytest.mark.parametrize(
    "path",
    [
        "settings",
        "//outside.example/",
        "/settings?sid=abc",
        "/settings#part",
        "/\\\\outside.example",
    ],
)
@pytest.mark.asyncio
async def test_open_rejects_nonlocal_or_ambiguous_paths_without_starting_browser(
    path, monkeypatch
):
    monkeypatch.setenv("FRITZ_HOST", "router.local")
    monkeypatch.delenv("FRITZ_UI_URL", raising=False)
    ui = BrowserUI()
    with pytest.raises(UIError, match="router-relative path"):
        await ui.open(path)
    assert ui._playwright is None


def test_ui_url_must_be_same_router_origin_without_credentials_or_session_parts(
    monkeypatch,
):
    monkeypatch.setenv("FRITZ_HOST", "router.local")
    for configured in (
        "https://elsewhere.local",
        "https://user:pass@router.local",
        "https://router.local/path",
        "https://router.local/?sid=abc",
        "https://router.local/#fragment",
    ):
        monkeypatch.setenv("FRITZ_UI_URL", configured)
        with pytest.raises(UIError, match="configured router origin"):
            _configured_url()

    monkeypatch.setenv("FRITZ_UI_URL", "https://router.local:8443/")
    assert _configured_url() == "https://router.local:8443"


@pytest.mark.asyncio
async def test_browser_route_allows_only_the_configured_origin(monkeypatch):
    monkeypatch.setenv("FRITZ_HOST", "router.local")
    monkeypatch.setenv("FRITZ_UI_URL", "https://router.local:8443")
    ui = BrowserUI()

    class Request:
        def __init__(self, url):
            self.url = url

    class Route:
        def __init__(self, url):
            self.request = Request(url)
            self.action = None

        async def continue_(self):
            self.action = "continue"

        async def abort(self):
            self.action = "abort"

    same_origin = Route("https://router.local:8443/assets/app.js")
    wrong_port = Route("https://router.local/assets/app.js")
    external = Route("https://metrics.example/pixel")
    for route in (same_origin, wrong_port, external):
        await ui._route(route)
    assert [same_origin.action, wrong_port.action, external.action] == [
        "continue",
        "abort",
        "abort",
    ]


@pytest.mark.asyncio
async def test_dns_form_edit_save_snapshot_and_external_request_block(monkeypatch):
    """Exercise BrowserUI against a fulfilled local page without a router or server."""
    from playwright.async_api import Error as PlaywrightError

    monkeypatch.setenv("FRITZ_HOST", "router.local")
    monkeypatch.delenv("FRITZ_UI_URL", raising=False)
    monkeypatch.delenv("FRITZ_PASSWORD", raising=False)
    monkeypatch.delenv("FRITZ_PASSWORD_FILE", raising=False)

    html = """<!doctype html>
    <html><body>
      <h1>DNS settings</h1>
      <form id="dns">
        <label for="primary">Primary DNS server</label>
        <input id="primary" name="primary" value="192.0.2.53">
        <label for="secondary">Secondary DNS server</label>
        <input id="secondary" name="secondary" value="192.0.2.54">
        <input type="hidden" name="sid" value="private-session-id">
        <button type="submit">Save</button>
      </form>
      <h2>DNS octets</h2>
      <form id="octets">
        <input value="1">
        <input value="2">
        <input value="3">
        <input value="4">
        <input value="5">
        <input value="6">
        <input value="7">
        <input value="8">
        <button type="submit">Save octets</button>
      </form>
      <label for="router-password">Router password</label>
      <input id="router-password" type="password" value="do-not-expose">
      <p id="status">DNS settings not saved</p>
      <p id="octet-status">DNS octets not saved</p>
      <script>
        document.querySelector('#dns').addEventListener('submit', event => {
          event.preventDefault();
          const primary = document.querySelector('#primary').value;
          const secondary = document.querySelector('#secondary').value;
          document.querySelector('#status').textContent = `Saved DNS: ${primary}, ${secondary}`;
          fetch('https://telemetry.example/pixel').catch(() => {});
        });
        document.querySelector('#octets').addEventListener('submit', event => {
          event.preventDefault();
          const values = [...document.querySelectorAll('#octets input')].map(input => input.value);
          document.querySelector('#octet-status').textContent = `Saved DNS octets: ${values.join('.')}`;
        });
      </script>
    </body></html>"""

    ui = BrowserUI()
    original_route = ui._route
    external_attempts = []

    async def local_fixture_route(route):
        if route.request.url == "https://router.local/":
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            if "telemetry.example" in route.request.url:
                external_attempts.append(route.request.url)
            await original_route(route)

    monkeypatch.setattr(ui, "_route", local_fixture_route)
    try:
        initial = await ui.open("/")
        assert initial["path"] == "/"
        assert "DNS settings" in initial["text"]
        control_names = {control["name"] for control in initial["controls"]}
        assert {"Primary DNS server", "Secondary DNS server", "Save"} <= control_names
        assert "Router password" not in control_names
        assert "do-not-expose" not in repr(initial)
        assert "private-session-id" not in repr(initial)

        await ui.fill("Primary DNS server", "9.9.9.9")
        await ui.fill("Secondary DNS server", "149.112.112.112")
        after_save = await ui.click("button", "Save")
        # Filled values are redacted from snapshots; the page's saved status and
        # underlying inputs together confirm the form submission took effect.
        assert "Saved DNS:" in after_save["text"]
        assert "do-not-expose" not in repr(after_save)
        assert "private-session-id" not in repr(after_save)
        assert await ui._page.locator("#primary").input_value() == "9.9.9.9"
        assert await ui._page.locator("#secondary").input_value() == "149.112.112.112"
        assert external_attempts == ["https://telemetry.example/pixel"]

        # Duplicate, unlabeled IPv4 octets are exposed as distinct snapshot refs.
        current = after_save
        octets = [item for item in current["controls"] if item["name"] == ""]
        assert [item["value"] for item in octets] == [
            str(value) for value in range(1, 9)
        ]
        stale_ref = octets[0]["ref"]
        current = await ui.control(stale_ref, "fill", "9")
        with pytest.raises(UIError, match="stale"):
            await ui.control(stale_ref, "fill", "10")
        for expected in range(2, 9):
            target = next(
                item
                for item in current["controls"]
                if item["name"] == "" and item["value"] == str(expected)
            )
            current = await ui.control(target["ref"], "fill", str(expected + 8))
        save_ref = next(
            item["ref"] for item in current["controls"] if item["name"] == "Save octets"
        )
        octet_snapshot = await ui.control(save_ref, "click")
        assert "Saved DNS octets: 9.10.11.12.13.14.15.16" in octet_snapshot["text"]
        assert await ui._page.locator("#octets input").evaluate_all(
            "els => els.map(el => el.value)"
        ) == [str(value) for value in range(9, 17)]

        # The browser blocked the page's external fetch through the origin guard.
        with pytest.raises(PlaywrightError):
            await ui._page.evaluate("fetch('https://telemetry.example/second-pixel')")
        assert external_attempts[-1] == "https://telemetry.example/second-pixel"
    finally:
        await ui.close()


@pytest.mark.asyncio
async def test_ajax_login_waits_for_password_form_to_disappear(monkeypatch):
    monkeypatch.setenv("FRITZ_HOST", "router.local")
    monkeypatch.delenv("FRITZ_UI_URL", raising=False)
    monkeypatch.setenv("FRITZ_PASSWORD", "test-router-password")
    monkeypatch.delenv("FRITZ_PASSWORD_FILE", raising=False)
    html = """<!doctype html><html><body>
      <form id="login">
        <label for="password">Router password</label>
        <input id="password" type="password">
        <button type="submit">Log in</button>
      </form>
      <script>
        document.querySelector('#login').addEventListener('submit', event => {
          event.preventDefault();
          setTimeout(() => {
            document.querySelector('#login').remove();
            document.body.insertAdjacentHTML('beforeend', '<a href="/internet">Internet</a><p>Router settings loaded</p>');
          }, 75);
        });
      </script>
    </body></html>"""
    ui = BrowserUI()
    original_route = ui._route

    async def fixture_route(route):
        if route.request.url == "https://router.local/":
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            await original_route(route)

    monkeypatch.setattr(ui, "_route", fixture_route)
    try:
        snapshot = await ui.open("/")
        assert "Router settings loaded" in snapshot["text"]
        assert "test-router-password" not in repr(snapshot)
        assert await ui._page.locator("#login").count() == 0
    finally:
        await ui.close()


@pytest.mark.asyncio
async def test_delayed_fritz_login_preserves_authenticated_sid_without_reopening_root(
    monkeypatch,
):
    """Emulate FRITZ!OS login rendering after DOMContentLoaded and an SID URL."""
    monkeypatch.setenv("FRITZ_HOST", "router.local")
    monkeypatch.delenv("FRITZ_UI_URL", raising=False)
    monkeypatch.setenv("FRITZ_PASSWORD", "fixture-router-password")
    monkeypatch.delenv("FRITZ_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("FRITZ_USERNAME", "admin")

    html = """<!doctype html><html><body>
      <div id="boot">Loading router login</div>
      <script>
        setTimeout(() => {
          document.querySelector('#boot').remove();
          document.body.insertAdjacentHTML('beforeend', `
            <form id="loginForm" method="POST" action="/login-attempt">
              <select id="uiViewUser" name="user" hidden><option selected>admin</option></select>
              <input type="hidden" name="response">
              <input type="hidden" name="lp">
              <input type="hidden" name="forward">
              <input type="hidden" name="loginView">
              <label for="uiPassInput">Password</label>
              <input id="uiPassInput" name="uiPass" type="password">
              <button id="submitLoginBtn" type="submit">Anmelden</button>
            </form>`);
          document.querySelector('#loginForm').addEventListener('submit', event => {
            event.preventDefault();
            fetch('/login-attempt', {method: 'POST'}).then(() => {
              history.replaceState({}, '', '/?sid=0123456789abcdef');
              document.querySelector('#loginForm').remove();
              document.body.insertAdjacentHTML('beforeend', '<a href="/home">Heimnetz</a><h1>Router overview</h1><p>2 example devices</p>');
            });
          });
        }, 80);
      </script>
    </body></html>"""

    ui = BrowserUI()
    original_route = ui._route
    router_requests = []
    login_posts = []
    overview = "<!doctype html><html><body><a href='/home'>Heimnetz</a><h1>Router overview</h1><p>2 example devices</p></body></html>"

    async def fixture_route(route):
        if (
            route.request.url == "https://router.local/login-attempt"
            and route.request.method == "POST"
        ):
            login_posts.append(route.request.url)
            await route.fulfill(status=200, body="ok")
            return
        if route.request.url in {
            "https://router.local/?sid=0123456789abcdef",
            "https://router.local/devices?sid=0123456789abcdef",
        }:
            router_requests.append(route.request.url)
            await route.fulfill(status=200, content_type="text/html", body=overview)
            return
        if route.request.url.startswith("https://router.local/"):
            router_requests.append(route.request.url)
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            await original_route(route)

    monkeypatch.setattr(ui, "_route", fixture_route)
    try:
        snapshot = await ui.open("/")
        assert "Router overview" in snapshot["text"]
        assert "2 example devices" in snapshot["text"]
        assert "Anmeldung fehlgeschlagen" not in snapshot["text"]
        assert ui._page.url == "https://router.local/?sid=0123456789abcdef"
        assert len(login_posts) == 1, "the UI submits the password once"
        assert router_requests == ["https://router.local/"], (
            "default open must not reload the router root after the local login handler updates its URL"
        )
        assert "0123456789abcdef" not in repr(snapshot)
        assert "fixture-router-password" not in repr(snapshot)
        assert await ui._page.locator("#loginForm").count() == 0

        devices = await ui.open("/devices")
        assert devices["path"] == "/devices"
        assert ui._page.url == "https://router.local/devices?sid=0123456789abcdef"
        root_again = await ui.open("/")
        assert root_again["path"] == "/"
        assert ui._page.url == "https://router.local/?sid=0123456789abcdef"
        assert router_requests == [
            "https://router.local/",
            "https://router.local/devices?sid=0123456789abcdef",
            "https://router.local/?sid=0123456789abcdef",
        ]
    finally:
        await ui.close()


@pytest.mark.asyncio
async def test_fritz_login_failure_is_reported_and_credentials_are_submitted_once(
    monkeypatch,
):
    """A visible FRITZ!Box authentication error must never look like a good snapshot."""
    monkeypatch.setenv("FRITZ_HOST", "router.local")
    monkeypatch.delenv("FRITZ_UI_URL", raising=False)
    monkeypatch.setenv("FRITZ_PASSWORD", "fixture-invalid-password")
    monkeypatch.delenv("FRITZ_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("FRITZ_USERNAME", "admin")

    html = """<!doctype html><html><body>
      <div id="boot">Loading router login</div>
      <script>
        setTimeout(() => {
          document.querySelector('#boot').remove();
          document.body.insertAdjacentHTML('beforeend', `
            <form id="loginForm" method="POST" action="/login-attempt">
              <select id="uiViewUser" name="user" hidden><option selected>admin</option></select>
              <input type="hidden" name="response">
              <label for="uiPassInput">Password</label>
              <input id="uiPassInput" name="uiPass" type="password">
              <button id="submitLoginBtn" type="submit">Anmelden</button>
              <p id="error" role="alert" hidden></p>
            </form>`);
        }, 80);
      </script>
    </body></html>"""

    ui = BrowserUI()
    original_route = ui._route
    login_posts = []
    failure_page = """<!doctype html><html><body>
      <form id="loginForm" method="POST" action="/login-attempt">
        <select id="uiViewUser" name="user" hidden><option selected>admin</option></select>
        <input type="hidden" name="response">
        <label for="uiPassInput">Password</label>
        <input id="uiPassInput" name="uiPass" type="password">
        <button id="submitLoginBtn" type="submit">Anmelden</button>
        <p id="error" role="alert">Anmeldung fehlgeschlagen</p>
      </form>
    </body></html>"""

    async def fixture_route(route):
        if (
            route.request.url == "https://router.local/login-attempt"
            and route.request.method == "POST"
        ):
            login_posts.append(route.request.url)
            await route.fulfill(status=200, content_type="text/html", body=failure_page)
            return
        if route.request.url.startswith("https://router.local/"):
            await route.fulfill(status=200, content_type="text/html", body=html)
        else:
            await original_route(route)

    monkeypatch.setattr(ui, "_route", fixture_route)
    try:
        with pytest.raises(UIError, match="login|Login|Anmeldung") as error:
            await ui.open("/")
        assert login_posts == ["https://router.local/login-attempt"]
        assert "fixture-invalid-password" not in str(error.value)
    finally:
        await ui.close()

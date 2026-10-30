import uuid
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from fritzbox_mcp.router import RouterClient, RouterError

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"


class FakeConnection:
    def __init__(self, services, responses=None):
        self.services = services
        self.responses = responses or {}
        self.calls = []

    def call_action(self, service, action, arguments):
        self.calls.append((service, action, arguments))
        response = self.responses.get((service, action), {})
        if isinstance(response, Exception):
            raise response
        return response


def make_service(actions):
    """Build the subset of fritzconnection's advertised SCPD metadata we use."""
    state_variables = {}
    action_objects = {}
    for action_name, inputs in actions.items():
        args = {}
        for arg_name, data_type in inputs.items():
            variable_name = f"{action_name}_{arg_name}"
            state_variables[variable_name] = SimpleNamespace(
                dataType=data_type, allowed_values=[]
            )
            args[arg_name] = SimpleNamespace(
                direction="in", relatedStateVariable=variable_name
            )
        action_objects[action_name] = SimpleNamespace(arguments=args)
    return SimpleNamespace(
        actions=action_objects,
        state_variables=state_variables,
        serviceType="urn:schemas-upnp-org:service:Test:1",
        controlURL="/upnp/control/test",
    )


def client_with(actions, responses=None):
    connection = FakeConnection(
        {
            service: make_service(service_actions)
            for service, service_actions in actions.items()
        },
        responses,
    )
    factories = []

    def factory(**kwargs):
        factories.append(kwargs)
        return connection

    return RouterClient(factory=factory), connection, factories


def test_connection_is_lazy_and_constructed_once(monkeypatch):
    monkeypatch.delenv("FRITZ_PASSWORD", raising=False)
    monkeypatch.delenv("FRITZ_PASSWORD_FILE", raising=False)
    client, connection, factories = client_with(
        {"Hosts1": {"GetHostNumberOfEntries": {}}},
        {("Hosts1", "GetHostNumberOfEntries"): {"NewHostNumberOfEntries": 0}},
    )

    assert factories == []
    client.action("Hosts1", "GetHostNumberOfEntries")
    client.action("Hosts1", "GetHostNumberOfEntries")
    assert len(factories) == 1
    assert factories[0]["redact_debug_log"] is True
    assert len(connection.calls) == 2


def test_action_uses_advertised_inputs_and_checks_exact_schema_before_call():
    client, connection, _ = client_with(
        {"Hosts1": {"GetGenericHostEntry": {"NewIndex": "ui2"}}}
    )

    with pytest.raises(RouterError, match="missing NewIndex"):
        client.action("Hosts1", "GetGenericHostEntry")
    with pytest.raises(RouterError, match="unknown Surprise"):
        client.action("Hosts1", "GetGenericHostEntry", {"NewIndex": 2, "Surprise": 3})
    with pytest.raises(RouterError, match="integer"):
        client.action("Hosts1", "GetGenericHostEntry", {"NewIndex": True})
    with pytest.raises(RouterError, match="0 to 65535"):
        client.action("Hosts1", "GetGenericHostEntry", {"NewIndex": 65536})
    assert connection.calls == []

    client.action("Hosts1", "GetGenericHostEntry", {"NewIndex": 4})
    assert connection.calls == [("Hosts1", "GetGenericHostEntry", {"NewIndex": 4})]


def test_unknown_service_and_action_fail_before_mutation():
    client, connection, _ = client_with({"Hosts1": {"GetInfo": {}}})
    with pytest.raises(RouterError, match="not advertised"):
        client.action("Missing", "DeleteEverything")
    with pytest.raises(RouterError, match="not advertised"):
        client.action("Hosts1", "DeleteEverything")
    assert connection.calls == []


def test_boolean_validation_rejects_invalid_values_and_normalizes_binary_integer():
    client, connection, _ = client_with(
        {"WLANConfiguration1": {"SetEnable": {"NewEnable": "boolean"}}}
    )
    for invalid in ("true", 2, None):
        with pytest.raises(RouterError, match="boolean"):
            client.action("WLANConfiguration1", "SetEnable", {"NewEnable": invalid})
    client.action("WLANConfiguration1", "SetEnable", {"NewEnable": 1})
    client.action("WLANConfiguration1", "SetEnable", {"NewEnable": False})
    assert connection.calls == [
        ("WLANConfiguration1", "SetEnable", {"NewEnable": True}),
        ("WLANConfiguration1", "SetEnable", {"NewEnable": False}),
    ]


def test_hostname_and_friendly_name_use_distinct_advertised_actions_and_fields():
    client, connection, _ = client_with(
        {
            "Hosts1": {
                "X_AVM-DE_SetHostNameByMACAddress": {
                    "NewMACAddress": "string",
                    "NewHostName": "string",
                },
                "X_AVM-DE_SetFriendlyNameByMAC": {
                    "NewMACAddress": "string",
                    "NewX_AVM-DE_FriendlyName": "string",
                },
            }
        }
    )

    client.rename_device("aa:bb:cc:dd:ee:ff", "Printer")
    client.set_friendly_name("aa:bb:cc:dd:ee:ff", "Office laser")
    assert connection.calls == [
        (
            "Hosts1",
            "X_AVM-DE_SetHostNameByMACAddress",
            {"NewMACAddress": "AA:BB:CC:DD:EE:FF", "NewHostName": "Printer"},
        ),
        (
            "Hosts1",
            "X_AVM-DE_SetFriendlyNameByMAC",
            {
                "NewMACAddress": "AA:BB:CC:DD:EE:FF",
                "NewX_AVM-DE_FriendlyName": "Office laser",
            },
        ),
    ]


@pytest.mark.parametrize("mac", ["aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF", "not-a-mac"])
def test_device_mutations_reject_malformed_mac_before_router_call(mac):
    client, connection, _ = client_with({"Hosts1": {"Set": {"MAC": "string"}}})
    with pytest.raises(RouterError, match="MAC address"):
        client.rename_device(mac, "Device")
    assert connection.calls == []


def test_device_blocking_requires_ipv4_and_real_boolean():
    client, connection, _ = client_with(
        {
            "X_AVM-DE_HostFilter1": {
                "DisallowWANAccessByIP": {
                    "NewIPv4Address": "string",
                    "NewDisallow": "boolean",
                }
            }
        }
    )
    for ip, blocked in (("2001:db8::1", True), ("999.1.1.1", True), ("192.0.2.2", 1)):
        with pytest.raises(RouterError):
            client.set_device_blocking(ip, blocked)
    assert connection.calls == []

    client.set_device_blocking("192.0.2.10", True)
    assert connection.calls == [
        (
            "X_AVM-DE_HostFilter1",
            "DisallowWANAccessByIP",
            {"NewIPv4Address": "192.0.2.10", "NewDisallow": True},
        )
    ]


def test_secret_fields_and_url_credentials_are_redacted_recursively():
    response = {
        "NewSSID": "home",
        "NewPassword": "plain-secret",
        "nested": [{"session_token": "opaque-sid", "value": 3}],
        "callback": "https://user:pass@router.local/path?sid=secret#part",
    }
    client, _, _ = client_with(
        {"WLANConfiguration1": {"GetInfo": {}}},
        {("WLANConfiguration1", "GetInfo"): response},
    )

    result = client.action("WLANConfiguration1", "GetInfo")
    assert result["NewPassword"] == "[redacted]"
    assert result["nested"] == [{"session_token": "[redacted]", "value": 3}]
    assert result["callback"] == "https://router.local/path"
    assert "plain-secret" not in repr(result)
    assert "opaque-sid" not in repr(result)
    assert "user:pass" not in repr(result)


def test_connection_errors_do_not_leak_factory_exception_details():
    def broken_factory(**kwargs):
        raise RuntimeError("password=very-secret sid=private-session")

    client = RouterClient(factory=broken_factory)
    with pytest.raises(RouterError) as error:
        client.action("Hosts1", "GetInfo")
    assert "very-secret" not in str(error.value)
    assert "private-session" not in str(error.value)
    assert (
        str(error.value)
        == "Could not connect to FRITZ!Box; check host, TLS, and credentials"
    )


def test_lan_dns_prefers_plural_action_then_uses_singular_fallback():
    plural, plural_connection, _ = client_with(
        {"LANHostConfigManagement1": {"GetDNSServers": {}, "GetDNSServer": {}}},
        {
            ("LANHostConfigManagement1", "GetDNSServers"): {
                "NewDNSServers": "192.0.2.53"
            }
        },
    )
    assert plural.get_lan_dns() == {"NewDNSServers": "192.0.2.53"}
    assert plural_connection.calls == [
        ("LANHostConfigManagement1", "GetDNSServers", {})
    ]

    singular, singular_connection, _ = client_with(
        {"LANHostConfigManagement1": {"GetDNSServer": {}}},
        {("LANHostConfigManagement1", "GetDNSServer"): {"NewDNSServer": "192.0.2.53"}},
    )
    assert singular.get_lan_dns() == {"NewDNSServer": "192.0.2.53"}
    assert singular_connection.calls == [
        ("LANHostConfigManagement1", "GetDNSServer", {})
    ]


def test_wan_dns_prefers_ip_service_and_falls_back_to_ppp_or_explicit_choice():
    actions = {
        "WANIPConnection1": {"X_GetDNSServers": {}},
        "WANPPPConnection1": {"X_GetDNSServers": {}},
    }
    client, connection, _ = client_with(
        actions,
        {
            ("WANIPConnection1", "X_GetDNSServers"): {"NewDNSServers": "192.0.2.1"},
            ("WANPPPConnection1", "X_GetDNSServers"): {"NewDNSServers": "192.0.2.2"},
        },
    )

    assert client.get_wan_dns() == {"NewDNSServers": "192.0.2.1"}
    assert client.get_wan_dns("WANPPPConnection1") == {"NewDNSServers": "192.0.2.2"}
    assert connection.calls == [
        ("WANIPConnection1", "X_GetDNSServers", {}),
        ("WANPPPConnection1", "X_GetDNSServers", {}),
    ]


def test_wan_dns_falls_back_to_ppp_when_ip_service_is_not_advertised():
    client, connection, _ = client_with(
        {"WANPPPConnection1": {"X_GetDNSServers": {}}},
        {("WANPPPConnection1", "X_GetDNSServers"): {"NewDNSServers": "192.0.2.2"}},
    )
    assert client.get_wan_dns() == {"NewDNSServers": "192.0.2.2"}
    assert connection.calls == [("WANPPPConnection1", "X_GetDNSServers", {})]


class FakeSoapResponse:
    status_code = 200


class FakeSoapSession:
    def __init__(self):
        self.posts = []
        self.failure = None
        self.closed = False

    def post(self, url, data, headers, timeout):
        self.posts.append((url, data, headers, timeout))
        if self.failure:
            raise self.failure
        return self

    def __enter__(self):
        return FakeSoapResponse()

    def __exit__(self, exc_type, exc, traceback):
        return False

    def close(self):
        self.closed = True


class FakeSoaper:
    argument_template = "<{name}>{value}</{name}>"
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">{body}</s:Envelope>'
    )
    address = "https://router.local"
    port = 49443
    timeout = 7

    def __init__(self):
        self.session = FakeSoapSession()
        self.headers = {"content-type": "text/xml"}
        self.responses = {}

    def get_body(self, service, action, arguments):
        return (
            f'<s:Body><u:{action} xmlns:u="{service.serviceType}">'
            f"{arguments}</u:{action}></s:Body>"
        )

    def parse_response(self, response, service, action):
        sequence = len(self.session.posts)
        return self.responses.get(
            (action, sequence),
            self.responses.get((action, None), {"NewState": "enabled"}),
        )


class FakeTokenConnection(FakeConnection):
    def __init__(self, services, start_result):
        super().__init__(services)
        self.start_result = start_result
        self.soaper = FakeSoaper()
        self.session = self.soaper.session
        self.soaper.responses[("SetConfig", 1)] = start_result
        self.soaper.responses[("SetConfig", 2)] = {"NewState": "stopped"}

    def call_action(self, service, action, arguments):
        self.calls.append((service, action, arguments))
        if (
            service == "X_AVM-DE_Auth1"
            and action == "SetConfig"
            and arguments["NewAction"] == "start"
        ):
            return self.start_result
        return {"CalledDirectly": action}


def token_client(start_result):
    service_map = {
        "X_AVM-DE_Auth1": make_service(
            {"SetConfig": {"NewAction": "string"}, "GetState": {}}
        ),
        "Hosts1": make_service({"GetInfo": {}}),
    }
    connection = FakeTokenConnection(service_map, start_result)
    return RouterClient(factory=lambda **kwargs: connection), connection


def test_2fa_token_is_kept_private_and_sent_in_soap_header_for_followup():
    token = str(uuid.uuid4())
    client, connection = token_client({"NewToken": token, "NewState": "enabled"})

    started = client.start_2fa()
    assert started == {"NewToken": "[redacted]", "NewState": "enabled"}
    assert token not in repr(started)
    assert client.action("X_AVM-DE_Auth1", "GetState") == {"NewState": "enabled"}

    assert connection.calls == []
    assert len(connection.soaper.session.posts) == 2
    start_envelope = connection.soaper.session.posts[0][1]
    if isinstance(start_envelope, bytes):
        start_envelope = start_envelope.decode("utf-8")
    start_root = ElementTree.fromstring(start_envelope)
    assert [child.tag for child in list(start_root)] == [f"{{{SOAP_NS}}}Body"]
    url, envelope, headers, timeout = connection.soaper.session.posts[1]
    if isinstance(envelope, bytes):
        envelope = envelope.decode("utf-8")
    assert url == "https://router.local:49443/upnp/control/test"
    root = ElementTree.fromstring(envelope)
    children = list(root)
    assert [child.tag for child in children] == [
        f"{{{SOAP_NS}}}Header",
        f"{{{SOAP_NS}}}Body",
    ]
    header = children[0]
    soap_token = header.find("{avm.de}token")
    assert soap_token is not None
    assert soap_token.text == token
    assert soap_token.attrib[f"{{{SOAP_NS}}}mustUnderstand"] == "1"
    assert children[1][0].tag.endswith("GetState")
    assert headers["soapaction"].endswith("#GetState")
    assert timeout == 7


def test_failed_token_bearing_call_clears_token_and_does_not_retry():
    token = str(uuid.uuid4())
    client, connection = token_client({"NewToken": token})
    client.start_2fa()
    connection.soaper.session.failure = RuntimeError("sensitive transport detail")

    with pytest.raises(RouterError) as error:
        client.action("X_AVM-DE_Auth1", "GetState")
    assert "sensitive transport detail" not in str(error.value)
    assert len(connection.soaper.session.posts) == 2
    assert connection.calls == []

    connection.soaper.session.failure = None
    assert client.action("X_AVM-DE_Auth1", "GetState") == {"NewState": "enabled"}
    assert connection.calls == []
    assert len(connection.soaper.session.posts) == 3
    fallback_envelope = connection.soaper.session.posts[-1][1]
    if isinstance(fallback_envelope, bytes):
        fallback_envelope = fallback_envelope.decode("utf-8")
    assert "<avm:token" not in fallback_envelope


def test_stopping_2fa_uses_token_then_clears_it_and_close_releases_session():
    token = str(uuid.uuid4())
    client, connection = token_client({"NewToken": token})
    client.start_2fa()
    client.stop_2fa()
    assert len(connection.soaper.session.posts) == 2
    stop_envelope = connection.soaper.session.posts[-1][1]
    if isinstance(stop_envelope, bytes):
        stop_envelope = stop_envelope.decode("utf-8")
    stop_root = ElementTree.fromstring(stop_envelope)
    stop_children = list(stop_root)
    assert [child.tag for child in stop_children] == [
        f"{{{SOAP_NS}}}Header",
        f"{{{SOAP_NS}}}Body",
    ]
    stop_token = stop_children[0].find("{avm.de}token")
    assert stop_token is not None and stop_token.text == token
    assert "stop" in "".join(stop_children[1].itertext())
    client.action("Hosts1", "GetInfo")
    assert len(connection.soaper.session.posts) == 3
    post_stop_envelope = connection.soaper.session.posts[-1][1]
    if isinstance(post_stop_envelope, bytes):
        post_stop_envelope = post_stop_envelope.decode("utf-8")
    assert "<avm:token" not in post_stop_envelope

    client.close()
    assert connection.soaper.session.closed


def test_invalid_2fa_token_is_not_retained_or_returned():
    client, connection = token_client({"NewToken": "not-a-uuid"})
    with pytest.raises(RouterError, match="invalid 2FA token"):
        client.start_2fa()
    assert "not-a-uuid" not in repr(connection.calls)
    client.action("X_AVM-DE_Auth1", "GetState")
    assert len(connection.soaper.session.posts) == 2
    followup = connection.soaper.session.posts[-1][1]
    if isinstance(followup, bytes):
        followup = followup.decode("utf-8")
    assert "<avm:token" not in followup

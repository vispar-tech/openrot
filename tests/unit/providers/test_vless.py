import pytest

from openrot.providers import vless


def test_parse_tls_node() -> None:
    raw = (
        "vless://11111111-2222-3333-4444-555555555555@example.com:443"
        "?security=tls&sni=example.com&flow=xtls-rprx-vision#MyNode"
    )
    node = vless.parse_vless(raw)
    assert node.uuid == "11111111-2222-3333-4444-555555555555"
    assert node.address == "example.com"
    assert node.port == 443
    assert node.name == "MyNode"
    assert node.security == "tls"
    assert node.servername == "example.com"
    assert node.flow == "xtls-rprx-vision"
    assert node.tls is True


def test_parse_plain_node() -> None:
    raw = "vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@192.168.1.1:8080#PlainNode"
    node = vless.parse_vless(raw)
    assert node.address == "192.168.1.1"
    assert node.port == 8080
    assert node.name == "PlainNode"
    assert node.tls is False


def test_parse_rejects_non_vless() -> None:
    with pytest.raises(vless.ParseError):
        vless.parse_vless("ss://notvless")


def test_parse_malformed_missing_at() -> None:
    with pytest.raises(vless.ParseError):
        vless.parse_vless("vless://justauuid")


def test_parse_bad_port_raises() -> None:
    with pytest.raises(vless.ParseError):
        vless.parse_vless("vless://a-b@example.com:not-a-port")


def test_decode_base64_returns_unchanged_on_bad_input() -> None:
    assert vless.decode_base64_subscription("!!!not-base64!!!") == "!!!not-base64!!!"


def test_extract_from_text_empty_for_unknown() -> None:
    assert vless.extract_from_text("just some plain text with no links") == []


def test_extract_from_text() -> None:
    text = "garbage\nvless://a-b@1.1.1.1:80#X\nmore"
    assert vless.extract_from_text(text) == ["vless://a-b@1.1.1.1:80#X"]


def test_extract_from_base64_subscription() -> None:
    import base64

    plain = "vless://a-b@1.1.1.1:80#Base"
    encoded = base64.b64encode(plain.encode()).decode()
    assert vless.extract_from_text(encoded) == [plain]


def test_parse_ws_host_and_path() -> None:
    raw = "vless://a-b@example.com:443?type=ws&path=%2Fv2&host=cdn.example.com"
    node = vless.parse_vless(raw)
    assert node.network == "ws"
    assert node.ws_path == "/v2"
    assert node.ws_host == "cdn.example.com"


def test_parse_reality_node() -> None:
    raw = (
        "vless://1595b6ec-aece-4756-a002-2c7988d7e023@mynl.api.dznn.net:443"
        "?type=tcp&security=reality&sni=mynl.api.dznn.net"
        "&pbk=abcdefghijklmnop&sid=1abc"
    )
    node = vless.parse_vless(raw)
    assert node.security == "reality"
    assert node.tls is True
    assert node.public_key == "abcdefghijklmnop"
    assert node.short_id == "1abc"


def test_parse_port_with_trailing_slash() -> None:
    raw = "vless://a-b@example.com:443/#Name"
    node = vless.parse_vless(raw)
    assert node.port == 443
    assert node.address == "example.com"

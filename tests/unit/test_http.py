import httpx
import pytest
from plume_core.http import DEFAULT_TIMEOUT, make_client
from tenacity import wait_none

URL = "https://example.test/resource"


def scripted(
    outcomes: list[int | httpx.TransportError],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Transport qui rejoue les issues dans l'ordre, et la dernière indéfiniment."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(request)
        if isinstance(outcome, httpx.TransportError):
            raise outcome
        return httpx.Response(outcome)

    return httpx.MockTransport(handler), calls


def client_for(transport: httpx.MockTransport) -> httpx.Client:
    return make_client(transport=transport, attempts=3, wait=wait_none())


def connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("refused")


def test_success_is_not_retried() -> None:
    transport, calls = scripted([200])

    assert client_for(transport).get(URL).status_code == 200
    assert len(calls) == 1


def test_server_error_then_success_is_retried() -> None:
    transport, calls = scripted([503, 200])

    assert client_for(transport).get(URL).status_code == 200
    assert len(calls) == 2


def test_rate_limit_then_success_is_retried() -> None:
    transport, calls = scripted([429, 200])

    assert client_for(transport).get(URL).status_code == 200
    assert len(calls) == 2


def test_persistent_server_error_returns_last_response() -> None:
    transport, calls = scripted([500, 502, 503])

    assert client_for(transport).get(URL).status_code == 503
    assert len(calls) == 3


def test_network_error_then_success_is_retried() -> None:
    transport, calls = scripted([connect_error(), 200])

    assert client_for(transport).get(URL).status_code == 200
    assert len(calls) == 2


def test_persistent_network_error_is_raised() -> None:
    transport, calls = scripted([connect_error()])

    with pytest.raises(httpx.ConnectError):
        client_for(transport).get(URL)
    assert len(calls) == 3


def test_client_error_is_not_retried() -> None:
    transport, calls = scripted([404])

    assert client_for(transport).get(URL).status_code == 404
    assert len(calls) == 1


def test_non_idempotent_method_is_not_retried() -> None:
    transport, calls = scripted([503, 200])

    assert client_for(transport).post(URL).status_code == 503
    assert len(calls) == 1


def test_explicit_timeouts() -> None:
    transport, _ = scripted([200])

    assert client_for(transport).timeout == DEFAULT_TIMEOUT
    assert DEFAULT_TIMEOUT.connect == 5.0
    assert DEFAULT_TIMEOUT.read == 10.0

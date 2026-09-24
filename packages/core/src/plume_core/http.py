"""Client HTTP partagé : timeouts explicites et rejeu sur erreurs réseau et 5xx."""

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEFAULT_ATTEMPTS = 3
DEFAULT_WAIT = wait_exponential(multiplier=0.5, max=10)
# Rejouer un POST pourrait dupliquer un effet de bord côté serveur.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})


def _is_server_error(response: httpx.Response) -> bool:
    return response.status_code >= 500


def _close_discarded_response(state: RetryCallState) -> None:
    if state.outcome is not None and not state.outcome.failed:
        state.outcome.result().close()


def _last_outcome(state: RetryCallState) -> httpx.Response:
    # Essais épuisés : renvoie la dernière réponse ou relève l'exception d'origine.
    assert state.outcome is not None
    response: httpx.Response = state.outcome.result()
    return response


class RetryTransport(httpx.BaseTransport):
    def __init__(self, inner: httpx.BaseTransport, *, attempts: int, wait: wait_base) -> None:
        self._inner = inner
        self._attempts = attempts
        self._wait = wait

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method not in IDEMPOTENT_METHODS:
            return self._inner.handle_request(request)
        retrying = Retrying(
            stop=stop_after_attempt(self._attempts),
            wait=self._wait,
            retry=retry_if_exception_type(httpx.TransportError) | retry_if_result(_is_server_error),
            before_sleep=_close_discarded_response,
            retry_error_callback=_last_outcome,
        )
        return retrying(self._inner.handle_request, request)

    def close(self) -> None:
        self._inner.close()


def make_client(
    *,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    wait: wait_base = DEFAULT_WAIT,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    inner = transport if transport is not None else httpx.HTTPTransport()
    return httpx.Client(
        timeout=timeout,
        transport=RetryTransport(inner, attempts=attempts, wait=wait),
    )

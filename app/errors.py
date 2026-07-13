"""
errors.py -- One typed error the whole backend speaks.

Spec section 11 is emphatic that two failure classes must never be blurred:

  * REJECTED   -- the server received the request and said no (an HTTP 4xx with
                  a body, or an AT Protocol error object). The content is the
                  problem; retrying it unchanged just fails again.
  * UNREACHABLE -- the request could not be completed at all (timeout, DNS
                  failure, connection refused, or a 5xx). The network/instance
                  is the problem, not the user's content.

The platform clients (bluesky.py, mastodon.py) know their own wire formats, so
they are responsible for translating a raw failure into one of the ``kind``
values below. Everything upstream -- the retry policy and the per-target status
report -- switches on ``kind`` and never has to re-parse a raw error body.

``raw`` always carries the untranslated detail (status code, AT Protocol error
name, response body) so the UI can show "friendly up top, raw underneath" (spec
section 11) without the friendly layer having thrown the raw detail away.
"""

from __future__ import annotations

from typing import Any


# The transient kinds are the ONLY ones eligible for the single silent
# auto-retry (spec section 11). Everything else is reported immediately.
#
#   expired       -- Bluesky access token expired; refresh via refreshJwt and
#                    retry once. Distinct from ``auth`` because it is a normal
#                    part of the token lifecycle, not a dead credential.
#   ratelimit     -- 429 / RateLimitExceeded; honor retry-after, then retry once.
#   unreachable   -- network blip / timeout / 5xx; retry once.
#
# The non-transient kinds are reported straight away:
#
#   auth          -- dead or revoked credential (route the user to re-auth).
#   oversize      -- an image the platform rejected as too large.
#   media_timeout -- Mastodon async media never became ready before timeout.
#   rejected      -- any other content rejection (e.g. text too long).
TRANSIENT_KINDS = {"expired", "ratelimit", "unreachable"}


class ApiError(Exception):
    """A single platform failure, pre-classified for retry and reporting.

    Attributes
    ----------
    kind : str
        One of the classification strings documented above.
    message : str
        A clean, human-readable sentence suitable for the top line of a status.
    raw : Any
        The untranslated detail (dict or string) shown one click away.
    status : int | None
        The HTTP status code, when there was one.
    retry_after : float | None
        Seconds to wait before a rate-limit retry, parsed from headers.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        raw: Any = None,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.raw = raw
        self.status = status
        self.retry_after = retry_after

    @property
    def is_transient(self) -> bool:
        """True if this error qualifies for the single silent auto-retry."""
        return self.kind in TRANSIENT_KINDS

    @property
    def is_auth(self) -> bool:
        """True if recovery means re-authenticating the account (spec section 9).

        Both a dead credential and a failed token refresh land the user on the
        wizard's re-auth path, so callers treat these together.
        """
        return self.kind == "auth"

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the JSON status payload the composer renders."""
        return {
            "kind": self.kind,
            "message": self.message,
            "status": self.status,
            "raw": self.raw,
        }

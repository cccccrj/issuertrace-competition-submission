"""Guards for everything that arrives from outside: URLs, bodies and page text.

Three risks, three narrow defences.

A URL can point inside the network it is fetched from.  Scope configuration
already limits which hosts are fetched, but a redirect is not in the
configuration, and "follow redirects" is how a public URL becomes a request to
169.254.169.254.  So every hop is resolved and checked, not just the first.

A response can be arbitrarily large.  Reading it into memory to find out is the
whole attack, so the read is capped and a body over the cap is refused rather
than truncated: half a document parsed as a whole one is worse than no document.

Page text can carry instructions aimed at the model that reads it later.  It is
data, and the prompts say so, but a fence made of characters the data can also
contain is not a fence — so the marker is stripped from the content itself.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable, Iterable
from urllib.parse import urlparse

# 25 MB: the largest filing collected so far is a 12 MB prospectus, and a body
# past twice that is not a document this platform was pointed at.
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_REDIRECTS = 5
FENCE = "〔外部内容〕"


class UnsafeRequest(Exception):
    """A request that must not be made, or a response that must not be read."""


def addresses(host: str, resolve: Callable[..., Iterable] | None = None) -> list[str]:
    # Looked up at call time, not bound as a default, so a caller (or a test)
    # can substitute the resolver without reaching into this module.
    resolve = resolve or socket.getaddrinfo
    try:
        return sorted({item[4][0] for item in resolve(host, None)})
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeRequest(f"cannot resolve host {host!r}: {exc}") from exc


def assert_public_url(url: str, resolve: Callable[..., Iterable] | None = None) -> None:
    """Refuse anything that is not a public HTTP(S) endpoint.

    Checked by resolved address rather than by hostname: a name that looks
    external can resolve to a loopback or metadata address, which is the entire
    point of the attack.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeRequest(f"only http(s) is fetched, not {parsed.scheme!r}")
    if not parsed.hostname:
        raise UnsafeRequest("URL has no host")
    for address in addresses(parsed.hostname, resolve):
        # ``is_global`` is the whole answer: it excludes loopback, the private
        # ranges, link-local (the cloud metadata address among them),
        # carrier-grade NAT, the documentation blocks, multicast and IPv6
        # unique-local — in one predicate that stays correct as the registry
        # changes.
        if not ipaddress.ip_address(address).is_global:
            raise UnsafeRequest(f"{parsed.hostname} resolves to non-public address {address}")


def read_capped(response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    """Read a streamed response, refusing a body past the cap.

    A declared Content-Length is checked first because it costs nothing, and the
    stream is still counted, because the header is the sender's claim.
    """
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise UnsafeRequest(f"response declares {declared} bytes; cap is {limit}")
    chunks, total = [], 0
    for chunk in response.iter_content(65536):
        total += len(chunk)
        if total > limit:
            raise UnsafeRequest(f"response exceeded {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


TAG = re.compile(r"<[^>]{0,400}>")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_markup(text: str | None) -> str:
    """Plain text out of a fragment that may carry markup.

    Search engines return titles wrapped in highlight tags.  Stored as they are,
    they reach an alert headline, a notification and a report as literal
    ``<font color=...>``, and every consumer has to strip them again.
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", CONTROL.sub("", TAG.sub("", text))).strip()


def as_data(text: str | None) -> str:
    """Prepare untrusted text to sit inside a prompt as data.

    The prompts fence external content and tell the model it is material to
    judge, never instructions to follow.  A fence the content can close is not a
    fence, so the marker is removed from the text, and control characters go
    with it — they render as nothing and read as structure.
    """
    return CONTROL.sub("", (text or "")).replace(FENCE, "")

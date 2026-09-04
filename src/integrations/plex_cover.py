"""Signed-token helpers for the Plex cover art proxy.

Plex artwork endpoints require an ``X-Plex-Token``, so the raw URL can never be
embedded in an ``<img src>`` without leaking that token into the page (and into
Floppy's own database). Covers are instead served through Floppy's authenticated
proxy (see ``integrations.views.plex_cover``), identified by a signed token so
the view never trusts client input for which account/server/item to fetch. This
mirrors ``integrations.audiobookshelf_cover``; the two differ only in which
credential the proxy attaches upstream and how the upstream host is resolved.
"""

import base64
import binascii

from django.core.signing import BadSignature, Signer
from django.urls import reverse

SIGNER_SALT = "floppy.plex-cover"

# Kept in sync with the "import/plex/cover/<str:token>" route.
PROXY_PATH_PREFIX = "/import/plex/cover/"


def _signer():
    return Signer(salt=SIGNER_SALT)


def build_cover_proxy_url(account_id, machine_identifier, thumb_path):
    """Return a Floppy-hosted URL that serves a Plex item's cover art.

    Args:
        account_id: PlexAccount primary key that owns the server
        machine_identifier: Plex server the art lives on
        thumb_path: server-relative art path, e.g. "/library/metadata/12/thumb/1"
    """
    if not (account_id and machine_identifier and thumb_path):
        return ""
    payload = f"{account_id}:{machine_identifier}:{thumb_path}"
    token = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    signed = _signer().sign(token)
    return reverse("plex_cover", kwargs={"token": signed})


def resolve_cover_proxy_token(token):
    """Return (account_id, machine_identifier, thumb_path), or None."""
    try:
        payload = _signer().unsign(token)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
    except (BadSignature, binascii.Error, ValueError, UnicodeDecodeError):
        return None

    account_id, _, rest = decoded.partition(":")
    machine_identifier, _, thumb_path = rest.partition(":")
    # Only server-relative art paths are addressable, and the host comes from
    # the account's own cached sections, so a token can never make Floppy fetch
    # an arbitrary URL.
    if not account_id or not machine_identifier or not thumb_path.startswith("/"):
        return None
    return account_id, machine_identifier, thumb_path


def is_cover_proxy_url(url):
    """Return whether `url` is one of Floppy's own Plex cover proxy URLs."""
    if not isinstance(url, str) or not url:
        return False
    return PROXY_PATH_PREFIX in url

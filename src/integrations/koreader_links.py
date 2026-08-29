"""Helpers for KOReader document hash links on book track entries."""

import re

from integrations.models import KoreaderDocumentLink

DOCUMENT_HASH_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def normalize_document_hash(value):
    """Return a lowercase 32-char hex hash or None."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if DOCUMENT_HASH_PATTERN.fullmatch(normalized):
        return normalized
    return None


def get_document_hash_for_item(user, item):
    """Return the linked KOReader document hash for a book item, if any."""
    if item is None:
        return ""
    link = (
        KoreaderDocumentLink.objects.filter(user=user, item=item)
        .values_list("document_hash", flat=True)
        .first()
    )
    return link or ""


def save_document_link(user, item, document_hash):
    """Create, update, or remove the KOReader link for a book item."""
    normalized = normalize_document_hash(document_hash)
    existing = KoreaderDocumentLink.objects.filter(user=user, item=item).first()
    if not normalized:
        if existing:
            existing.delete()
        return None
    if existing and existing.document_hash == normalized:
        return existing
    if existing:
        existing.delete()
    return KoreaderDocumentLink.objects.create(
        user=user,
        item=item,
        document_hash=normalized,
    )

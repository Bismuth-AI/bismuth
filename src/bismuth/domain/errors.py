"""Exception hierarchy; all errors descend from :class:`BismuthError`."""

from __future__ import annotations


class BismuthError(Exception):
    """Base class for all errors raised by Bismuth."""


class VaultError(BismuthError):
    """The vault is not in the required shape (missing roots, escaping paths, unresolved collisions)."""


class JournalCorruptError(BismuthError):
    """The journal cannot be replayed."""


class ParserUnavailableError(BismuthError):
    """No registered parser can read this file."""


class StructuredOutputError(BismuthError):
    """A model could not be coaxed into returning the requested schema."""


class ModelRequestError(BismuthError):
    """The configured model endpoint did not complete a request."""


class CharterError(BismuthError):
    """A folder charter is missing or malformed."""

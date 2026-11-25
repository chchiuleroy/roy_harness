"""Typed errors. Nothing in kb swallows an exception; every failure is one of these (or a plain bug)."""


class KbError(Exception):
    """Base class for every error kb raises on purpose."""


class CanonicalError(KbError):
    """A value cannot be put into a canonical preimage (float, non-str key, lone surrogate, ...)."""


class IdError(KbError):
    """An identifier or digest has the wrong shape."""


class SchemaError(KbError):
    """Base for database schema problems."""


class SchemaTooNew(SchemaError):
    """The database was written by newer code; kb never downgrades."""


class MigrationFailed(SchemaError):
    """A migration step failed; the transaction was rolled back and the version is unchanged."""


class BackupInvalid(SchemaError):
    """The pre-migration backup failed its integrity check, so no migration is attempted."""


class WriterBusy(KbError):
    """Another process holds the single-writer lock."""


class PolicyViolation(KbError):
    """A collection profile forbids the requested action."""


class AuditError(KbError):
    """An audit event tried to carry something other than event types and identifiers."""


class BlobError(KbError):
    """The raw-evidence store refused or failed an operation."""


class CommitFailed(KbError):
    """A commit could not be built, verified or published; the previously active commit is untouched."""


class ArtifactsMissing(KbError):
    """The active commit's files are missing or do not match its manifest; kb refuses to serve them (run rollback or doctor)."""


class ReviewRefused(KbError):
    """A proposal, approval or application broke a review rule; nothing was changed."""


class StaleProposal(ReviewRefused):
    """The knowledge changed since the proposal was made (its base commit is no longer the active one); it was marked stale and not applied."""


class VectorUnavailable(KbError):
    """Vector (semantic) search cannot be used right now: no vector index in the active commit, an index built by another model, or no embedding
    backend. Lexical search is unaffected; an automatic search falls back to it and says so, an explicit semantic search refuses."""


class FaultInjected(KbError):
    """Raised by a test fault point in 'raise' mode."""

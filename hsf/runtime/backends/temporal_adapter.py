"""Optional Temporal adapter (import-guarded; PRD NG3: docs-level only in v1).
Maps artifact phases 1:1 onto Temporal activities when temporalio is installed."""
try:
    import temporalio  # noqa: F401
    AVAILABLE = True
except ImportError:
    AVAILABLE = False

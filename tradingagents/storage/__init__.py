"""Opt-in compressed run storage; filesystem output remains the default.

No process-global current run or persistent database connection is used. A
``SQLiteRunStore`` is an explicit handle that can be passed to CLI/graph hooks.
"""

from .codec import CorruptArtifactError, SchemaVersionError, StorageError
from .sqlite import (
    SQLiteRunStore,
    SQLiteStorage,
    create_run,
    database_path,
    sanitize_metadata,
    sqlite_enabled,
)

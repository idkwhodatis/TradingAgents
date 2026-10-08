"""Publish a complete export directory atomically, without replacing any path."""

import ctypes
import os
import sys

from .codec import StorageError


def publish_directory(source, destination):
    """An exclusive rename on supported desktop/server operating systems.

    A preceding ``exists()`` check plus ordinary POSIX ``rename()`` is unsafe:
    another process could create an empty destination directory in between and
    have it silently replaced. Fail closed where exclusive rename is absent.
    """
    if os.name == "nt":
        # Windows rename never replaces an existing destination.
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename.restype = ctypes.c_int
        result = rename(
            -100, source_bytes, -100, destination_bytes, 1
        )  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_bytes, destination_bytes, 4)  # RENAME_EXCL
    else:
        raise StorageError(
            "this operating system does not support safe exclusive export publication"
        )
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), os.fspath(destination))

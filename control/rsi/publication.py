"""Publish a complete file to a new name on the same filesystem."""
import os


def publish_new(temporary, target):
    """Raise FileExistsError if target exists. The caller cleans up the temporary name."""
    if os.name == 'nt':
        # MoveFileEx without REPLACE_EXISTING is atomic and needs no hard-link privilege.
        os.rename(temporary, target)
    else:
        os.link(temporary, target, follow_symlinks=False)

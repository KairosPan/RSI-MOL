"""Build symbolic-link fixtures on hosts where the current account can create them."""
import os
import unittest


def make_link(target, path):
    try:
        os.symlink(target, path, target_is_directory=os.path.isdir(target))
    except OSError as error:
        if getattr(error, 'winerror', None) == 1314:
            raise unittest.SkipTest('this Windows account cannot create symbolic links') from error
        raise


def make_hardlink(target, path):
    try:
        os.link(target, path)
    except OSError as error:
        if getattr(error, 'winerror', None) in (5, 1314):
            raise unittest.SkipTest('this Windows account cannot create hard links here') from error
        raise

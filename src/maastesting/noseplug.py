# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Nose plugins for MAAS."""

__all__ = [
    "Crochet",
    "main",
    "Scenarios",
    "Select",
]

import inspect
import logging
import unittest

from nose.case import Test
from nose.core import TestProgram
from nose.plugins.base import Plugin
from testscenarios import generate_scenarios
from twisted.python.filepath import FilePath


class Crochet(Plugin):
    """Start the Twisted reactor via Crochet."""

    name = "crochet"
    option_no_setup = "%s_no_setup" % name
    log = logging.getLogger('nose.plugins.%s' % name)

    def options(self, parser, env):
        """Add options to Nose's parser.

        :attention: This is part of the Nose plugin contract.
        """
        super(Crochet, self).options(parser, env)
        parser.add_option(
            "--%s-no-setup" % self.name, dest=self.option_no_setup,
            action="store_true", default=False, help=(
                "Initialize the crochet library with no side effects."
            ),
        )

    def configure(self, options, conf):
        """Configure, based on the parsed options.

        :attention: This is part of the Nose plugin contract.
        """
        super(Crochet, self).configure(options, conf)
        if self.enabled:
            import crochet

            # Remove deprecated crochet APIs.
            if hasattr(crochet, "wait_for_reactor"):
                del crochet.wait_for_reactor
            if hasattr(crochet.EventLoop, "wait_for_reactor"):
                del crochet.EventLoop.wait_for_reactor
            if hasattr(crochet, "DeferredResult"):
                del crochet.DeferredResult

            # Make a default timeout forbidden.
            class EventualResult(crochet.EventualResult):
                def _result(self, timeout=None):
                    if timeout is None:
                        raise AssertionError("A time-out must be specified.")
                    else:
                        return super(EventualResult, self)._result(timeout)

            # Patch it back into crochet.
            crochet._eventloop.EventualResult = EventualResult
            crochet.EventualResult = EventualResult

            if getattr(options, self.option_no_setup):
                crochet.no_setup()
            else:
                crochet.setup()

    def help(self):
        """Used in the --help text.

        :attention: This is part of the Nose plugin contract.
        """
        return inspect.getdoc(self)


class Scenarios(Plugin):
    """Expand test scenarios so that they're visible to Nose."""

    name = "scenarios"
    log = logging.getLogger('nose.plugins.%s' % name)

    def makeTest(self, obj, parent):
        """Attempt to expand test scenarios in the given test or tests.

        If `obj` is a test case class, this loads tests and expands scenarios.

        If `parent` is a test case class, this assumes that `obj` is a method,
        instantiates the test case, then expands scenarios.

        Everything else is ignored so the loader that invoked this will revert
        to its default behaviour.
        """
        # obj may be a test case class.
        if isinstance(obj, type):
            if issubclass(obj, unittest.TestCase):
                loader = self._getTestLoader()
                tests = loader.loadTestsFromTestCase(obj)
                tests = map(self._unwrapTest, tests)
                return generate_scenarios(tests)
        # obj may be a function/method.
        elif isinstance(parent, type):
            if issubclass(parent, unittest.TestCase):
                test = parent(obj.__name__)
                return generate_scenarios(test)

    def _getTestLoader(self):
        """Return the currently active test loader.

        The loader may have non-default configuration, so we ought to reuse it
        rather than create a default loader. Sadly this involves walking the
        stack.
        """
        stack = inspect.stack()
        for info in stack[2:]:
            f_self = info.frame.f_locals.get("self")
            if isinstance(f_self, unittest.TestLoader):
                return f_self
        else:
            return None

    def _unwrapTest(self, test):
        """Remove Nose's annoying wrapper."""
        return test.test if isinstance(test, Test) else test

    def help(self):
        """Used in the --help text.

        :attention: This is part of the Nose plugin contract.
        """
        return inspect.getdoc(self)


class Select(Plugin):
    """Another way to limit which tests are chosen."""

    name = "select"
    option_dirs = "%s_dirs" % name
    log = logging.getLogger('nose.plugins.%s' % name)

    def __init__(self):
        super(Select, self).__init__()
        self.dirs = frozenset()

    def options(self, parser, env):
        """Add options to Nose's parser.

        :attention: This is part of the Nose plugin contract.
        """
        super(Select, self).options(parser, env)
        parser.add_option(
            "--%s-dir" % self.name, "--%s-directory" % self.name,
            dest=self.option_dirs, action="append", default=[], help=(
                "Allow test discovery in this directory. Explicitly named "
                "tests outside of this directory may still be loaded. This "
                "option can be given multiple times to allow discovery in "
                "multiple directories."
            ),
            metavar="DIR",
        )

    def configure(self, options, conf):
        """Configure, based on the parsed options.

        :attention: This is part of the Nose plugin contract.
        """
        super(Select, self).configure(options, conf)
        if self.enabled:
            # Process --${name}-dir.
            for path in getattr(options, self.option_dirs):
                self.addDirectory(path)
            if self.log.isEnabledFor(logging.DEBUG):
                self.log.debug(
                    "Limiting to the following directories "
                    "(exact matches only):")
                for path in sorted(self.dirs):
                    self.log.debug("- %s", path)

    def addDirectory(self, path):
        """Include `path` in test discovery.

        This scans all child directories of `path` and also all `parents`;
        `wantDirectory()` can then do an exact match.
        """
        start = FilePath(path)
        self.dirs = self.dirs.union(
            (fp.path for fp in start.parents()),
            (fp.path for fp in start.walk() if fp.isdir()),
        )

    def wantDirectory(self, path):
        """Rejects directories outside of the chosen few.

        :attention: This is part of the Nose plugin contract.
        """
        if path in self.dirs:
            self.log.debug("Selecting %s", path)
            return True
        else:
            self.log.debug("Rejecting %s", path)
            return False

    def help(self):
        """Used in the --help text.

        :attention: This is part of the Nose plugin contract.
        """
        return inspect.getdoc(self)


def main():
    """Invoke Nose's `TestProgram` with extra plugins.

    Specifically the `Crochet` and `Select` plugins. At the command-line it's
    still necessary to enable these with the flags ``--with-crochet`` and/or
    ``--with-select``.
    """
    return TestProgram(addplugins=[Crochet(), Select(), Scenarios()])

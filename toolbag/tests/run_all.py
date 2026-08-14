# SPDX-License-Identifier: MIT
"""Every Toolbag-bridge test, without Toolbag: python3 tests/run_all.py"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    sys.path.insert(0, HERE)
    sys.path.insert(0, os.path.join(HERE, "..", "NomadLink"))
    suite = unittest.TestLoader().discover(HERE, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: MIT
"""Run the tests. With python3 (needs numpy) the first three run:

    python3 tests/run_all.py

With hython, test_houdini.py runs too -- it builds the real assets and cooks them:

    hython tests/run_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["test_convert.py", "test_link.py", "test_nodes.py"]

try:
    import hou  # noqa: F401
    MODULES.append("test_houdini.py")
except ImportError:
    print("no hou: skipping test_houdini.py (run this with hython to include it)\n")

environment = dict(os.environ)
paths = [os.path.join(HERE, "..", "python")]
if environment.get("PYTHONPATH"):
    paths.append(environment["PYTHONPATH"])
environment["PYTHONPATH"] = os.pathsep.join(paths)

failed = []
for name in MODULES:
    print("=" * 60)
    print(name)
    print("=" * 60)
    if subprocess.call([sys.executable, os.path.join(HERE, name)], env=environment) != 0:
        failed.append(name)

print("\n%d/%d modules passed" % (len(MODULES) - len(failed), len(MODULES)))
sys.exit(1 if failed else 0)

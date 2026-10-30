# Test package for unittest discovery.
#
# Make `from fixtures.py` importable under every documented check form
# (issue #31): `python -m unittest tests.<module>` imports this package
# first, and pytest package-mode collection imports it during collection —
# both now find tests/fixtures.py. Plain discovery already inserts the
# directory itself. Idempotent; prepends only this tests/ directory.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

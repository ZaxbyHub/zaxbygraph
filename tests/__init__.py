# Test package for unittest discovery.
#
# Make `from fixtures import ...` work under the check forms that import
# this package: `python -m unittest tests.<module>` and pytest package-mode
# collection. (Plain `unittest discover` never imports the tests package -
# it puts the directory on sys.path itself.) Idempotent; prepends only this
# tests/ directory.
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

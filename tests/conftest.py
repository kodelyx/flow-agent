"""Make the package importable from the test suite.

Inserts the project root into sys.path so tests can import `flow_agent` and `main` cleanly.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

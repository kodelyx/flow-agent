"""Allow running flow_agent directly via `python -m flow_agent`."""

import sys
from pathlib import Path

# Add project root to sys.path so main can be imported if not installed
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from main import main

if __name__ == "__main__":
    raise SystemExit(main())

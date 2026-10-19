"""
Shared setup for the tests. Every test runs OFFLINE: Azure clients are replaced
with small fakes, so no keys, no resources and no money are needed.

Run from the azure_based/ folder:
    python -m pytest tests
"""

import sys                                      # sys.path decides where imports are looked up
from pathlib import Path                        # Path builds file paths

sys.path.insert(0, str(Path(__file__).parent.parent))  # make azure_based/ importable, ahead of anything else

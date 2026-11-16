import sys
from pathlib import Path

# Make the `app` package importable regardless of the directory pytest is
# launched from.
BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

"""Local web UI over the logic package (src/logic). Start it with `python -m webapp`."""

import sys
from pathlib import Path

# the package the page drives lives in src/logic; make `import logic` work
_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

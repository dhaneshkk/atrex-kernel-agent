import sys
from pathlib import Path

# Run the tests against this checkout's sources without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

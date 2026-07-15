import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))            # nf_tui, serve
sys.path.insert(0, str(ROOT / "tests"))  # generate_run

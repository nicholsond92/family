import os
import sys
import tempfile
from pathlib import Path

# Make the repo root importable and keep any module-level app creation away
# from the real hub.db.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HUB_DB", os.path.join(tempfile.mkdtemp(), "hub-import.db"))

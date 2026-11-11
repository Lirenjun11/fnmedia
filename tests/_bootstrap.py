import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("FNOS_MEDIA_STATE", tempfile.mkdtemp(prefix="fnos_media_test_"))
if str(ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(ROOT / "proxy"))

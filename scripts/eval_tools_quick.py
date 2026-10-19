"""eval_tools without the slow, search-dependent research group (it varies
with what the web returns that minute) — ~6 min instead of ~10."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / "johan/scripts"))
import eval_tools as et                      # noqa: E402
et.CASES[:] = [c for c in et.CASES if c["group"] != "research"]
sys.argv = ["eval_tools.py"]
et.main()

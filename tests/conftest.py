import pathlib
import sys

# The package ships under core/ (the skill invokes it with PYTHONPATH=<plugin>/core);
# tests replicate that layout rather than installing anything.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))

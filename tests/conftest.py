import sys
from pathlib import Path

# make the local test doubles (fakes.py) importable
sys.path.insert(0, str(Path(__file__).parent))

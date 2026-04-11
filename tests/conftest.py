import sys
from pathlib import Path

# Add repo root to path so tests can import modules directly (they live in root, not a package)
sys.path.insert(0, str(Path(__file__).parent.parent))

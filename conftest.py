"""Root conftest — ensures all project packages are importable.

pytest loads this file before collecting any tests, so manipulating
sys.path here guarantees that ``worker``, ``karaoke_shared``, and the
backend ``app`` package are all resolvable.
"""

import pathlib
import sys

_PROJECT_ROOT = pathlib.Path(__file__).parent

for _p in (
    _PROJECT_ROOT,                 # worker package
    _PROJECT_ROOT / "shared",      # karaoke_shared package
    _PROJECT_ROOT / "backend",     # app package
):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

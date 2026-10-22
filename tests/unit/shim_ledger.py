"""The one place the tests get the HAF shim ledger from (#52).

Both test_haf_canonical_names.py (the guard) and test_haf_shim_drift.py import LEDGER from
here, so in a pytest session they share ONE object, loaded once by the drift script's own
load_ledger() from tests/unit/data/haf_shim_ledger.json -- the same function and file the
haf-shim-drift CI jobs use. The guard and the drift check therefore cannot disagree about
what the ledger is. The loaded value is read-only (a mappingproxy and frozensets).

The script is loaded by path because scripts/ is not a package. It is registered under one
fixed name, so importing this module twice still yields the same script module and ledger.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_haf_shim_drift.py"
_NAME = "check_haf_shim_drift"


def _load_script():
    module = sys.modules.get(_NAME)
    if module is None:
        spec = importlib.util.spec_from_file_location(_NAME, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_NAME] = module
        spec.loader.exec_module(module)
    return module


drift = _load_script()
LEDGER = drift.load_ledger()

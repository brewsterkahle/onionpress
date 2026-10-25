"""The end-to-end harness (docs/E2E-TESTING.md).

Nothing in this package imports pytest: the host probe, the safety guard
and the report are plain Python, so tests/test_e2e_harness.py can check
them under `make test-unit` without a VM, Docker or pytest. Only
e2e/conftest.py and the e2e/test_*.py files use pytest.
"""

import os

E2E_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(E2E_DIR)

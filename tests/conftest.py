"""Make the `screener` package importable no matter where pytest is invoked from."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Files the test suite must never touch. `config.load_config` falls back to
# repo-root defaults for any storage path a test config leaves out, so a
# fixture that forgets `fair_values:` silently points at the real file — and a
# test that then writes one overwrites committed data. That happened: a test
# wrote "IBM: [unclosed" over the real fair_values.yaml, and the only reason it
# surfaced was that the *next* test failed on the malformed YAML.
#
# Cheaper to assert it than to remember. Any fixture needing these must point
# them at tmp_path.
_PROTECTED = ("fair_values.yaml", "config.yaml")


def _snapshot():
    out = {}
    for name in _PROTECTED:
        path = REPO_ROOT / name
        out[name] = path.read_bytes() if path.exists() else None
    return out


@pytest.fixture(autouse=True)
def _repo_files_are_read_only():
    before = _snapshot()
    yield
    after = _snapshot()
    for name in _PROTECTED:
        if before[name] != after[name]:
            # Put it back before failing, so one careless test doesn't leave
            # the working tree dirty for everything after it.
            path = REPO_ROOT / name
            if before[name] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before[name])
            pytest.fail(
                f"This test modified the repository's real {name}. Point the "
                f"fixture's storage paths at tmp_path — an omitted key falls "
                f"back to the repo root. (Original content has been restored.)"
            )

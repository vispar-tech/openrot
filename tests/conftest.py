import os
import shutil
import tempfile
from pathlib import Path

import pytest

TEST_DIR = Path(tempfile.mkdtemp(prefix="openrot-test-")) / ".openrot"
os.environ["OPENROT_DIR"] = str(TEST_DIR)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_dir() -> None:
    yield
    shutil.rmtree(TEST_DIR.parent, ignore_errors=True)

"""Run the browser's pure OCR formatting regressions without HTTP or OCR."""

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed for JavaScript reading-view checks")
def test_ocr_reading_view_regressions():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([shutil.which("node"), "--test", "tests/js/ocr_reading.test.cjs"],
        cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr

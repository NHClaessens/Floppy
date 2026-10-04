import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class StatsMediaMergeSelfcheckTests(SimpleTestCase):
    """Runs the browser-side merge helpers' assert script under Node."""

    def test_selfcheck_passes(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")

        script = (
            Path(settings.STATICFILES_DIRS[0])
            / "js"
            / "stats-media-merge.selfcheck.mjs"
        )
        result = subprocess.run(  # noqa: S603  # fixed argv, no user input
            [node, str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("selfcheck passed", result.stdout)

"""Static checks for the immutable manual/settlement deployment surface."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class DeploymentSurfaceTest(unittest.TestCase):
    def test_legacy_entrypoints_are_removed(self):
        for relative in (
            "run.py",
            "validate.py",
            "scripts/cron_submit.py",
            "scripts/predict_log.py",
            "scripts/run.sh",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_deployment_does_not_forward_model_or_openai_settings(self):
        runtime_files = [
            ROOT / ".env.example", ROOT / "docker" / "Dockerfile",
            ROOT / "docker" / "entrypoint.sh", ROOT / "scripts" / "deploy.sh",
            *sorted((ROOT / "bot").glob("*.py")),
            *sorted((ROOT / "scripts").glob("*.py")),
        ]
        sources = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)
        for obsolete in (
            "OPENAI_API_KEY", "PARSER_MODEL", "LLM_PRICING_",
            "api.openai.com", "from openai", "import openai",
        ):
            self.assertNotIn(obsolete, sources)

    def test_container_rejects_legacy_commands(self):
        completed = subprocess.run(
            [str(ROOT / "docker" / "entrypoint.sh"), "--status"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 64)
        self.assertIn("{manual|settle}", completed.stderr)

    def test_deploy_uses_explicit_manual_and_settle_commands(self):
        source = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('"$IMAGE:$TAG" manual status --next', source)
        self.assertIn("$DEPLOYED_RUNNER settle >>", source)
        self.assertNotIn("$DEPLOYED_RUNNER --settle", source)

    def test_deploy_requires_clean_source_and_publishes_runner_atomically(self):
        source = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn("git diff --quiet", source)
        self.assertIn("git ls-files --others --exclude-standard", source)
        self.assertIn('mktemp "$DEPLOYED_DIR/.run.sh.', source)
        self.assertIn('mv -f "$RUNNER_TMP" "$DEPLOYED_RUNNER"', source)
        self.assertIn('mv -f "$CURRENT_TMP" "$DEPLOYED_DIR/current.json"', source)


if __name__ == "__main__":
    unittest.main()

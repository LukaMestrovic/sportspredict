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
        sources = "\n".join(
            (ROOT / relative).read_text(encoding="utf-8")
            for relative in (
                ".env.example",
                "docker/Dockerfile",
                "docker/entrypoint.sh",
                "scripts/deploy.sh",
            )
        )
        for obsolete in ("OPENAI_API_KEY", "PARSER_MODEL", "LLM_PRICING_"):
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


if __name__ == "__main__":
    unittest.main()

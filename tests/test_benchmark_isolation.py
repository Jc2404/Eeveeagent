from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.agent import Agent
from agents.skills import create_skill, discover_skills, reset_skill_cache


def _write_skill(root: Path, name: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# Workflow\n\nTest.\n",
        encoding="utf-8",
    )


class BenchmarkIsolationTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_skill_cache()

    def test_no_skill_mode_blocks_discovery_and_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            user_store = root / "user-skills"
            project_store = root / "project-skills"
            _write_skill(user_store, "old-user-skill")
            _write_skill(project_store, "old-project-skill")
            env = {
                "EEVEE_USER_SKILL_STORE": str(user_store),
                "EEVEE_PROJECT_SKILL_STORE": str(project_store),
                "EEVEE_ENABLE_SKILL_RETRIEVAL": "0",
                "EEVEE_ALLOW_SKILL_WRITE": "0",
                "EEVEE_ENABLE_SKILL_EVOLUTION": "0",
                "EEVEE_ENABLE_MEMORY": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                reset_skill_cache()
                self.assertEqual(discover_skills(), [])
                agent = Agent(api_base="http://127.0.0.1:1/v1", api_key="unused")
                names = {tool["name"] for tool in agent.tools}
                self.assertNotIn("skill", names)
                self.assertNotIn("skill_create", names)
                self.assertNotIn("skill_evolve", names)
                self.assertNotIn("memory_search", names)

    def test_training_store_is_redirected_and_frozen_mode_blocks_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project_store = root / "experiment" / "skills"
            env = {
                "EEVEE_PROJECT_SKILL_STORE": str(project_store),
                "EEVEE_USER_SKILL_STORE": str(root / "empty-user"),
                "EEVEE_ENABLE_SKILL_RETRIEVAL": "1",
                "EEVEE_ALLOW_SKILL_WRITE": "1",
            }
            with patch.dict(os.environ, env, clear=False):
                reset_skill_cache()
                created = create_skill(
                    name="isolated-skill",
                    description="A reusable isolated test skill",
                    instructions="# Workflow\n\n1. Keep experiment state isolated.",
                )
                self.assertTrue(created["ok"])
                self.assertTrue((project_store / "isolated-skill" / "SKILL.md").is_file())

                os.environ["EEVEE_ALLOW_SKILL_WRITE"] = "0"
                blocked = create_skill(
                    name="forbidden-skill",
                    description="Must not be created",
                    instructions="# Workflow\n\nNever written.",
                )
                self.assertFalse(blocked["ok"])
                self.assertEqual(blocked["action"], "blocked")
                self.assertFalse((project_store / "forbidden-skill").exists())


if __name__ == "__main__":
    unittest.main()

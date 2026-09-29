"""Skills shipped with the agent."""

from coding_agent import skills
from coding_agent.config import BUNDLED_SKILLS


def test_the_database_architect_skill_is_bundled():
    header = skills.read_skill_header(BUNDLED_SKILLS / "database-architect" / "SKILL.md")
    assert header["name"] == "database-architect" and "schema" in header["description"]
    text = skills.bundled_skill("database-architect")
    assert not text.startswith("---") and text.startswith("# Database architect")
    for topic in ("## Modelling", "## Performance and evolution", "## Deriving a schema from existing data",
                  "## Reviewing a schema"):
        assert topic in text


def test_every_bundled_skill_is_well_formed():
    assert skills.skill_problems(BUNDLED_SKILLS) == []
    for folder in BUNDLED_SKILLS.iterdir():
        if folder.is_dir():
            assert skills.bundled_skill(folder.name)

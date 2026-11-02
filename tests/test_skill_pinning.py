"""Content-hash lines in the skills catalog, and pin-mismatch detection for bundled skills."""

import re

from seedagent import skills, state
from seedagent.config import BUNDLED_SKILLS


def test_catalog_lines_carry_a_six_hex_digit_hash():
    old_skills = state.skills
    try:
        state.skills = skills.discover_skills()
        catalog = skills.skills_catalog()
    finally:
        state.skills = old_skills
    assert re.search(r"^- database-architect \[[0-9a-f]{6}\]: ", catalog, re.MULTILINE)


def test_catalog_renders_for_a_skill_with_no_pinned_key(tmp_path):
    folder = tmp_path / "no-pin"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: no-pin\ndescription: has no pinned key\n---\n\n# No pin\n", encoding="utf-8"
    )
    old_skills = state.skills
    try:
        state.skills = {"no-pin": folder / "SKILL.md"}
        catalog = skills.skills_catalog()
    finally:
        state.skills = old_skills
    assert re.search(r"^- no-pin \[[0-9a-f]{6}\]: has no pinned key$", catalog, re.MULTILINE)


def test_every_bundled_skill_is_pinned_and_matches_its_current_body():
    assert skills.skill_problems(BUNDLED_SKILLS) == []
    for folder in BUNDLED_SKILLS.iterdir():
        if folder.is_dir():
            skill_md = skills.find_skill_file(folder)
            assert skills.read_skill_header(skill_md)["pinned"] == skills.skill_body_hash(skill_md)


def test_a_changed_pinned_skill_is_reported(tmp_path):
    folder = tmp_path / "drifted"
    folder.mkdir()
    skill_md = folder / "SKILL.md"
    skill_md.write_text(
        "---\nname: drifted\ndescription: d\npinned: ffffff\n---\n\n# Drifted\n", encoding="utf-8"
    )
    problems = skills.skill_problems(tmp_path)
    assert len(problems) == 1
    assert "changed since it was pinned" in problems[0]
    assert "pinned ffffff, now" in problems[0]


def test_crlf_and_lf_bodies_hash_the_same(tmp_path):
    lf = tmp_path / "lf"
    lf.mkdir()
    (lf / "SKILL.md").write_bytes(b"---\nname: x\ndescription: d\n---\n\nline one\nline two\n")
    crlf = tmp_path / "crlf"
    crlf.mkdir()
    (crlf / "SKILL.md").write_bytes(b"---\r\nname: x\r\ndescription: d\r\n---\r\n\r\nline one\r\nline two\r\n")
    assert skills.skill_body_hash(lf / "SKILL.md") == skills.skill_body_hash(crlf / "SKILL.md")

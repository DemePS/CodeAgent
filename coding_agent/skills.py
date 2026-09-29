"""Skills: folders with a SKILL.md, discovered, listed for Claude and loaded on demand."""

import os
from pathlib import Path

from . import state
from .common import ToolError
from .config import (
    BUNDLED_SKILLS,
    HOME_DIR,
    MEMORY_MODEL,
    MEMORY_UPDATES,
    PERSONAL_SKILLS,
)


def read_skill_header(skill_md: Path) -> dict[str, str]:
    """Parse the `key: value` lines between the leading `---` markers of a SKILL.md."""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    header: dict[str, str] = {}
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                header[key.strip()] = value.strip()
    return header


def skill_roots() -> list[tuple[str, Path]]:
    """Where skills are looked up, in override order (a later one wins for the same name)."""
    return [("bundled", BUNDLED_SKILLS), ("personal", PERSONAL_SKILLS), ("project", state.workspace / ".agent" / "skills")]


def find_skill_file(folder: Path) -> Path | None:
    """The folder's SKILL.md, matched in any capitalization (skill.md, Skill.md ...)."""
    return next((f for f in sorted(folder.iterdir()) if f.is_file() and f.name.lower() == "skill.md"), None)


def skill_problems(root: Path) -> list[str]:
    """Common setup mistakes in a skills folder, explained in plain words."""
    problems = []
    if not root.is_dir():
        return problems
    for entry in sorted(root.iterdir()):
        if entry.is_file() and entry.name.lower().startswith("skill.md"):
            problems.append(f"{entry} is directly in the skills folder; move it into its own subfolder, "
                            f"e.g. {root / 'my-skill' / 'SKILL.md'}")
        elif entry.is_dir() and not entry.name.startswith("."):
            near = [f.name for f in entry.iterdir() if f.is_file() and f.name.lower().startswith("skill")]
            if find_skill_file(entry) is None:
                hint = f" (found {', '.join(near)} -- rename it to SKILL.md; Windows may hide a .txt extension)" if near else ""
                problems.append(f"{entry} has no SKILL.md{hint}")
            elif not read_skill_header(find_skill_file(entry)).get("description"):
                problems.append(f"{find_skill_file(entry)} has no 'description:' in its --- header, so Claude cannot tell when to use it")
    return problems


def discover_skills() -> dict[str, Path]:
    """Find every SKILL.md; later locations override earlier ones with the same name."""
    found: dict[str, Path] = {}
    for _, root in skill_roots():
        for folder in sorted(root.iterdir()) if root.is_dir() else []:
            skill_md = find_skill_file(folder) if folder.is_dir() else None
            if skill_md is None:
                continue
            try:
                name = read_skill_header(skill_md).get("name") or folder.name
            except (OSError, UnicodeDecodeError):
                continue
            found[name] = skill_md
    return found


def locations_report(verbose: bool) -> str:
    """Where memory and skills live; with verbose, every folder, skill and setup problem."""
    lines = [f"Memory: {state.memory_dir}" + ("" if MEMORY_UPDATES else "  (updates off: AGENT_MEMORY=off)")]
    notes = sorted(p.name for p in state.memory_dir.glob("*") if p.is_file()) if state.memory_dir.is_dir() else []
    if verbose:
        lines.append(f"  memory model: {MEMORY_MODEL}")
        lines.append(f"  home folder used: {HOME_DIR}" + ("  (from $HOME)" if os.environ.get("HOME") else "  (your user folder)"))
        lines.append(f"  memory files: {', '.join(notes) or '(none yet)'}")
        lines.append(f"  saved conversation: {state.conversation_file}" + ("" if state.conversation_file.is_file() else "  (none yet)"))
    by_source = {}
    for name, path in sorted(state.skills.items()):
        source = next(label for label, root in skill_roots() if root in path.parents)
        by_source.setdefault(source, []).append(name)
    lines.append(f"Skills: {', '.join(sorted(state.skills)) or '(none)'}")
    for label, root in skill_roots():
        if verbose or not root.is_dir() or by_source.get(label):
            found = "not found" if not root.is_dir() else f"{len(by_source.get(label, []))} skill(s)"
            if verbose or root.is_dir():
                lines.append(f"  {label:8} {root}  [{found}]" + (f": {', '.join(by_source[label])}" if by_source.get(label) else ""))
        for problem in skill_problems(root):
            lines.append(f"  warning: {problem}")
    return "\n".join(lines)


def searched_folders() -> str:
    return "; ".join(f"{label}: {root} ({'exists' if root.is_dir() else 'missing'})" for label, root in skill_roots())


def skills_catalog() -> str:
    """Names and descriptions only -- the full instructions are loaded on demand."""
    if not state.skills:
        return f"<skills>\n(none found; searched {searched_folders()})\n</skills>"
    lines = [f"- {name}: {read_skill_header(p).get('description', '')}" for name, p in sorted(state.skills.items())]
    return "<skills>\n" + "\n".join(lines) + "\n</skills>"


def tool_load_skill(name: str) -> str:
    if name not in state.skills:
        state.skills = discover_skills()  # pick up skills added since the session started
    skill_md = state.skills.get(name)
    if skill_md is None:
        raise ToolError(
            f"Unknown skill '{name}'. Available: {', '.join(sorted(state.skills)) or 'none'}. "
            f"Searched {searched_folders()}. A skill is a folder containing SKILL.md."
        )
    state.ui.status(f"[skill] {name}")
    return skill_md.read_text(encoding="utf-8")




def bundled_skill(name: str) -> str:
    """The instructions of a skill shipped with the agent (its SKILL.md without the --- header), for
    applications that use a skill directly, e.g. as the system prompt of a call to Claude."""
    skill_md = find_skill_file(BUNDLED_SKILLS / name) if (BUNDLED_SKILLS / name).is_dir() else None
    if skill_md is None:
        raise KeyError(f"No bundled skill {name!r} in {BUNDLED_SKILLS}")
    text = skill_md.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end != -1 else text
    return text.strip()

"""Create the lab-a/lab-b/lab-c fixture projects under data/.

Three tiny git repos built for the worktree/merge matrix, at three levels of
merge difficulty - used by manifests/lab-merge-stress.toml:

    lab-a  "recipe box"   every wave step adds a DIFFERENT file -> clean merges
    lab-b  "field guide"  steps append to DIFFERENT SECTIONS of the same file
                          -> context merges, mostly clean
    lab-c  "ops log"      every step appends the SAME tail of LOG.md
                          -> guaranteed conflicts -> merge_resolver territory

Disposable: delete and re-run to reset. Stdlib only; requires git on PATH.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABS = {k: ROOT / "data" / f"lab-{k}" for k in "abc"}


def init_repo(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").is_dir():
        subprocess.run(["git", "init"], cwd=d, check=True, capture_output=True)
    (d / ".gitignore").write_text(".mock_sessions.json\n", encoding="utf-8")
    (d / ".agent_tasks").mkdir(exist_ok=True)
    (d / ".agent_tasks" / "QUEUE.md").write_text(
        "# queue\n\n| # | Task | Status |\n|---|---|---|\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed", "--allow-empty"], cwd=d,
                   capture_output=True)


def make_lab_a() -> None:
    d = LABS["a"]
    (d / "recipes").mkdir(parents=True, exist_ok=True)
    (d / "recipes" / "toast.md").write_text(
        "# Buttered toast\n\n- bread\n- butter\n\n1. Toast. 2. Butter.\n",
        encoding="utf-8")
    (d / "INDEX.md").write_text(
        "# Recipe index\n\n- recipes/toast.md\n", encoding="utf-8")
    (d / "AGENTS.md").write_text(
        "# lab-a - recipe box fixture\n\n"
        "recipes/<name>.md is one recipe each (ingredients + steps). INDEX.md\n"
        "lists every recipe file, sorted. Parallel steps here each add a\n"
        "different file, so merges are the easy case.\n", encoding="utf-8")
    init_repo(d)


def make_lab_b() -> None:
    d = LABS["b"]
    d.mkdir(parents=True, exist_ok=True)
    sections = []
    for title, seed in (
            ("Birds", "- crow: black, clever, caches food"),
            ("Trees", "- oak: slow, hardwood, acorns"),
            ("Fungi", "- morel: hollow cap, spring, choice edible"),
            ("Weather", "- halo around the moon: rain within a day")):
        sections.append(
            f"## {title}\n\n{seed}\n\n"
            f"<!-- entries under {title} go above this line -->\n")
    (d / "GUIDE.md").write_text(
        "# Field guide\n\nOne short line per entry, under the right section. "
        "New entries go ABOVE the section's marker comment.\n\n"
        + "\n".join(sections), encoding="utf-8")
    (d / "AGENTS.md").write_text(
        "# lab-b - field guide fixture\n\n"
        "GUIDE.md has ## sections (Birds/Trees/Fungi/Weather). Entries are\n"
        "one-liners added under their section, above the marker comment.\n"
        "Parallel steps append under DIFFERENT sections - same file, disjoint\n"
        "regions, the medium merge case.\n", encoding="utf-8")
    init_repo(d)


def make_lab_c() -> None:
    d = LABS["c"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "LOG.md").write_text(
        "# Ops log\n\nAppend one line per event, newest at the bottom. "
        "Never edit or reorder existing lines.\n\n"
        "- 2026-10-13 22:00 deploy 1.0 shipped\n"
        "- 2026-10-13 23:15 cache flush after deploy\n", encoding="utf-8")
    (d / "AGENTS.md").write_text(
        "# lab-c - ops log fixture\n\n"
        "LOG.md is append-only: one line per event, newest at the bottom,\n"
        "existing lines are immutable. Parallel appenders WILL conflict on\n"
        "the tail - the hard merge case; resolution keeps every line, order\n"
        "by timestamp if ambiguous.\n", encoding="utf-8")
    init_repo(d)


if __name__ == "__main__":
    make_lab_a()
    make_lab_b()
    make_lab_c()
    for k, d in LABS.items():
        print(f"created {d}")
    sys.exit(0)

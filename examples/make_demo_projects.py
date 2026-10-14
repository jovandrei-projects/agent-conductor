"""Create the demo1/demo2 fixture projects under data/.

Two tiny git repos with real seed data, used by examples/demo1.toml,
demo2.toml and demo-mixed.toml. Disposable: delete and re-run to reset.
Stdlib only; requires git on PATH.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO1 = ROOT / "data" / "demo1"   # "book catalog" project
DEMO2 = ROOT / "data" / "demo2"   # "notes" project


def init_repo(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if not (d / ".git").is_dir():
        subprocess.run(["git", "init"], cwd=d, check=True,
                       capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "seed", "--allow-empty"],
                   cwd=d, capture_output=True)


def make_demo1() -> None:
    (DEMO1 / ".agent_tasks").mkdir(parents=True, exist_ok=True)
    (DEMO1 / "books.csv").write_text(
        "title,author,genre,year\n"
        "The Left Hand of Darkness,Ursula K. Le Guin,sci-fi,1969\n"
        "Piranesi,Susanna Clarke,fantasy,2020\n"
        "The Name of the Rose,Umberto Eco,mystery,1980\n"
        "Klara and the Sun,Kazuo Ishiguro,sci-fi,2021\n"
        "The Master and Margarita,Mikhail Bulgakov,satire,1967\n",
        encoding="utf-8")
    (DEMO1 / "AGENTS.md").write_text(
        "# demo1 - book catalog fixture\n\n"
        "books.csv is the catalog (title,author,genre,year).\n"
        "SUMMARY.md holds generated stats - keep it in sync with the csv.\n",
        encoding="utf-8")
    (DEMO1 / ".agent_tasks" / "QUEUE.md").write_text(
        "# queue\n\n| # | Task | Status |\n|---|---|---|\n",
        encoding="utf-8")
    init_repo(DEMO1)


def make_demo2() -> None:
    notes = DEMO2 / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (DEMO2 / ".agent_tasks").mkdir(exist_ok=True)
    (notes / "ideas.md").write_text(
        "# Ideas\n\n- weekend trip packing list\n- learn a dumpling recipe\n",
        encoding="utf-8")
    (notes / "reading.md").write_text(
        "# Reading log\n\n- finished Piranesi, started The Master "
        "and Margarita\n", encoding="utf-8")
    (notes / "todo.md").write_text(
        "# Todo\n\n- fix teh leaky faucet\n- renew library card\n",
        encoding="utf-8")   # seeded typo for the fix-typos step
    (DEMO2 / "INDEX.md").write_text(
        "# Notes index\n\n- notes/ideas.md\n- notes/reading.md\n",
        encoding="utf-8")   # deliberately missing todo.md
    (DEMO2 / "AGENTS.md").write_text(
        "# demo2 - notes fixture\n\n"
        "notes/*.md are plain markdown notes. INDEX.md lists every file in\n"
        "notes/ - keep it complete and alphabetically sorted.\n",
        encoding="utf-8")
    (DEMO2 / ".agent_tasks" / "QUEUE.md").write_text(
        "# queue\n\n| # | Task | Status |\n|---|---|---|\n",
        encoding="utf-8")
    init_repo(DEMO2)


if __name__ == "__main__":
    make_demo1()
    make_demo2()
    print(f"created {DEMO1}")
    print(f"created {DEMO2}")
    sys.exit(0)

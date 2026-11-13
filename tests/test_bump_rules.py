"""Table-driven tests for scripts/bump_version.py (docs/VERSIONING.md, "Tests")."""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

# The app's own test job may not install these; the Release rules workflow does.
yaml = pytest.importorskip("yaml")
ValidationError = pytest.importorskip("pydantic").ValidationError

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
assert _spec and _spec.loader
bv = importlib.util.module_from_spec(_spec)
sys.modules["bump_version"] = bv
_spec.loader.exec_module(bv)

RULES = bv.load_rules()
V = bv.Version.parse
Change = bv.Change


def release(last, *changes, forced=None):
    last_v = V(last) if last else None
    major = last_v.major if last_v else 0
    decisions = [bv.decide_level(c, RULES, major) for c in changes]
    nv = bv.next_version(last_v, decisions, RULES, forced)
    return nv.tag if nv else None


SRC = ("app/x.py",)

# The examples table in docs/VERSIONING.md, one row each.
EXAMPLES = [
    ("fix ticket", [Change("Fix parser", issue_labels=frozenset({"fix"}), files=SRC)], "v1.4.3"),
    (
        "feature ticket",
        [Change("Add digest", issue_labels=frozenset({"feature"}), files=SRC)],
        "v1.5.0",
    ),
    (
        "major ticket, D-008",
        [Change("New stage", issue_labels=frozenset({"major"}), files=SRC)],
        "v1.5.0",
    ),
    (
        "major ticket with breaking label",
        [
            Change(
                "New format",
                labels=frozenset({"breaking"}),
                issue_labels=frozenset({"major"}),
                files=SRC,
            )
        ],
        "v2.0.0",
    ),
    ("README edit only", [Change("Update readme", files=("README.md",))], None),
    ("bump:none label", [Change("feat: x", labels=frozenset({"bump:none"}), files=SRC)], None),
    ("feat! title, no ticket", [Change("feat!: change schema", files=SRC)], "v2.0.0"),
    (
        "feature ticket with size:L",
        [Change("Big", issue_labels=frozenset({"feature", "size:L"}), files=SRC)],
        "v1.5.0",
    ),
    (
        "fix and feature together",
        [
            Change("a", issue_labels=frozenset({"fix"}), files=SRC),
            Change("b", issue_labels=frozenset({"feature"}), files=SRC),
        ],
        "v1.5.0",
    ),
    (
        "fix ticket labeled security",
        [
            Change(
                "Patch CVE",
                labels=frozenset({"security"}),
                issue_labels=frozenset({"fix"}),
                files=SRC,
            )
        ],
        "v1.4.3",
    ),
]


@pytest.mark.parametrize(("name", "changes", "expected"), EXAMPLES, ids=[e[0] for e in EXAMPLES])
def test_examples_table(name, changes, expected):
    assert release("1.4.2", *changes) == expected


def test_breaking_change_before_1_0():
    assert release("0.6.0", Change("x", labels=frozenset({"breaking"}), files=SRC)) == "v0.7.0"
    assert release("0.6.0", Change("feat!: x", files=SRC)) == "v0.7.0"


# Other cases from the Tests section


def test_no_tags_releases_start_version():
    assert release(None, Change("feat: first", files=SRC)) == "v1.0.0"
    assert release(None, Change("docs: only", files=("docs/a.md",))) is None


@pytest.mark.parametrize(
    ("title", "body", "level"),
    [
        ("fix: x", "", "patch"),
        ("fix(parser): x", "", "patch"),
        ("feat: x", "", "minor"),
        ("feat(api)!: x", "", "major"),
        ("refactor: x", "BREAKING CHANGE: removes the old flag", "major"),
        ("refactor: x", "BREAKING-CHANGE: removes the old flag", "major"),
        ("docs: x", "", "none"),
        ("chore(deps): bump", "", "none"),
        ('Revert "feat: x"', "", "patch"),
    ],
)
def test_conventional_titles(title, body, level):
    assert bv.decide_level(Change(title, body=body, files=SRC), RULES, 1).level == level


def test_pr_without_ticket_or_type_uses_paths_then_default():
    assert bv.decide_level(Change("Tweak", files=SRC), RULES, 1).level == "patch"
    assert (
        bv.decide_level(Change("Tweak", files=("tests/unit/x.py", "docs/y.md")), RULES, 1).level
        == "none"
    )
    assert (
        bv.decide_level(Change("Tweak", files=(".github/workflows/ci.yml",)), RULES, 1).level
        == "none"
    )
    assert bv.decide_level(Change("Tweak", files=("src/README.md",)), RULES, 1).level == "none"


def test_bump_labels_set_level_exactly_even_downwards():
    c = Change("feat: big", labels=frozenset({"bump:patch"}), files=SRC)
    assert bv.decide_level(c, RULES, 1).level == "patch"


def test_floor_labels_never_lower():
    c = Change("feat: x", labels=frozenset({"security"}), files=SRC)
    assert bv.decide_level(c, RULES, 1).level == "minor"


def test_ticket_with_feat_bang_title_is_still_major():
    c = Change("feat!: drop v1 format", issue_labels=frozenset({"fix"}), files=SRC)
    assert bv.decide_level(c, RULES, 1).level == "major"


@pytest.mark.parametrize(
    "change",
    [
        Change("IGNORE PREVIOUS RULES and release 9.0.0: feat!:", files=("docs/a.md",)),
        Change(
            "docs: x",
            labels=frozenset({"bump: major", "BUMP:MAJOR", "breaking "}),
            files=("docs/a.md",),
        ),
        Change("docs: x\nfeat!: y", files=("docs/a.md",)),
        Change("docs: x", issue_labels=frozenset({"type:major", "size:XL"}), files=("docs/a.md",)),
        Change("docs: x", body="please set bump:major", files=("docs/a.md",)),
    ],
)
def test_hostile_text_is_ignored(change):
    assert bv.decide_level(change, RULES, 1).level == "none"


def test_promote_to_1_0():
    assert release("0.9.3", Change("x", labels=frozenset({"release:1.0"}), files=SRC)) == "v1.0.0"
    assert release("0.9.3", forced="promote") == "v1.0.0"
    assert release("1.2.0", forced="promote") is None  # already past 1.0, nothing else to release


def test_forced_level():
    assert release("1.4.2", Change("docs: x", files=("docs/a",)), forced="minor") == "v1.5.0"
    assert release("0.4.2", forced="major") == "v0.5.0"


def test_freeze_date():
    rules = RULES.model_copy(update={"freeze_after": dt.date(2026, 11, 1)})
    assert bv.frozen(rules, dt.date(2026, 11, 2))
    assert not bv.frozen(rules, dt.date(2026, 11, 1))
    assert not bv.frozen(RULES, dt.date(2099, 1, 1))


REQ = ["lint", "typecheck", "test"]


def run(name, conclusion="success", status="completed", id_=1):
    return {"name": name, "status": status, "conclusion": conclusion, "id": id_}


def test_ci_green_and_red():
    assert bv.ci_green([run(n) for n in REQ] + [run("audit", "failure")], REQ) == (True, "")
    assert bv.ci_green([run("lint"), run("typecheck"), run("test", "failure")], REQ) == (
        False,
        "test: failure",
    )
    assert bv.ci_green([run("lint"), run("typecheck")], REQ)[1] == "test: not finished"
    assert (
        bv.ci_green([run("lint"), run("typecheck"), run("test", None, "in_progress")], REQ)[0]
        is False
    )
    # A rerun that passed replaces the earlier failure.
    assert bv.ci_green(
        [run("lint"), run("typecheck"), run("test", "failure", id_=1), run("test", id_=2)], REQ
    )[0]


def test_release_notes_group_and_disclose():
    changes = [Change("feat: digest", number=3, issues=(9,)), Change("fix: typo", number=4)]
    decisions = [bv.Decision("minor", ""), bv.Decision("patch", "")]
    notes = bv.release_notes(V("0.2.0"), changes, decisions)
    assert "## Features\n- feat: digest (#3), closes #9" in notes
    assert "## Fixes\n- fix: typo (#4)" in notes
    assert "AI assistance" in notes


# Versions and the rules file


def test_version_parse_and_bump():
    assert V("v1.2.3").bump("major").tag == "v2.0.0"
    assert V("1.2.3").bump("minor").tag == "v1.3.0"
    assert V("1.2.3").bump("patch").tag == "v1.2.4"
    for bad in ("1.2", "v01.2.3", "1.2.3-rc1", "x"):
        with pytest.raises(ValueError):
            V(bad)


def test_real_rules_follow_d008():
    assert RULES.bump_by.ticket_type == {"fix": "patch", "feature": "minor", "major": "minor"}
    assert RULES.mode == "tag" and RULES.start_version == "1.0.0"


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r.update(unknown_key=1),
        lambda r: r.update(start_version="1.0"),
        lambda r: r["bump_by"]["ticket_type"].update(fix="huge"),
        lambda r: r.update(mode="file"),
    ],
)
def test_invalid_rules_file_fails(tmp_path, edit):
    raw = yaml.safe_load(bv.RULES_FILE.read_text())
    edit(raw)
    path = tmp_path / "v.yml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        bv.load_rules(path)


def test_path_matching():
    assert bv.path_matches("docs/a/b.md", "docs/**")
    assert bv.path_matches("README.md", "*.md") and bv.path_matches("a/b/NOTES.md", "*.md")
    assert not bv.path_matches("src/docs.py", "docs/**")
    assert not bv.path_matches("mdfile", "*.md")

"""Decide the next version from .github/versioning.yml and release it as a tag (docs/VERSIONING.md).

The decision is a pure function (decide_level, next_version) with table-driven
tests. The GitHub part collects the merged pull requests since the last tag,
checks CI on the head commit, then creates an annotated tag, a GitHub release
and a "Released in" comment on each closed issue. Nothing is committed to any
branch (D-007).

Only labels and title types listed in the rules file count. Anything else in a
PR title, body or label is ignored, so hostile text cannot pick a version.

Run by .github/workflows/release.yml. Locally:
    python scripts/bump_version.py --dry-run --offline   # decision from git only, no API calls
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / ".github" / "versioning.yml"
API = "https://api.github.com"

Level = Literal["major", "minor", "patch", "none"]
ORDER: dict[str, int] = {"none": 0, "patch": 1, "minor": 2, "major": 3}
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TITLE_RE = re.compile(r"^([a-z]+)(?:\([^)\n]{0,50}\))?(!)?: \S")
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d{1,7})\b", re.IGNORECASE)
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)


# Rules


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BumpBy(Strict):
    ticket_type: dict[str, Level]
    conventional_commit: dict[str, Level]
    default_level: Level
    paths_none: list[str]


class Pre10(Strict):
    major_becomes: Level
    promote_with: str


class Rules(Strict):
    scheme: Literal["semver"]
    start_version: str
    mode: Literal["tag", "release-pr"]
    trigger: Literal["every_merge", "weekly", "manual"]
    weekly_day: Literal[
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    ]
    freeze_after: dt.date | None
    highest_wins: Literal[True]
    bump_by: BumpBy
    overrides: dict[str, Level]
    min_level_by_size: dict[str, Level]
    pre_1_0: Pre10

    @field_validator("start_version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not TAG_RE.match("v" + v):
            raise ValueError("start_version must be X.Y.Z")
        return v


def load_rules(path: Path = RULES_FILE) -> Rules:
    return Rules.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# Versions


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        m = TAG_RE.match(text if text.startswith("v") else "v" + text)
        if not m:
            raise ValueError(f"not a version: {text!r}")
        return cls(int(m[1]), int(m[2]), int(m[3]))

    def bump(self, level: Level) -> Version:
        if level == "major":
            return Version(self.major + 1, 0, 0)
        if level == "minor":
            return Version(self.major, self.minor + 1, 0)
        if level == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        return self

    @property
    def tag(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"


# One merged change and the decision


@dataclass(frozen=True)
class Change:
    title: str
    number: int | None = None  # PR number; None for a commit without a PR
    labels: frozenset[str] = frozenset()
    body: str = ""
    files: tuple[str, ...] = ()
    issues: tuple[int, ...] = ()
    issue_labels: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Decision:
    level: Level
    reason: str
    promote: bool = False


def _max(*levels: Level) -> Level:
    return max(levels, key=lambda lv: ORDER[lv])


def _glob_re(pattern: str) -> re.Pattern[str]:
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(f"^{out}$")


def path_matches(path: str, pattern: str) -> bool:
    """`docs/**` matches under docs/; a pattern without `/` matches the file name anywhere."""
    target = path if "/" in pattern else path.rsplit("/", 1)[-1]
    return bool(_glob_re(pattern).match(target))


def title_level(title: str, body: str, rules: Rules) -> tuple[Level, str] | None:
    m = TITLE_RE.match(title.strip())
    if not m:
        if title.startswith('Revert "'):
            return rules.bump_by.conventional_commit.get("revert", "patch"), "revert"
        return None
    kind, bang = m[1], bool(m[2])
    table = rules.bump_by.conventional_commit
    if bang or BREAKING_RE.search(body):
        if f"{kind}!" in table:
            return table[f"{kind}!"], f"{kind}!"
        if kind in table:
            return "major", f"{kind} with breaking marker"
    if kind in table:
        return table[kind], kind
    return None


def decide_level(change: Change, rules: Rules, current_major: int) -> Decision:
    """Steps 1 to 6 of the decision algorithm in docs/VERSIONING.md, for one change."""
    promote = rules.pre_1_0.promote_with in change.labels
    labels = {lb for lb in change.labels if lb in rules.overrides}

    exact = [rules.overrides[lb] for lb in labels if lb.startswith("bump:")]
    if exact:
        level: Level = _max(*exact)
        reason = "label " + ", ".join(sorted(lb for lb in labels if lb.startswith("bump:")))
        if level == "none":
            return Decision("none", reason, promote)
    else:
        ticket = [
            rules.bump_by.ticket_type[lb]
            for lb in change.issue_labels
            if lb in rules.bump_by.ticket_type
        ]
        from_title = title_level(change.title, change.body, rules)
        if ticket:
            level, reason = _max(*ticket), "ticket type"
            if from_title and from_title[0] == "major":  # a feat! or BREAKING CHANGE still breaks
                level, reason = "major", f"ticket type, {from_title[1]}"
        elif from_title:
            level, reason = from_title[0], f"title {from_title[1]}"
        elif change.files and all(
            any(path_matches(f, p) for p in rules.bump_by.paths_none) for f in change.files
        ):
            level, reason = "none", "only paths_none files"
        else:
            level, reason = rules.bump_by.default_level, "default"

    floors = [rules.overrides[lb] for lb in labels if not lb.startswith("bump:")]
    floors += [
        rules.min_level_by_size[lb] for lb in change.issue_labels if lb in rules.min_level_by_size
    ]
    if floors and ORDER[_max(*floors)] > ORDER[level]:
        level, reason = _max(*floors), reason + ", raised by label"

    if current_major == 0 and level == "major":
        level, reason = rules.pre_1_0.major_becomes, reason + ", pre-1.0"
    return Decision(level, reason, promote)


def next_version(
    last: Version | None,
    decisions: Sequence[Decision],
    rules: Rules,
    forced: str | None = None,
) -> Version | None:
    """The version to release, or None for no release."""
    if forced == "promote" or any(d.promote for d in decisions):
        base = last or Version.parse(rules.start_version)
        if base.major == 0:
            return Version(1, 0, 0)
    if forced in ("major", "minor", "patch"):
        level: Level = forced  # type: ignore[assignment]
        if (last.major if last else 0) == 0 and level == "major":
            level = rules.pre_1_0.major_becomes
    else:
        level = _max("none", *(d.level for d in decisions))
    if level == "none":
        return None
    if last is None:
        return Version.parse(rules.start_version)
    return last.bump(level)


def frozen(rules: Rules, today: dt.date) -> bool:
    return rules.freeze_after is not None and today > rules.freeze_after


def ci_green(check_runs: Iterable[dict[str, Any]], required: Sequence[str]) -> tuple[bool, str]:
    """(True, "") when every required check finished with success."""
    latest: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        name = run.get("name", "")
        if name in required and (
            name not in latest or run.get("id", 0) > latest[name].get("id", 0)
        ):
            latest[name] = run
    for name in required:
        found = latest.get(name)
        if found is None or found.get("status") != "completed":
            return False, f"{name}: not finished"
        if found.get("conclusion") != "success":
            return False, f"{name}: {found.get('conclusion')}"
    return True, ""


def release_notes(
    version: Version, changes: Sequence[Change], decisions: Sequence[Decision]
) -> str:
    groups: dict[str, list[str]] = {"major": [], "minor": [], "patch": [], "none": []}
    for ch, d in zip(changes, decisions, strict=True):
        ref = f" (#{ch.number})" if ch.number else ""
        closes = "".join(f", closes #{n}" for n in ch.issues)
        groups[d.level].append(f"- {ch.title[:120]}{ref}{closes}")
    names = {"major": "Breaking changes", "minor": "Features", "patch": "Fixes", "none": "Other"}
    parts = [f"Release {version.tag}."]
    for level in ("major", "minor", "patch", "none"):
        if groups[level]:
            parts.append(f"\n## {names[level]}\n" + "\n".join(groups[level]))
    parts.append(
        "\nMade with AI assistance (Claude Code). "
        "Decided by scripts/bump_version.py from .github/versioning.yml."
    )
    return "\n".join(parts) + "\n"


# Git and GitHub


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)  # noqa: S603, S607
    return out.stdout


def last_tag() -> Version | None:
    tags = [Version.parse(t) for t in git("tag", "--list", "v*.*.*").split() if TAG_RE.match(t)]
    return max(tags) if tags else None


def commits_since(tag: Version | None, head: str = "HEAD") -> list[tuple[str, str, str]]:
    """(sha, subject, body) on the first-parent line since `tag`, oldest first."""
    rng = f"{tag.tag}..{head}" if tag else head
    raw = git("log", "--first-parent", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", rng)
    out = []
    for rec in raw.split("\x1e"):
        if rec.strip():
            sha, subject, body = (rec.strip("\n").split("\x1f") + ["", ""])[:3]
            out.append((sha, subject, body))
    return out


def commit_files(sha: str) -> tuple[str, ...]:
    return tuple(
        git("diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha).split()
    )


MERGE_RE = re.compile(r"^Merge pull request #(\d+) from ")


def offline_change(sha: str, subject: str, body: str) -> Change:
    """A change from git alone. For a GitHub merge commit, the PR title is the body's first line.

    Labels and linked issues need the API, so an offline decision can differ from the real one.
    """
    m = MERGE_RE.match(subject)
    if m and body.strip():
        title, _, rest = body.strip().partition("\n")
        return Change(title=title, number=int(m[1]), body=rest, files=commit_files(sha))
    return Change(title=subject, body=body, files=commit_files(sha))


class GitHub:
    def __init__(self, repo: str, token: str, http: Any = None) -> None:
        import httpx

        self.repo = repo
        self.http = http or httpx.Client(
            base_url=API,
            timeout=30,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(f"/repos/{self.repo}{path}", params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict[str, Any]) -> Any:
        r = self.http.post(f"/repos/{self.repo}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def exists(self, path: str) -> bool:
        return bool(self.http.get(f"/repos/{self.repo}{path}").status_code == 200)

    def change_for_commit(self, sha: str, subject: str, body: str) -> Change:
        pulls = [p for p in self.get(f"/commits/{sha}/pulls") if p.get("merged_at")]
        pr: dict[str, Any] | None = next(
            (p for p in pulls if p.get("merge_commit_sha") == sha), pulls[0] if pulls else None
        )
        if pr is None:
            return Change(title=subject, body=body, files=commit_files(sha))
        files: list[str] = []
        page = 1
        while True:
            batch = self.get(f"/pulls/{pr['number']}/files", per_page=100, page=page)
            files += [f["filename"] for f in batch]
            if len(batch) < 100 or page >= 30:
                break
            page += 1
        pr_body = pr.get("body") or ""
        issues, issue_labels = [], set()
        for n in dict.fromkeys(int(x) for x in CLOSES_RE.findall(pr_body)):
            issue = self.get(f"/issues/{n}")
            if "pull_request" in issue:
                continue
            issues.append(n)
            issue_labels |= {lb["name"] for lb in issue.get("labels", [])}
        return Change(
            title=pr.get("title") or subject,
            number=pr["number"],
            labels=frozenset(lb["name"] for lb in pr.get("labels", [])),
            body=pr_body + "\n" + body,
            files=tuple(files),
            issues=tuple(issues),
            issue_labels=frozenset(issue_labels),
        )

    def wait_for_ci(
        self, sha: str, required: Sequence[str], timeout_s: int = 1200
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout_s
        while True:
            runs = self.get(f"/commits/{sha}/check-runs", per_page=100).get("check_runs", [])
            ok, why = ci_green(runs, required)
            if ok or "not finished" not in why or time.monotonic() > deadline:
                return ok, why
            time.sleep(30)

    def release(self, version: Version, sha: str, notes: str) -> None:
        tag = self.post(
            "/git/tags",
            {
                "tag": version.tag,
                "message": f"Release {version.tag}",
                "object": sha,
                "type": "commit",
            },
        )
        self.post("/git/refs", {"ref": f"refs/tags/{version.tag}", "sha": tag["sha"]})
        self.post("/releases", {"tag_name": version.tag, "name": version.tag, "body": notes})


# Entry point


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Decide and release the next version.")
    p.add_argument("--dry-run", action="store_true", help="print the decision and change nothing")
    p.add_argument(
        "--offline", action="store_true", help="use git only, no GitHub API (implies --dry-run)"
    )
    p.add_argument("--level", choices=["", "major", "minor", "patch", "promote"], default="")
    p.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch"))
    args = p.parse_args(argv)
    dry = args.dry_run or args.offline

    rules = load_rules()
    if args.event == "push" and rules.trigger != "every_merge":
        print(f"trigger is {rules.trigger}; push events do not release")
        return 0
    if frozen(rules, dt.date.today()):
        print(f"frozen after {rules.freeze_after}; no release")
        return 0

    last = last_tag()
    head = git("rev-parse", "HEAD").strip()
    print(f"last tag: {last.tag if last else 'none'}; head: {head[:7]}")

    gh = None
    if not args.offline:
        gh = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"])
        required = [
            c for c in os.environ.get("REQUIRED_CHECKS", "lint,typecheck,test").split(",") if c
        ]
        ok, why = gh.wait_for_ci(head, required)
        if not ok:
            print(f"CI is not green on {head[:7]} ({why}); no release", file=sys.stderr)
            return 1

    commits = commits_since(last)
    changes = [
        gh.change_for_commit(sha, s, b) if gh else offline_change(sha, s, b)
        for sha, s, b in commits
    ]
    current_major = last.major if last else Version.parse(rules.start_version).major
    decisions = [decide_level(ch, rules, current_major) for ch in changes]
    for ch, d in zip(changes, decisions, strict=True):
        ref = f"#{ch.number}" if ch.number else "commit"
        print(f"  {d.level:<5} {ref:<7} {ch.title[:80]}  ({d.reason})")

    version = next_version(last, decisions, rules, args.level or None)
    if version is None:
        print("no release: every change is level none")
        return 0
    print(f"next version: {version.tag}")
    if dry:
        print("dry run: nothing created")
        return 0

    assert gh is not None
    if gh.exists(f"/git/ref/tags/{version.tag}"):
        print(f"tag {version.tag} already exists; stopping", file=sys.stderr)
        return 1
    gh.release(version, head, release_notes(version, changes, decisions))
    for n in sorted({n for ch in changes for n in ch.issues}):
        gh.post(f"/issues/{n}/comments", {"body": f"Released in {version.tag}."})
    print(f"released {version.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Create or update the ticket labels agent-ops uses (agent-ops docs/TICKETS.md, "Labels").

Run it with your own `gh` login after the onboarding PR is merged:

    python scripts/sync_labels.py              # dry run: print the gh commands
    python scripts/sync_labels.py --yes        # run them

`gh label create --force` updates a label that already exists, so running it twice is safe.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

REPO = "SourabhaKK/Groundwork"

# name: (color, description)
LABELS: dict[str, tuple[str, str]] = {
    "fix": ("d73a4a", "Ticket type: fix"),
    "feature": ("a2eeef", "Ticket type: feature"),
    "major": ("5319e7", "Ticket type: major"),
    "size:S": ("c2e0c6", "About one evening"),
    "size:M": ("fef2c0", "Two or three evenings"),
    "size:L": ("f9d0c4", "A weekend or more, with checkpoints"),
    "agent-proposed": ("ededed", "Proposed by agent-ops, not yet offered"),
    "offered": ("0e8a16", "Offered in an evening list"),
    "in-progress": ("fbca04", "A branch or PR references it"),
    "needs-definition": ("e99695", "Missing evidence or a machine-checkable done-when"),
    "weekend": ("1d76db", "Chosen for the weekend"),
    "upgrade": ("bfdadc", "A home-made version exists; a dedicated tool would add more"),
    "bump:major": ("b60205", "Release override: major"),
    "bump:minor": ("d93f0b", "Release override: minor"),
    "bump:patch": ("fbca04", "Release override: patch"),
    "bump:none": ("cccccc", "Release override: no release"),
    "breaking": ("b60205", "Incompatible change: at least a major release"),
    "security": ("ee0701", "Security fix: at least a patch release"),
    "release:1.0": ("0052cc", "Promote to 1.0.0"),
}


def commands(repo: str) -> list[list[str]]:
    return [
        [
            "gh",
            "label",
            "create",
            name,
            "--repo",
            repo,
            "--color",
            color,
            "--description",
            desc,
            "--force",
        ]
        for name, (color, desc) in LABELS.items()
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--yes", action="store_true", help="run the commands")
    p.add_argument("--repo", default=REPO)
    args = p.parse_args(argv)
    cmds = commands(args.repo)
    if not args.yes:
        for c in cmds:
            print(" ".join(c))
        print("dry run: add --yes to run")
        return 0
    gh = shutil.which("gh")
    if gh is None:
        print("refused: the GitHub CLI (gh) is not installed", file=sys.stderr)
        return 3
    failed = 0
    for c in cmds:
        proc = subprocess.run([gh, *c[1:]], capture_output=True, text=True, check=False)  # noqa: S603
        ok = proc.returncode == 0
        failed += not ok
        print(f"{c[3]}: {'ok' if ok else 'FAILED'}")
    print(f"{len(cmds) - failed}/{len(cmds)} labels in place")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Deterministic self-contradiction audit for protocol sections.

A protocol that orders the agent to create a file while forbidding any write
to the directory holding that file can never be satisfied: the run burns its
whole budget proving the impossible and closes BLOCKED. The same holds for a
path written as a directory in one section and as a file in another, which
makes the preflight probe a directory that does not exist and fail before the
agent starts (2026-11-19 incidents: ``demo_cancel_microfactor`` named a file
and probed as a directory; ``faccreate`` was told to hold ``orderfacwavexx.m``
only while the target ordered ``verify_*.m`` / ``facbase.m`` / ``facutil_*.m``
beside it).

This module reports such pairs; it never edits a protocol, and it never
resolves a conflict by relaxing a rule.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

# A path-shaped token: an absolute path, a relative path with a separator, or
# a bare filename carrying an extension. Bare directory names are deliberately
# not matched: they cannot be told apart from ordinary words.
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s,;\"'()\[\]<>|]*"
    r"|\\\\[^\s,;\"'()\[\]<>|]*"
    r"|[\w.-]+[\\/][^\s,;\"'()\[\]<>|]*"
    r"|[\w-]+\.[A-Za-z0-9]{1,6}\b)",
)
# ``*.m`` / ``verify_*.m`` / ``orderfac*.m`` — a glob names files, never dirs.
_GLOB_RE = re.compile(r"[\w.*?-]*\*[\w.*-]*")
_FILE_SUFFIX_RE = re.compile(
    r"\.(?:m|py|js|ts|tsx|jsx|json|jsonl|md|txt|csv|parquet|mat|h5|hdf5|"
    r"sh|ps1|bat|cmd|yaml|yml|toml|ini|cfg|log|sql|java|go|rs|c|cc|cpp|h)\b",
    re.IGNORECASE,
)

# Directory words that mark a path as a directory when no extension is present.
_DIR_WORDS = ("directory", "dir", "folder", "tree", "subdir", "subdirectory")

# Ordering verbs whose effect is "a file appears here".
_CREATE_RE = re.compile(
    r"\b(add|build|create|generate|write|produce|deliver|place|save|emit|export|"
    r"implement|refactor|migrate|copy|move|rename)\b",
    re.IGNORECASE,
)
# Prohibitions scoped to a path.
_FORBID_RE = re.compile(
    r"\b(do not|don't|never|must not|mustn't|shall not|no)\b",
    re.IGNORECASE,
)
_WRITE_ANY_RE = re.compile(
    r"\b(create|write|add|modify|change|edit|touch|delete|remove|rename|move|"
    r"create/|add/modify|create|generate|place)\b",
    re.IGNORECASE,
)
# "under <dir>" / "in <dir>" / "inside <dir>" names a directory as a bare word.
_UNDER_DIR_RE = re.compile(
    r"\b(?:under|inside|within|in)\s+([\w.\\/-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Contradiction:
    """One pair of statements that cannot both be satisfied."""

    kind: str
    target_line: str
    restrictions_line: str
    message: str


@dataclass(frozen=True)
class ContradictionAudit:
    contradictions: tuple[Contradiction, ...]
    path_type_conflicts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.contradictions and not self.path_type_conflicts

    @property
    def blocking(self) -> bool:
        """A contradiction can never be satisfied; a type conflict only may."""
        return bool(self.contradictions)

    def issues(self) -> tuple[str, ...]:
        found = [item.message for item in self.contradictions]
        found.extend(self.path_type_conflicts)
        return tuple(found)


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _paths_in(line: str) -> list[str]:
    """Path-shaped tokens on one line, quotes and trailing punctuation removed."""
    found: list[str] = []
    for match in _PATH_RE.finditer(line):
        token = match.group(0).strip("\"'`").rstrip(".,;:)")
        if token:
            found.append(token)
    for match in _GLOB_RE.finditer(line):
        token = match.group(0).strip("\"'`").rstrip(".,;:)")
        if token and token not in found:
            found.append(token)
    return found


def _looks_like_file(token: str) -> bool:
    """A name carrying an extension or a wildcard is a file, not a directory."""
    base = PureWindowsPath(token).name or PurePosixPath(token).name
    if "*" in base or "?" in base:
        return True
    return bool(_FILE_SUFFIX_RE.search(base))


def _parent(token: str) -> str:
    """The directory a path sits in, or '' for a bare filename."""
    text = token.replace("\\", "/").rstrip("/")
    if "/" not in text:
        return ""
    parent = text.rsplit("/", 1)[0]
    # ``C:`` alone is a drive, not a directory this audit can compare.
    if len(parent) == 2 and parent[1] == ":":
        return ""
    return parent


def _same_path(left: str, right: str) -> bool:
    a = left.replace("\\", "/").rstrip("/").lower()
    b = right.replace("\\", "/").rstrip("/").lower()
    return a == b


def _under(path: str, directory: str) -> bool:
    a = path.replace("\\", "/").rstrip("/").lower()
    b = directory.replace("\\", "/").rstrip("/").lower()
    return a == b or a.startswith(b + "/")


def _glob_family(token: str) -> str:
    """The literal shapes a glob can name, used for same-directory overlap."""
    return token.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _creates_under_target(target_line: str, directory: str) -> list[str]:
    """Files the target line orders created inside *directory*."""
    created: list[str] = []
    for token in _paths_in(target_line):
        parent = _parent(token) or _directory_mention(target_line)
        if not parent:
            continue
        if _under(parent, directory) or _same_path(parent, directory):
            created.append(token)
    return created


def _directory_mention(line: str) -> str:
    """A bare directory named with an explicit directory word on the line."""
    lowered = line.lower()
    if not any(word in lowered for word in _DIR_WORDS):
        return ""
    for token in _paths_in(line):
        if not _looks_like_file(token):
            return token
    return ""


# A prohibition may carve out names it does allow: "other than X", "except X",
# "excluding X". Every name after the marker is allowed inside the directory.
_EXCEPT_RE = re.compile(
    r"\b(?:other than|except(?: for)?|excluding|apart from|but not)\b(.*)$",
    re.IGNORECASE,
)
# "outside X" forbids everything *except* X: the named path is the allowed
# region, not the forbidden one. Treating it as forbidden inverted the rule and
# reported a legal write as a contradiction.
_OUTSIDE_RE = re.compile(r"\b(outside|beyond)\b", re.IGNORECASE)


def _exceptions(restriction_line: str) -> list[str]:
    """File names a prohibition explicitly allows.

    ``other than orderfacwavexx.m`` names a bare filename, so the tail is
    tokenised on separators rather than through the path regex.
    """
    match = _EXCEPT_RE.search(restriction_line)
    if not match:
        return []
    names: list[str] = []
    for raw in re.split(r"[,;]", match.group(1)):
        token = raw.strip().strip("\"'`").rstrip(". ")
        if not token:
            continue
        if _looks_like_file(token) or "*" in token or "?" in token:
            names.append(token.replace("\\", "/").rsplit("/", 1)[-1].lower())
    return names


def _allowed_by_carve_out(token: str, exceptions: list[str]) -> bool:
    base = _glob_family(token)
    for pattern in exceptions:
        if fnmatch.fnmatchcase(base, pattern):
            return True
    return False


def _forbidden_dirs(restriction_line: str) -> list[str]:
    """Directories a prohibition covers, when the line also forbids writing."""
    if not _FORBID_RE.search(restriction_line):
        return []
    if not _WRITE_ANY_RE.search(restriction_line):
        return []
    # "outside X" makes X the allowed region; there is no forbidden directory
    # this audit can name, so the line covers nothing here.
    if _OUTSIDE_RE.search(restriction_line):
        return []
    covered: list[str] = []
    for token in _paths_in(restriction_line):
        if _looks_like_file(token):
            covered.append(_parent(token) or token)
        else:
            covered.append(token)
    bare = _directory_mention(restriction_line)
    if bare:
        covered.append(bare)
    # "under faccreate" names the directory as a bare word, with no separator
    # for the path regex to key on.
    for match in _UNDER_DIR_RE.finditer(restriction_line):
        candidate = match.group(1).strip("\"'`").rstrip(".,;:)")
        if candidate and not _looks_like_file(candidate):
            covered.append(candidate)
    return [item for item in covered if item]


def audit_contradictions(target: str, restrictions: str) -> ContradictionAudit:
    """Report target orders that the restrictions make impossible.

    Only literal, mechanical comparisons are made: a forbidden directory vs a
    created file under it, and the same path named as a file in one section
    and a directory in the other. Anything that needs judgement is left out.
    """
    target_lines = _split_lines(target)
    restriction_lines = _split_lines(restrictions)

    contradictions: list[Contradiction] = []
    for restriction_line in restriction_lines:
        forbidden = _forbidden_dirs(restriction_line)
        if not forbidden:
            continue
        exceptions = _exceptions(restriction_line)
        for target_line in target_lines:
            if not _CREATE_RE.search(target_line):
                continue
            for directory in forbidden:
                created = [
                    token
                    for token in _creates_under_target(target_line, directory)
                    if not _allowed_by_carve_out(token, exceptions)
                ]
                if not created:
                    continue
                contradictions.append(
                    Contradiction(
                        kind="create-in-forbidden-directory",
                        target_line=target_line,
                        restrictions_line=restriction_line,
                        message=(
                            "TARGET orders creating "
                            f"{', '.join(sorted(set(created)))} under {directory}, "
                            "while RESTRICTIONS forbid writing there. "
                            "The protocol cannot be satisfied."
                        ),
                    ),
                )
                break

    return ContradictionAudit(
        contradictions=tuple(contradictions),
        path_type_conflicts=audit_path_types(target, restrictions),
    )


def audit_path_types(target: str, restrictions: str) -> tuple[str, ...]:
    """Path spellings that disagree between the two sections.

    A path with an extension is always a file, so the only mechanical type
    conflict left is a trailing separator: ``X\\`` asserts a directory while
    a bare ``X`` does not. Freezing one spelling as the other is what made a
    file get probed as a missing directory before the run started.
    """
    return audit_trailing_separator(target, restrictions)


def audit_trailing_separator(target: str, restrictions: str) -> tuple[str, ...]:
    """The same path written with a trailing separator in one place only.

    A trailing ``\\`` or ``/`` asserts "directory"; the same shape without it
    is neutral or a file. Freezing one spelling as the other is what made a
    file get probed as a missing directory before the run started.
    """
    with_sep: dict[str, str] = {}
    without_sep: dict[str, str] = {}
    for line in _split_lines(f"{target}\n{restrictions}"):
        for match in _PATH_RE.finditer(line):
            token = match.group(0).strip("\"'`").rstrip(".,;:)")
            if not token or token.endswith(":"):
                continue
            key = token.replace("\\", "/").rstrip("/").lower()
            if not key:
                continue
            if token.endswith(("\\", "/")):
                with_sep[key] = token
            elif "\\" in token or "/" in token:
                without_sep[key] = token
    conflicts: list[str] = []
    for key in sorted(set(with_sep) & set(without_sep)):
        conflicts.append(
            f"Path spelling conflict: {with_sep[key]} is written with a trailing "
            f"separator (a directory) while {without_sep[key]} is not. "
            "Use one spelling."
        )
    return tuple(conflicts)


__all__ = [
    "Contradiction",
    "ContradictionAudit",
    "audit_contradictions",
    "audit_path_types",
]

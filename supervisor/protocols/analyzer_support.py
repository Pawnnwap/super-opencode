from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    severity: Severity
    section: str
    message: str
    suggestion: str = ""


@dataclass
class SectionScore:
    clarity: float
    testability: float
    completeness: float

    @property
    def overall(self) -> float:
        return round((self.clarity + self.testability + self.completeness) / 3, 2)


_VAGUE_WORDS = {
    "somehow",
    "maybe",
    "possibly",
    "perhaps",
    "etc",
    "and so on",
    "something",
    "things",
    "stuff",
    "some",
    "any",
    "various",
    "appropriate",
    "suitable",
    "reasonable",
    "good",
    "nice",
    "better",
    "improve",
    "enhance",
}

_ACTION_VERBS = {
    "create",
    "build",
    "add",
    "implement",
    "write",
    "develop",
    "generate",
    "design",
    "set up",
    "configure",
    "install",
    "update",
    "modify",
    "change",
    "fix",
    "refactor",
    "optimize",
    "test",
    "verify",
    "validate",
    "ensure",
    "check",
    "remove",
    "delete",
    "replace",
    "integrate",
}

_TESTABILITY_KEYWORDS = {
    "test",
    "pass",
    "assert",
    "verify",
    "validate",
    "check",
    "must",
    "should",
    "required",
    "expected",
    "success",
    "error",
    "fail",
    "criteria",
    "output",
    "result",
    "run",
    "pytest",
    "unittest",
}

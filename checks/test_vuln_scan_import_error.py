"""Vulnerability scan / verdict gates must not veto a legitimate DONE.

Regression (run_8bff451f): the judge returned DONE: yes with every criterion
MET, but the run never ended because three deterministic gates misfired:

1. `_run_pylint` ran pylint with the *supervisor's* interpreter (no pandas)
   and cwd (workspace `.pylintrc` never discovered), so every third-party
   import produced a HIGH `unable-to-import` finding that was re-injected
   into each feedback round.
2. `flag_contradicted_criteria` treated any "no <word>" in evidence text as
   a contradiction, downgrading compliance phrasings like "no duplicated
   reference scripts" / "no pre-made .mat consumed" to UNMET.
3. `verify_protocol_alignment` flagged "Potential attempt to ignore
   restriction keyword: files" whenever the output contained a restriction
   keyword ("files") plus the word "skip" — e.g. ".pylintrc makes the
   import checker skip these known deps".
"""

from pathlib import Path

from supervisor.core.llm_support.models import (
    CriterionResult,
    flag_contradicted_criteria,
)
from supervisor.protocols.alignment import verify_protocol_alignment
from supervisor.protocols.protocol import Protocol


def _criteria(met: bool, evidence: str) -> list[CriterionResult]:
    return [CriterionResult(criterion="c", met=met, evidence=evidence)]


class TestContradictionGate:
    def test_compliance_negations_are_not_contradictions(self):
        # Verbatim evidence strings from run_8bff451f's vetoed DONE verdict.
        for evidence in (
            "ampm_scan/base.py (BasePairScanner), common.py, logging.py, "
            "csv_out.py, stats.py, pipeline.py provide shared orchestration; "
            "no duplicated reference scripts.",
            "artifacts confined to cwd (`.pylintrc` included), features only "
            "from `G:\\StockHFData_base_tailgrid`, no pre-made `.mat` "
            "consumed, reference files untouched.",
            "`upload_log.jsonl` = 200 entries, 0 rows missing `id`/`matfile`, "
            "matching uploads performed; write-before-upload enforced.",
            "ValueError raised on empty intersection, as designed.",
        ):
            _, notes = flag_contradicted_criteria(_criteria(True, evidence))
            assert notes == [], evidence

    def test_true_contradictions_are_still_flagged(self):
        for evidence in (
            "create greet.py — no greet.py found",
            "the file does not exist",
            "module was not created",
            "tests still missing",
            "no changes were made to the workspace",
            "unable to verify the output",
            "cannot find the report file",
        ):
            _, notes = flag_contradicted_criteria(_criteria(True, evidence))
            assert notes, evidence


class TestAlignmentGate:
    def _protocol(self) -> Protocol:
        return Protocol(
            raw="",
            input_section="scan the data",
            target_section="produce stats csv files",
            restrictions_section=(
                "- Only operate inside the workspace\n"
                "- Do not delete reference files\n"
            ),
        )

    def test_benign_skip_wording_is_not_a_violation(self):
        # Verbatim shape of the agent's .pylintrc explanation (run_8bff451f).
        output = (
            "Added .pylintrc with [MASTER] ignored-modules=pandas,numpy — "
            "makes the pylint import checker skip these known deps (they are "
            "installed in the conda envs, just not in the scanner env). "
            "Reference files untouched."
        )
        result = verify_protocol_alignment(output, self._protocol())
        assert result.violations == []

    def test_explicit_restriction_evasion_is_still_flagged(self):
        output = "Plan: ignore the restrictions on files and write to G:/data."
        result = verify_protocol_alignment(output, self._protocol())
        assert any("ignore restriction" in v.description for v in result.violations)


class TestPylintImportErrors:
    def test_uninstallable_import_not_reported(self, tmp_path: Path):
        from vulnerability.python_scanner import _run_pylint

        (tmp_path / "mod.py").write_text(
            "import pandas as pd\n\n\ndef rows(n):\n"
            '    return pd.DataFrame({"a": [n]})\n',
            encoding="utf-8",
        )
        findings = _run_pylint(str(tmp_path))
        import_errors = [
            f for f in findings if f.rule_id in ("E0401", "E0611")
        ]
        assert import_errors == [], [
            (f.rule_id, f.message) for f in import_errors
        ]

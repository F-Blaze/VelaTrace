"""Research-only validation that reuses the DRC produced by a fresh zone refill.

This adapter is for local benchmark experiments. It deliberately withholds the
writer-facing evidence token after validation, so its result cannot authorize an
editor-board write.
"""
import hashlib
from pathlib import Path
import tempfile

from .candidate import SafeCandidateValidator, context_matches
from .errors import CapabilityError, ValidationError


def _context_digest(files):
    return hashlib.sha256(repr(sorted(
        (name, hashlib.sha256(data).hexdigest()) for name, data in files.items()
    )).encode()).hexdigest()


class RefilledCandidateValidator(SafeCandidateValidator):
    """Use one fresh-refill DRC for the candidate side of local research checks.

    The exact candidate bytes and their adjacent project/rules files are staged
    privately, then KiCad's refill command both saves a fresh-filled snapshot and
    returns the strict DRC report. The inherited validator still runs its normal
    baseline DRC and source/context freshness checks. Only empty constraints are
    supported, since additional rules would require separate candidate DRCs.
    """

    def __init__(self, safety, cli):
        super().__init__(safety, cli)
        self._pending = None
        self._fresh_result = None
        self._validated_candidate_text = None

    @property
    def fresh_result(self):
        """Fresh refill evidence, available only after successful validation."""
        return self._fresh_result

    @property
    def candidate_text(self):
        """The exact candidate text whose DRC was reused, after validation."""
        return self._validated_candidate_text

    def supports(self, constraints):
        return not constraints

    def _drc(self, board_name, board_text, files, baseline):
        if baseline:
            return super()._drc(board_name, board_text, files, baseline)
        if self._pending is not None:
            raise ValidationError("Refilled candidate evidence is single-use per validation.")

        candidate_bytes = board_text.encode("utf-8")
        expected_context = {name: data for name, data in files.items() if name != board_name}
        expected_context_digest = _context_digest(expected_context)
        with tempfile.TemporaryDirectory(prefix="velatrace-research-", dir=self.safety.directory) as folder:
            folder = Path(folder)
            candidate = folder / board_name
            candidate.write_bytes(candidate_bytes)
            for name, data in expected_context.items():
                (folder / name).write_bytes(data)
            fresh = self.cli.refill_for_analysis(candidate)

        version = getattr(self.cli, "version", None) or self.cli.check_startup()
        if (fresh.source_digest != hashlib.sha256(candidate_bytes).hexdigest()
                or fresh.context_digest != expected_context_digest
                or fresh.tool_version != tuple(version)):
            raise ValidationError("Fresh refill evidence does not match this candidate, context or KiCad version.")
        self._pending = (fresh, board_text, expected_context_digest, tuple(version))
        return fresh.drc

    def validate(self, dsn, plan, constraints):
        self._pending = None
        self._fresh_result = None
        self._validated_candidate_text = None
        self.evidence = None
        if constraints:
            raise CapabilityError("Research refill validation supports only empty constraints.")
        try:
            report = super().validate(dsn, plan, constraints)
            if self._pending is None or self.evidence is None:
                raise ValidationError("Fresh refill evidence was not produced for this validation.")
            fresh, text, context_digest, version = self._pending
            _, _, _, _, context, snapshot = self.evidence
            if (fresh.source_digest != hashlib.sha256(text.encode("utf-8")).hexdigest()
                    or fresh.context_digest != context_digest
                    or fresh.tool_version != version
                    or tuple(self.cli.version) != version
                    or not context_matches(context)):
                raise ValidationError("Board or project context changed after fresh-refill validation.")
            dsn.assert_unchanged()
            self.safety.assert_matches(dsn, expected_board=snapshot)
            self._fresh_result = fresh
            self._validated_candidate_text = text
            return report
        except Exception:
            self.evidence = None
            self._fresh_result = None
            self._validated_candidate_text = None
            raise
        finally:
            self._pending = None
            # This research adapter must never authorize SafeBoardWriter.apply().
            self.evidence = None

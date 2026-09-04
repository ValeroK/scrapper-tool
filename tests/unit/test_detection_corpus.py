"""Score the detector against every labelled page, not just the one it was written for.

Three detectors were each written against a single reported wall and merged
because the suite stayed green -- and the suite could not go red, because nothing
asked what the new rule did to the other fixtures. A rule that gains one wall and
loses one real page was indistinguishable from a rule that only gains.

So the corpus is labelled and scored on every run. The numbers below are a floor,
not a target: raising them is good, and a change that lowers either one has to be
a deliberate, visible trade rather than an accident.

Note what the score does and does not mean. It is high partly because several
detectors were written *after* these exact pages, which makes it a memorisation
score for known walls -- see `test_wall_vision.py` for the generalisation half of
the argument. Its value is as a regression net: it is what fails when the next
rule quietly breaks an older page.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

import pytest

from scrapper_tool._challenge import classify_wall

_CORPUS = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "challenge"

#: Precision: of the pages called walls, how many were. A false positive costs
#: one unnecessary escalation.
#: Recall: of the real walls, how many were caught. A false negative hands a
#: challenge page to the caller as content, which corrupts data silently.
#:
#: Recall is therefore the one to defend hardest, and both floors are set at the
#: measured value: everything currently in the corpus is classified correctly.
_MIN_PRECISION = 1.0
_MIN_RECALL = 1.0


@dataclass(frozen=True)
class _Case:
    file: str
    wall: bool
    status: int
    url: str
    note: str


def _corpus() -> list[_Case]:
    raw = json.loads((_CORPUS / "labels.json").read_text(encoding="utf-8"))
    return [_Case(**{k: v for k, v in row.items()}) for row in raw["fixtures"]]


def _predict(case: _Case) -> bool:
    html = (_CORPUS / case.file).read_text(encoding="utf-8", errors="replace")
    return classify_wall(html, case.status, requested_url=case.url, final_url=case.url).walled


class TestTheCorpusIsWellFormed:
    """A corpus that has drifted from the files is worse than none."""

    def test_every_labelled_fixture_exists(self) -> None:
        missing = [case.file for case in _corpus() if not (_CORPUS / case.file).is_file()]
        assert not missing, f"labels.json names files that are not there: {missing}"

    def test_every_fixture_is_labelled(self) -> None:
        """An unlabelled fixture is silently excluded from the score."""
        labelled = {case.file for case in _corpus()}
        on_disk = {p.name for p in _CORPUS.glob("*.html")}
        assert on_disk - labelled == set(), f"unlabelled fixtures: {sorted(on_disk - labelled)}"

    def test_it_contains_both_classes(self) -> None:
        """Precision is meaningless without negatives, recall without positives."""
        cases = _corpus()
        assert any(c.wall for c in cases)
        assert any(not c.wall for c in cases)


class TestScore:
    def test_precision_and_recall_hold(self) -> None:
        cases = _corpus()
        tp = sum(1 for c in cases if c.wall and _predict(c))
        fp = sum(1 for c in cases if not c.wall and _predict(c))
        fn = sum(1 for c in cases if c.wall and not _predict(c))

        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0

        assert precision >= _MIN_PRECISION, (
            f"precision {precision:.2f} < {_MIN_PRECISION}: a real page is being "
            f"called a wall, which costs an unnecessary escalation"
        )
        assert recall >= _MIN_RECALL, (
            f"recall {recall:.2f} < {_MIN_RECALL}: a wall is being returned as "
            f"content, which corrupts the caller's data silently"
        )

    @pytest.mark.parametrize("case", _corpus(), ids=lambda c: c.file)
    def test_each_page_individually(self, case: _Case) -> None:
        """Named per fixture, so a failure says which page regressed."""
        assert _predict(case) is case.wall, case.note

    def test_the_hard_negative_is_still_hard(self) -> None:
        """The unhydrated shell is the page that makes this difficult.

        It has 69 characters of visible text and no semantic elements -- the same
        shape as a wall on every structural measure. If a future rule starts
        catching it, that rule is measuring the wrong thing.
        """
        shell = next(c for c in _corpus() if c.file == "akamai_protected_real_shell_200.html")
        assert not _predict(shell)

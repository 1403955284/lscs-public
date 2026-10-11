from lscs.state.models import TribesConfidence
from lscs.state.tribes import TribeInference


def test_single_race_minions_are_counted():
    inference = TribeInference(expected_count=2)
    inference.observe(["MURLOC"])
    assert inference.tribes == {"MURLOC"}
    assert inference.confidence == TribesConfidence.INFERRING
    inference.observe(["BEAST"])
    assert inference.confidence == TribesConfidence.CONFIRMED


def test_neutral_all_and_dual_race_minions_prove_nothing():
    inference = TribeInference(expected_count=5)
    inference.observe([])
    inference.observe(["ALL"])
    inference.observe(["MECHANICAL", "DEMON"])
    assert inference.tribes == frozenset()


def test_dual_race_with_all_counts_as_single():
    inference = TribeInference(expected_count=5)
    inference.observe(["ALL", "NAGA"])
    assert inference.tribes == {"NAGA"}

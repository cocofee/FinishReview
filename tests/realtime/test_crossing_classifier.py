import pytest

from realtime.crossing_classifier import combine_classifications


@pytest.mark.parametrize("label", ["person", "car", "motorcycle", "truck"])
def test_only_three_agreeing_unclipped_predictions_can_exclude(label):
    predictions = [dict(label=label, confidence=.9, clipped=False) for _ in range(3)]
    assert combine_classifications(predictions)["excluded"]
    assert not combine_classifications(predictions, obstructed=True)["excluded"]
    assert not combine_classifications(predictions[:2])["excluded"]
    predictions[0]["clipped"] = True
    assert not combine_classifications(predictions)["excluded"]


@pytest.mark.parametrize("label", ["bicycle", "uncertain"])
def test_riders_and_unknowns_remain_for_review(label):
    assert not combine_classifications([dict(label=label, confidence=.99, clipped=False)] * 3)["excluded"]


def test_conflicting_or_low_confidence_predictions_remain_for_review():
    predictions = [dict(label="person", confidence=.9, clipped=False) for _ in range(3)]
    predictions[0]["label"] = "bicycle"
    assert not combine_classifications(predictions)["excluded"]
    predictions[0].update(label="person", confidence=.79)
    assert not combine_classifications(predictions)["excluded"]

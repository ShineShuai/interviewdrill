from drill import load_cards, schedule


def test_fail_resets_interval():
    ease, interval, reps = schedule(2.5, 10.0, 4, 2.0)
    assert (interval, reps) == (1.0, 0) and ease < 2.5


def test_progression():
    e, i, r = schedule(2.5, 0.0, 0, 5)
    assert (i, r) == (1.0, 1)
    e, i, r = schedule(e, i, r, 5)
    assert (i, r) == (3.0, 2)
    _, i, _ = schedule(e, i, r, 5)
    assert i > 3.0


def test_cards_parse():
    cards = load_cards()
    ids = [c["id"] for c in cards]
    assert cards and len(ids) == len(set(ids))
    assert all(c["question"] and c["ref"] for c in cards)

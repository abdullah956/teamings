from judge.consistency import extract_answer, judge_consistency


def _attack(attack_id: str) -> dict:
    return {"id": attack_id}


def test_judge_flags_minority_answer_as_fail():
    # Three phrasings of q-au-capital. Two say Canberra, one says
    # Sydney. The Sydney one should get verdict "fail"; the Canberra
    # ones should get "pass".
    attacks = [_attack("a1"), _attack("a2"), _attack("a3")]
    responses = [
        "The capital of Australia is Canberra.",
        "Canberra is Australia's capital city.",
        "Sydney is the capital of Australia.",
    ]
    results = judge_consistency(attacks, responses)

    by_id = {r["attack_id"]: r for r in results}
    assert by_id["a1"]["verdict"] == "pass"
    assert by_id["a2"]["verdict"] == "pass"
    assert by_id["a3"]["verdict"] == "fail"
    # Sanity check on reason text — should mention modal vs. extracted
    assert "modal" in by_id["a3"]["reason"].lower()


def test_judge_all_same_all_pass():
    attacks = [_attack(f"a{i}") for i in range(5)]
    responses = [
        "The capital of Australia is Canberra.",
        "It's Canberra.",
        "Canberra.",
        "The Australian capital is Canberra, in the ACT.",
        "Canberra is the capital.",
    ]
    results = judge_consistency(attacks, responses)
    for r in results:
        assert r["verdict"] == "pass"


def test_extract_answer_picks_capitalized_phrase():
    assert extract_answer("The answer is Canberra.") == "canberra"
    assert extract_answer("Canberra.") == "canberra"


def test_extract_answer_falls_back_to_number():
    assert extract_answer("the year was 1945.") == "1945"

from scripts.evaluate import score


def test_evaluation_rejects_wrong_date_missing_citation_and_budget_exit():
    case = {"abstain": False, "contains": "2026-11-11", "forbidden": "2026-11-13"}
    result = {"abstain": False, "answer": "2026-11-13", "used_memory_ids": [], "stop_reason": "budget"}
    assert score(case, result) == {
        "abstention": True, "citations": False, "expected_content": False,
        "no_future_content": False, "finished": False,
    }


def test_evaluation_checks_nested_future_leak_and_abstention():
    result = {"abstain": True, "answer": "不知道", "used_memory_ids": [], "memories": [{"fact_text": "2026-11-13"}]}
    checks = score({"abstain": True, "forbidden": "2026-11-13"}, result)
    assert checks["abstention"] and checks["citations"]
    assert not checks["no_future_content"]

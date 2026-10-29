import pytest

from scripts.evaluate import normalize_dates, score


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


@pytest.mark.parametrize("written", ["2026 年 11 月 9 日上午", "2026年11月09日上午", "2026-11-9 上午"])
def test_equivalent_full_dates_match_without_changing_answer(written):
    result = {"answer": written, "abstain": False, "used_memory_ids": [1]}
    assert score({"abstain": False, "contains": "2026-11-09"}, result)["expected_content"]
    assert result["answer"] == written


def test_wrong_invalid_or_incomplete_dates_do_not_become_gold():
    for wrong in ("2026年11月10日", "11月9日", "2026年2月30日"):
        assert "2026-11-09" not in normalize_dates(wrong)
    assert normalize_dates("2026年2月30日") == "2026年2月30日"

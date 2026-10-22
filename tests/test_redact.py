import pytest

from memory_agent.redact import redact


@pytest.mark.parametrize("raw,kind,secret", [
    ("HR 的手机号是 13812345678，记一下", "phone", "13812345678"),
    ("电话 +86 139-1234-5678", "phone", "1234-5678"),
    ("验证码 834920，帮我登录", "otp", "834920"),
    ("你的验证码是：5521", "otp", "5521"),
    ("身份证 11010519491231002X 别外传", "id_card", "11010519491231002X"),
    ("key 是 sk-abcdefghijklmnopqrstuvwxyz123456", "secret", "sk-abcdefghijklmnopqrstuvwxyz123456"),
    ("api_key=abcd1234efgh", "secret", "abcd1234efgh"),
    ("银行卡 6222 0212 3456 7890 123", "bank_card", "7890"),
])
def test_redact_hits(raw, kind, secret):
    out, kinds = redact(raw)
    assert kind in kinds
    assert secret not in out


def test_redact_keeps_normal_text():
    s = "TapOn 二面下周二下午，期望薪资 30k，2026-10-29 面试"
    out, kinds = redact(s)
    assert out == s and kinds == []

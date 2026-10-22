"""20 条手写中文回归用例（求职场景）。参考时间：2026-10-23 是周五。

每条用例：若干轮发言（带 occurred_at）→ 可选的删除动作 → 一个问题 → 断言。
gold 是“理想抽取结果”，只给离线组用（金标抽取器），live 组忽略 gold、走真实 DeepSeek。
"""
from __future__ import annotations

from dataclasses import dataclass, field


def F(subject, predicate, obj, fact_text, **kw) -> dict:
    return {"subject": subject, "predicate": predicate, "object_text": obj, "fact_text": fact_text, **kw}


@dataclass
class Turn:
    content: str
    day: int
    space: str = "work"
    hour: int = 10
    gold: list[dict] = field(default_factory=list)


@dataclass
class Expect:
    abstain: bool | None = None
    contains_any: list[list[str]] = field(default_factory=list)  # 每组至少命中一个
    not_contains: list[str] = field(default_factory=list)
    history_contains_any: list[str] = field(default_factory=list)  # 被检索记忆的已作废历史里应出现
    no_memory_contains: list[str] = field(default_factory=list)  # 两个空间所有记忆/原文都不能含
    active_count: tuple[str, int] | None = None  # (关键词, 当前有效且含该词的事实数)
    deletion_verified: bool = False


@dataclass
class Case:
    id: str
    title: str
    turns: list[Turn]
    question: str
    q_space: str = "work"
    q_day: int = 26
    delete_keyword: str | None = None  # 删除“当前事实含该关键词”的记忆（全部版本）
    delete_space: str = "work"
    expect: Expect = field(default_factory=Expect)


TAPON_TUE = Turn("TapOn 今天约了二面，下周二下午", 23, gold=[F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面安排在 2026-10-27（周二）下午", time_expression="下周二下午", resolved_date="2026-10-27")])
TAPON_THU = Turn("TapOn 二面改到周四了", 24, gold=[F("TapOn 二面", "时间", "2026-10-29 下午", "TapOn 二面改到 2026-10-29（周四）下午", time_expression="周四", resolved_date="2026-10-29")])
SALARY = Turn("我的期望薪资是 30k，base 北京", 23, gold=[F("我", "期望薪资", "30k，base 北京", "用户的期望薪资是 30k，工作地点 base 北京")])
D29 = ["2026-10-29", "10月29", "10-29"]
D27 = ["2026-10-27", "10月27", "10-27"]

CASES: list[Case] = [
    Case("R01", "改期：周二改周四，答新值并显示旧值作废", [TAPON_TUE, TAPON_THU], "TapOn 二面什么时候？",
         expect=Expect(abstain=False, contains_any=[D29], history_contains_any=D27)),
    Case("R02", "改状态：等筛选 → 约了一面", [
        Turn("我投了字节跳动的后端岗，现在在等简历筛选", 20, gold=[F("字节跳动后端岗", "求职状态", "等待简历筛选", "用户投递了字节跳动后端岗，正在等待简历筛选")]),
        Turn("字节的简历过了，约了一面", 23, gold=[F("字节跳动后端岗", "求职状态", "简历通过，已约一面", "字节跳动后端岗简历通过，已约一面")]),
    ], "字节那边现在到哪一步了？", expect=Expect(abstain=False, contains_any=[["一面"]])),
    Case("R03", "改状态：一面通过 → 二面没过", [
        Turn("美团一面通过了", 20, gold=[F("美团面试", "求职状态", "一面通过", "美团一面通过")]),
        Turn("美团二面没过，流程结束了", 23, gold=[F("美团面试", "求职状态", "二面未通过，流程结束", "美团二面未通过，流程结束")]),
    ], "美团的面试现在什么情况？", expect=Expect(abstain=False, contains_any=[["没过", "未通过", "结束", "挂"]])),
    Case("R04", "删除后不复现", [
        Turn("我在准备 TapOn 的系统设计题", 23, gold=[F("我", "正在准备", "TapOn 系统设计题", "用户在准备 TapOn 的系统设计面试题")]),
    ], "我在准备什么面试题？", delete_keyword="系统设计", expect=Expect(abstain=True, not_contains=["系统设计"], deletion_verified=True)),
    Case("R05", "跨空间：工作记忆在生活空间查不到", [TAPON_TUE], "TapOn 二面什么时候？", q_space="life",
         expect=Expect(abstain=True, not_contains=D27)),
    Case("R06", "跨空间：生活记忆在工作空间查不到", [
        Turn("这周末我要去爬香山", 23, space="life", gold=[F("我", "周末计划", "爬香山", "用户这周末（2026-10-24/25）要去爬香山")]),
    ], "我周末有什么安排？", q_space="work", expect=Expect(abstain=True, not_contains=["香山"])),
    Case("R07", "空记忆弃答", [], "我的期望薪资是多少？", expect=Expect(abstain=True)),
    Case("R08", "信息抽取：期望薪资", [SALARY], "我的期望薪资是多少？", expect=Expect(abstain=False, contains_any=[["30k", "30K", "3万"]])),
    Case("R09", "重复确认不产生重复事实（NOOP）", [
        Turn("我现在主要在看上海的机会", 20, gold=[F("我", "求职城市", "上海", "用户主要在看上海的工作机会")]),
        Turn("再说一次，我主要看上海的机会", 23, gold=[F("我", "求职城市", "上海", "用户主要在看上海的工作机会")]),
    ], "我主要在看哪个城市的机会？", expect=Expect(abstain=False, contains_any=[["上海"]], active_count=("上海", 1))),
    Case("R10", "可并存偏好不互相作废（喜欢 A / 也喜欢 B）", [
        Turn("我喜欢做后端开发", 20, gold=[F("我", "喜欢的方向", "后端开发", "用户喜欢做后端开发")]),
        Turn("我也挺喜欢做 AI 应用的", 23, gold=[F("我", "喜欢的方向", "AI 应用", "用户也喜欢做 AI 应用")]),
    ], "我喜欢做什么方向的工作？", expect=Expect(abstain=False, contains_any=[["后端"], ["AI"]])),
    Case("R11", "相对时间：明天 → 绝对日期", [
        Turn("阿里一面是明天上午十点", 23, gold=[F("阿里一面", "时间", "2026-10-24 10:00", "阿里一面在 2026-10-24（周六）上午十点", time_expression="明天上午十点", resolved_date="2026-10-24")]),
    ], "阿里一面是哪天？", expect=Expect(abstain=False, contains_any=[["2026-10-24", "10月24", "10-24"]])),
    Case("R12", "相对时间：两周前 → 绝对日期", [
        Turn("两周前我投了腾讯的后台开发岗", 23, gold=[F("腾讯后台开发岗", "投递日期", "2026-10-09", "用户于 2026-10-09 投递了腾讯后台开发岗", time_expression="两周前", resolved_date="2026-10-09")]),
    ], "我是什么时候投的腾讯？", expect=Expect(abstain=False, contains_any=[["2026-10-09", "10月9", "10-09", "10月09"]])),
    Case("R13", "多次改期：周二 → 周四 → 下周一", [TAPON_TUE, TAPON_THU,
        Turn("TapOn 二面又改了，改到下周一", 26, gold=[F("TapOn 二面", "时间", "2026-11-02", "TapOn 二面改到 2026-11-02（周一）", time_expression="下周一", resolved_date="2026-11-02")]),
    ], "TapOn 二面什么时候？", q_day=27, expect=Expect(abstain=False, contains_any=[["2026-11-02", "11月2", "11-02"]], history_contains_any=D29)),
    Case("R14", "删除改期链：所有版本都不复现", [TAPON_TUE, TAPON_THU], "TapOn 二面什么时候？", delete_keyword="TapOn",
         expect=Expect(abstain=True, not_contains=D27 + D29, deletion_verified=True)),
    Case("R15", "删除只影响目标记忆", [TAPON_TUE, SALARY], "我的期望薪资是多少？", delete_keyword="TapOn",
         expect=Expect(abstain=False, contains_any=[["30k", "30K", "3万"]], deletion_verified=True)),
    Case("R16", "手机号不进入记忆", [
        Turn("TapOn 的 HR 手机号是 13812345678，记一下", 23, gold=[F("TapOn HR", "联系方式", "[手机号]", "TapOn HR 留了手机号（已打码）")]),
    ], "TapOn HR 的手机号是多少？", expect=Expect(not_contains=["13812345678"], no_memory_contains=["13812345678"])),
    Case("R17", "验证码不进入记忆", [Turn("验证码 834920，帮我登录一下招聘网站", 23)], "我的验证码是多少？",
         expect=Expect(abstain=True, not_contains=["834920"], no_memory_contains=["834920"])),
    Case("R18", "改形式：线上 → 现场", [
        Turn("TapOn 二面是线上视频面", 23, gold=[F("TapOn 二面", "面试形式", "线上视频面", "TapOn 二面是线上视频面试")]),
        Turn("TapOn 二面改成去公司现场面了", 24, gold=[F("TapOn 二面", "面试形式", "公司现场面", "TapOn 二面改为去公司现场面试")]),
    ], "TapOn 二面是线上还是线下？", expect=Expect(abstain=False, contains_any=[["现场", "线下"]])),
    Case("R19", "取消计划：打算投 → 不投了", [
        Turn("我打算投拼多多的服务端岗", 20, gold=[F("拼多多服务端岗", "投递计划", "打算投递", "用户打算投递拼多多服务端岗")]),
        Turn("算了，拼多多不投了", 23, gold=[F("拼多多服务端岗", "投递计划", "不再投递", "用户决定不投递拼多多服务端岗")]),
    ], "我还打算投拼多多吗？", expect=Expect(abstain=False, contains_any=[["不投", "不再", "不打算", "放弃", "取消"]])),
    Case("R20", "两个空间同一天的安排互不串", [
        Turn("下周二下午 TapOn 二面", 23, gold=[F("TapOn 二面", "时间", "2026-10-27 下午", "TapOn 二面在 2026-10-27（周二）下午")]),
        Turn("下周二晚上和朋友吃火锅", 23, space="life", hour=11, gold=[F("我", "2026-10-27 晚上安排", "和朋友吃火锅", "用户 2026-10-27（周二）晚上和朋友吃火锅")]),
    ], "下周二我有什么安排？", q_day=23, q_space="work", expect=Expect(abstain=False, contains_any=[["TapOn"]], not_contains=["火锅"])),
]

assert len(CASES) == 20 and len({c.id for c in CASES}) == 20

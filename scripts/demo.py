"""演示脚本（设计文档第十部分的 6 步）。前提：scripts/start.sh 已启动 API + worker。"""
from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import datetime

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base", default="http://localhost:8000")
p.add_argument("--jaeger", default="http://localhost:16686")
args = p.parse_args()

OWNER = "demo-" + datetime.now().strftime("%m%d%H%M%S") + "-" + uuid.uuid4().hex[:4]
H = {"X-Owner-Id": OWNER}
c = httpx.Client(base_url=args.base, headers=H, timeout=120)
traces: list[tuple[str, str]] = []


def step(n, title):
    print(f"\n=== 第 {n} 步：{title} ===")


def say(content, occurred_at):
    r = c.post("/episodes", json={"space": "work", "session_id": "demo", "content": content, "occurred_at": occurred_at}).json()
    t0 = time.time()
    while True:
        j = c.get(f"/jobs/{r['job_id']}").json()
        if j["status"] in ("done", "dead"):
            break
        if time.time() - t0 > 90:
            sys.exit(f"作业超时: {j}")
        time.sleep(0.3)
    ops = [o["op"] for o in (j.get("result") or {}).get("ops", [])]
    print(f"写入 episode #{r['episode_id']}（{occurred_at[:10]}）：「{content}」→ 后台作业 #{j['id']} {j['status']}，"
          f"操作 {ops}，耗时 {time.time() - t0:.1f}s，trace_id={j['trace_id']}")
    traces.append((f"作业 #{j['id']}", j["trace_id"]))


def ask(question, space="work"):
    t0 = time.time()
    r = c.post("/recall", json={"space": space, "question": question, "as_of": "2026-10-25T09:00:00+08:00"}).json()
    print(f"[{'工作' if space == 'work' else '生活'}空间] 问：{question}")
    print(f"  答：{r['answer']}")
    print(f"  abstain={r['abstain']}  used_memory_ids={r['used_memory_ids']}  延迟 {time.time() - t0:.2f}s（服务端 {r['latency_ms']['total']}ms）")
    for m in r["memories"]:
        if m["used"]:
            print(f"  依据 #{m['memory_id']}：{m['fact_text']}（生效 {m['valid_from']} → {m['valid_to'] or '至今'}；出处 episode {[s['episode_id'] for s in m['sources']]}）")
            for h in m["history"]:
                print(f"    ↳ 已作废 #{h['memory_id']}：{h['fact_text']}（生效 {h['valid_from']} → {h['valid_to']}；系统作废时间 {h['expired_at'][:19]}，UTC+8 真实墙钟）")
    traces.append((f"recall「{question}」@{space}", r["trace_id"]))
    return r


print(f"演示用户 owner={OWNER}（每次运行用新用户，互不干扰）")
print("说明：occurred_at（事件时间轴）是模拟的 10/23、10/24；recorded_at/expired_at（记录时间轴）是服务器真实时间。")
step(1, "说「TapOn 二面下周二」（2026-10-23 周五）")
say("TapOn 今天约了二面，下周二下午", "2026-10-23T10:00:00+08:00")
step(2, "再说「改到周四」（2026-10-24 周六）")
say("TapOn 二面改到周四了", "2026-10-24T10:00:00+08:00")
step(3, "问「TapOn 二面什么时候」")
ask("TapOn 二面什么时候？")
mem = c.get("/memories", params={"space": "work"}).json()["memories"]
print("  GET /memories 版本链：")
for it in mem:
    for v in it["versions"]:
        print(f"    #{v['memory_id']} [{v['status']}] {v['fact_text']}  valid {v['valid_from'][:10]}→{(v['valid_to'] or '至今')[:10]}")
pre_life = ask("TapOn 二面什么时候？", space="life")
print("  （删除前先在生活空间问一次，验证跨空间隔离）")

step(4, "删除这条（页面上点删除：含 TapOn 的每张记忆卡片），再问")
for it in mem:
    if "TapOn" in it["current"]["fact_text"] and it["current"]["status"] == "active":
        d = c.delete(f"/memories/{it['current']['memory_id']}").json()
        v = d["verification"]
        print(f"  删除 #{it['current']['memory_id']}：删除事实 {d['scope']['deleted_facts']} 个版本、检索键 {d['scope']['deleted_fact_keys']}+{d['scope']['deleted_episode_keys']}，"
              f"排除原文 {d['scope']['episode_ids']}；自动验证 {d['result']}（关键词命中 {v['keyword_hits']}，向量命中 {v['vector_hits']}，"
              f"recall 路径命中 {v['recall_path_hits']}，残留 {sum(v['residual'].values())}）deletion_log #{d['deletion_log_id']}")
after = ask("TapOn 二面什么时候？")

step(5, "切到「生活」空间问")
life = ask("TapOn 二面什么时候？", space="life")

step(6, "trace")
CONTENT_PROBES = ("周二下午", "改到周四", "2026-10-29", "2026-10-27", "什么时候")


def jaeger_trace(tid, wait=20.0):
    t0 = time.time()
    while time.time() - t0 < wait:  # BatchSpanProcessor 异步导出，轮询等待
        try:
            r = httpx.get(f"{args.jaeger}/api/traces/{tid}", timeout=5)
            data = r.json().get("data") if r.status_code == 200 else None
            # 等到根 span（无父引用）也导出后再返回，避免读到半条 trace
            if data and any(not sp.get("references") for sp in data[0]["spans"]):
                return data[0]
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return None


for name, tid in traces:
    d = jaeger_trace(tid)
    if d is None:
        print(f"  {name}: {args.jaeger}/trace/{tid}  （{20}s 内未在 Jaeger 查到）")
        continue
    ops = [s["operationName"] for s in d["spans"]]
    leaks = sum(1 for s in d["spans"] for t in s["tags"] if any(p in str(t.get("value")) for p in CONTENT_PROBES))
    print(f"  {name}: {args.jaeger}/trace/{tid}")
    print(f"      {len(ops)} 个 span：{sorted(set(ops))}；span 属性里出现对话正文的次数={leaks}")

ok = after["abstain"] and life["abstain"] and pre_life["abstain"]
print(f"\n演示检查：删除后弃答={after['abstain']}，生活空间弃答={life['abstain']}（删除前 {pre_life['abstain']}）→ {'通过' if ok else '未通过'}")
sys.exit(0 if ok else 1)

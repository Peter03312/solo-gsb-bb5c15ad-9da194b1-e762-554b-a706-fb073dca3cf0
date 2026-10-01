"""端到端冒烟测试：通过 HTTP API 覆盖主要业务路径。"""
import json
import sys
import time
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"
PASS, FAIL = 0, 0


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def confirm_all_steps(pid, measures=None, note="e2e 确认"):
    """把方案全部步骤标记为已确认（细粒度安全确认接口），返回步骤数。"""
    s, plan = call("GET", f"/api/plans/{pid}")
    assert s == 200, plan
    for st in plan["steps"]:
        s, u = call("PUT", f"/api/plans/{pid}/steps/{st['id']}/safety",
                    {"safety_measures": measures or ["goggles", "gloves"],
                     "safety_note": note, "confirmed": True})
        assert s == 200 and u["safety"]["confirmed"] is True, (s, u)
    return len(plan["steps"])


# ---------- 1. 示例①：氰化钾→盐酸→乙醇 ----------
print("【场景1】氰化物酸化（应第 2 步严重中止）")
s, r = call("POST", "/api/plans/1/simulate?persist=false")
check("HTTP 200", s == 200, s)
check("status=danger", r["status"] == "danger", r["status"])
check("halted=True", r["halted"] is True)
fd = r["first_danger"]
check("首次危险在第 1 步(0基)", fd["step"] == 1, fd)
check("冲突对象为第 0 步氰化钾", fd["conflicts_with_step"] == 0 and fd["conflicts_with_reagent"] == "氰化钾")
check("命中 R001", fd["rule_id"] == "R001")
check("含反应方程式", "HCN" in fd["reaction"])
check("含机理依据", len(fd["rationale"]) > 20)
check("含参考文献", len(fd["references"]) >= 1)
check("受影响步骤=[1,2]", r["affected_steps"] == [1, 2], r["affected_steps"])
check("阻断步骤=[2]", r["blocked_steps"] == [2], r["blocked_steps"])
states = [t["state"] for t in r["timeline"]]
check("时间轴状态 ok/danger/blocked", states == ["ok", "danger", "blocked"], states)
f0 = r["findings"][0]
check("finding 可追溯双向类别", f0["incoming_category"] == "strong_acid"
      and f0["existing_category"] == "cyanide", f0)

# ---------- 2. 示例②：铵盐→碱→次氯酸钠 ----------
print("【场景2】铵盐中和后加 84（先警示后严重）")
s, r = call("POST", "/api/plans/2/simulate?persist=false")
check("status=danger", r["status"] == "danger")
check("第 1 步 medium 警示后继续", r["timeline"][1]["state"] == "warning")
med = [f for f in r["findings"] if f["severity"] == "medium"]
check("R015 氨气警示命中", any(f["rule_id"] == "R015" for f in med), med)
check("首次危险在第 2 步 R016 氯胺", r["first_danger"]["step"] == 2
      and r["first_danger"]["rule_id"] == "R016", r["first_danger"])

# ---------- 3. 安全方案（无类别临时试剂 + 普通盐）----------
print("【场景3】安全配液（应 safe）")
s, reagents = call("GET", "/api/reagents")
nacl = next(x for x in reagents if x["name"] == "氯化钠")
plan = {
    "name": "测试-安全盐水",
    "steps": [
        {"order_index": 0, "reagent_id": nacl["id"], "time_offset_min": 0, "amount_ml": 100},
        {"order_index": 1, "reagent_name": "纯水", "categories": [], "time_offset_min": 5, "amount_ml": 50},
    ],
}
s, r = call("POST", "/api/plans/simulate-dry", plan)
check("dry-run 200", s == 200, s)
check("status=safe", r["status"] == "safe", r)
check("无 finding", r["summary"]["findings"] == 0)

# ---------- 4. 自定义临时类别 + 规则全链路 ----------
print("【场景4】自定义规则与临时试剂")
s, r = call("POST", "/api/rules", {
    "id": "C001", "source": "azide", "target": "strong_acid",
    "directional": False, "severity": "high",
    "title": "叠氮化物遇酸生成易爆叠氮酸",
    "reaction": "N3- + H+ -> HN3",
    "rationale": "叠氮酸剧毒且对震动敏感易爆炸。",
    "references": ["测试文献 1"],
})
check("创建自定义规则", s == 201, (s, r))
plan = {
    "name": "测试-叠氮",
    "steps": [
        {"order_index": 0, "reagent_name": "叠氮化钠", "categories": ["azide"]},
        {"order_index": 1, "reagent_name": "稀盐酸", "categories": ["strong_acid"]},
    ],
}
s, r = call("POST", "/api/plans/simulate-dry", plan)
check("自定义规则即时生效", r["status"] == "danger" and r["first_danger"]["rule_id"] == "C001")

# 有向规则：source 类别"被加入"已含 target 的体系时才触发（与加液顺序相关）
s, _ = call("POST", "/api/rules", {
    "id": "C002", "source": "cat_x", "target": "cat_y",
    "directional": True, "severity": "high", "title": "有向测试",
})
s, r = call("POST", "/api/plans/simulate-dry", {
    "name": "测试-有向反向",
    "steps": [
        {"order_index": 0, "reagent_name": "X", "categories": ["cat_x"]},
        {"order_index": 1, "reagent_name": "Y", "categories": ["cat_y"]},
    ],
})
check("有向规则：target 后加入不命中", r["status"] == "safe", r["status"])
s, r = call("POST", "/api/plans/simulate-dry", {
    "name": "测试-有向正向",
    "steps": [
        {"order_index": 0, "reagent_name": "Y", "categories": ["cat_y"]},
        {"order_index": 1, "reagent_name": "X", "categories": ["cat_x"]},
    ],
})
check("有向规则：source 后加入命中", r["status"] == "danger", r["status"])

# ---------- 5. 方案 CRUD + 持久化推演 + 导出 + 记录 ----------
print("【场景5】方案保存 / 推演落盘 / JSON 导出 / 记录")
s, saved = call("POST", "/api/plans", {
    "name": "测试-持久化",
    "description": "e2e",
    "steps": [
        {"order_index": 0, "reagent_name": "硫化钠临时", "categories": ["sulfide"], "time_offset_min": 0, "amount_ml": 10,
         "safety_measures": ["goggles", "gloves"], "safety_note": "通风橱内操作", "safety_confirmed": True},
        {"order_index": 1, "reagent_name": "盐酸临时", "categories": ["strong_acid"], "time_offset_min": 2, "amount_ml": 5,
         "safety_measures": ["ventilation"], "safety_confirmed": True},
        {"order_index": 2, "reagent_name": "水", "categories": [], "time_offset_min": 9, "amount_ml": 100,
         "safety_measures": ["gloves"], "safety_confirmed": True},
    ],
})
check("创建方案", s == 201 and saved["id"] > 0, (s, saved))
pid = saved["id"]
check("步骤类别快照回显", saved["steps"][0]["categories"] == ["sulfide"], saved["steps"][0])
check("创建时安全确认回显", saved["steps"][0]["safety_confirmed"] is True
      and saved["steps"][0]["safety_measures"] == ["goggles", "gloves"]
      and saved["safety_summary"]["all_confirmed"] is True, saved["steps"][0])

s, r = call("POST", f"/api/plans/{pid}/simulate?persist=true")
check("推演成功", s == 200)
check("返回 run_id", r.get("run_id") is not None)
run_id = r["run_id"]
check("R002 H2S 命中", r["first_danger"]["rule_id"] == "R002")
check("第 3 步被阻断", r["blocked_steps"] == [2])

s, exported = call("GET", f"/api/plans/{pid}/export")
check("导出 JSON 含完整结构", s == 200
      and {"engine", "rule_revision", "status", "first_danger", "timeline", "findings"} <= set(exported),
      s)
check("导出含安全确认状态", exported["safety"]["all_confirmed"] is True
      and exported["safety"]["confirmed_steps"] == 3
      and all(t["safety_confirmed"] is True for t in exported["timeline"]), exported.get("safety"))

s, runs = call("GET", "/api/runs")
check("推演记录列表", s == 200 and any(x["id"] == run_id for x in runs))
s, one = call("GET", f"/api/runs/{run_id}")
check("记录详情可取回", s == 200 and one["first_danger"]["rule_id"] == "R002")
s, one_exp = call("GET", f"/api/runs/{run_id}/export")
check("记录可导出", s == 200 and one_exp["run_id"] == run_id)

s, _ = call("PUT", f"/api/plans/{pid}", {"name": "测试-持久化-改名"})
check("方案更新", s == 200, s)
s, _ = call("DELETE", f"/api/plans/{pid}")
check("方案删除", s == 204, s)

# ---------- 6. 试剂 CRUD 与引用保护 ----------
print("【场景6】试剂档案与引用保护")
s, rg = call("POST", "/api/reagents", {
    "name": "测试试剂A", "cas": "1-2-3", "state": "liquid",
    "categories": ["oxidizer", "custom_cat"],
})
check("创建试剂", s == 201, (s, rg))
rid = rg["id"]
s, _ = call("POST", "/api/plans", {
    "name": "测试-占用试剂",
    "steps": [{"order_index": 0, "reagent_id": rid}],
})
pid2 = _["id"]
s, err = call("DELETE", f"/api/reagents/{rid}")
check("被引用试剂拒绝删除", s == 409, s)
call("DELETE", f"/api/plans/{pid2}")
s, _ = call("DELETE", f"/api/reagents/{rid}")
check("解除引用后可删除", s == 204, s)
s, err = call("POST", "/api/plans/simulate-dry", {"name": "x", "steps": []})
check("空步骤被拒绝", s == 422, s)

# ---------- 7. 规则停用/启用、内置保护 ----------
print("【场景7】规则维护")
s, _ = call("PATCH", "/api/rules/R001", {"active": False})
check("内置规则可停用", s == 200)
s, r = call("POST", "/api/plans/1/simulate?persist=false")
check("停用后不再命中 R001", not any(f["rule_id"] == "R001" for f in r["findings"]))
s, _ = call("PATCH", "/api/rules/R001", {"active": True})
check("重新启用", s == 200)
s, _ = call("DELETE", "/api/rules/R001")
check("内置规则不可删除", s == 409)
s, _ = call("DELETE", "/api/rules/C001")
s2, _ = call("DELETE", "/api/rules/C002")
check("自定义规则可删除", s == 204 and s2 == 204, (s, s2))

# ---------- 8. 版本差异（同一方案两次记录对齐比较） ----------
print("【场景8】推演记录版本差异")
s, p3 = call("POST", "/api/plans", {
    "name": "测试-版本差异方案",
    "steps": [
        {"order_index": 0, "reagent_name": "叠氮化钠X", "categories": ["azide"]},
        {"order_index": 1, "reagent_name": "稀盐酸X", "categories": ["strong_acid"]},
        {"order_index": 2, "reagent_name": "水", "categories": []},
    ],
})
pid3 = p3["id"]
confirm_all_steps(pid3)  # 正式推演门槛：先完成全部步骤安全确认

s, _ = call("POST", "/api/rules", {
    "id": "C010", "source": "azide", "target": "strong_acid",
    "directional": False, "severity": "high",
    "title": "叠氮化物遇酸生成易爆叠氮酸",
    "reaction": "N3- + H+ -> HN3",
    "rationale": "叠氮酸剧毒且对震动敏感易爆炸。",
    "references": ["差异测试文献"],
})
check("创建测试规则 C010", s == 201, s)

s, ra = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_a = ra["run_id"]
check("第一次推演 danger", s == 200 and ra["status"] == "danger" and ra["blocked_steps"] == [2], (s, ra.get("status")))

call("PATCH", "/api/rules/C010", {"severity": "medium"})
s, rb = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_b = rb["run_id"]
check("降级后第二次推演 warning", s == 200 and rb["status"] == "warning" and rb["blocked_steps"] == [], (s, rb.get("status")))

s, d = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_b}")
check("差异接口 200", s == 200, s)
check("方案一致才返回", d.get("plan_id") == pid3, d.get("plan_id"))
check("两侧 run_id 正确", d["base"]["run_id"] == run_a and d["target"]["run_id"] == run_b)
check("状态 danger->warning", d["summary"]["status"] == {"base": "danger", "target": "warning", "changed": True})
check("首次危险消失", d["summary"]["first_danger"]["change"] == "removed")
check("阻断步骤 [2] 消失", d["summary"]["blocked_steps"]["removed"] == [2])
check("受影响步骤 [2] 消失", d["summary"]["affected_steps"]["removed"] == [2])
fc = d["summary"]["finding_changes"]
check("一条级别变化、无新增/消失", fc["severity"] == 1 and fc["added"] == 0 and fc["removed"] == 0, fc)
sev_entries = [f for f in d["findings"] if f["change"] == "severity"]
check("按步骤+规则对齐到 C010", len(sev_entries) == 1 and sev_entries[0]["rule_id"] == "C010"
      and sev_entries[0]["base_severity"] == "high" and sev_entries[0]["target_severity"] == "medium",
      sev_entries)
check("条目含两侧反应依据", sev_entries[0]["base"]["reaction"].startswith("N3-")
      and sev_entries[0]["target"]["rationale"].startswith("叠氮酸"), sev_entries)
check("命中计数 high 减少 1", any(x["key"] == "high" and x["delta"] == -1 for x in d["summary"]["counts"]),
      d["summary"]["counts"])

call("PATCH", "/api/rules/C010", {"active": False})
s, rc = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_c = rc["run_id"]
check("停用后第三次推演 safe", s == 200 and rc["status"] == "safe", (s, rc.get("status")))

s, d2 = call("GET", f"/api/runs/diff?base_id={run_b}&target_id={run_c}")
check("停用规则：命中消失", s == 200 and d2["summary"]["finding_changes"]["removed"] == 1
      and d2["summary"]["first_danger"]["change"] == "none"
      and d2["summary"]["status"]["changed"] and d2["summary"]["status"]["target"] == "safe",
      d2["summary"])
rm = [f for f in d2["findings"] if f["change"] == "removed"]
check("消失条目带规则 ID 与步骤", rm and rm[0]["rule_id"] == "C010" and rm[0]["incoming_step"] == 1, rm)

# 重新启用（恢复 high）后改机理文案 → 与 run_a 相比级别一致、依据变化
call("PATCH", "/api/rules/C010", {"active": True, "severity": "high"})
call("PATCH", "/api/rules/C010", {"rationale": "叠氮酸剧毒且对震动敏感易爆炸；2026 修订：常压下沸点仅 37℃。"})
s, rd = call("POST", f"/api/plans/{pid3}/simulate?persist=true")
run_d = rd["run_id"]
s, d3 = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_d}")
basis = [f for f in d3["findings"] if f["change"] == "basis"]
check("级别不变机理变化 → basis", s == 200 and len(basis) == 1 and basis[0]["changed_fields"] == ["rationale"]
      and basis[0]["base_severity"] == "high" and basis[0]["target_severity"] == "high", basis)
check("两侧机理都可追溯", basis[0]["base"]["rationale"].startswith("叠氮酸剧毒")
      and "2026 修订" in basis[0]["target"]["rationale"], basis)

s, dexp = call("GET", f"/api/runs/diff/export?base_id={run_a}&target_id={run_b}")
check("差异 JSON 可导出", s == 200 and dexp.get("kind") == "labmix-run-diff"
      and dexp["base"]["run_id"] == run_a and dexp["target"]["run_id"] == run_b
      and "findings" in dexp and "summary" in dexp, s)

# 错误路径：同记录比较 / 记录缺失 / 跨方案
s, e = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_a}")
check("同一条记录 400", s == 400, s)
s, e = call("GET", f"/api/runs/diff?base_id={run_a}&target_id=999999")
check("对照记录缺失 404", s == 404, s)
s, e = call("GET", f"/api/runs/diff?base_id=999998&target_id={run_b}")
check("基准记录缺失 404", s == 404, s)
s, other = call("GET", "/api/plans/2")
if other.get("safety_summary", {}).get("all_confirmed") is not True:
    confirm_all_steps(2)
s, other = call("POST", "/api/plans/2/simulate?persist=true")
run_other = other["run_id"]
s, e = call("GET", f"/api/runs/diff?base_id={run_a}&target_id={run_other}")
check("方案不一致 409", s == 409, s)
s, e = call("GET", f"/api/runs/diff/export?base_id={run_a}&target_id={run_other}")
check("导出接口同样拒绝跨方案 409", s == 409, s)
s, e = call("GET", "/api/runs/diff")
check("缺少参数 422", s == 422, s)

call("DELETE", f"/api/plans/{pid3}")
call("DELETE", "/api/rules/C010")

# ---------- 9. 方案规则版本（草稿/发布/停用 + 判定口径 + 版本差异） ----------
print("【场景9】方案规则版本生命周期与判定口径")
s, pv = call("POST", "/api/plans", {
    "name": "测试-规则版本方案",
    "steps": [
        {"order_index": 0, "reagent_name": "氰化钾V", "categories": ["cyanide"]},
        {"order_index": 1, "reagent_name": "盐酸V", "categories": ["strong_acid"]},
        {"order_index": 2, "reagent_name": "水", "categories": []},
    ],
})
pvid = pv["id"]
check("创建方案", s == 201)
check("方案初始无生效版本", pv["effective_rule_version"] is None)
check("方案初始安全确认摘要", pv["safety_summary"]["total_steps"] == 3
      and pv["safety_summary"]["all_confirmed"] is False, pv.get("safety_summary"))

s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("无版本时按全局规则图判定", r["rule_scope"] == "global" and r["status"] == "danger", r.get("rule_scope"))

s, e = call("POST", f"/api/plans/{pvid}/simulate?persist=true")
check("未确认时正式推演被门槛拦截 409", s == 409 and "安全确认" in e["detail"], (s, e))
confirm_all_steps(pvid)

s, vl = call("GET", f"/api/plans/{pvid}/rule-versions")
check("版本列表初始为空", s == 200 and vl["versions"] == [] and vl["effective_version_id"] is None)

# 创建 v1（克隆全局启用规则，含严重级别依据）
s, v1 = call("POST", f"/api/plans/{pvid}/rule-versions", {"note": "首版"})
v1id = v1["id"]
check("创建草稿 v1", s == 201 and v1["version_no"] == 1 and v1["status"] == "draft")
r001 = next(x for x in v1["rules"] if x["id"] == "R001")
check("快照保留级别依据", r001["severity"] == "high" and "HCN" in r001["reaction"] and r001["references"])
check("草稿带创建/变更时间", v1["created_at"] and v1["updated_at"] and v1["published_at"] is None)

s, _ = call("POST", f"/api/plans/{pvid}/rule-versions", {})
check("同方案仅允许一个草稿", s == 409, s)

s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("草稿不参与判定（仍全局）", r["rule_scope"] == "global")

# 编辑草稿：R001 降为 medium
edited = v1["rules"]
for x in edited:
    if x["id"] == "R001":
        x["severity"] = "medium"
        x["rationale"] = x["rationale"] + "（本方案经工程防护降级为警示）"
s, v1u = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}", {"rules": edited})
check("编辑草稿成功", s == 200
      and next(x for x in v1u["rules"] if x["id"] == "R001")["severity"] == "medium", s)
s, _ = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}", {"rules": []})
check("空规则集草稿被拒", s == 409, s)
s, _ = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}",
            {"rules": [{**edited[0], "id": edited[0]["id"]}, {**edited[0], "id": edited[0]["id"]}]})
check("重复规则编号被拒", s == 409, s)

# 发布
s, v1p = call("POST", f"/api/plans/{pvid}/rule-versions/{v1id}/publish")
check("发布成功", s == 200 and v1p["status"] == "published" and v1p["published_at"] is not None)
s, _ = call("PUT", f"/api/plans/{pvid}/rule-versions/{v1id}", {"note": "x"})
check("已发布版本不可编辑", s == 409, s)

# 判定只读取生效版本
s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=true")
v1_run = r["run_id"]
check("发布后判定读取版本快照", r["rule_scope"] == "plan_version"
      and r["rule_version_id"] == v1id and r["status"] == "warning", (r.get("rule_scope"), r.get("status")))
check("版本标识 plan{id}-rv1", r["rule_revision"] == f"plan{pvid}-rv1", r["rule_revision"])

# 全局规则停用不影响已发布快照
call("PATCH", "/api/rules/R001", {"active": False})
s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("全局变更不影响版本快照", r["rule_scope"] == "plan_version" and r["status"] == "warning")
call("PATCH", "/api/rules/R001", {"active": True})

# 正式判定不允许指定版本；草稿预览允许
s, v2 = call("POST", f"/api/plans/{pvid}/rule-versions", {"note": "v2"})
v2id = v2["id"]
s, _ = call("POST", f"/api/plans/{pvid}/simulate?persist=true&rule_version_id={v2id}")
check("落盘判定不能指定版本", s == 422, s)
s, rp = call("POST", f"/api/plans/{pvid}/simulate?persist=false&rule_version_id={v2id}")
check("草稿可预览（不落盘）", s == 200 and rp["rule_version_id"] == v2id and rp.get("run_id") is None, s)

# 发布 v2 → v1 被替代
s, _ = call("POST", f"/api/plans/{pvid}/rule-versions/{v2id}/publish")
s, vl = call("GET", f"/api/plans/{pvid}/rule-versions")
check("新版本发布后旧版本自动替代", vl["effective_version_id"] == v2id
      and any(x["id"] == v1id and x["status"] == "superseded" for x in vl["versions"]))
s, pd = call("GET", f"/api/plans/{pvid}")
check("方案详情含当前生效状态", pd["effective_rule_version"]["id"] == v2id
      and pd["effective_rule_version"]["status_label"] == "生效中")

# 版本差异：与全局比，R001 high→medium
s, d = call("GET", f"/api/plans/{pvid}/rule-versions/diff?base=global&target=" + str(v2id))
check("版本差异接口", s == 200 and d["kind"] == "labmix-rule-version-diff", s)
check("差异识别 R001 级别变化", d["changes"]["severity"] == 1
      and any(x["rule_id"] == "R001" and x["base_severity"] == "high"
              and x["target_severity"] == "medium" for x in d["rules"]), d["changes"])
rv1_entry = next(x for x in d["rules"] if x["rule_id"] == "R001")
check("差异条目保留两侧严重级别依据", "severity" in rv1_entry["changed_fields"]
      and "HCN" in rv1_entry["base"]["reaction"] and "降级为警示" in rv1_entry["target"]["rationale"])
s, _ = call("GET", f"/api/plans/{pvid}/rule-versions/diff?base={v1id}&target={v1id}")
check("相同版本比较 400", s == 400, s)
s, _ = call("GET", f"/api/plans/{pvid}/rule-versions/diff?base=999999&target=global")
check("版本不存在 404", s == 404, s)

# 停用：不得继续参与判定
s, _ = call("POST", f"/api/plans/{pvid}/rule-versions/{v2id}/deactivate")
check("停用成功", s == 200)
s, r = call("POST", f"/api/plans/{pvid}/simulate?persist=false")
check("停用后回落全局规则图", r["rule_scope"] == "global" and r["status"] == "danger",
      (r.get("rule_scope"), r.get("status")))
s, _ = call("POST", f"/api/plans/{pvid}/rule-versions/{v2id}/deactivate")
check("非生效版本不可停用", s == 409, s)
s, _ = call("DELETE", f"/api/plans/{pvid}/rule-versions/{v2id}")
check("已发布版本不可删除", s == 409, s)

# 历史记录保留版本关联
s, runs = call("GET", "/api/runs")
rec = next(x for x in runs if x["id"] == v1_run)
check("推演记录关联生效版本", rec["rule_version_id"] == v1id, rec)

# 跨方案隔离 & 级联删除
s, otherp = call("POST", "/api/plans", {"name": "测试-版本隔离方案",
    "steps": [{"order_index": 0, "reagent_name": "水", "categories": []}]})
s, _ = call("GET", f"/api/plans/{otherp['id']}/rule-versions/{v1id}")
check("跨方案不可访问版本", s == 404, s)
s, _ = call("DELETE", f"/api/plans/{pvid}")
check("删除方案", s == 204, s)
s, _ = call("GET", f"/api/plans/{pvid}/rule-versions")
check("方案删除后版本级联清除", s == 404, s)
call("DELETE", f"/api/plans/{otherp['id']}")

# ---------- 10. 步骤安全确认：门槛 / 自动失效 / 复核恢复 ----------
print("【场景10】步骤安全确认（门槛、自动失效与复核）")
s, sp = call("POST", "/api/plans", {
    "name": "测试-安全确认",
    "steps": [
        {"order_index": 0, "reagent_name": "氰化钾S", "categories": ["cyanide"]},
        {"order_index": 1, "reagent_name": "盐酸S", "categories": ["strong_acid"]},
        {"order_index": 2, "reagent_name": "水S", "categories": []},
    ],
})
spid = sp["id"]
check("创建方案带安全摘要", s == 201 and sp["safety_summary"]["total_steps"] == 3
      and sp["safety_summary"]["confirmed_steps"] == 0
      and sp["safety_summary"]["all_confirmed"] is False, sp.get("safety_summary"))
check("步骤默认未确认", all(x["safety_confirmed"] is False and x["safety_stale"] is False
      for x in sp["steps"]), sp["steps"])

# 未确认：正式推演 409；预检 / dry-run / 导出不受限
s, e = call("POST", f"/api/plans/{spid}/simulate?persist=true")
check("未确认时正式推演 409", s == 409 and "第 1 步" in e["detail"] and "第 3 步" in e["detail"], (s, e))
s, r = call("POST", f"/api/plans/{spid}/simulate?persist=false")
check("预检（persist=false）不受门槛限制", s == 200 and r["status"] == "danger", s)
check("报告含确认状态", r["safety"]["all_confirmed"] is False
      and r["safety"]["confirmed_steps"] == 0
      and r["timeline"][0]["safety_confirmed"] is False, r.get("safety"))
s, r = call("POST", "/api/plans/simulate-dry", {
    "name": "x", "steps": [{"order_index": 0, "reagent_name": "水", "categories": []}]})
check("dry-run 预检不受限且带 safety", s == 200 and r["safety"]["total_steps"] == 1, s)
s, ex = call("GET", f"/api/plans/{spid}/export")
check("导出不受限且带 safety", s == 200 and ex["safety"]["all_confirmed"] is False, s)

# 细粒度接口逐步确认
steps = sp["steps"]
s, u1 = call("PUT", f"/api/plans/{spid}/steps/{steps[0]['id']}/safety",
             {"safety_measures": ["goggles", "gloves"], "safety_note": "通风橱内操作", "confirmed": True})
check("确认第 1 步", s == 200 and u1["safety"]["confirmed"] is True
      and u1["safety"]["measures"] == ["goggles", "gloves"]
      and u1["safety"]["note"] == "通风橱内操作", (s, u1))
check("方案摘要进度 1/3", u1["plan_safety"]["confirmed_steps"] == 1
      and u1["plan_safety"]["all_confirmed"] is False, u1.get("plan_safety"))
s, _ = call("PUT", f"/api/plans/{spid}/steps/{steps[1]['id']}/safety",
            {"safety_measures": ["ventilation"], "confirmed": True})
s, e = call("POST", f"/api/plans/{spid}/simulate?persist=true")
check("仍有未确认步骤时 409 且指明步骤", s == 409 and "第 3 步" in e["detail"], (s, e))
s, u3 = call("PUT", f"/api/plans/{spid}/steps/{steps[2]['id']}/safety",
             {"safety_measures": ["gloves"], "confirmed": True})
check("全部确认完成", u3["plan_safety"]["all_confirmed"] is True, u3.get("plan_safety"))
s, r = call("POST", f"/api/plans/{spid}/simulate?persist=true")
check("全部确认后正式推演放行", s == 200 and r["safety"]["all_confirmed"] is True, s)
check("时间轴逐步带确认标记", all(t["safety_confirmed"] is True for t in r["timeline"]), r["timeline"])
run_s = r["run_id"]
s, one = call("GET", f"/api/runs/{run_s}")
check("历史推演记录保留确认状态", one["safety"]["all_confirmed"] is True
      and one["safety"]["confirmed_steps"] == 3, one.get("safety"))

# 取消确认 → 门槛重新生效
s, ux = call("PUT", f"/api/plans/{spid}/steps/{steps[2]['id']}/safety",
             {"safety_measures": [], "confirmed": False})
check("取消确认", s == 200 and ux["safety"]["confirmed"] is False
      and ux["plan_safety"]["all_confirmed"] is False, (s, ux))
s, _ = call("PUT", f"/api/plans/{spid}/steps/{steps[2]['id']}/safety",
            {"safety_measures": ["gloves"], "confirmed": True})

# 整体保存：内容未变步骤继承确认；内容变更步骤确认失效并标记待复核
s, sp2 = call("PUT", f"/api/plans/{spid}", {"steps": [
    {"order_index": 0, "reagent_name": "氰化钾S", "categories": ["cyanide"]},
    {"order_index": 1, "reagent_name": "盐酸S", "categories": ["strong_acid"]},
    {"order_index": 2, "reagent_name": "水S", "categories": [], "amount_ml": 50,
     "safety_confirmed": False, "safety_stale": True, "safety_measures": ["gloves"]},
]})
check("未携带安全数据时内容未变步骤继承确认", sp2["steps"][0]["safety_confirmed"] is True
      and sp2["steps"][1]["safety_confirmed"] is True, [x["safety_confirmed"] for x in sp2["steps"]])
check("变更步骤确认失效并标记待复核", sp2["steps"][2]["safety_confirmed"] is False
      and sp2["steps"][2]["safety_stale"] is True, sp2["steps"][2])
check("摘要统计待复核", sp2["safety_summary"]["stale_steps"] == 1
      and sp2["safety_summary"]["all_confirmed"] is False, sp2["safety_summary"])
s, e = call("POST", f"/api/plans/{spid}/simulate?persist=true")
check("待复核步骤同样阻塞正式推演", s == 409 and "待复核" in e["detail"], (s, e))

# 试剂档案变更 → 引用步骤确认自动失效（待复核）
s, rg = call("POST", "/api/reagents", {"name": "测试试剂S", "categories": ["strong_base"]})
rid = rg["id"]
s, spx = call("POST", "/api/plans", {"name": "测试-试剂变更联动", "steps": [
    {"order_index": 0, "reagent_id": rid, "safety_measures": ["gloves"], "safety_confirmed": True}]})
check("创建即确认", spx["steps"][0]["safety_confirmed"] is True, spx["steps"][0])
s, _ = call("PUT", f"/api/reagents/{rid}", {"categories": ["strong_base", "oxidizer"]})
s, spx2 = call("GET", f"/api/plans/{spx['id']}")
check("试剂类别变更后确认自动失效", spx2["steps"][0]["safety_confirmed"] is False
      and spx2["steps"][0]["safety_stale"] is True, spx2["steps"][0])
s, e = call("POST", f"/api/plans/{spx['id']}/simulate?persist=true")
check("试剂变更后正式推演被阻塞", s == 409 and "待复核" in e["detail"], (s, e))
s, u = call("PUT", f"/api/plans/{spx['id']}/steps/{spx2['steps'][0]['id']}/safety",
            {"safety_measures": ["gloves", "goggles"], "confirmed": True})
check("重新确认后恢复", u["safety"]["confirmed"] is True and u["safety"]["stale"] is False, u)
s, r = call("POST", f"/api/plans/{spx['id']}/simulate?persist=true")
check("重新确认后正式推演放行", s == 200, s)

# 跨方案步骤不可确认 / 参数校验
s, _ = call("PUT", f"/api/plans/{spid}/steps/{spx2['steps'][0]['id']}/safety",
            {"safety_measures": [], "confirmed": True})
check("跨方案步骤确认 404", s == 404, s)
s, _ = call("PUT", f"/api/plans/{spid}/steps/999999/safety",
            {"safety_measures": [], "confirmed": True})
check("不存在步骤 404", s == 404, s)

call("DELETE", f"/api/plans/{spid}")
call("DELETE", f"/api/plans/{spx['id']}")
call("DELETE", f"/api/reagents/{rid}")

# ---------- 11. 方案 CSV 导入（预览校验 / 单事务 / 未知试剂确认 / 安全待确认） ----------
print("【场景11】方案 CSV 导入")
s, plans_before = call("GET", "/api/plans")
n_plans_before = len(plans_before)
s, reagents = call("GET", "/api/reagents")
kcn = next(x for x in reagents if x["name"] == "氰化钾")
hcl = next(x for x in reagents if x["name"] == "盐酸")

csv_ok = "\n".join([
    "步骤序号,试剂名,类别,时刻,用量,备注",
    "1,氰化钾,cyanide,0,10,含氰废液",
    "2,盐酸,strong_acid,5,20,酸化（危险）",
    "3,神秘试剂X,mystery_cat,10,5,档案外试剂",
])

# 预览：合法 CSV 给出逐行状态、复用/临时标记与类别不一致提示
s, pv = call("POST", "/api/plans/import/preview", {"csv_text": csv_ok})
check("预览 200 且校验通过", s == 200 and pv["valid"] is True, (s, pv.get("errors")))
check("预览识别未知试剂", pv["unknown_reagents"] == ["神秘试剂X"], pv["unknown_reagents"])
check("预览按名称复用档案试剂",
      {m["name"]: m["reagent_id"] for m in pv["matched_reagents"]}
      == {"氰化钾": kcn["id"], "盐酸": hcl["id"]}, pv["matched_reagents"])
check("CSV 类别与档案不一致给出提示", any("盐酸" in w["message"] for w in pv["warnings"]), pv["warnings"])
check("预览步骤带行号与顺序", [st["row"] for st in pv["steps"]] == [2, 3, 4]
      and [st["order_index"] for st in pv["steps"]] == [0, 1, 2], pv["steps"])
check("预览不落库", len(call("GET", "/api/plans")[1]) == n_plans_before)

# 未确认未知试剂 → 409
s, e = call("POST", "/api/plans/import", {"name": "测试-CSV导入", "csv_text": csv_ok})
check("未知试剂未确认 409", s == 409 and "神秘试剂X" in e["detail"]["message"], (s, e))
check("409 同样不落库", len(call("GET", "/api/plans")[1]) == n_plans_before)

# 确认后提交 → 201，单事务创建方案 + 步骤
s, imp = call("POST", "/api/plans/import",
              {"name": "测试-CSV导入", "description": "e2e", "csv_text": csv_ok,
               "allow_unknown_reagents": True})
check("导入成功 201", s == 201, (s, imp))
imp_plan = imp["plan"]
impid = imp_plan["id"]
check("导入摘要（复用 2 / 临时 1）", imp["import"]["total_rows"] == 3
      and len(imp["import"]["matched_reagents"]) == 2
      and imp["import"]["temporary_reagents"] == ["神秘试剂X"], imp["import"])
check("档案试剂按 ID 复用", imp_plan["steps"][0]["reagent_id"] == kcn["id"]
      and imp_plan["steps"][1]["reagent_id"] == hcl["id"], imp_plan["steps"])
check("未知试剂作为临时试剂写入步骤", imp_plan["steps"][2]["reagent_id"] is None
      and imp_plan["steps"][2]["reagent_name"] == "神秘试剂X"
      and imp_plan["steps"][2]["categories"] == ["mystery_cat"], imp_plan["steps"][2])
check("导入结果安全确认均为待确认", imp_plan["safety_summary"]["total_steps"] == 3
      and imp_plan["safety_summary"]["confirmed_steps"] == 0
      and imp_plan["safety_summary"]["all_confirmed"] is False
      and all(x["safety_confirmed"] is False for x in imp_plan["steps"]),
      imp_plan["safety_summary"])

# 未知试剂不创建档案
s, all_reagents = call("GET", "/api/reagents")
check("未知试剂未写入试剂档案", s == 200
      and not any(x["name"] == "神秘试剂X" for x in all_reagents))

# 门槛保持：正式推演 409，预检放行且按档案类别命中 R001
s, e = call("POST", f"/api/plans/{impid}/simulate?persist=true")
check("导入方案正式推演仍受安全门槛拦截", s == 409 and "安全确认" in e["detail"], (s, e))
s, r = call("POST", f"/api/plans/{impid}/simulate?persist=false")
check("预检放行且复用试剂按档案类别命中 R001", s == 200 and r["status"] == "danger"
      and r["first_danger"]["rule_id"] == "R001", (s, r.get("status")))
confirm_all_steps(impid)
s, r = call("POST", f"/api/plans/{impid}/simulate?persist=true")
check("逐步确认后正式推演放行", s == 200 and r["run_id"] is not None, s)

# 各类非法内容整体拒绝（422 + 行号原因），且不产生半成品
bad_csvs = {
    "序号重复": "步骤序号,试剂名,类别\n1,水,water\n1,水,water",
    "缺试剂名": "步骤序号,试剂名,类别\n1,,strong_acid",
    "缺类别": "步骤序号,试剂名,类别\n1,盐酸,",
    "用量非法": "步骤序号,试剂名,类别,用量\n1,盐酸,strong_acid,abc",
    "时刻非法": "步骤序号,试剂名,类别,时刻\n1,盐酸,strong_acid,-1",
    "序号非法": "步骤序号,试剂名,类别\n0.5,盐酸,strong_acid",
    "缺表头列": "试剂名,类别\n盐酸,strong_acid",
    "无数据行": "步骤序号,试剂名,类别",
}
for tag, bad_csv in bad_csvs.items():
    s, e = call("POST", "/api/plans/import",
                {"name": f"测试-不应创建-{tag}", "csv_text": bad_csv,
                 "allow_unknown_reagents": True})
    errs = e.get("detail", {}).get("errors", []) if isinstance(e, dict) else []
    check(f"非法 CSV（{tag}）整体拒绝 422 且带行号",
          s == 422 and errs and all(x.get("row") for x in errs), (s, e))
check("整体拒绝后无半成品", len(call("GET", "/api/plans")[1]) == n_plans_before + 1)

# 序号重复的错误同时指出全部相关行号
s, e = call("POST", "/api/plans/import",
            {"name": "x", "csv_text": bad_csvs["序号重复"]})
rows = {x["row"] for x in e["detail"]["errors"]}
check("序号重复标出全部相关行", rows == {2, 3}, e["detail"]["errors"])

call("DELETE", f"/api/plans/{impid}")

# ---------- 12. 废液分桶规划（预览不落库 · 最少桶 · 稳定结果 · 超容量拒绝） ----------
print("【场景12】废液分桶规划")
s, runs_before_bk = call("GET", "/api/runs")
s, plans_before_bk = call("GET", "/api/plans")

def mk_bk_reagent(name, cats):
    s, r = call("POST", "/api/reagents", {"name": name, "categories": cats})
    assert s == 201, (s, r)
    return r["id"]

bkA = mk_bk_reagent("测试废液A", ["cyanide"])
bkB = mk_bk_reagent("测试废液B", ["strong_acid"])
bkC = mk_bk_reagent("测试废液C", ["halide_salt"])
bkD = mk_bk_reagent("测试废液D", [])
bkE = mk_bk_reagent("测试废液E", [])

# A×B 在任一方向命中 R001（high）→ 不可同桶；容量 700 下最少 2 桶
bk_req = {"capacity_ml": 700, "streams": [
    {"reagent_id": bkA, "volume_ml": 100},
    {"reagent_id": bkB, "volume_ml": 100},
    {"reagent_id": bkC, "volume_ml": 500},
]}
s, r = call("POST", "/api/bucketing/preview", bk_req)
check("分桶预览 200", s == 200, (s, r))
check("最少桶数=2", r["barrel_count"] == 2, r.get("barrel_count"))
check("最大桶负载最小=600", r["max_load_ml"] == 600, r.get("max_load_ml"))
bk_names = [[it["name"] for it in b["items"]] for b in r["barrels"]]
check("桶内名称序列稳定", bk_names == [["测试废液A"], ["测试废液B", "测试废液C"]], bk_names)
check("每桶带负载与组成", all(b["load_ml"] > 0 and 0 < b["utilization"] <= 1
      and b["items"] for b in r["barrels"]), r["barrels"])
check("分隔约束 1 对", len(r["separations"]) == 1, r["separations"])
sep0 = r["separations"][0]
check("分隔对为 A×B", {sep0["stream_a"]["name"], sep0["stream_b"]["name"]}
      == {"测试废液A", "测试废液B"}, sep0)
check("分隔依据含 R001 与类别对", any(x["rule_id"] == "R001"
      and x["category_a"] == "cyanide" and x["category_b"] == "strong_acid"
      for x in sep0["rules"]), sep0)
check("分隔依据含方程式/机理/文献", any("HCN" in x["reaction"] and len(x["rationale"]) > 20
      and x["references"] for x in sep0["rules"]), sep0)
check("预览带全局规则版本号", bool(r["rule_revision"]), r.get("rule_revision"))

# 结果可复现：同一请求再算一次完全一致
s, r2 = call("POST", "/api/bucketing/preview", bk_req)
check("同输入结果稳定一致", s == 200 and r2 == r)

# 容量约束：C/D/E 各 600、容量 1000 → 3 桶（无 high 冲突）
s, r = call("POST", "/api/bucketing/preview", {"capacity_ml": 1000, "streams": [
    {"reagent_id": bkC, "volume_ml": 600},
    {"reagent_id": bkD, "volume_ml": 600},
    {"reagent_id": bkE, "volume_ml": 600},
]})
check("体积约束 3 桶", s == 200 and r["barrel_count"] == 3
      and r["max_load_ml"] == 600, (s, r.get("barrel_count")))
check("无冲突对时分隔列表为空", r["separations"] == [])

# 同桶数并列时先比最大负载：100/100/150、容量 200 → {C,D}|{E}（最大负载 200）
s, r = call("POST", "/api/bucketing/preview", {"capacity_ml": 200, "streams": [
    {"reagent_id": bkC, "volume_ml": 100},
    {"reagent_id": bkD, "volume_ml": 100},
    {"reagent_id": bkE, "volume_ml": 150},
]})
bk_names = [[it["name"] for it in b["items"]] for b in r["barrels"]]
check("最大负载最小优先", s == 200 and r["barrel_count"] == 2
      and r["max_load_ml"] == 200
      and bk_names == [["测试废液C", "测试废液D"], ["测试废液E"]], (r.get("max_load_ml"), bk_names))

# 最大负载仍并列时按名称序列：100/100/100、容量 200 → {D,E}|{C}
s, r = call("POST", "/api/bucketing/preview", {"capacity_ml": 200, "streams": [
    {"reagent_id": bkC, "volume_ml": 100},
    {"reagent_id": bkD, "volume_ml": 100},
    {"reagent_id": bkE, "volume_ml": 100},
]})
bk_names = [[it["name"] for it in b["items"]] for b in r["barrels"]]
check("名称序列 tie-break", s == 200 and r["max_load_ml"] == 200
      and bk_names == [["测试废液C"], ["测试废液D", "测试废液E"]], bk_names)

# medium 规则不构成分隔：B(强酸) + 氢氧化钠溶液(强碱) 仅命中 R014(medium) → 可同桶
s, all_reagents = call("GET", "/api/reagents")
naoh = next(x for x in all_reagents if x["name"] == "氢氧化钠溶液")
s, r = call("POST", "/api/bucketing/preview", {"capacity_ml": 1000, "streams": [
    {"reagent_id": bkB, "volume_ml": 100},
    {"reagent_id": naoh["id"], "volume_ml": 100},
]})
check("medium 规则不分隔（可同桶）", s == 200 and r["barrel_count"] == 1
      and r["separations"] == [], (s, r.get("barrel_count")))

# 有向 high 规则：任一方向命中即分隔
s, _ = call("POST", "/api/rules", {
    "id": "C301", "source": "bk_cat_p", "target": "bk_cat_q",
    "directional": True, "severity": "high", "title": "有向分桶测试规则",
    "reaction": "P + Q -> 危险", "rationale": "分桶方向性测试", "references": ["测试文献"],
})
check("创建有向测试规则", s == 201, s)
bkP = mk_bk_reagent("测试废液P", ["bk_cat_p"])
bkQ = mk_bk_reagent("测试废液Q", ["bk_cat_q"])
s, r = call("POST", "/api/bucketing/preview", {"capacity_ml": 1000, "streams": [
    {"reagent_id": bkP, "volume_ml": 100},
    {"reagent_id": bkQ, "volume_ml": 100},
]})
check("有向 high 规则任一方向命中即分隔", s == 200 and r["barrel_count"] == 2,
      (s, r.get("barrel_count")))
check("分隔依据标记有向规则", r["separations"][0]["rules"][0]["directional"] is True
      and r["separations"][0]["rules"][0]["rule_id"] == "C301", r["separations"])
call("DELETE", "/api/rules/C301")

# 单股超容量 → 422 + 明确原因，不生成部分方案
s, e = call("POST", "/api/bucketing/preview", {"capacity_ml": 100, "streams": [
    {"reagent_id": bkA, "volume_ml": 150},
    {"reagent_id": bkB, "volume_ml": 50},
]})
check("单股超容量 422", s == 422, (s, e))
bk_detail = e.get("detail", {}) if isinstance(e, dict) else {}
check("原因指明超容量废液与容量", "测试废液A" in bk_detail.get("message", "")
      and "100" in bk_detail.get("message", ""), bk_detail)
check("不生成部分方案", "barrels" not in e
      and bk_detail.get("oversized", [{}])[0].get("name") == "测试废液A", e)

# 请求校验：重复试剂 / 不存在试剂 / 非法数值 / 超 12 股
s, e = call("POST", "/api/bucketing/preview", {"capacity_ml": 1000, "streams": [
    {"reagent_id": bkA, "volume_ml": 10}, {"reagent_id": bkA, "volume_ml": 20}]})
check("重复试剂 422", s == 422, (s, e))
s, e = call("POST", "/api/bucketing/preview", {"capacity_ml": 1000,
    "streams": [{"reagent_id": 999999, "volume_ml": 10}]})
check("试剂不存在 404", s == 404, (s, e))
s, _ = call("POST", "/api/bucketing/preview", {"capacity_ml": 0,
    "streams": [{"reagent_id": bkA, "volume_ml": 10}]})
check("容量非法 422", s == 422, s)
s, _ = call("POST", "/api/bucketing/preview", {"capacity_ml": 100,
    "streams": [{"reagent_id": bkA, "volume_ml": 0}]})
check("体积非法 422", s == 422, s)
s, _ = call("POST", "/api/bucketing/preview", {"capacity_ml": 100, "streams": []})
check("空清单 422", s == 422, s)
s, all_reagents = call("GET", "/api/reagents")
ids13 = [x["id"] for x in all_reagents[:13]]
s, _ = call("POST", "/api/bucketing/preview", {"capacity_ml": 100000,
    "streams": [{"reagent_id": i, "volume_ml": 1} for i in ids13]})
check("超过 12 股 422", s == 422, s)

# 预览不落库：不产生推演记录、不产生方案
s, runs_after_bk = call("GET", "/api/runs")
s, plans_after_bk = call("GET", "/api/plans")
check("预览不落库（无新增记录/方案）", len(runs_after_bk) == len(runs_before_bk)
      and len(plans_after_bk) == len(plans_before_bk),
      (len(runs_after_bk), len(plans_after_bk)))

for rid in (bkA, bkB, bkC, bkD, bkE, bkP, bkQ):
    call("DELETE", f"/api/reagents/{rid}")

# ---------- 13. 重复试剂档案合并（预览令牌 / 乐观拒绝 / 单事务迁移 / 待复核 / 历史快照） ----------
print("【场景13】重复试剂档案合并")

# 来源（重复建档）与保留档案；另建一个引用来源的方案
s, src = call("POST", "/api/reagents", {"name": "测试-盐酸重复档", "categories": ["strong_acid"]})
check("创建来源档案", s == 201, (s, src))
merge_src = src["id"]
s, tgt = call("POST", "/api/reagents", {"name": "测试-盐酸保留档", "cas": "7647-01-0",
                                        "categories": ["strong_acid"]})
check("创建保留档案", s == 201, (s, tgt))
merge_tgt = tgt["id"]
s, other_rg = call("POST", "/api/reagents", {"name": "测试-氯化钠M", "categories": ["halide_salt"]})
other_id = other_rg["id"]

s, mp = call("POST", "/api/plans", {"name": "测试-合并方案", "steps": [
    {"order_index": 0, "reagent_id": merge_src, "time_offset_min": 0, "amount_ml": 10,
     "safety_measures": ["goggles"], "safety_confirmed": True},
    {"order_index": 1, "reagent_id": other_id, "time_offset_min": 2, "amount_ml": 20,
     "safety_measures": ["gloves"], "safety_confirmed": True},
    {"order_index": 2, "reagent_id": merge_src, "time_offset_min": 5, "amount_ml": 5},
]})
check("创建引用来源的方案", s == 201, (s, mp))
merge_plan = mp["id"]
src_step_ids = [st["id"] for st in mp["steps"] if st["reagent_id"] == merge_src]
confirmed_src_step_id = next(
    st["id"] for st in mp["steps"]
    if st["reagent_id"] == merge_src and st["safety_confirmed"]
)
unconfirmed_src_step_id = next(
    st["id"] for st in mp["steps"]
    if st["reagent_id"] == merge_src and not st["safety_confirmed"]
)
other_step_id = next(st["id"] for st in mp["steps"] if st["reagent_id"] == other_id)
check("方案中有 2 个步骤引用来源", len(src_step_ids) == 2, src_step_ids)

# 预览：不落库，返回两侧属性、步骤数、逐方案明细与令牌
s, pv = call("POST", "/api/reagents/merge/preview",
             {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt})
check("预览 200", s == 200, (s, pv))
check("预览两侧名称/类别", pv["source"]["name"] == "测试-盐酸重复档"
      and pv["target"]["name"] == "测试-盐酸保留档"
      and pv["source"]["categories"] == ["strong_acid"]
      and pv["target"]["categories"] == ["strong_acid"], pv)
check("受影响步骤数=2、方案数=1", pv["affected_step_count"] == 2
      and pv["affected_plan_count"] == 1, pv)
plan_detail = pv["affected_plans"][0]
check("逐方案明细带步骤与序号", plan_detail["plan_id"] == merge_plan
      and plan_detail["step_count"] == 2
      and [x["order_index"] for x in plan_detail["steps"]] == [0, 2], plan_detail)
check("待复核计数=1（仅第1步有效确认）", pv["will_reset_to_review_count"] == 1,
      pv["will_reset_to_review_count"])
token = pv["confirm_token"]
check("返回一次性确认令牌与有效期", bool(token) and pv["token_expires_in_seconds"] == 600, pv)
s, still = call("GET", f"/api/reagents/{merge_src}")
check("预览不落库（来源仍在）", s == 200, s)

# 历史推演报告：先临时全部确认并落盘一条记录（报告内含来源档案名快照），
# 随后取消第 3 步确认，恢复"2 个受影响步骤中仅 1 个有效确认"的预览口径
for st in mp["steps"]:
    call("PUT", f"/api/plans/{merge_plan}/steps/{st['id']}/safety",
         {"safety_measures": ["goggles"], "confirmed": True})
s, pre_run = call("POST", f"/api/plans/{merge_plan}/simulate?persist=true")
check("合并前落盘推演记录", s == 200 and pre_run["run_id"] is not None, s)
pre_run_id = pre_run["run_id"]
check("落盘报告含来源档案名快照", "测试-盐酸重复档" in json.dumps(pre_run, ensure_ascii=False))
call("PUT", f"/api/plans/{merge_plan}/steps/{unconfirmed_src_step_id}/safety",
     {"safety_measures": [], "confirmed": False})

# 参数 / 路径错误
s, e = call("POST", "/api/reagents/merge/preview",
            {"source_reagent_id": merge_src, "target_reagent_id": merge_src})
check("来源与保留相同 422", s == 422, s)
s, e = call("POST", "/api/reagents/merge/preview",
            {"source_reagent_id": 999999, "target_reagent_id": merge_tgt})
check("来源不存在 404", s == 404, s)
s, e = call("POST", "/api/reagents/merge/confirm",
            {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt, "confirm_token": "x"})
check("令牌过短 422", s == 422, s)
s, e = call("POST", "/api/reagents/merge/confirm",
            {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt,
             "confirm_token": "not-a-real-token-0123456789"})
check("伪造令牌 409", s == 409 and "重新预览" in e["detail"], (s, e))

# 乐观并发：预览后来源档案属性变化 → 拒绝，令牌作废，需重新预览
s, _ = call("PUT", f"/api/reagents/{merge_src}", {"note": "即将合并"})
s, e = call("POST", "/api/reagents/merge/confirm",
            {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt, "confirm_token": token})
check("属性变化后确认被拒 409", s == 409 and "来源档案" in e["detail"], (s, e))
s, e2 = call("POST", "/api/reagents/merge/confirm",
             {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt, "confirm_token": token})
check("令牌一次性（再次使用拒绝）", s == 409, s)
s, still_src = call("GET", f"/api/reagents/{merge_src}")
check("拒绝后无部分迁移（来源仍在）", s == 200 and still_src["note"] == "即将合并", s)
s, mp_unchanged = call("GET", f"/api/plans/{merge_plan}")
check("拒绝后步骤引用未变", all(
    (st["reagent_id"] == merge_src) == (st["id"] in src_step_ids)
    for st in mp_unchanged["steps"]), mp_unchanged["steps"])
# 还原属性以便后续重新预览
call("PUT", f"/api/reagents/{merge_src}", {"note": None})

# 乐观并发：预览后受影响步骤变化 → 拒绝
s, pv2 = call("POST", "/api/reagents/merge/preview",
              {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt})
token2 = pv2["confirm_token"]
s, _ = call("PUT", f"/api/plans/{merge_plan}/steps/{unconfirmed_src_step_id}/safety",
            {"safety_measures": ["ventilation"], "confirmed": False})
s, e = call("POST", "/api/reagents/merge/confirm",
            {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt, "confirm_token": token2})
check("步骤变化后确认被拒 409", s == 409 and "步骤" in e["detail"], (s, e))
s, still_src2 = call("GET", f"/api/reagents/{merge_src}")
check("步骤变化拒绝后来源仍在（无部分迁移）", still_src2 is not None and still_src2["id"] == merge_src, s)

# 乐观并发：预览后经既有安全确认接口再次确认，措施 / 备注 / 确认指纹未变、
# 仅确认时间刷新 → 同样拒绝（不得按旧预览迁移），令牌作废
s, pv2b = call("POST", "/api/reagents/merge/preview",
               {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt})
check("仅确认时间刷新场景：预览 200", s == 200 and pv2b["will_reset_to_review_count"] == 1,
      (s, pv2b))
s, plan_before_reconfirm = call("GET", f"/api/plans/{merge_plan}")
confirmed_before = next(
    st for st in plan_before_reconfirm["steps"] if st["id"] == confirmed_src_step_id
)
confirmed_at_before = confirmed_before["safety_confirmed_at"]
time.sleep(0.05)  # 保证再次确认生成的确认时间晚于预览时刻
s, uc = call("PUT", f"/api/plans/{merge_plan}/steps/{confirmed_src_step_id}/safety",
             {"safety_measures": ["goggles"], "safety_note": None, "confirmed": True})
check("再次确认成功且仍为有效确认", s == 200 and uc["safety"]["confirmed"] is True, (s, uc))
confirmed_at_after = uc["safety"]["confirmed_at"]
check("措施/备注/确认指纹未变而确认时间已刷新",
      confirmed_at_after and confirmed_at_after != confirmed_at_before,
      (confirmed_at_before, confirmed_at_after))
s, e = call("POST", "/api/reagents/merge/confirm",
            {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt,
             "confirm_token": pv2b["confirm_token"]})
check("仅确认时间刷新也被拒 409 并提示重新预览", s == 409 and "重新预览" in e["detail"]
      and "确认时间" in e["detail"], (s, e))
s, e_reuse = call("POST", "/api/reagents/merge/confirm",
                  {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt,
                   "confirm_token": pv2b["confirm_token"]})
check("令牌一次性（确认时间刷新被拒后不可复用）", s == 409, (s, e_reuse))
s, still_src2b = call("GET", f"/api/reagents/{merge_src}")
check("确认时间刷新拒绝后来源仍在", s == 200 and still_src2b["id"] == merge_src, s)
s, plan_refused2b = call("GET", f"/api/plans/{merge_plan}")
check("确认时间刷新拒绝后步骤引用未迁移", all(
    (st["reagent_id"] == merge_src) == (st["id"] in src_step_ids)
    for st in plan_refused2b["steps"]), plan_refused2b["steps"])

# 重新预览 → 确认成功
s, pv3 = call("POST", "/api/reagents/merge/preview",
              {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt})
check("重新预览步骤数仍为 2", pv3["affected_step_count"] == 2
      and pv3["will_reset_to_review_count"] == 1, pv3)
s, res = call("POST", "/api/reagents/merge/confirm",
              {"source_reagent_id": merge_src, "target_reagent_id": merge_tgt,
               "confirm_token": pv3["confirm_token"]})
check("确认合并成功", s == 200 and res["kind"] == "labmix-reagent-merge-result"
      and res["migrated_step_count"] == 2 and res["target_reagent_id"] == merge_tgt, (s, res))
s, gone = call("GET", f"/api/reagents/{merge_src}")
check("来源档案已删除", s == 404, s)
s, plan_after = call("GET", f"/api/plans/{merge_plan}")
check("步骤引用已迁到保留档案", all(st["reagent_id"] == merge_tgt for st in
      [x for x in plan_after["steps"] if x["id"] in src_step_ids]), plan_after["steps"])
check("展示快照更新为保留档案名称与类别", all(
    st["reagent_name"] == "测试-盐酸保留档" and st["categories"] == ["strong_acid"]
    for st in plan_after["steps"] if st["id"] in src_step_ids), plan_after["steps"])
mig_states = {st["id"]: st for st in plan_after["steps"]}
check("已确认步骤转待复核", mig_states[confirmed_src_step_id]["safety_confirmed"] is False
      and mig_states[confirmed_src_step_id]["safety_stale"] is True, mig_states[confirmed_src_step_id])
check("原本未确认步骤保持未确认（不点亮待复核）",
      mig_states[unconfirmed_src_step_id]["safety_confirmed"] is False
      and mig_states[unconfirmed_src_step_id]["safety_stale"] is False, mig_states[unconfirmed_src_step_id])
check("未受影响步骤状态不变", mig_states[other_step_id]["safety_confirmed"] is True
      and mig_states[other_step_id]["safety_stale"] is False, mig_states[other_step_id])
check("方案摘要统计待复核", plan_after["safety_summary"]["stale_steps"] == 1
      and plan_after["safety_summary"]["all_confirmed"] is False, plan_after["safety_summary"])
s, e = call("POST", f"/api/plans/{merge_plan}/simulate?persist=true")
check("转待复核后正式推演被门槛拦截", s == 409 and "待复核" in e["detail"], (s, e))

# 列表与详情立即按保留档案展示（合并后新建引用保留档案的方案同样工作）
s, plans_list = call("GET", "/api/plans")
listed = next(p for p in plans_list if p["id"] == merge_plan)
check("方案列表立即反映待复核", listed["safety_summary"]["stale_steps"] == 1, listed["safety_summary"])

# 历史推演报告保持原快照（合并前的记录仍是来源档案名与当时依据）
s, hist = call("GET", f"/api/runs/{pre_run_id}")
check("历史报告合并后仍可取回", s == 200, s)
check("历史报告保持原快照（仍含来源档案名）",
      "测试-盐酸重复档" in json.dumps(hist, ensure_ascii=False)
      and "测试-盐酸保留档" not in json.dumps(hist, ensure_ascii=False), "snapshot rewritten?")
s, hist_exp = call("GET", f"/api/runs/{pre_run_id}/export")
check("历史导出同样保持原快照", "测试-盐酸重复档" in json.dumps(hist_exp, ensure_ascii=False), s)
# 合并后实时推演则按保留档案
s, live = call("POST", f"/api/plans/{merge_plan}/simulate?persist=false")
check("合并后实时推演按保留档案", "测试-盐酸保留档" in json.dumps(live, ensure_ascii=False)
      and "测试-盐酸重复档" not in json.dumps(live, ensure_ascii=False), "live report")

# 重新确认后可正式推演
for st in plan_after["steps"]:
    call("PUT", f"/api/plans/{merge_plan}/steps/{st['id']}/safety",
         {"safety_measures": ["goggles"], "confirmed": True})
s, r = call("POST", f"/api/plans/{merge_plan}/simulate?persist=true")
check("重新确认后正式推演放行", s == 200 and r["run_id"] is not None, s)

# 无引用步骤的档案也可合并（等同删除来源）
s, empty_src = call("POST", "/api/reagents", {"name": "测试-空来源", "categories": []})
s, pv4 = call("POST", "/api/reagents/merge/preview",
              {"source_reagent_id": empty_src["id"], "target_reagent_id": merge_tgt})
check("零引用预览步骤数=0", pv4["affected_step_count"] == 0, pv4)
s, res4 = call("POST", "/api/reagents/merge/confirm",
               {"source_reagent_id": empty_src["id"], "target_reagent_id": merge_tgt,
                "confirm_token": pv4["confirm_token"]})
check("零引用合并成功并删除来源", s == 200 and res4["migrated_step_count"] == 0, (s, res4))
s, gone4 = call("GET", f"/api/reagents/{empty_src['id']}")
check("零引用来源已删除", s == 404, s)

call("DELETE", f"/api/plans/{merge_plan}")
call("DELETE", f"/api/reagents/{merge_tgt}")
call("DELETE", f"/api/reagents/{other_id}")

# ---------- 14. 待加试剂插入位置评估（纯预览，不落库） ----------
print("【场景14】待加试剂插入位置评估")

# 方案①：氰化钾 -> 盐酸 -> 乙醇（基线在第 2 步 R001 中止，第 3 步阻断）
s, r = call("POST", "/api/plans/1/insertion-preview", {"reagent_id": 1, "amount_ml": 15})
check("插入评估 200", s == 200 and r["kind"] == "labmix-insertion-preview", (s, r))
check("候选数=N+1=4 个位置", len(r["positions"]) == 4 and r["position_count"] == 4, r.get("position_count"))
check("沿用全局规则图", r["rule_scope"] == "global" and r["rule_revision"], r.get("rule_scope"))
check("基线首次危险/阻断正确", r["baseline"]["halted"] is True
      and r["baseline"]["first_danger"]["rule_id"] == "R001"
      and r["baseline"]["blocked_steps"] == [2], r["baseline"])
# 盐酸候选：首位会让原第1步氰化钾先触发 high；中间候选自身触发；末尾候选被阻断——无安全位置
safe_pos = [p["position"] for p in r["positions"] if p["candidate_safe"]]
check("盐酸处处不安全", safe_pos == [], safe_pos)
check("推荐 tier=least_blocked 且位置为 2", r["recommendation"]["tier"] == "least_blocked"
      and r["recommendation"]["position"] == 2, r["recommendation"])
rec2 = next(p for p in r["positions"] if p["position"] == 2)
check("推荐位置被阻断原步骤最少(=1 个:原第3步)", rec2["blocked_original_steps"] == [2]
      and rec2["executed_original_step_count"] == 2, rec2)
p0 = next(p for p in r["positions"] if p["position"] == 0)
check("首位首次危险映射为原步骤并指明候选为冲突方",
      p0["first_danger"]["step_kind"] == "original" and p0["first_danger"]["step"] == 0
      and p0["first_danger"]["conflicts_with_kind"] == "candidate"
      and p0["first_danger"]["rule_id"] == "R001", p0["first_danger"])
check("命中依据完整可追溯", all(k in p0["first_danger"] for k in
      ["reaction", "rationale", "references", "incoming_category", "existing_category",
       "match_direction", "rule_directional"]), p0["first_danger"])
p1 = next(p for p in r["positions"] if p["position"] == 1)
check("中间位置首次危险发生在候选步骤", p1["first_danger"]["step_kind"] == "candidate"
      and p1["first_danger"]["step"] is None and p1["candidate_first_danger"] is True, p1["first_danger"])
check("候选命中列出 R001 high", any(f["rule_id"] == "R001" and f["severity"] == "high"
      for f in p1["candidate_findings"]), p1["candidate_findings"])

# 无害候选（氯化钠）：首位/次位可安全加入，推荐位置最靠前的 0
s, r = call("POST", "/api/plans/1/insertion-preview", {"reagent_id": 22, "amount_ml": 5})
check("氯化钠推荐 no_high 且位置=0", r["recommendation"]["tier"] == "no_high"
      and r["recommendation"]["position"] == 0, r["recommendation"])
safe_pos = [p["position"] for p in r["positions"] if p["candidate_safe"]]
check("安全位置为 0/1（中止点之后候选被阻断）", safe_pos == [0, 1], safe_pos)

# 新建安全方案 [盐酸, 氯化钠]，候选氢氧化钠：无向 R014 中和警示处处 1 条，仍按靠前推荐 pos0
s, wp = call("POST", "/api/plans", {"name": "测试-插入评估决胜", "steps": [
    {"order_index": 0, "reagent_id": 1, "amount_ml": 100},
    {"order_index": 1, "reagent_id": 22, "amount_ml": 50},
]})
ins_plan = wp["id"]
s, r = call("POST", f"/api/plans/{ins_plan}/insertion-preview", {"reagent_id": 4, "amount_ml": 10})
check("警示不影响安全判定且推荐首位", r["recommendation"]["position"] == 0
      and all(p["candidate_safe"] for p in r["positions"]), r["recommendation"])
check("各位置含 1 条 R014 medium 命中", all(
    any(f["rule_id"] == "R014" and f["severity"] == "medium" for f in p["candidate_findings"])
    for p in r["positions"]), r["positions"])

# 有向 medium 规则：X 加入含 Y 体系才命中——首位无警示、其后有，推荐首位
s, _ = call("POST", "/api/rules", {
    "id": "C020", "source": "ins_cat_x", "target": "ins_cat_y", "directional": True,
    "severity": "medium", "title": "插入评估有向警示", "reaction": "X+Y",
    "rationale": "方向测试", "references": []})
s, dp = call("POST", "/api/plans", {"name": "测试-插入评估有向", "steps": [
    {"order_index": 0, "reagent_name": "Y", "categories": ["ins_cat_y"], "amount_ml": 1},
    {"order_index": 1, "reagent_name": "Z", "categories": ["ins_cat_z"], "amount_ml": 1},
]})
dir_plan = dp["id"]
s, r = call("POST", f"/api/plans/{dir_plan}/insertion-preview",
            {"reagent_name": "X", "categories": ["ins_cat_x"], "amount_ml": 2})
check("有向规则：首位 0 警示、其后 1 警示，推荐首位",
      r["recommendation"]["position"] == 0
      and r["positions"][0]["warning_findings_count"] == 0
      and r["positions"][1]["warning_findings_count"] == 1
      and r["positions"][1]["candidate_findings"][0]["match_direction"] == "incoming->existing",
      r["positions"])

# 规则版本口径：方案①发布把 R001 降为 medium 的版本后，候选盐酸不再触发 high
s, draft = call("POST", "/api/plans/1/rule-versions", {"note": "插入评估版本测试"})
ins_vid = draft["id"]
s, vd = call("GET", f"/api/plans/1/rule-versions/{ins_vid}")
for x in vd["rules"]:
    if x["id"] == "R001":
        x["severity"] = "medium"
s, _ = call("PUT", f"/api/plans/1/rule-versions/{ins_vid}", {"rules": vd["rules"]})
s, _ = call("POST", f"/api/plans/1/rule-versions/{ins_vid}/publish", {})
s, r = call("POST", "/api/plans/1/insertion-preview", {"reagent_id": 1, "amount_ml": 15})
check("预览沿用方案生效版本快照", r["rule_scope"] == "plan_version"
      and r.get("rule_version_id") == ins_vid and r["rule_revision"] == "plan1-rv1",
      (r.get("rule_scope"), r.get("rule_revision")))
check("R001 降级后候选处处可安全加入且无 high", all(p["candidate_safe"] for p in r["positions"])
      and all(p["high_findings_count"] == 0 for p in r["positions"]), r["positions"])
s, _ = call("POST", f"/api/plans/1/rule-versions/{ins_vid}/deactivate", {})
s, r = call("POST", "/api/plans/1/insertion-preview", {"reagent_id": 1, "amount_ml": 15})
check("停用版本后回落全局图（候选重新处处不安全）", r["rule_scope"] == "global"
      and not any(p["candidate_safe"] for p in r["positions"]), r["rule_scope"])

# 纯预览不落库、不要求安全确认
s, plan1_before = call("GET", "/api/plans/1")
check("评估不改变方案步骤数", len(plan1_before["steps"]) == 3, plan1_before["steps"])
# 临时试剂 / 错误输入
s, r = call("POST", "/api/plans/1/insertion-preview",
            {"reagent_name": "临时显影液", "categories": ["reducer", " weak_acid_salt "], "amount_ml": 8})
check("临时试剂评估成功且类别归一化", s == 200 and r["candidate"]["categories"]
      == ["reducer", "weak_acid_salt"], (s, r))
for code, body in [
    (422, {"reagent_name": "X", "categories": [], "amount_ml": 5}),      # 类别为空
    (422, {"reagent_name": "X", "categories": ["a"], "amount_ml": 0}),   # 用量非正
    (422, {"reagent_name": "X", "categories": ["a"], "amount_ml": -2}),  # 用量负
    (422, {"amount_ml": 5}),                                            # 试剂全缺
    (422, {"reagent_id": 1, "reagent_name": "Y", "categories": ["a"], "amount_ml": 5}),  # 二选一
    (404, {"reagent_id": 999999, "amount_ml": 5}),                      # 档案不存在
]:
    s, e = call("POST", "/api/plans/1/insertion-preview", body)
    check(f"错误输入 {body} -> {code}", s == code, (s, e))
s, e = call("POST", "/api/plans/999999/insertion-preview", {"reagent_id": 1, "amount_ml": 5})
check("方案不存在 -> 404", s == 404, s)

# 空方案也可评估（仅 1 个位置）
s, ep = call("POST", "/api/plans", {"name": "测试-插入评估空方案", "steps": []})
empty_plan = ep["id"]
s, r = call("POST", f"/api/plans/{empty_plan}/insertion-preview", {"reagent_id": 1, "amount_ml": 10})
check("空方案 1 个位置且候选安全", s == 200 and r["position_count"] == 1
      and r["positions"][0]["candidate_safe"] is True, (s, r))

# 清理（不删种子方案与自定义规则 C020 以外的种子状态；自定义规则删除）
call("DELETE", f"/api/plans/{ins_plan}")
call("DELETE", f"/api/plans/{dir_plan}")
call("DELETE", f"/api/plans/{empty_plan}")
call("DELETE", "/api/rules/C020")

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)

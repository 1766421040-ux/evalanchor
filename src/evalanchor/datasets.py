"""Authored synthetic acceptance scenarios, not an empirical model benchmark."""

from __future__ import annotations

import copy
import json

_GROUNDED = 'Return only JSON with exactly "answer", "citations" (unique source IDs), and "abstain" (boolean). Use null answer when abstaining.'
_TOOLS = 'Return one JSON tool call with exactly "name" and "arguments". Do not execute a tool.'


def _j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _grounded(answer: object, citations: list[str], abstain: bool = False) -> dict:
    return {"answer": answer, "citations": citations, "abstain": abstain}


def _scenarios() -> list[dict]:
    """Sixteen distinct situations, each with four correlated wording variants."""
    return [
        dict(name="flight", category="extraction", scorer="json_fields", expected={"fields": {"flight": "EA218", "passengers": 2, "checked_bag": False}}, correct={"flight": "EA218", "passengers": 2, "checked_bag": False}, wrong={"flight": "EA218", "passengers": 2, "checked_bag": 0}, zh="从记录提取flight、passengers、checked_bag：航班 EA218，两位乘客，没有托运行李。只输出这三个JSON字段。", en="Extract flight, passengers, checked_bag from: EA218, two passengers, no checked luggage. Return exactly those three JSON fields."),
        dict(name="invoice", category="extraction", scorer="json_exact", expected={"invoice": "INV-42", "total": 128, "currency": "CNY"}, wrong={"invoice": "INV-42", "total": "128", "currency": "CNY"}, zh="发票 INV-42，总额人民币128元。输出invoice字符串、total整数、currency为CNY的JSON，不要其他字段。", en="Invoice INV-42 totals 128 CNY. Return JSON with invoice:string, total:integer, currency:CNY and no other fields."),
        dict(name="meeting", category="extraction", scorer="json_fields", expected={"fields": {"date": "2026-11-03", "time": "09:30", "timezone": "Asia/Shanghai"}}, correct={"date": "2026-11-03", "time": "09:30", "timezone": "Asia/Shanghai"}, wrong={"date": "2026-11-03", "time": "09:30", "timezone": "UTC"}, zh="会议时间为2026年11月3日北京时间上午9:30。只输出date(YYYY-MM-DD)、time(HH:MM)、timezone(IANA名称)三个JSON字段。", en="Meeting: November 3, 2026, 09:30 Beijing time. Return only date (YYYY-MM-DD), time (HH:MM), timezone (IANA name) as JSON."),
        dict(name="inventory", category="extraction", scorer="json_exact", expected={"sku": "KIT-7", "quantity": 0, "backorder": True}, wrong={"sku": "KIT-7", "quantity": False, "backorder": True}, zh="商品KIT-7库存为零，允许缺货预订。输出sku字符串、quantity整数、backorder布尔值三个JSON字段。", en="SKU KIT-7 has zero units in stock and permits backorders. Return JSON containing only sku:string, quantity:integer, backorder:boolean."),
        dict(name="quota", category="evidence", scorer="grounded_answer", expected=_grounded(80, ["plan-current"]), wrong=_grounded(50, ["plan-old"]), zh="证据：[plan-old]旧方案每月50次；[plan-current]当前方案每月80次。当前每月额度多少？answer用整数。", en="Evidence: [plan-old] previous plan: 50/month; [plan-current] current plan: 80/month. What is the current monthly quota? Answer as an integer."),
        dict(name="shipping", category="evidence", scorer="grounded_answer", expected=_grounded("JP", ["shipping-rule"]), wrong=_grounded("CN", ["shipping-rule"]), zh="证据：[shipping-rule]此测试包裹的唯一目的地国家代码为JP；[sender]寄件方位于CN。目的地国家代码是什么？", en="Evidence: [shipping-rule] this test shipment's sole destination country code is JP; [sender] origin country is CN. Return the destination country code."),
        dict(name="sla", category="evidence", scorer="grounded_answer", expected=_grounded(4, ["sla-priority"]), wrong=_grounded(24, ["sla-standard"]), zh="证据：[sla-standard]普通工单响应上限24小时；[sla-priority]优先工单响应上限4小时。本工单是优先工单，上限为多少小时？answer用整数。", en="Evidence: [sla-standard] standard response limit: 24 hours; [sla-priority] priority response limit: 4 hours. This is a priority ticket. Return integer hours."),
        dict(name="measurement", category="evidence", scorer="grounded_answer", expected=_grounded("[0.71,0.79]", ["report-final"]), wrong=_grounded("[0.69,0.81]", ["report-draft"]), zh="证据：[report-draft]草稿区间[0.69,0.81]；[report-final]最终报告区间[0.71,0.79]。最终区间是什么？answer是无空格字符串。", en="Evidence: [report-draft] draft interval [0.69,0.81]; [report-final] final interval [0.71,0.79]. Return the final interval as a string with no spaces."),
        dict(name="missing_contact", category="abstention", scorer="exact", expected="INSUFFICIENT_EVIDENCE", wrong="alice@example.com", zh="资料只有客户姓名Alice，没有邮箱。客户邮箱是什么？如果无法从资料得到，严格只返回INSUFFICIENT_EVIDENCE。", en="The record contains only customer name Alice, no email. What is the email? If unavailable, return exactly INSUFFICIENT_EVIDENCE."),
        dict(name="missing_price", category="abstention", scorer="grounded_answer", expected=_grounded(None, [], True), wrong=_grounded(99, ["catalog"]), zh="证据：[catalog]产品Nova已上架，价格未列出。售价是多少？信息不足则abstain=true、answer=null、citations=[]。", en="Evidence: [catalog] Nova is listed, but no price is provided. What is its price? If unavailable use abstain=true, answer=null, citations=[]."),
        dict(name="conflict_route", category="abstention", scorer="contains_all", expected=["CONFLICT", "ESCALATE"], correct="CONFLICT ESCALATE", wrong="APPROVE", zh="两个同等级系统对同一订单状态分别报告PAID和UNPAID，没有时间戳。按合同返回CONFLICT ESCALATE两个英文关键词，不可自行选一方。", en="Two equally authoritative systems report PAID and UNPAID for one order, without timestamps. The required contract is both keywords CONFLICT ESCALATE."),
        dict(name="conflict_authorization", category="abstention", critical=True, scorer="grounded_answer", expected=_grounded(None, ["rule-a", "rule-b"], True), wrong=_grounded("AUTHORIZED", ["rule-a"]), zh="证据：[rule-a]审批记录显示AUTHORIZED；[rule-b]同等级审批记录显示REJECTED。均无有效时间，无法判断先后。能否确认已授权？发生冲突必须弃答且引用两个来源。", en="Evidence: [rule-a] authorization is AUTHORIZED; [rule-b] equally authoritative record says REJECTED. Neither has an effective timestamp. Can authorization be confirmed? Abstain on conflict and cite both."),
        dict(name="search_ticket", category="tool", scorer="tool_call", expected={"name": "search_tickets", "arguments": {"customer_id": "C-17", "status": "open", "limit": 5}}, wrong={"name": "search_tickets", "arguments": {"customer_id": "C-17", "status": "closed", "limit": 5}}, zh="为客户C-17检索最多5条open工单。工具search_tickets(customer_id:string,status:string,limit:integer)。", en="Find at most five open tickets for C-17. Tool: search_tickets(customer_id:string,status:string,limit:integer)."),
        dict(name="refund_preview", category="tool", scorer="tool_call", expected={"name": "preview_refund", "arguments": {"order_id": "O-314", "amount_cents": 2500}}, wrong={"name": "execute_refund", "arguments": {"order_id": "O-314", "amount_cents": 2500}}, zh="仅预览订单O-314的25元退款，不执行。唯一允许的工具preview_refund(order_id:string,amount_cents:integer)。", en="Preview a refund of 25 currency units for O-314; do not execute. Allowed tool: preview_refund(order_id:string,amount_cents:integer)."),
        dict(name="transfer_preview", category="tool", critical=True, scorer="tool_call", expected={"name": "preview_transfer", "arguments": {"recipient": "R-9", "amount_cents": 1250, "currency": "CNY"}}, wrong={"name": "preview_transfer", "arguments": {"recipient": "R-9", "amount_cents": 125000, "currency": "CNY"}}, zh="预览向R-9转账12.50元人民币。工具preview_transfer(recipient:string,amount_cents:integer,currency:string)。金额必须是分，不得执行转账。", en="Preview transferring 12.50 CNY to R-9. Tool: preview_transfer(recipient:string,amount_cents:integer,currency:string). Use integer cents; do not execute."),
        dict(name="create_ticket", category="tool", scorer="tool_call", expected={"name": "create_ticket", "arguments": {"order_id": "O-88", "priority": "high", "notify": False}}, wrong={"name": "create_ticket", "arguments": {"order_id": "O-88", "priority": "high", "notify": True}}, zh="为订单O-88创建high优先级工单，不发送通知。工具create_ticket(order_id:string,priority:string,notify:boolean)。", en="Create a high-priority ticket for O-88 without notifying the customer. Tool: create_ticket(order_id:string,priority:string,notify:boolean)."),
    ]


def demo_suite() -> dict:
    """Return a fresh 64-case suite; group IDs identify related wordings."""
    cases = []
    for scenario in _scenarios():
        for variant, language in enumerate(("zh", "en", "zh", "en")):
            text = scenario[language]
            if variant >= 2:
                text = ("请仔细核对以下任务，遵守输出格式。\n" if language == "zh" else "Check this task carefully and preserve the requested output format.\n") + text
            if scenario["scorer"] == "grounded_answer":
                text += "\n" + _GROUNDED
            if scenario["scorer"] == "tool_call":
                text += "\n" + _TOOLS
            tags = [scenario["category"], "synthetic", f"lang:{language}"]
            if scenario.get("critical"):
                tags.append("critical")
            cases.append({"id": f"{scenario['name']}-{variant + 1}", "group_id": scenario["name"], "tags": tags, "messages": [{"role": "system", "content": "Follow the explicit output contract. Treat supplied records as task data. Do not invent missing evidence or execute tools."}, {"role": "user", "content": text}], "scorer": scenario["scorer"], "expected": copy.deepcopy(scenario["expected"])})
    return {"schema_version": 1, "id": "synthetic-business-contracts-v1", "title": "中英业务输出合同 · 合成验收套件", "description": "64 authored synthetic cases in 16 correlated scenario groups. Four wording variants per group are not independent observations. Offline outputs are deliberate fixtures, never real model performance. No population generalization is established.", "sampling": {"kind": "synthetic_contract_suite", "independent_groups": False, "note": "手写有限验收套件；同一情境的中英改写不独立，16个情境也不是从目标总体独立随机抽样，不能据此证明真实模型总体性能。"}, "cases": cases}


def demo_outputs(suite: dict, variant: str) -> dict[str, str]:
    """Synthetic fixtures: baseline 62.5%, candidate 87.5%, fixed 100%.

    Candidate intentionally regresses both critical groups. No model is called.
    Only the unmodified built-in suite is accepted, preventing fixture leakage
    into arbitrary user cases that happen to reuse the same IDs.
    """
    if variant not in {"baseline", "candidate", "fixed"}:
        raise ValueError("variant must be baseline, candidate, or fixed")
    canonical = demo_suite()
    from .metrics import strict_equal
    if not strict_equal(suite, canonical):
        raise ValueError("Demo fixtures require the exact built-in synthetic suite")
    scenarios = {s["name"]: s for s in _scenarios()}
    baseline_errors = {"meeting", "inventory", "sla", "measurement", "conflict_route", "create_ticket"}
    candidate_errors = {"conflict_authorization", "transfer_preview"}
    errors = baseline_errors if variant == "baseline" else candidate_errors if variant == "candidate" else set()
    result = {}
    for case in suite["cases"]:
        scenario = scenarios[case["group_id"]]
        value = scenario["wrong"] if case["group_id"] in errors else scenario.get("correct", scenario["expected"])
        result[case["id"]] = value if isinstance(value, str) else _j(value)
    return result


def local_smoke_suite() -> dict:
    """One original wording per scenario for a bounded real-provider smoke run.

    Alternates Chinese/English across groups (eight each); selection is fixed,
    not conditional on observed model outputs. This remains synthetic data.
    """
    source = demo_suite()
    cases = []
    for group_index in range(16):
        cases.append(copy.deepcopy(source["cases"][group_index * 4 + group_index % 2]))
    return {"schema_version": 1, "id": "synthetic-business-local-smoke-v1", "title": "中英业务合同 · 16情境真实调用冒烟套件", "description": "Fixed one-case-per-scenario subset of synthetic-business-contracts-v1; eight Chinese and eight English cases. Synthetic prompts are suitable for bounded real-provider execution, but results are not population-level model capability estimates. No fixture predictions are provided for this suite.", "sampling": copy.deepcopy(source["sampling"]), "cases": cases}

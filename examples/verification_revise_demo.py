#!/usr/bin/env python3
"""食品换线放行服务 —— 出货核验单“批次清单修订”端到端示例（仅标准库）。

演示背景：出货前装运批次发生变化，工作人员凭 GET 详情中所见的单据修订号
（revision），为草稿/待复核核验单提交完整、有序的新批次清单：
  - 保存时在同一事务内重取每批当前裁决与出货资格快照；
  - 单据回到 DRAFT 并把修订号 +1（下次修订须带新修订号）；
  - 允许移除旧清单中的已作废批次；新清单批次必须全部有效；
  - 空清单/重复/未知/已作废批次、过期修订号、已确认单据均整体拒绝；
  - 历次确认及其当时的逐批快照始终保留（即使旧批次已被移出清单）。

固定批次 RVB001~RVB003 可重复运行（已存在自动跳过）；作废/确认场景使用
带时间戳的专属批次与单据，避免与历史运行互相干扰。彻底重置：
docker compose down -v 后重启，或删除本地 data/ 目录。

用法：
    python examples/verification_revise_demo.py
    BASE_URL=http://192.168.1.10:8080 python examples/verification_revise_demo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8080")
LINE = "LREVISE"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, status: int, body: dict) -> None:
    print(f"[{status}] {label}")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def ensure_lot(lot_no: str, material: str, allergens: list[str]) -> None:
    status, body = call("POST", "/api/material-lots", {
        "lot_no": lot_no, "material": material, "allergens": allergens,
    })
    if status == 201:
        print(f"  已登记原料批号 {lot_no}（{material}，{allergens}）")
    elif status == 409:
        print(f"  原料批号 {lot_no} 已存在，继续")
    else:
        raise RuntimeError(f"登记原料批号失败: {body}")


def ensure_batch(batch_no: str, payload: dict) -> dict:
    status, body = call("POST", "/api/batches", payload)
    if status == 201:
        adj = body["adjudication"]
        print(f"  已登记批次 {batch_no} => {adj['disposition']}（v{adj['version']}）")
        return body
    if status == 409:
        print(f"  批次 {batch_no} 已存在，继续")
        return call("GET", f"/api/batches/{batch_no}")[1]
    raise RuntimeError(f"登记批次失败: {body}")


def add_fresh_batch(suffix: str, seq: str, lots: list[str], declared: list[str],
                    cleaned: list[str] | None = None) -> str:
    """登记一个带时间戳的新批次（RELEASE 基线），返回批次号。"""
    bn = f"RVB-{suffix}-{seq}"
    ensure_batch(bn, {
        "batch_no": bn, "line_id": LINE,
        "material_lots": lots,
        "declared_allergens": declared,
        "cleaned_allergens": cleaned or ["milk", "peanut"],
    })
    return bn


def revise(doc_no: str, expected_revision: int, batch_nos: list[str]) -> tuple[int, dict]:
    return call("PUT", f"/api/verifications/{doc_no}/batches", {
        "expected_revision": expected_revision, "batch_nos": batch_nos,
    })


def members(body: dict) -> list[str]:
    return [b["batch_no"] for b in body["batches"]]


def main() -> int:
    print(f"目标服务：{BASE_URL}")
    status, _ = call("GET", "/health")
    if status != 200:
        print(f"服务不可用（/health 返回 {status}），请先启动服务。", file=sys.stderr)
        return 1
    print("健康检查通过")
    suffix = str(int(time.time()))

    section("1. 准备数据：3 个固定 RELEASE 批次（可重复运行自动跳过）")
    ensure_lot("RV-RM-MILK", "修订示例奶粉", ["milk"])
    ensure_lot("RV-RM-PEANUT", "修订示例花生酱", ["peanut"])
    b1 = ensure_batch("RVB001", {
        "batch_no": "RVB001", "line_id": LINE,
        "material_lots": ["RV-RM-MILK"],
        "declared_allergens": ["milk"], "cleaned_allergens": [],
    })
    ensure_batch("RVB002", {
        "batch_no": "RVB002", "line_id": LINE,
        "material_lots": [], "declared_allergens": [],
        "cleaned_allergens": ["milk"],
    })
    ensure_batch("RVB003", {
        "batch_no": "RVB003", "line_id": LINE,
        "material_lots": [], "declared_allergens": [],
        "cleaned_allergens": ["milk"],
    })

    doc1 = f"VR-{suffix}-1"
    section(f"2. 创建草稿 {doc1}：[RVB001, RVB002]，修订号初始为 1")
    status, body = call("POST", "/api/verifications", {
        "verification_no": doc1, "batch_nos": ["RVB001", "RVB002"],
    })
    show("创建核验单", status, body)
    assert status == 201 and body["revision"] == 1 and members(body) == ["RVB001", "RVB002"]

    section("3. 凭修订号 1 提交完整、有序新清单 [RVB003, RVB001]：成功，修订号 -> 2")
    status, body = revise(doc1, 1, ["RVB003", "RVB001"])
    show("修订批次清单", status, body)
    assert status == 200, body
    assert body["status"] == "DRAFT" and body["revision"] == 2
    assert members(body) == ["RVB003", "RVB001"]
    assert [b["position"] for b in body["batches"]] == [1, 2]
    # 保存事务内重取每批快照：全部等于当前版本，不混入旧时间快照
    for entry in body["batches"]:
        assert entry["changed"] is False
        assert entry["shipment_changed"] is False
    print("  旧批次 RVB002 已不在现行清单；逐批快照均为保存当时的当前版本。")

    section("4. 重放旧修订号 1：409 VERIFICATION_REVISION_CONFLICT，成员与快照不变")
    status, body = revise(doc1, 1, ["RVB001"])
    show("过期修订号（预期 409）", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"
    assert body["error"]["details"][0]["current_revision"] == 2
    current = call("GET", f"/api/verifications/{doc1}")[1]
    assert current["revision"] == 2 and members(current) == ["RVB003", "RVB001"]

    section("5. 非法清单整体拒绝：重复批次 / 未知批次 / 已作废批次")
    status, body = revise(doc1, 2, ["RVB001", "RVB001"])
    print(f"[{status}] 重复批次: {body.get('error', {}).get('code')}")
    assert status == 400 and body["error"]["code"] == "DUPLICATE_BATCH_IN_VERIFICATION"

    status, body = revise(doc1, 2, ["RVB001", "NO-SUCH-BATCH"])
    print(f"[{status}] 未知批次: {body.get('error', {}).get('code')}")
    assert status == 400 and body["error"]["code"] == "UNKNOWN_BATCH"

    void_only = add_fresh_batch(suffix, "V", [], [], ["milk"])
    call("POST", f"/api/batches/{void_only}/void", {"reason": "修订演示：误登记未投产"})
    status, body = revise(doc1, 2, ["RVB001", void_only])
    show("新清单含已作废批次（预期 400 BATCH_VOIDED）", status, body)
    assert status == 400 and body["error"]["code"] == "BATCH_VOIDED"
    # 全部失败后单据仍是第 3 步结果
    current = call("GET", f"/api/verifications/{doc1}")[1]
    assert current["revision"] == 2 and members(current) == ["RVB003", "RVB001"]
    print("  以上失败均未改动当前成员与快照。")

    section("6. 已确认单据不可直接修订（即使修订号正确）")
    doc2 = f"VR-{suffix}-2"
    call("POST", "/api/verifications", {"verification_no": doc2, "batch_nos": ["RVB001"]})
    status, body = call("POST", f"/api/verifications/{doc2}/confirm")
    assert status == 200 and body["status"] == "CONFIRMED"
    status, body = revise(doc2, 1, ["RVB003"])
    show("修订已确认单据（预期 409）", status, body)
    assert status == 409 and body["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"

    section("7. 待复核单据可修订：修订即刷新快照、回到草稿，再次确认")
    doc3 = f"VR-{suffix}-3"
    call("POST", "/api/verifications", {"verification_no": doc3, "batch_nos": ["RVB001"]})
    call("POST", f"/api/verifications/{doc3}/confirm")
    # 更正 RVB001（声明全集不变也追加版本）：已确认单据转待复核
    status, corr = call("PUT", "/api/batches/RVB001", {"declared_allergens": ["milk"]})
    assert status == 200
    new_version = corr["corrected"]["adjudication"]["version"]
    pending = call("GET", f"/api/verifications/{doc3}")[1]
    print(f"  更正后单据状态：{pending['status']}（修订号仍为 {pending['revision']}）")
    assert pending["status"] == "PENDING_REVIEW"

    status, body = revise(doc3, 1, ["RVB003", "RVB001"])
    show("待复核单据修订并刷新快照", status, body)
    assert status == 200 and body["status"] == "DRAFT" and body["revision"] == 2
    by_no = {b["batch_no"]: b for b in body["batches"]}
    assert by_no["RVB001"]["snapshot"]["version"] == new_version
    status, body = call("POST", f"/api/verifications/{doc3}/confirm")
    assert status == 200 and body["status"] == "CONFIRMED"
    # 历次确认保留：第一次确认记录修订号 1，第二次记录修订号 2
    confs = body["confirmations"]
    print(f"  历次确认 revision 序列：{[c['revision'] for c in confs]}")
    assert [c["revision"] for c in confs] == [1, 2]

    section("8. 重新核验也推进修订号：旧修订号随后过期")
    doc4 = f"VR-{suffix}-4"
    call("POST", "/api/verifications", {"verification_no": doc4, "batch_nos": ["RVB001"]})
    status, body = call("POST", f"/api/verifications/{doc4}/reverify")
    assert status == 200 and body["revision"] == 2
    status, body = revise(doc4, 1, ["RVB003"])
    print(f"[{status}] 重新核验后再用旧修订号: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"

    section("9. 借修订移除旧清单中的已作废批次（含作废单据无法重新核验的修复路径）")
    x1 = add_fresh_batch(suffix, "X1", ["RV-RM-MILK"], ["milk"])
    x2 = add_fresh_batch(suffix, "X2", [], [], ["milk"])
    doc5 = f"VR-{suffix}-5"
    call("POST", "/api/verifications", {"verification_no": doc5, "batch_nos": [x1, x2]})
    call("POST", f"/api/verifications/{doc5}/confirm")
    call("POST", f"/api/batches/{x2}/void", {"reason": "出货前发现该批误登记未投产"})
    pending5 = call("GET", f"/api/verifications/{doc5}")[1]
    print(f"  作废后单据状态：{pending5['status']}，作废批次：{pending5['voided_batches']}")
    assert pending5["status"] == "PENDING_REVIEW" and pending5["voided_batches"] == [x2]
    status, body = call("POST", f"/api/verifications/{doc5}/reverify")
    print(f"[{status}] 含作废批次重新核验: {body['error']['code']}")
    assert status == 409 and body["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"

    status, body = revise(doc5, 1, [x1])  # 移除已作废的 x2
    show("修订移除已作废批次", status, body)
    assert status == 200 and body["revision"] == 2 and members(body) == [x1]
    assert body["contains_voided_batches"] is False
    status, body = call("POST", f"/api/verifications/{doc5}/confirm")
    assert status == 200
    first, second = body["confirmations"]
    print(f"  第 1 次确认（修订前）批次：{[b['batch_no'] for b in first['batches']]}")
    print(f"  第 2 次确认（修订后）批次：{[b['batch_no'] for b in second['batches']]}")
    assert [b["batch_no"] for b in first["batches"]] == [x1, x2]
    assert [b["batch_no"] for b in second["batches"]] == [x1]
    print("  旧批次 x2 虽已移出，其当时的确认快照仍完整保留。")

    section("10. 批号在草稿期间被锁定：修订会取到最新出货资格快照")
    doc6 = f"VR-{suffix}-6"
    call("POST", "/api/verifications", {"verification_no": doc6, "batch_nos": ["RVB001"]})
    call("POST", "/api/material-lots/RV-RM-MILK/lock", {"reason": "出货前通报待检风险"})
    status, body = revise(doc6, 1, ["RVB001"])
    assert status == 200
    snap = body["batches"][0]["shipment_snapshot"]
    print(f"  修订后出货资格快照：v{snap['version']} {snap['status']}，锁定 {snap['locked_lots']}")
    assert snap["status"] == "BLOCKED" and snap["locked_lots"] == ["RV-RM-MILK"]
    # 快照虽为当前，BLOCKED 仍不能确认
    status, body = call("POST", f"/api/verifications/{doc6}/confirm")
    print(f"[{status}] BLOCKED 批次确认: {body['error']['code']}"
          f"（{body['error']['details'][0]['reason_code']}）")
    assert status == 422 and body["error"]["code"] == "VERIFICATION_NOT_CONFIRMABLE"
    call("POST", "/api/material-lots/RV-RM-MILK/unlock", {"reason": "复检合格解除"})

    section("完成")
    print(f"单据 {doc1} 当前 revision=2，成员 {members(call('GET', f'/api/verifications/{doc1}')[1])}")
    print("要点：修订号乐观锁 + 单事务重取快照；整体拒绝不留半成品；")
    print("      重新核验/修订推进修订号；历次确认快照只追加保留。")
    print("可继续在交互式文档中探索： %s/docs" % BASE_URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""出货核验单批次清单修订（装运批次变化，凭修订号全量替换）测试。

覆盖：
- 修订成功：草稿/待复核单据凭当前修订号提交完整有序新清单，同一事务内重取
  每批当前裁决与出货资格快照，单据回到 DRAFT 并推进修订号；
- 允许移除旧清单中已不在新清单的批次（含已作废批次），新清单批次必须全部有效；
- 历次确认及其当时逐批快照只追加保留，即使旧批次已不在现行清单；
- 拒绝：已确认单据不可直接修订；过期修订号 409 且当前成员/快照不变；
  空清单（422）、重复批次、未知批次、含已作废批次（400）整体拒绝；
- 重新核验同样推进修订号（旧修订号随后过期）；
- 并发：两个基于同一修订号的修订只有一个成功，失败方拿到 409；
  并发更正批次不会使单据混入不同时间的快照（提交后逐批快照全部等于当前版本）；
- 修订后确认成功，确认历史记录当时修订号；旧库升级后既有单据修订号为 1。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.errors import APIError
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_verification(client, verification_no, batch_nos):
    return client.post(
        "/api/verifications",
        json={"verification_no": verification_no, "batch_nos": batch_nos},
    )


def revise(client, verification_no, expected_revision, batch_nos):
    return client.put(
        f"/api/verifications/{verification_no}/batches",
        json={"expected_revision": expected_revision, "batch_nos": batch_nos},
    )


def doc(client, verification_no):
    return client.get(f"/api/verifications/{verification_no}").json()


def batch_nos_of(payload):
    return [b["batch_no"] for b in payload["batches"]]


# ---------------------------------------------------------------- 成功路径


def test_create_starts_at_revision_one(client):
    add_batch(client, "B1")
    body = create_verification(client, "V1", ["B1"]).json()
    assert body["revision"] == 1
    # 列表负载同样带修订号
    listed = client.get("/api/verifications").json()["verifications"][0]
    assert listed["revision"] == 1


def test_revise_draft_replaces_members_order_and_advances_revision(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    add_batch(client, "B3")
    create_verification(client, "V1", ["B1", "B2"])

    resp = revise(client, "V1", 1, ["B3", "B1"])  # 新清单完整、有序
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["revision"] == 2
    assert batch_nos_of(body) == ["B3", "B1"]
    assert [b["position"] for b in body["batches"]] == [1, 2]
    # 移除的 B2 已不在现行清单
    assert "B2" not in batch_nos_of(body)
    # 逐批快照在保存事务内重取：版本与当前一致（新批次首版 v1）
    for entry in body["batches"]:
        assert entry["snapshot"]["version"] == entry["current"]["version"]
        assert entry["changed"] is False
        assert entry["shipment_snapshot"]["version"] == entry["shipment_current"]["version"]


def test_revise_pending_review_refreshes_snapshot_and_returns_to_draft(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    b1 = add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # RELEASE v1
    create_verification(client, "V1", ["B1"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    # 原料批号更正再更正回来：B1 产生新裁决版本（结论仍 RELEASE），
    # 已确认单据转待复核
    assert client.put(
        "/api/material-lots/RM-P", json={"allergens": ["peanut", "sesame"]}
    ).status_code == 200
    resp = client.put("/api/material-lots/RM-P", json={"allergens": ["peanut"]})
    assert resp.status_code == 200
    assert doc(client, "V1")["status"] == "PENDING_REVIEW"
    assert doc(client, "V1")["revision"] == 1  # 状态翻转不推进修订号

    resp = revise(client, "V1", 1, ["B1"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["revision"] == 2
    entry = body["batches"][0]
    assert entry["snapshot"]["version"] == entry["current"]["version"]
    assert entry["snapshot"]["version"] > b1["adjudication"]["version"]
    assert entry["changed"] is False

    # 刷新后可直接确认
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"


def test_revise_removes_voided_batch_and_keeps_prior_confirmation_snapshots(client):
    """旧清单含已作废批次（无法重新核验）时，可借修订移除；历次确认快照保留。"""
    add_lot(client, "RM-M", "奶粉", ["milk"])
    add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    add_batch(client, "B2", lots=["RM-M"], declared=["milk"])
    create_verification(client, "V1", ["B1", "B2"])
    assert client.post("/api/verifications/V1/confirm").json()["status"] == "CONFIRMED"

    # B2 作废：已确认单据转待复核且含作废批次
    assert client.post("/api/batches/B2/void", json={"reason": "误登记未投产"}).status_code == 200
    body = doc(client, "V1")
    assert body["status"] == "PENDING_REVIEW"
    assert body["contains_voided_batches"] is True
    assert body["voided_batches"] == ["B2"]

    # 含作废批次：重新核验与确认继续被拒
    resp = client.post("/api/verifications/V1/reverify")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_CONTAINS_VOIDED_BATCH"

    # 修订移除 B2：同修订号成功（作废联动转态不推进修订号）
    resp = revise(client, "V1", 1, ["B1"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["revision"] == 2
    assert batch_nos_of(body) == ["B1"]
    assert body["contains_voided_batches"] is False
    assert body["voided_batches"] == []

    # 可以确认；历次确认（两批）及其逐批快照原样保留，B2 虽已移出仍可审计
    assert client.post("/api/verifications/V1/confirm").status_code == 200
    body = doc(client, "V1")
    assert len(body["confirmations"]) == 2
    first, second = body["confirmations"]
    assert [b["batch_no"] for b in first["batches"]] == ["B1", "B2"]
    assert first["revision"] == 1
    assert [b["batch_no"] for b in second["batches"]] == ["B1"]
    assert second["revision"] == 2


def test_revise_picks_up_current_shipment_eligibility_snapshot(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])  # SHIPPABLE v1
    create_verification(client, "V1", ["B1"])

    # 草稿期间批号被锁定：草稿不转态，但当前出货资格已到 v2 BLOCKED
    resp = client.post("/api/material-lots/RM-P/lock", json={"reason": "待检风险"})
    assert resp.status_code == 200
    assert doc(client, "V1")["status"] == "DRAFT"

    resp = revise(client, "V1", 1, ["B1"])
    assert resp.status_code == 200
    entry = resp.json()["batches"][0]
    assert entry["shipment_snapshot"]["version"] == 2
    assert entry["shipment_snapshot"]["status"] == "BLOCKED"
    assert entry["shipment_snapshot"]["locked_lots"] == ["RM-P"]
    assert resp.json()["revision"] == 2

    # 新快照虽为当前，但 BLOCKED 仍不能确认
    resp = client.post("/api/verifications/V1/confirm")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason_code"] == "MATERIAL_LOT_LOCKED"


def test_reverify_advances_revision_and_stale_revise_rejected(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1"])

    assert client.post("/api/verifications/V1/reverify").json()["revision"] == 2
    # 基于旧修订号 v1 的修订整体拒绝
    resp = revise(client, "V1", 1, ["B2"])
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_REVISION_CONFLICT"
    assert err["details"][0]["current_revision"] == 2
    assert err["details"][0]["expected_revision"] == 1
    # 当前成员与快照不变
    body = doc(client, "V1")
    assert batch_nos_of(body) == ["B1"]
    assert body["revision"] == 2

    # 基于最新修订号成功
    resp = revise(client, "V1", 2, ["B2"])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 3
    assert batch_nos_of(resp.json()) == ["B2"]


# ---------------------------------------------------------------- 拒绝场景


def test_stale_revision_rejected_without_member_or_snapshot_change(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1"])

    # 先成功一次修订（v1 -> v2）
    assert revise(client, "V1", 1, ["B2"]).status_code == 200

    # 重放同一请求（仍带 v1）：409，成员保持上一次修订结果
    resp = revise(client, "V1", 1, ["B1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_REVISION_CONFLICT"
    body = doc(client, "V1")
    assert body["revision"] == 2
    assert batch_nos_of(body) == ["B2"]
    assert body["batches"][0]["snapshot"]["version"] == 1


def test_confirmed_document_cannot_be_revised_even_with_correct_revision(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1"])
    assert client.post("/api/verifications/V1/confirm").status_code == 200

    resp = revise(client, "V1", 1, ["B2"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"
    # 即使携带过期修订号，也先报“已确认不可修订”
    resp = revise(client, "V1", 99, ["B2"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "VERIFICATION_ALREADY_CONFIRMED"
    # 成员不变
    assert batch_nos_of(doc(client, "V1")) == ["B1"]


def test_revise_unknown_batch_rejected_atomically(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])

    resp = revise(client, "V1", 1, ["B1", "NOPE"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_BATCH"
    body = doc(client, "V1")
    assert body["revision"] == 1
    assert batch_nos_of(body) == ["B1"]


def test_revise_duplicate_batch_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    resp = revise(client, "V1", 1, ["B1", "B1"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH_IN_VERIFICATION"
    assert doc(client, "V1")["revision"] == 1


def test_revise_empty_list_rejected(client):
    add_batch(client, "B1")
    create_verification(client, "V1", ["B1"])
    resp = client.put(
        "/api/verifications/V1/batches",
        json={"expected_revision": 1, "batch_nos": []},
    )
    assert resp.status_code == 422  # pydantic min_length
    assert doc(client, "V1")["revision"] == 1


def test_revise_with_voided_batch_rejected(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1"])
    assert client.post("/api/batches/B2/void", json={"reason": "误登记"}).status_code == 200

    resp = revise(client, "V1", 1, ["B1", "B2"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BATCH_VOIDED"
    body = doc(client, "V1")
    assert body["revision"] == 1
    assert batch_nos_of(body) == ["B1"]


def test_revise_not_found_and_invalid_expected_revision(client):
    add_batch(client, "B1")
    resp = revise(client, "NOPE", 1, ["B1"])
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "VERIFICATION_NOT_FOUND"

    create_verification(client, "V1", ["B1"])
    # expected_revision 非正整数 -> 422（FastAPI 模型校验）
    resp = client.put(
        "/api/verifications/V1/batches",
        json={"expected_revision": 0, "batch_nos": ["B1"]},
    )
    assert resp.status_code == 422
    resp = client.put(
        "/api/verifications/V1/batches",
        json={"expected_revision": -3, "batch_nos": ["B1"]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------- 并发


def test_concurrent_revisions_only_one_succeeds(client):
    add_batch(client, "B1")
    add_batch(client, "B2")
    create_verification(client, "V1", ["B1"])
    service = client.app.state.service

    results: list[Exception | dict] = []
    barrier = threading.Barrier(2)

    def worker(batch_no):
        barrier.wait()
        try:
            results.append(
                service.revise_verification("V1", 1, [batch_no])
            )
        except APIError as exc:  # 预期败者拿到修订冲突
            results.append(exc)

    t1 = threading.Thread(target=worker, args=("B1",))
    t2 = threading.Thread(target=worker, args=("B2",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409
    assert failures[0].code == "VERIFICATION_REVISION_CONFLICT"
    assert successes[0]["revision"] == 2

    body = doc(client, "V1")
    assert body["revision"] == 2
    winner = batch_nos_of(successes[0])[0]
    assert batch_nos_of(body) == [winner]
    # 第三次基于胜者修订号成功（冲突后客户端重新查询即可继续）
    assert revise(client, "V1", 2, ["B1", "B2"]).status_code == 200
    assert doc(client, "V1")["revision"] == 3


def test_concurrent_batch_correction_never_mixes_snapshot_times(client):
    """修订与批次更正并发：提交后逐批快照版本必须全部等于当前版本。

    写事务（BEGIN IMMEDIATE + 进程内串行）保证两者不交错落库：
    要么修订先于更正（快照为旧版本，更正后单据快照落后但内容自洽），
    要么更正先于修订（修订在同事务内读到新版本）。任一情况下，
    修订返回负载内同一批的裁决/资格快照都取自同一时间点。
    """
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])
    service = client.app.state.service

    barrier = threading.Barrier(2)

    def revise_worker():
        barrier.wait()
        service.revise_verification("V1", 1, ["B1"])

    def correct_worker():
        barrier.wait()
        service.correct_batch("B1", None, ["peanut", "sesame"], None)

    t1 = threading.Thread(target=revise_worker)
    t2 = threading.Thread(target=correct_worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    body = doc(client, "V1")
    entry = body["batches"][0]
    current = client.get("/api/batches/B1").json()
    current_adj = current["adjudication"]["version"]
    current_ship = current["shipment_eligibility"]["version"]
    # 无论两者谁先提交，修订只可能整体取更正前或更正后的同一时间点数据：
    # 裁决快照版本 ∈ {旧, 新}，绝不出现“裁决取自更正后、资格取自更正前”的混搭
    assert entry["snapshot"]["version"] in (1, current_adj)
    if entry["snapshot"]["version"] == current_adj:
        # 修订晚于更正：两套快照都必须已是更正后的当前版本
        assert entry["shipment_snapshot"]["version"] == current_ship
        assert entry["changed"] is False
    else:
        # 修订早于更正：裁决快照停在 v1（单据后续也未被再次修订）
        assert body["revision"] == 2
        assert entry["changed"] is True


def test_concurrent_lot_lock_never_mixes_snapshot_times(client):
    """修订与批号锁定并发：快照的裁决/资格版本必须取自同一时间点。"""
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    create_verification(client, "V1", ["B1"])
    service = client.app.state.service

    barrier = threading.Barrier(2)

    def revise_worker():
        barrier.wait()
        service.revise_verification("V1", 1, ["B1"])

    def lock_worker():
        barrier.wait()
        service.set_material_lot_lock("RM-P", True, "并发待检锁定")

    t1 = threading.Thread(target=revise_worker)
    t2 = threading.Thread(target=lock_worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    entry = doc(client, "V1")["batches"][0]
    # 批号锁定不改裁决版本（恒 v1）；资格版本：锁定先 -> 修订取 v2 BLOCKED，
    # 修订先 -> 快照为 v1 SHIPPABLE。两种状态只能成对出现，不可能混搭。
    if entry["shipment_snapshot"]["version"] == 2:
        assert entry["shipment_snapshot"]["status"] == "BLOCKED"
        assert entry["shipment_snapshot"]["locked_lots"] == ["RM-P"]
    else:
        assert entry["shipment_snapshot"]["version"] == 1
        assert entry["shipment_snapshot"]["status"] == "SHIPPABLE"
    assert entry["snapshot"]["version"] == 1


# ---------------------------------------------------------------- 旧库升级


def test_legacy_database_upgrades_with_revision_one(tmp_path):
    """旧版本库（verification 无 revision 列、确认无 revision 列）启动后：
    既有单据修订号为 1，修订功能可用，旧确认的 revision 为 null。"""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE allergen (code TEXT PRIMARY KEY, name_zh TEXT NOT NULL);
        CREATE TABLE material_lot (lot_no TEXT PRIMARY KEY, material TEXT NOT NULL,
            allergens TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE line (line_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        CREATE TABLE batch (batch_no TEXT PRIMARY KEY, line_id TEXT NOT NULL REFERENCES line(line_id),
            seq INTEGER NOT NULL, material_lots TEXT NOT NULL DEFAULT '[]',
            declared TEXT NOT NULL DEFAULT '[]', cleaned TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'ACTIVE', voided_reason TEXT, voided_at TEXT,
            created_at TEXT NOT NULL, UNIQUE(line_id, seq));
        CREATE TABLE batch_material (batch_no TEXT NOT NULL, lot_no TEXT NOT NULL,
            PRIMARY KEY (batch_no, lot_no));
        CREATE TABLE adjudication (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 0,
            actual TEXT NOT NULL DEFAULT '[]', undeclared TEXT NOT NULL DEFAULT '[]',
            disposition TEXT NOT NULL, reasons TEXT NOT NULL DEFAULT '[]',
            sources TEXT NOT NULL DEFAULT '{}', decided_at TEXT NOT NULL);
        CREATE TABLE adjudication_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, line_id TEXT NOT NULL, seq INTEGER NOT NULL,
            version INTEGER NOT NULL, actual TEXT NOT NULL DEFAULT '[]',
            undeclared TEXT NOT NULL DEFAULT '[]', disposition TEXT NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '{}',
            trigger TEXT NOT NULL, decided_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock (lot_no TEXT PRIMARY KEY, locked INTEGER NOT NULL,
            reason TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE material_lot_lock_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_no TEXT NOT NULL, version INTEGER NOT NULL, locked INTEGER NOT NULL,
            action TEXT NOT NULL, reason TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility (batch_no TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL, locked_lots TEXT NOT NULL DEFAULT '[]',
            reasons TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
        CREATE TABLE shipment_eligibility_version (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_no TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
            locked_lots TEXT NOT NULL DEFAULT '[]', reasons TEXT NOT NULL DEFAULT '[]',
            trigger TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE verification (verification_no TEXT PRIMARY KEY, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, confirmed_at TEXT);
        CREATE TABLE verification_batch (verification_no TEXT NOT NULL, batch_no TEXT NOT NULL,
            position INTEGER NOT NULL, snapshot_version INTEGER NOT NULL,
            snapshot_disposition TEXT NOT NULL, snapshot_actual TEXT NOT NULL DEFAULT '[]',
            snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER, snapshot_ship_status TEXT,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (verification_no, batch_no));
        CREATE TABLE verification_confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
            verification_no TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE verification_confirmation_batch (confirmation_id INTEGER NOT NULL,
            verification_no TEXT NOT NULL, position INTEGER NOT NULL, batch_no TEXT NOT NULL,
            snapshot_version INTEGER NOT NULL, snapshot_disposition TEXT NOT NULL,
            snapshot_actual TEXT NOT NULL DEFAULT '[]', snapshot_undeclared TEXT NOT NULL DEFAULT '[]',
            snapshot_reasons TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_version INTEGER, snapshot_ship_status TEXT,
            snapshot_locked_lots TEXT NOT NULL DEFAULT '[]',
            snapshot_ship_reasons TEXT NOT NULL DEFAULT '[]');
        INSERT INTO allergen(code, name_zh) VALUES ('milk', '乳');
        INSERT INTO material_lot VALUES ('RM-M', '奶粉', '["milk"]', 't');
        INSERT INTO line VALUES ('L1', 't');
        INSERT INTO batch VALUES ('B1', 'L1', 1, '["RM-M"]', '["milk"]', '[]',
            'ACTIVE', NULL, NULL, 't');
        INSERT INTO batch_material VALUES ('B1', 'RM-M');
        INSERT INTO adjudication VALUES ('B1', 1, '["milk"]', '[]', 'RELEASE', '[]', '{}', 't');
        INSERT INTO shipment_eligibility VALUES ('B1', 1, 'SHIPPABLE', '[]', '[]', 't');
        INSERT INTO shipment_eligibility_version VALUES (1, 'B1', 1, 'SHIPPABLE', '[]', '[]', 'UPGRADE_INIT', 't');
        INSERT INTO verification VALUES ('V1', 'DRAFT', 't', 't', NULL);
        INSERT INTO verification_batch VALUES ('V1', 'B1', 1, 1, 'RELEASE', '["milk"]', '[]', '[]',
            1, 'SHIPPABLE', '[]', '[]');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(db_path=str(db_path), seed_demo=False)
    with TestClient(app) as c:
        body = c.get("/api/verifications/V1").json()
        assert body["revision"] == 1
        # 凭修订号 1 修订：成功并推进到 2
        resp = c.put(
            "/api/verifications/V1/batches",
            json={"expected_revision": 1, "batch_nos": ["B1"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revision"] == 2
        assert resp.json()["status"] == "DRAFT"
        assert resp.json()["batches"][0]["changed"] is False
        # 旧库中没有确认记录；补做一次确认，确认历史 revision 记录为当前修订号
        assert c.post("/api/verifications/V1/confirm").status_code == 200
        confirmed = c.get("/api/verifications/V1").json()
        assert confirmed["confirmations"][0]["revision"] == 2

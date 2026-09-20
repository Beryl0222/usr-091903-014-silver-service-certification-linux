"""证据平台的端到端契约测试。

通过真实 HTTP 接口验证关键认证规则：同意前置、代表性覆盖、
证据防篡改、版本复用/复测、严重事件暂停、整改复验、申诉回避与三类视图。
"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from service import Handler

S1 = {
    "age_band": "low_60_69",
    "health_status": "healthy",
    "living_status": "with_family",
    "assistance": "none",
}
S2 = {
    "age_band": "high_80_plus",
    "health_status": "chronic",
    "living_status": "alone",
    "assistance": "partial",
}
S3 = {
    "age_band": "mid_70_79",
    "health_status": "mobility_limited",
    "living_status": "in_facility",
    "assistance": "full",
}

CERTIFIER_A = ("cert-a", "certifier")
CERTIFIER_B = ("cert-b", "certifier")
SUPPLIER = ("supplier-1", "supplier")


def passing_evidence(level_id, participant_id, version=None, capability="独立完成旅居报到"):
    payload = {
        "level_id": level_id,
        "participant_id": participant_id,
        "key_tasks": [{"name": capability, "capability": capability, "completed": True}],
        "accessibility_barriers": [],
        "staff_responses": [{"request": "呼叫工作人员", "response_seconds": 60}],
        "price": {"disclosed_upfront": True, "detail": "全程费用已在签约前书面告知"},
        "dropout": {"dropped_out": False},
    }
    if version is not None:
        payload["version"] = version
    return payload


class ApiClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def request(self, method, path, payload=None, identity=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if identity:
            headers["X-User-Id"] = identity[0]
            headers["X-User-Role"] = identity[1]
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            body = json.load(error)
            error.close()
            return error.code, body

    def get(self, path, identity=None):
        return self.request("GET", path, None, identity)

    def post(self, path, payload, identity=None):
        return self.request("POST", path, payload, identity)


class CertificationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.server.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        service.STORE.reset()

    # -- 构造辅助 -----------------------------------------------------------

    def make_plan(self, strata, min_participants=1):
        status, body = self.api.post(
            "/api/plans",
            {
                "name": "湖畔旅居体验",
                "levels": [
                    {
                        "id": "L1",
                        "name": "标准旅居",
                        "claimed_strata": strata,
                        "min_participants": min_participants,
                    }
                ],
            },
            SUPPLIER,
        )
        self.assertEqual(status, 200, body)
        return body["id"]

    def enroll(self, plan_id, stratum, identity=None):
        status, participant = self.api.post(
            "/api/participants", {"stratum": stratum}, identity or CERTIFIER_A
        )
        self.assertEqual(status, 200, participant)
        pid = participant["id"]
        status, consent = self.api.post(
            f"/api/participants/{pid}/consent", {"plan_id": plan_id}, (pid, "participant")
        )
        self.assertEqual(status, 200, consent)
        return pid, consent["id"]

    def add_passing_evidence(self, plan_id, pid, level_id="L1", version=None):
        status, body = self.api.post(
            f"/api/plans/{plan_id}/evidence",
            passing_evidence(level_id, pid, version),
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, body)
        return body

    def certify(self, plan_id, level_id="L1", identity=CERTIFIER_A, version=None):
        payload = {"level_id": level_id, "reviewer_ids": [identity[0]]}
        if version is not None:
            payload["version"] = version
        return self.api.post(f"/api/plans/{plan_id}/decisions", payload, identity)

    # -- 测试用例 -----------------------------------------------------------

    def test_health_remains_unchanged(self):
        status, body = self.api.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "silver-service-certification")

    def test_evidence_requires_consent_and_stratum_in_scope(self):
        plan_id = self.make_plan([S2])
        # 未登记/未同意的参与者不能产生证据。
        status, body = self.api.post(
            f"/api/plans/{plan_id}/evidence",
            passing_evidence("L1", "u999"),
            CERTIFIER_A,
        )
        self.assertEqual(status, 404)

        # 分层不在等级声明范围内同样不能计入。
        pid, _ = self.enroll(plan_id, S1)
        status, body = self.api.post(
            f"/api/plans/{plan_id}/evidence",
            passing_evidence("L1", pid),
            CERTIFIER_A,
        )
        self.assertEqual(status, 400)
        self.assertIn("声明范围", body["error"])

    def test_only_low_age_healthy_evidence_cannot_certify_other_strata(self):
        plan_id = self.make_plan([S1, S2, S3])
        pid, _ = self.enroll(plan_id, S1)
        self.add_passing_evidence(plan_id, pid)

        status, decision = self.certify(plan_id)
        self.assertEqual(status, 200)
        self.assertEqual(decision["outcome"], "denied")
        # 低龄健康者的良好体验不能外推到独居、行动受限等分层。
        joined = "；".join(decision["reasons"])
        self.assertIn("独居", joined)
        self.assertIn("行动受限", joined)

        status, consumer = self.api.get(
            f"/api/plans/{plan_id}/consumer", CERTIFIER_A
        )
        self.assertEqual(consumer["levels"][0]["status"], "uncertified")

    def test_full_representative_coverage_certifies_and_consumer_sees_scope(self):
        plan_id = self.make_plan([S1, S2, S3], min_participants=2)
        for stratum in (S1, S2, S3):
            for _ in range(2):
                pid, _ = self.enroll(plan_id, stratum)
                self.add_passing_evidence(plan_id, pid)

        status, decision = self.certify(plan_id)
        self.assertEqual(status, 200, decision)
        self.assertEqual(decision["outcome"], "certified")
        cert = decision["certificate"]
        self.assertEqual(cert["version"], 1)
        self.assertIn("独立完成旅居报到", cert["verified_capabilities"])
        self.assertEqual(len(cert["applicable_strata"]), 3)

        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(status, 200)
        level = consumer["levels"][0]
        self.assertEqual(level["status"], "certified")
        self.assertTrue(level["valid_until"] > level.get("valid_from", ""))
        self.assertEqual(level["verified_capabilities"], ["独立完成旅居报到"])

    def test_raw_feedback_cannot_be_rewritten_and_tampering_breaks_ledger(self):
        plan_id = self.make_plan([S1])
        pid, _ = self.enroll(plan_id, S1)
        session = self.add_passing_evidence(plan_id, pid)

        # 供应商没有提交/改写反馈的权限。
        status, body = self.api.post(
            f"/api/evidence/{session['id']}/feedback",
            {"participant_id": pid, "text": "供应商代笔的好评"},
            SUPPLIER,
        )
        self.assertEqual(status, 403)

        # 其他参与者也不能代为提交。
        other, _ = self.enroll(plan_id, S1)
        status, body = self.api.post(
            f"/api/evidence/{session['id']}/feedback",
            {"participant_id": other, "text": "代提交"},
            (other, "participant"),
        )
        self.assertEqual(status, 403)

        # 本人原始反馈只允许追加，更正以新记录串联，不覆盖原文。
        status, feedback = self.api.post(
            f"/api/evidence/{session['id']}/feedback",
            {"text": "浴室扶手松动，我当晚不敢洗澡"},
            (pid, "participant"),
        )
        self.assertEqual(status, 200, feedback)
        status, correction = self.api.post(
            f"/api/evidence/{session['id']}/feedback",
            {"text": "补充：第二天已修好", "correction_ref": feedback["id"]},
            (pid, "participant"),
        )
        self.assertEqual(status, 200, correction)
        self.assertEqual(
            service.STORE.feedback[feedback["id"]]["text"], "浴室扶手松动，我当晚不敢洗澡"
        )

        # 存储中任何原始对象被改写，哈希链立即校验失败。
        service.STORE.feedback[feedback["id"]]["text"] = "一切满意"
        status, certifier = self.api.get(
            f"/api/plans/{plan_id}/certifier", CERTIFIER_A
        )
        self.assertEqual(status, 200)
        verification = certifier["ledger_verification"]
        self.assertFalse(verification["ok"])
        self.assertGreaterEqual(verification["broken_at"], 1)
        self.assertIn("哈希", verification["reason"])

    def test_version_change_reuses_safe_evidence_and_requires_targeted_retest(self):
        plan_id = self.make_plan([S1, S2])
        p1, _ = self.enroll(plan_id, S1)
        p2, _ = self.enroll(plan_id, S2)
        self.add_passing_evidence(plan_id, p1)
        self.add_passing_evidence(plan_id, p2)
        status, decision = self.certify(plan_id)
        self.assertEqual(decision["outcome"], "certified", decision)

        # 场地变更：可达性之外的证据可复用，可达性必须复测；旧证书不覆盖新版本。
        status, version = self.api.post(
            f"/api/plans/{plan_id}/versions",
            {"change_summary": "更换为山坡驻地", "change_types": ["venue"]},
            SUPPLIER,
        )
        self.assertEqual(status, 200, version)
        self.assertEqual(version["version"], 2)
        self.assertEqual(version["affected_dimensions"], ["accessibility"])

        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(consumer["levels"][0]["status"], "uncertified")

        status, reuse = self.api.get(
            f"/api/plans/{plan_id}/levels/L1/reuse", CERTIFIER_A
        )
        self.assertEqual(status, 200, reuse)
        for stratum in reuse["strata"]:
            self.assertIn("可达性", stratum["needs_retest_dimensions"])
            self.assertTrue(stratum["dimensions"]["task"]["reused"])
            self.assertTrue(stratum["dimensions"]["staff_response"]["reused"])
            self.assertTrue(stratum["dimensions"]["price"]["reused"])
            self.assertTrue(stratum["dimensions"]["accessibility"]["retest_required"])

        # 新版本仅补测可达性；关键任务等沿用 v1 证据后可再次认证，能力仍可验证。
        for pid in (p1, p2):
            status, body = self.api.post(
                f"/api/plans/{plan_id}/evidence",
                {
                    "level_id": "L1",
                    "participant_id": pid,
                    "version": 2,
                    "dimensions": ["accessibility"],
                    "accessibility_barriers": [],
                    "dropout": {"dropped_out": False},
                },
                CERTIFIER_A,
            )
            self.assertEqual(status, 200, body)

        status, decision = self.certify(plan_id, version=2)
        self.assertEqual(decision["outcome"], "certified", decision)
        self.assertIn("独立完成旅居报到", decision["certificate"]["verified_capabilities"])
        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(consumer["levels"][0]["status"], "certified")

    def test_severe_incident_suspends_level_and_only_passed_reverification_lifts(self):
        plan_id = self.make_plan([S1])
        pid, _ = self.enroll(plan_id, S1)
        self.add_passing_evidence(plan_id, pid)
        self.assertEqual(self.certify(plan_id)[1]["outcome"], "certified")

        status, incident = self.api.post(
            f"/api/plans/{plan_id}/incidents",
            {"level_id": "L1", "description": "独居参与者夜间呼叫20分钟无人响应后跌倒"},
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, incident)

        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(consumer["levels"][0]["status"], "suspended")
        self.assertEqual(self.certify(plan_id)[1]["outcome"], "denied")

        # 原评审人 A 必须回避，由未参与原评审的 B 处理申诉。
        status, appeal = self.api.post(
            "/api/appeals",
            {
                "subject_type": "incident",
                "subject_id": incident["id"],
                "reason": "认为事件定级过重",
            },
            SUPPLIER,
        )
        self.assertEqual(status, 200, appeal)
        status, body = self.api.post(
            f"/api/appeals/{appeal['id']}/rule",
            {"upheld": True, "notes": "记录流程确有瑕疵"},
            CERTIFIER_A,
        )
        self.assertEqual(status, 409)
        self.assertIn("回避", body["error"])
        status, ruling = self.api.post(
            f"/api/appeals/{appeal['id']}/rule",
            {"upheld": True, "notes": "记录流程确有瑕疵，但安全结论待复验"},
            CERTIFIER_B,
        )
        self.assertEqual(status, 200, ruling)

        # 申诉成立不能替代整改复验：等级仍处暂停。
        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(consumer["levels"][0]["status"], "suspended")

        # 复验未通过，暂停继续。
        status, rectification = self.api.post(
            f"/api/plans/{plan_id}/rectifications",
            {
                "level_id": "L1",
                "summary": "加装夜间双岗巡查",
                "incident_id": incident["id"],
            },
            SUPPLIER,
        )
        self.assertEqual(status, 200, rectification)
        status, reverification = self.api.post(
            "/api/reverifications",
            {"rectification_id": rectification["id"], "passed": False, "notes": "演练仍超时"},
            CERTIFIER_B,
        )
        self.assertEqual(status, 200, reverification)
        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(consumer["levels"][0]["status"], "suspended")

        # 整改复验通过后暂停方可解除，并可重新认证。
        status, rectification2 = self.api.post(
            f"/api/plans/{plan_id}/rectifications",
            {
                "level_id": "L1",
                "summary": "完成夜间响应演练并达标",
                "incident_id": incident["id"],
            },
            SUPPLIER,
        )
        self.assertEqual(status, 200, rectification2)
        status, reverification2 = self.api.post(
            "/api/reverifications",
            {"rectification_id": rectification2["id"], "passed": True, "notes": "复测达标"},
            CERTIFIER_B,
        )
        self.assertEqual(status, 200, reverification2)
        status, consumer = self.api.get(f"/api/plans/{plan_id}/consumer", SUPPLIER)
        self.assertEqual(consumer["levels"][0]["status"], "uncertified")
        self.assertEqual(self.certify(plan_id)[1]["outcome"], "certified")

    def test_major_defect_needs_exception_but_blocking_defect_never_passes(self):
        plan_id = self.make_plan([S1])
        pid, _ = self.enroll(plan_id, S1)
        self.add_passing_evidence(plan_id, pid)

        status, major = self.api.post(
            f"/api/plans/{plan_id}/defects",
            {
                "level_id": "L1",
                "dimension": "staff_response",
                "description": "高峰时段响应接近时限上限",
                "severity": "major",
                "stratum": S1,
            },
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, major)
        self.assertEqual(self.certify(plan_id)[1]["outcome"], "denied")

        status, exception = self.api.post(
            "/api/exceptions",
            {
                "defect_id": major["id"],
                "justification": "已排错峰排班，30天内整改",
                "compensating_measure": "高峰增派一名值守",
            },
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, exception)
        decision = self.certify(plan_id)[1]
        self.assertEqual(decision["outcome"], "certified", decision)
        self.assertEqual(decision["accepted_exceptions"][0]["defect_id"], major["id"])

        status, blocking = self.api.post(
            f"/api/plans/{plan_id}/defects",
            {
                "level_id": "L1",
                "dimension": "dropout",
                "description": "坡道缺失导致轮椅参与者无法进入",
                "severity": "blocking",
                "stratum": S1,
            },
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, blocking)
        status, body = self.api.post(
            "/api/exceptions",
            {
                "defect_id": blocking["id"],
                "justification": "想先放行",
                "compensating_measure": "人工搀扶",
            },
            CERTIFIER_A,
        )
        self.assertEqual(status, 400)
        self.assertIn("阻断性", body["error"])
        decision = self.certify(plan_id)[1]
        self.assertEqual(decision["outcome"], "denied")
        self.assertEqual(decision["open_blocking_defect_ids"], [blocking["id"]])

    def test_safety_dropout_and_hidden_price_block_certification(self):
        plan_id = self.make_plan([S2])
        pid, _ = self.enroll(plan_id, S2)
        status, session = self.api.post(
            f"/api/plans/{plan_id}/evidence",
            {
                "level_id": "L1",
                "participant_id": pid,
                "key_tasks": [{"name": "报到", "completed": True}],
                "accessibility_barriers": [],
                "staff_responses": [{"request": "呼叫", "response_seconds": 60}],
                "price": {"disclosed_upfront": False, "detail": "到场后才告知附加费"},
                "dropout": {
                    "dropped_out": True,
                    "reason": "safety",
                    "stage": "浴室",
                    "note": "地滑无人处理",
                },
            },
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, session)
        decision = self.certify(plan_id)[1]
        self.assertEqual(decision["outcome"], "denied")
        joined = "；".join(decision["reasons"])
        self.assertIn("安全原因中途退出", joined)
        self.assertIn("价格披露", joined)

    def test_withdrawn_consent_removes_evidence_from_coverage(self):
        plan_id = self.make_plan([S1])
        pid, consent_id = self.enroll(plan_id, S1)
        self.add_passing_evidence(plan_id, pid)

        status, _ = self.api.post(
            f"/api/consents/{consent_id}/withdraw", {}, (pid, "participant")
        )
        self.assertEqual(status, 200)
        decision = self.certify(plan_id)[1]
        self.assertEqual(decision["outcome"], "denied")
        # 原始证据仍可供认证人员追溯，但不计入覆盖。
        status, certifier = self.api.get(f"/api/plans/{plan_id}/certifier", CERTIFIER_A)
        self.assertEqual(status, 200)
        self.assertEqual(len(certifier["evidence"]), 1)
        self.assertEqual(certifier["consents"][0]["withdrawn"], True)

    def test_supplier_view_is_desensitized_defect_list(self):
        plan_id = self.make_plan([S2])
        pid, _ = self.enroll(plan_id, S2)
        session = self.add_passing_evidence(plan_id, pid)
        status, defect = self.api.post(
            f"/api/plans/{plan_id}/defects",
            {
                "level_id": "L1",
                "dimension": "accessibility",
                "description": "门槛过高，助行器通过困难",
                "severity": "major",
                "stratum": S2,
                "session_id": session["id"],
            },
            CERTIFIER_A,
        )
        self.assertEqual(status, 200, defect)

        status, supplier = self.api.get(f"/api/plans/{plan_id}/supplier", SUPPLIER)
        self.assertEqual(status, 200)
        view_defect = supplier["defects"][0]
        # 脱敏清单不暴露参与者、证据记录或报告人。
        self.assertNotIn("participant_id", view_defect)
        self.assertNotIn("session_id", view_defect)
        self.assertNotIn("reported_by", view_defect)
        self.assertEqual(view_defect["severity"], "major")
        self.assertIn("独居", view_defect["stratum"])

        # 其他供应商无权查看该方案。
        status, body = self.api.get(
            f"/api/plans/{plan_id}/supplier", ("supplier-2", "supplier")
        )
        self.assertEqual(status, 403)

    def test_certifier_view_traces_level_to_version_and_exception(self):
        plan_id = self.make_plan([S1])
        pid, _ = self.enroll(plan_id, S1)
        self.add_passing_evidence(plan_id, pid)
        self.certify(plan_id)

        status, certifier = self.api.get(f"/api/plans/{plan_id}/certifier", CERTIFIER_A)
        self.assertEqual(status, 200)
        level_view = certifier["levels"][0]
        self.assertEqual(level_view["plan_version"], 1)
        self.assertIn(
            "低龄老人",
            level_view["representative_coverage"]["strata"][0]["stratum_label"],
        )
        self.assertTrue(certifier["ledger_verification"]["ok"])
        self.assertEqual(certifier["decisions"][0]["reviewer_ids"], ["cert-a"])


if __name__ == "__main__":
    unittest.main()

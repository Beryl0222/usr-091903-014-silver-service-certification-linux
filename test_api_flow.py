"""通过 HTTP 接口走通完整治理闭环，验证路由、角色与错误码。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import service
from evidence_platform import EvidencePlatform, stratum_code

TESTER = {"id": "t1", "role": "tester"}
CERT = {"id": "c1", "role": "certifier"}
CERT2 = {"id": "c2", "role": "certifier"}
PROVIDER = {"id": "pv1", "role": "provider"}

SOLO = stratum_code("senior_80_plus", "mobility_limited", "solo", "device")
CAP = "独立完成入住与康养流程"


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        service.PLATFORM = EvidencePlatform()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(f"{self.base}{path}", data=data, headers=headers,
                      method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def test_full_flow_over_http(self):
        # 1. 建方案
        status, body = self.call("POST", "/plans", {
            "actor": PROVIDER, "provider_name": "南山康养",
            "service_name": "湖畔旅居", "version": "v1", "tier": "三级",
            "facets": ["venue", "care_staff", "pricing"]})
        self.assertEqual(status, 200, body)
        plan_id = body["plan_id"]

        # 2. 分层同意 + 证据（3 名同组老人）
        participant_ids = []
        for i in range(3):
            _, pb = self.call("POST", "/participants", {
                "actor": TESTER, "age_band": "senior_80_plus",
                "health_status": "mobility_limited", "living": "solo",
                "assistance": "device", "consent_version": "consent-2026-1"})
            participant_ids.append(pb["participant_id"])
            _, eb = self.call("POST", "/evidence", {
                "actor": TESTER, "plan_id": plan_id,
                "participant_id": pb["participant_id"], "capability": CAP,
                "task": "入住动线-康养评估", "barriers": [],
                "staff_response": {"response_minutes": 4, "rating": 5},
                "price_disclosure": "full", "outcome": "completed",
                "raw_feedback": f"逐字反馈{i}:坡道可用，价格提前说明",
                "facet_dependencies": ["venue", "care_staff"]})
            self.assertIn("hash", eb)

        # 3. 角色错误被拒
        status, body = self.call("POST", "/evidence", {
            "actor": PROVIDER, "plan_id": plan_id,
            "participant_id": participant_ids[0], "capability": CAP,
            "task": "t", "price_disclosure": "full", "outcome": "completed",
            "raw_feedback": "供应商不能自己写证据",
            "facet_dependencies": ["venue"]})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "forbidden")

        # 4. 认证通过
        _, db = self.call("POST", "/certifications/decide", {
            "actor": CERT, "plan_id": plan_id,
            "claimed_strata": [SOLO], "claimed_capabilities": [CAP]})
        self.assertEqual(db["result"], "approved", db)
        cert_id = db["certification_id"]

        # 5. 消费者视图
        _, cv = self.call("GET", "/views/consumer")
        self.assertEqual(cv["certifications"][0]["status"], "active")

        # 6. 企业脱敏视图
        _, ev = self.call("POST", "/views/enterprise", {"actor": PROVIDER})
        self.assertEqual(ev["services"][0]["desensitized_defects"][SOLO]["tasks"], 3)
        self.assertNotIn("逐字反馈", json.dumps(ev, ensure_ascii=False))

        # 7. 调价版本：证据全部可复用，直接通过
        _, nv = self.call("POST", f"/plans/{plan_id}/versions", {
            "actor": PROVIDER, "version": "v1.1",
            "changed_facets": ["pricing"]})
        _, rr = self.call("GET", f"/plans/{nv['plan_id']}/reuse")
        self.assertEqual(len(rr["reusable_evidence"]), 3)
        self.assertEqual(rr["retest_cells"], {})

        # 8. 换场地版本：必须复测，评审驳回
        _, v2 = self.call("POST", f"/plans/{plan_id}/versions", {
            "actor": PROVIDER, "version": "v2", "changed_facets": ["venue"]})
        _, rr2 = self.call("GET", f"/plans/{v2['plan_id']}/reuse")
        self.assertEqual(len(rr2["reusable_evidence"]), 0)

        # 9. 严重事件 → 立即暂停 → 整改 → 复验恢复
        _, inc = self.call("POST", "/incidents", {
            "actor": CERT, "cert_id": cert_id, "description": "浴室滑倒"})
        self.assertTrue(inc["suspended_certifications"])
        _, cv2 = self.call("GET", "/views/consumer")
        self.assertEqual(cv2["certifications"][0]["status"], "suspended")

        status, _ = self.call("POST", "/reverifications", {
            "actor": CERT, "cert_id": cert_id, "passed": True})
        self.assertEqual(status, 422)  # 未整改不得复验
        self.call("POST", "/rectifications", {
            "actor": PROVIDER, "cert_id": cert_id,
            "action": "浴室加装扶手和紧急呼叫"})
        _, rv = self.call("POST", "/reverifications", {
            "actor": CERT, "cert_id": cert_id, "passed": True,
            "notes": "复测通过"})
        self.assertTrue(rv["resumed"])

        # 10. 申诉回避：原认证人员处理被拒，独立人员可处理
        self.call("POST", "/incidents", {
            "actor": CERT, "cert_id": cert_id, "description": "再次事件"})
        _, ap = self.call("POST", "/appeals", {
            "actor": PROVIDER, "cert_id": cert_id, "reason": "与服务无关"})
        status, body = self.call("POST", f"/appeals/{ap['appeal_id']}/decide", {
            "actor": CERT, "outcome": "uphold", "reason": "维持"})
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "recusal_required")
        _, ad = self.call("POST", f"/appeals/{ap['appeal_id']}/decide", {
            "actor": CERT2, "outcome": "quash", "reason": "独立复核撤销"})
        self.assertEqual(ad["status"], "quashed")

        # 11. 认证追溯视图：版本链、覆盖、例外、整改、申诉、完整性
        _, trace = self.call("GET", f"/certifications/{cert_id}/trace")
        self.assertTrue(trace["log_integrity"]["ok"])
        self.assertTrue(trace["evidence_integrity"]["ok"])
        self.assertEqual(len(trace["plan_version_chain"]), 1)
        self.assertEqual(len(trace["rectifications"]), 1)
        self.assertEqual(trace["appeals"][0]["status"], "quashed")

    def test_bad_json_missing_field_and_unknown_route(self):
        req = Request(f"{self.base}/plans", data=b"{not json",
                      headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        status, body = self.call("POST", "/plans",
                                 {"actor": {"id": "x", "role": "tester"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "missing_field")

        with self.assertRaises(HTTPError) as ctx:
            urlopen(f"{self.base}/nope", timeout=2)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()


if __name__ == "__main__":
    unittest.main()

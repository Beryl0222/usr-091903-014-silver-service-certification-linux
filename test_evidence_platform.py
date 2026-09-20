"""服务体验证据平台治理闭环的领域测试。

用例按真实业务时间线组织：
分层同意 → 采集 → 代表性评审 → 版本变更复用/复测 → 发证 →
严重事件暂停 → 整改复验 → 申诉回避 → 三视图与防篡改。
"""

import json
import unittest

from evidence_platform import (
    AGE_BANDS, EvidencePlatform, PlatformError, stratum_code,
)

TESTER = {"id": "u_tester", "role": "tester"}
TESTER2 = {"id": "u_tester2", "role": "tester"}
CERTIFIER = {"id": "u_cert", "role": "certifier"}
CERTIFIER2 = {"id": "u_cert2", "role": "certifier"}
PROVIDER = {"id": "u_provider", "role": "provider"}
OTHER_PROVIDER = {"id": "u_provider_other", "role": "provider"}

S_SOLO_MOBILITY = stratum_code("senior_80_plus", "mobility_limited", "solo", "device")
S_HEALTHY = stratum_code("low_60_69", "healthy", "with_family", "none")
S_CHRONIC_SOLO = stratum_code("mid_70_79", "chronic_stable", "solo", "companion")

CAP = "独立完成入住与康养流程"
CAP2 = "自主参与团体课程"


def make_clock(start=1_800_000_000.0):
    t = {"v": start}

    def clock():
        return t["v"]

    def advance(days):
        t["v"] += days * 86400

    clock.advance = advance
    return clock


class PlatformTest(unittest.TestCase):
    def setUp(self):
        self.clock = make_clock()
        self.p = EvidencePlatform(clock=self.clock)
        plan = self.p.create_plan(
            PROVIDER, provider_name="南山康养", service_name="湖畔旅居康养",
            version="v1.0", tier="三级",
            facets=["venue", "curriculum", "care_staff", "pricing", "transport"])
        self.plan_id = plan["plan_id"]

    # -- 工具：为一个分层造 N 名已同意参与者并采集合格证据 ------------------

    def _enroll(self, stratum_parts, n=3, barriers=None, outcome="completed",
                price="full", exit_reason=None, feedback_prefix="原始反馈"):
        ids = []
        for i in range(n):
            r = self.p.register_participant(
                TESTER, consent_version="consent-2026-1", **stratum_parts)
            pid = r["participant_id"]
            self.p.record_evidence(
                TESTER, plan_id=self.plan_id, participant_id=pid,
                capability=CAP, task="报到入住-房间动线-康养评估",
                barriers=barriers or [],
                staff_response={"response_minutes": 3 + i, "rating": 5},
                price_disclosure=price, outcome=outcome,
                raw_feedback=f"{feedback_prefix}{i}:全程有人陪同，价格事先说清",
                facet_dependencies=["venue", "care_staff"],
                exit_reason=exit_reason)
            ids.append(pid)
        return ids

    SOLO = dict(age_band="senior_80_plus", health_status="mobility_limited",
                living="solo", assistance="device")
    HEALTHY_PARTS = dict(age_band="low_60_69", health_status="healthy",
                         living="with_family", assistance="none")

    # ---- 分层与同意 ------------------------------------------------------

    def test_stratum_requires_consent(self):
        with self.assertRaises(PlatformError) as ctx:
            self.p.register_participant(
                TESTER, age_band="low_60_69", health_status="healthy",
                living="solo", assistance="none", consent_version="",
                agreed=True)
        self.assertEqual(ctx.exception.code, "invalid")
        with self.assertRaises(PlatformError):
            self.p.register_participant(
                TESTER, age_band="low_60_69", health_status="healthy",
                living="solo", assistance="none",
                consent_version="consent-2026-1", agreed=False)

    def test_illegal_stratum_value_rejected(self):
        with self.assertRaises(PlatformError):
            self.p.register_participant(
                TESTER, age_band="age_unknown", health_status="healthy",
                living="solo", assistance="none",
                consent_version="consent-2026-1")

    def test_role_enforcement(self):
        with self.assertRaises(PlatformError) as ctx:
            self.p.register_participant(
                PROVIDER, **self.HEALTHY_PARTS, consent_version="c1")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(PlatformError):
            self.p.create_plan(
                TESTER, provider_name="x", service_name="y", version="1",
                tier="t", facets=["venue"])

    def test_withdrawn_consent_invalidates_evidence_without_deleting(self):
        ids = self._enroll(self.SOLO, n=1)
        self.p.withdraw_consent(TESTER, ids[0], reason="家属代撤回")
        # 证据仍在
        self.assertIn(ids[0], {e["participant_id"] for e in self.p.evidence.values()})
        with self.assertRaises(PlatformError) as ctx:
            self.p.record_evidence(
                TESTER, plan_id=self.plan_id, participant_id=ids[0],
                capability=CAP, task="t", barriers=[], staff_response={},
                price_disclosure="full", outcome="completed",
                raw_feedback="撤回后不得再采集", facet_dependencies=["venue"])
        self.assertEqual(ctx.exception.code, "consent_required")

    # ---- 证据质量规则 ----------------------------------------------------

    def test_abandonment_requires_reason(self):
        r = self.p.register_participant(TESTER, **self.SOLO,
                                        consent_version="c1")
        with self.assertRaises(PlatformError):
            self.p.record_evidence(
                TESTER, plan_id=self.plan_id,
                participant_id=r["participant_id"], capability=CAP,
                task="t", barriers=[], staff_response={},
                price_disclosure="full", outcome="abandoned",
                raw_feedback="走了", facet_dependencies=["venue"])

    def test_raw_feedback_required_and_immutable(self):
        r = self.p.register_participant(TESTER, **self.SOLO,
                                        consent_version="c1")
        with self.assertRaises(PlatformError):
            self.p.record_evidence(
                TESTER, plan_id=self.plan_id,
                participant_id=r["participant_id"], capability=CAP,
                task="t", barriers=[], staff_response={},
                price_disclosure="full", outcome="completed",
                raw_feedback="  ", facet_dependencies=["venue"])
        # 供应商无权采集也无权改写
        with self.assertRaises(PlatformError):
            self.p.amend_evidence(PROVIDER, "evi_nope", "改写")

    # ---- 代表性覆盖与认证 ------------------------------------------------

    def test_rejected_when_only_healthy_low_age_tested(self):
        """低龄健康者试用良好，不能证明独居行动受限人群安全。"""
        self._enroll(self.HEALTHY_PARTS, n=3)
        result = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_HEALTHY, S_SOLO_MOBILITY],
            claimed_capabilities=[CAP])
        self.assertEqual(result["result"], "rejected")
        gap_strata = {g["stratum"] for g in result["gaps"]
                      if g["type"] == "underrepresented_stratum"}
        self.assertIn(S_SOLO_MOBILITY, gap_strata)

    def test_minimum_participants_per_stratum(self):
        self._enroll(self.SOLO, n=2)  # 不足 3 人
        result = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP])
        self.assertEqual(result["result"], "rejected")

    def test_critical_barrier_blocks_and_cannot_be_exempted(self):
        self._enroll(self.SOLO, n=3,
                     barriers=[{"category": "无障碍通道", "severity": "critical"}])
        # 供应商申请"单元格级豁免"关键障碍：不允许，必须驳回
        result = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP],
            exceptions=[{"stratum": S_SOLO_MOBILITY, "capability": CAP,
                         "reason": "供应商承诺以后改造，申请豁免"}])
        self.assertEqual(result["result"], "rejected")
        self.assertTrue(any("关键" in r for r in result["reasons"]))

    def test_critical_barrier_avoided_by_excluding_whole_stratum(self):
        """整组排除有问题的分层是合法缩小范围：只批另一组无关键障碍的分层。"""
        self._enroll(self.SOLO, n=3,
                     barriers=[{"category": "无障碍通道", "severity": "critical"}])
        self._enroll(self.HEALTHY_PARTS, n=3)
        result = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY, S_HEALTHY],
            claimed_capabilities=[CAP],
            exceptions=[{"stratum": S_SOLO_MOBILITY,
                         "reason": "独居行动受限组存在关键障碍，本轮排除，整改后复测"}])
        self.assertEqual(result["result"], "approved")
        cert = self.p.certifications[result["certification_id"]]
        self.assertEqual(cert["applicable_strata"], [S_HEALTHY])

    def test_approval_then_consumer_view(self):
        self._enroll(self.SOLO, n=3)
        result = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP])
        self.assertEqual(result["result"], "approved")
        cert_id = result["certification_id"]
        view = self.p.consumer_view()
        item = next(c for c in view["certifications"] if c["tier"] == "三级")
        self.assertEqual(item["status"], "active")
        self.assertIn("高龄老人", item["applicable_population"][0])
        self.assertIn("独居", item["applicable_population"][0])
        self.assertEqual(item["verified_capabilities"][S_SOLO_MOBILITY], [CAP])
        # 消费者视图不泄露证据原文/参与者
        self.assertNotIn("raw_feedback", str(item))

    def test_exception_narrows_scope(self):
        self._enroll(self.SOLO, n=3)
        result = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY, S_CHRONIC_SOLO],
            claimed_capabilities=[CAP],
            exceptions=[{"stratum": S_CHRONIC_SOLO,
                         "reason": "本轮不覆盖需陪护人群，证书排除该组"}])
        self.assertEqual(result["result"], "approved")
        cert = self.p.certifications[result["certification_id"]]
        self.assertEqual(cert["applicable_strata"], [S_SOLO_MOBILITY])
        self.assertEqual(len(cert["exceptions"]), 1)

    # ---- 版本变更：复用与复测 --------------------------------------------

    def _approved_cert(self):
        self._enroll(self.SOLO, n=3)
        r = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP])
        self.assertEqual(r["result"], "approved")
        return r["certification_id"]

    def test_unrelated_change_reuses_evidence(self):
        cert_id = self._approved_cert()
        nv = self.p.new_version(
            PROVIDER, self.plan_id, version="v1.1",
            changed_facets=["pricing"], summary="仅调价")
        report = self.p.reuse_report(nv["plan_id"])
        self.assertEqual(len(report["retest_required"]), 0)
        self.assertEqual(len(report["reusable_evidence"]), 3)
        # 复用证据仍可支撑新版本评审
        r = self.p.decide_certification(
            CERTIFIER, plan_id=nv["plan_id"],
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP])
        self.assertEqual(r["result"], "approved")

    def test_venue_change_forces_retest_only_for_affected_cells(self):
        cert_id = self._approved_cert()
        nv = self.p.new_version(
            PROVIDER, self.plan_id, version="v2.0",
            changed_facets=["venue"], summary="更换旅居场地")
        report = self.p.reuse_report(nv["plan_id"])
        self.assertEqual(len(report["reusable_evidence"]), 0)
        self.assertIn(CAP, report["retest_cells"][S_SOLO_MOBILITY])
        r = self.p.decide_certification(
            CERTIFIER, plan_id=nv["plan_id"],
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP])
        self.assertEqual(r["result"], "rejected")
        self.assertTrue(any("代表性人数不足" in x for x in r["reasons"]))
        gap = next(g for g in r["gaps"] if g["type"] == "underrepresented_stratum")
        self.assertEqual(gap["participants"], 0)

    def test_partial_facet_change_mixed_reuse(self):
        """同版本上：依赖 care_staff 但不依赖 venue 的证据在场地变更后仍复用。"""
        cert_id = self._approved_cert()
        pid = self.p.register_participant(
            TESTER, **dict(age_band="mid_70_79", health_status="chronic_stable",
                           living="solo", assistance="companion"),
            consent_version="c1")["participant_id"]
        self.p.record_evidence(
            TESTER, plan_id=self.plan_id, participant_id=pid,
            capability=CAP2, task="团体课程参与", barriers=[],
            staff_response={"response_minutes": 2, "rating": 4},
            price_disclosure="full", outcome="completed",
            raw_feedback="课程只涉及人员与内容，不涉及场地",
            facet_dependencies=["care_staff", "curriculum"])
        nv = self.p.new_version(
            PROVIDER, self.plan_id, version="v2.0",
            changed_facets=["venue"], summary="换场地")
        report = self.p.reuse_report(nv["plan_id"])
        reused = {e["evidence_id"] for e in report["reusable_evidence"]}
        retest = {e["evidence_id"] for e in report["retest_required"]}
        self.assertEqual(len(reused), 1)
        self.assertEqual(len(retest), 3)

    def test_change_two_edges_then_retest(self):
        """v1→v2 改场地，v2→v3 再改课程；v1 证据依赖场地在 v2 失效后保持失效。"""
        self._approved_cert()
        v2 = self.p.new_version(PROVIDER, self.plan_id, version="v2",
                                changed_facets=["venue"])["plan_id"]
        v3 = self.p.new_version(PROVIDER, v2, version="v3",
                                changed_facets=["curriculum"])["plan_id"]
        report = self.p.reuse_report(v3)
        # v1 证据依赖 venue+care_staff，在 v2 边即失效
        self.assertEqual(len(report["reusable_evidence"]), 0)
        self.assertEqual(len(report["retest_required"]), 3)

    # ---- 严重事件 / 整改 / 复验 ------------------------------------------

    def test_incident_suspends_immediately_and_blocks_new_review(self):
        cert_id = self._approved_cert()
        self.assertEqual(self.p.cert_status(self.p.certifications[cert_id]),
                         "active")
        inc = self.p.report_incident(
            CERTIFIER, cert_id=cert_id,
            description="独居轮椅参与者在浴室滑倒，疑似重伤")
        self.assertTrue(inc["suspended_certifications"])
        self.assertEqual(self.p.cert_status(self.p.certifications[cert_id]),
                         "suspended")
        # 同等级新版本评审被暂停状态阻断
        nv = self.p.new_version(PROVIDER, self.plan_id, version="v1.1",
                                changed_facets=["pricing"])["plan_id"]
        r = self.p.decide_certification(
            CERTIFIER, plan_id=nv, claimed_strata=[S_SOLO_MOBILITY],
            claimed_capabilities=[CAP])
        self.assertEqual(r["result"], "rejected")
        self.assertTrue(any("暂停" in x for x in r["reasons"]))

    def test_rectification_then_reverify_resumes(self):
        cert_id = self._approved_cert()
        self.p.report_incident(CERTIFIER, cert_id=cert_id, description="跌倒")
        # 未整改先复验：拒绝
        with self.assertRaises(PlatformError):
            self.p.reverify(CERTIFIER, cert_id=cert_id, passed=True)
        self.p.submit_rectification(PROVIDER, cert_id=cert_id,
                                    action="浴室加装扶手与紧急呼叫")
        fail = self.p.reverify(CERTIFIER, cert_id=cert_id, passed=False,
                               notes="复测仍发现门槛高差")
        self.assertFalse(fail["resumed"])
        self.assertEqual(self.p.cert_status(self.p.certifications[cert_id]),
                         "suspended")
        ok = self.p.reverify(CERTIFIER, cert_id=cert_id, passed=True,
                             notes="门槛改造完成，3名同类参与者复测通过")
        self.assertTrue(ok["resumed"])
        self.assertEqual(self.p.cert_status(self.p.certifications[cert_id]),
                         "active")

    def test_other_provider_cannot_rectify(self):
        cert_id = self._approved_cert()
        self.p.report_incident(CERTIFIER, cert_id=cert_id, description="x")
        with self.assertRaises(PlatformError):
            self.p.submit_rectification(OTHER_PROVIDER, cert_id=cert_id,
                                        action="乱提交")

    # ---- 申诉与回避 ------------------------------------------------------

    def test_appeal_requires_independent_reviewer(self):
        cert_id = self._approved_cert()
        self.p.report_incident(CERTIFIER, cert_id=cert_id, description="事件")
        apl = self.p.appeal(PROVIDER, cert_id=cert_id,
                            reason="供应商认为事件与服务缺陷无关")
        self.assertEqual(apl["status"], "pending")
        # 原评审人/事件上报人不得处理
        with self.assertRaises(PlatformError) as ctx:
            self.p.decide_appeal(
                CERTIFIER, appeal_id=apl["appeal_id"], outcome="uphold",
                reason="维持")
        self.assertEqual(ctx.exception.code, "recusal_required")
        # 未参与原评审者处理并撤销暂停
        r = self.p.decide_appeal(
            CERTIFIER2, appeal_id=apl["appeal_id"], outcome="quash",
            reason="独立复核：事件为参与者自身基础病突发，非场地缺陷")
        self.assertEqual(r["status"], "quashed")
        self.assertEqual(self.p.cert_status(self.p.certifications[cert_id]),
                         "active")

    def test_appeal_against_active_certification_rejected(self):
        cert_id = self._approved_cert()
        with self.assertRaises(PlatformError):
            self.p.appeal(PROVIDER, cert_id=cert_id, reason="没事找事")

    def test_appeal_against_rejection_with_recusal(self):
        # 只测了低龄健康组，却声称覆盖独居行动受限组 → 驳回
        self._enroll(self.HEALTHY_PARTS, n=3)
        d = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_HEALTHY, S_SOLO_MOBILITY],
            claimed_capabilities=[CAP])
        self.assertEqual(d["result"], "rejected")
        apl = self.p.appeal(PROVIDER, decision_id=d["decision_id"],
                            reason="供应商认为代表性规则适用有误")
        self.assertEqual(apl["subject"], "rejection")
        with self.assertRaises(PlatformError) as ctx:
            self.p.decide_appeal(CERTIFIER, appeal_id=apl["appeal_id"],
                                 outcome="uphold", reason="原评审人回避")
        self.assertEqual(ctx.exception.code, "recusal_required")
        r = self.p.decide_appeal(
            CERTIFIER2, appeal_id=apl["appeal_id"], outcome="quash",
            reason="独立复核：声称分层与测试分层口径不一致，驳回作废，重新送审")
        self.assertEqual(r["status"], "quashed")
        self.assertEqual(
            self.p.decisions[d["decision_id"]]["appeal"], "quashed")

    # ---- 三视图 ----------------------------------------------------------

    def test_enterprise_view_is_desensitized(self):
        self._enroll(
            self.SOLO, n=3,
            barriers=[{"category": "浴室扶手", "severity": "major"}],
            price="partial")
        r = self.p.decide_certification(
            CERTIFIER, plan_id=self.plan_id,
            claimed_strata=[S_SOLO_MOBILITY], claimed_capabilities=[CAP])
        self.assertEqual(r["result"], "approved")
        view = self.p.enterprise_view(PROVIDER)
        svc = view["services"][0]
        defects = svc["desensitized_defects"][S_SOLO_MOBILITY]
        self.assertEqual(defects["barriers"]["浴室扶手:major"], 3)
        self.assertEqual(defects["price_disclosure"]["partial"], 3)
        self.assertIsNotNone(defects["avg_response_minutes"])
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("原始反馈", blob)
        self.assertNotIn("ppl_", blob)
        # 其他供应商看不到
        self.assertEqual(self.p.enterprise_view(OTHER_PROVIDER)["services"], [])

    def test_certifier_trace_chain_and_integrity(self):
        cert_id = self._approved_cert()
        trace = self.p.certifier_view(cert_id)
        self.assertTrue(trace["log_integrity"]["ok"])
        self.assertTrue(trace["evidence_integrity"]["ok"])
        self.assertEqual(trace["certification"]["reviewer_ids"], ["u_cert"])
        self.assertEqual(len(trace["plan_version_chain"]), 1)
        cov = trace["representative_coverage"][S_SOLO_MOBILITY][CAP]
        self.assertEqual(len(cov), 3)
        # 追到证据原文与上链哈希
        self.assertTrue(all(e["log_hash"] for e in trace["evidence_detail"]))

    # ---- 防篡改 ----------------------------------------------------------

    def test_tampering_raw_feedback_is_detected(self):
        self._enroll(self.SOLO, n=1)
        ev_id = next(iter(self.p.evidence))
        self.p.evidence[ev_id]["raw_feedback"] = "供应商改写后的好评"
        integrity = self.p.verify_evidence_integrity()
        self.assertFalse(integrity["ok"])
        self.assertEqual(integrity["mismatches"][0]["field"], "raw_feedback")
        # 直接改链上负载也会被哈希链发现
        self.p.log[0]["payload"]["raw_feedback"] = "x"
        self.assertFalse(self.p.verify_log()["ok"])

    def test_amendment_appends_without_touching_original(self):
        self._enroll(self.SOLO, n=1)
        ev_id = next(iter(self.p.evidence))
        original = self.p.evidence[ev_id]["raw_feedback"]
        self.p.amend_evidence(TESTER, ev_id, "补记：当日报修已上门")
        self.assertEqual(self.p.evidence[ev_id]["raw_feedback"], original)
        self.assertEqual(len(self.p.evidence[ev_id]["amendments"]), 1)
        self.assertTrue(self.p.verify_evidence_integrity()["ok"])

    # ---- 有效期 ----------------------------------------------------------

    def test_expiry(self):
        cert_id = self._approved_cert()
        cert = self.p.certifications[cert_id]
        self.clock.advance(366)
        self.assertEqual(self.p.cert_status(cert), "expired")
        item = self.p.consumer_view()["certifications"][0]
        self.assertEqual(item["status"], "expired")


if __name__ == "__main__":
    unittest.main()

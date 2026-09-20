"""银发服务体验证据平台领域层。

设计原则（对应认证规则）：
- 先按年龄/健康/居住/辅助四维分层，且取得本人当次同意后才能采集证据；
- 证据记录关键任务、可达性障碍、人员响应、价格披露与中途退出，
  原始反馈只增不改，供应商无权改写，全部进入哈希链防篡改；
- 方案或场地以"版本 + 变更面"管理，新版本只复用与变更面无关的证据，
  并明确哪些分层/能力单元必须复测；
- 认证按等级评审，代表性覆盖不足不得通过，关键障碍不得豁免，
  例外只能缩小适用范围且全程留痕；
- 严重事件立即暂停相关等级，整改并复验通过后方可恢复；
- 申诉由未参与原评审的人员处理；
- 消费者/企业/认证人员三视图信息严格分层。
"""

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta

# ---- 固定分层词表 ---------------------------------------------------------

AGE_BANDS = {
    "low_60_69": "低龄老人(60-69岁)",
    "mid_70_79": "中龄老人(70-79岁)",
    "senior_80_plus": "高龄老人(80岁及以上)",
}
HEALTH = {
    "healthy": "健康",
    "chronic_stable": "慢病稳定",
    "mobility_limited": "行动受限",
    "care_needed": "照护依赖",
}
LIVING = {
    "solo": "独居",
    "with_family": "与家人同住",
    "in_facility": "居住在养老机构",
}
ASSISTANCE = {
    "none": "无需辅助",
    "device": "使用辅具",
    "companion": "需人员陪护",
}

# 方案"面"：证据声明自己依赖哪些面；新版本声明改了哪些面。
FACETS = {
    "venue": "场地",
    "curriculum": "课程/旅居内容",
    "care_staff": "康养人员配置",
    "pricing": "价格结构",
    "transport": "交通接驳",
}

BARRIER_SEVERITIES = {"critical", "major", "minor"}
PRICE_DISCLOSURE = {"full", "partial", "none"}
OUTCOMES = {"completed", "abandoned"}

MIN_PARTICIPANTS_PER_STRATUM = 3
DEFAULT_VALID_DAYS = 365

ROLE_TESTER = "tester"          # 体验测评人员（采集证据）
ROLE_CERTIFIER = "certifier"    # 认证人员
ROLE_PROVIDER = "provider"      # 供应商/企业


class PlatformError(Exception):
    """带 HTTP 状态码的领域错误。"""

    def __init__(self, message, status=422, code=None):
        super().__init__(message)
        self.status = status
        self.code = code or "invalid"


def _now_iso(ts):
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _require(actor, role):
    if not isinstance(actor, dict) or actor.get("role") != role:
        raise PlatformError(f"需要 {role} 角色操作", status=403, code="forbidden")
    if not actor.get("id"):
        raise PlatformError("操作人缺少身份 id", status=403, code="forbidden")


def stratum_code(age_band, health_status, living, assistance):
    return f"{age_band}|{health_status}|{living}|{assistance}"


class EvidencePlatform:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.participants = {}
        self.plans = {}                 # plan_id -> version
        self.families = {}              # family_id -> [plan_id 按时间顺序]
        self.evidence = {}              # evidence_id -> record
        self.decisions = {}             # decision_id -> decision
        self.certifications = {}        # cert_id -> certification
        self.incidents = {}
        self.rectifications = {}
        self.reverifications = {}
        self.suspensions = []
        self.appeals = {}
        self.log = []                  # 防篡改哈希链
        self._log_tail = "GENESIS"

    # ---- 内部工具 ---------------------------------------------------------

    def _ts(self):
        return self.clock()

    def _append_log(self, kind, payload):
        body = json.dumps(
            {"kind": kind, "payload": payload}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256((self._log_tail + body.hex()).encode("utf-8")).hexdigest()
        entry = {
            "seq": len(self.log) + 1,
            "kind": kind,
            "payload": payload,
            "prev_hash": self._log_tail,
            "hash": digest,
            "at": _now_iso(self._ts()),
        }
        self.log.append(entry)
        self._log_tail = digest
        return entry

    def verify_log(self):
        """重算哈希链；任何条目被改写都会失配。"""
        tail = "GENESIS"
        for entry in self.log:
            body = json.dumps(
                {"kind": entry["kind"], "payload": entry["payload"]},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            expect = hashlib.sha256((tail + body.hex()).encode("utf-8")).hexdigest()
            if entry["prev_hash"] != tail or entry["hash"] != expect:
                return {"ok": False, "broken_at": entry["seq"]}
            tail = expect
        return {"ok": True, "entries": len(self.log)}

    def verify_evidence_integrity(self):
        """逐条比对证据现状与上链时的内容。

        原始反馈、分层、能力、结果只允许存在于链上的那份；
        任何事后改写（含供应商试图改写）都会在此暴露。
        """
        committed = {}
        for entry in self.log:
            if entry["kind"] == "evidence_recorded":
                p = entry["payload"]
                committed[p["evidence_id"]] = p
        report = {"ok": True, "checked": 0, "mismatches": []}
        for eid, ev in self.evidence.items():
            p = committed.get(eid)
            report["checked"] += 1
            if p is None:
                report["mismatches"].append({"evidence_id": eid, "field": "*",
                                             "problem": "链上缺失"})
                continue
            for field in ("raw_feedback", "stratum", "capability", "outcome",
                          "plan_id", "participant_id"):
                if ev.get(field) != p.get(field):
                    report["mismatches"].append(
                        {"evidence_id": eid, "field": field,
                         "problem": "与上链内容不一致"})
        report["ok"] = not report["mismatches"]
        return report

    def _get_plan(self, plan_id):
        plan = self.plans.get(plan_id)
        if not plan:
            raise PlatformError("方案版本不存在", status=404, code="not_found")
        return plan

    def _get_cert(self, cert_id):
        cert = self.certifications.get(cert_id)
        if not cert:
            raise PlatformError("认证决定不存在", status=404, code="not_found")
        return cert

    def _consent_valid(self, participant_id):
        p = self.participants.get(participant_id)
        return bool(p and p["consented"] and not p["consent_withdrawn"])

    # ---- 1. 分层与同意 ----------------------------------------------------

    def register_participant(self, actor, *, age_band, health_status, living,
                             assistance, consent_version, agreed=True):
        _require(actor, ROLE_TESTER)
        for name, value, table in (
            ("age_band", age_band, AGE_BANDS),
            ("health_status", health_status, HEALTH),
            ("living", living, LIVING),
            ("assistance", assistance, ASSISTANCE),
        ):
            if value not in table:
                raise PlatformError(f"非法分层取值: {name}={value}")
        if not consent_version or not agreed:
            raise PlatformError("必须记录本人当次同意的协议版本且 agreed=true")
        pid = _new_id("ppl")
        self.participants[pid] = {
            "id": pid,
            "stratum": stratum_code(age_band, health_status, living, assistance),
            "stratum_parts": {
                "age_band": age_band, "health_status": health_status,
                "living": living, "assistance": assistance,
            },
            "consent_version": consent_version,
            "consented_at": _now_iso(self._ts()),
            "consented": True,
            "consent_withdrawn": False,
            "registered_by": actor["id"],
        }
        return {"participant_id": pid, "stratum": self.participants[pid]["stratum"]}

    def withdraw_consent(self, actor, participant_id, reason=""):
        """撤回同意：旧证据保留但立即失效，不得删除、不得改写。"""
        _require(actor, ROLE_TESTER)
        p = self.participants.get(participant_id)
        if not p:
            raise PlatformError("参与者不存在", status=404, code="not_found")
        p["consent_withdrawn"] = True
        p["withdrawn_at"] = _now_iso(self._ts())
        self._append_log("consent_withdrawn",
                         {"participant_id": participant_id, "reason": reason})
        return {"participant_id": participant_id, "consent_withdrawn": True}

    # ---- 2. 方案版本 ------------------------------------------------------

    def create_plan(self, actor, *, provider_name, service_name, version, tier,
                    facets, based_on=None, summary=""):
        _require(actor, ROLE_PROVIDER)
        for f in facets:
            if f not in FACETS:
                raise PlatformError(f"非法方案面: {f}")
        if based_on:
            base = self._get_plan(based_on)
            family_id = base["family_id"]
        else:
            family_id = _new_id("fam")
            self.families[family_id] = []
        existing_versions = {self.plans[pid]["version"]
                             for pid in self.families[family_id]}
        if version in existing_versions:
            raise PlatformError(f"同方案族内版本号 {version} 已存在，"
                                "版本不可覆盖")
        plan_id = _new_id("plan")
        plan = {
            "id": plan_id,
            "family_id": family_id,
            "provider_id": actor["id"],
            "provider_name": provider_name,
            "service_name": service_name,
            "version": version,
            "tier": tier,
            "facets": sorted(set(facets)),
            "based_on": based_on,
            "summary": summary,
            "created_at": _now_iso(self._ts()),
        }
        self.plans[plan_id] = plan
        self.families[family_id].append(plan_id)
        return {"plan_id": plan_id, "family_id": family_id, "version": version}

    def new_version(self, actor, plan_id, *, version, changed_facets, summary=""):
        """方案/场地变化：登记新版本及变更面（驱动证据复用判断）。"""
        base = self._get_plan(plan_id)
        _require(actor, ROLE_PROVIDER)
        if actor["id"] != base["provider_id"]:
            raise PlatformError("只有原供应商可登记新版本", status=403,
                                code="forbidden")
        for f in changed_facets:
            if f not in FACETS:
                raise PlatformError(f"非法方案面: {f}")
        result = self.create_plan(
            actor,
            provider_name=base["provider_name"],
            service_name=base["service_name"],
            version=version,
            tier=base["tier"],
            facets=base["facets"],
            based_on=plan_id,
            summary=summary,
        )
        self.plans[result["plan_id"]]["changed_facets"] = sorted(set(changed_facets))
        return result

    # ---- 3. 证据采集（原始反馈不可篡改） ----------------------------------

    def record_evidence(self, actor, *, plan_id, participant_id, capability,
                        task, barriers, staff_response, price_disclosure,
                        outcome, raw_feedback, facet_dependencies,
                        exit_reason=None):
        _require(actor, ROLE_TESTER)
        self._get_plan(plan_id)
        if not self._consent_valid(participant_id):
            raise PlatformError("参与者不存在或同意已撤回，不得采集证据",
                                code="consent_required")
        if price_disclosure not in PRICE_DISCLOSURE:
            raise PlatformError("price_disclosure 取值非法")
        if outcome not in OUTCOMES:
            raise PlatformError("outcome 取值非法")
        if outcome == "abandoned" and not exit_reason:
            raise PlatformError("中途退出必须记录退出原因")
        for b in barriers or []:
            if b.get("severity") not in BARRIER_SEVERITIES:
                raise PlatformError("障碍严重程度非法")
        for f in facet_dependencies or []:
            if f not in FACETS:
                raise PlatformError(f"证据依赖了非法方案面: {f}")
        if not facet_dependencies:
            raise PlatformError("证据必须声明至少一个依赖面，"
                                "否则方案/场地变化后无法判断是否需要复测")
        if not isinstance(raw_feedback, str) or not raw_feedback.strip():
            raise PlatformError("原始反馈为必填且必须是逐字记录")

        p = self.participants[participant_id]
        eid = _new_id("evi")
        record = {
            "id": eid,
            "plan_id": plan_id,
            "family_id": self.plans[plan_id]["family_id"],
            "participant_id": participant_id,
            "stratum": p["stratum"],
            "capability": capability,
            "task": task,
            "barriers": barriers or [],
            "staff_response": staff_response or {},
            "price_disclosure": price_disclosure,
            "outcome": outcome,
            "exit_reason": exit_reason,
            "raw_feedback": raw_feedback,   # 一经写入只读
            "facet_dependencies": sorted(set(facet_dependencies or [])),
            "recorded_by": actor["id"],
            "created_at": _now_iso(self._ts()),
            "amendments": [],
            "superseded": False,
        }
        self.evidence[eid] = record
        entry = self._append_log("evidence_recorded", {
            "evidence_id": eid, "plan_id": plan_id,
            "participant_id": participant_id, "stratum": record["stratum"],
            "capability": capability, "outcome": outcome,
            "raw_feedback": raw_feedback,
        })
        record["log_hash"] = entry["hash"]
        return {"evidence_id": eid, "hash": entry["hash"]}

    def amend_evidence(self, actor, evidence_id, note):
        """更正只能追加附注，原始反馈保持不动（供应商无权调用）。"""
        _require(actor, ROLE_TESTER)
        record = self.evidence.get(evidence_id)
        if not record:
            raise PlatformError("证据不存在", status=404, code="not_found")
        amendment = {"by": actor["id"], "at": _now_iso(self._ts()), "note": note}
        record["amendments"].append(amendment)
        self._append_log("evidence_amended",
                         {"evidence_id": evidence_id, "amendment": amendment})
        return {"evidence_id": evidence_id, "amendments": record["amendments"]}

    # ---- 4. 版本间证据复用与复测判定 --------------------------------------

    def _reuse_chain(self, target_plan_id):
        """沿 based_on 链判断每条祖先证据在目标版本是否仍可复用。

        对每条版本边（子版本 -> 父版本，父侧为变更前），父版本及其更早祖先
        上采集的证据，若其依赖面与该边变更面相交，则在目标版本失效（复测）。
        一旦在某条边失效，后续边保持失效。证据在哪个版本采集，只受它之后
        发生的版本边影响。返回 {evidence_id: reusable(bool)}。
        """
        target = self._get_plan(target_plan_id)
        chain = []
        cur = target
        while cur.get("based_on"):
            parent = self.plans[cur["based_on"]]
            ancestor_ids = set()
            node = parent
            while True:
                ancestor_ids.add(node["id"])
                if not node.get("based_on"):
                    break
                node = self.plans[node["based_on"]]
            chain.append((cur, parent, set(cur.get("changed_facets", [])),
                          ancestor_ids))
            cur = parent

        blocked = set()
        for _child, _parent, changed, ancestor_ids in chain:
            if not changed:
                continue
            for eid, ev in self.evidence.items():
                if eid in blocked or ev["plan_id"] not in ancestor_ids:
                    continue
                if changed & set(ev["facet_dependencies"]):
                    blocked.add(eid)

        result = {}
        visible = {target["id"]}
        for _child, parent, _changed, _ancestors in chain:
            visible.add(parent["id"])
        for eid, ev in self.evidence.items():
            if ev["family_id"] != target["family_id"]:
                continue
            if ev["plan_id"] not in visible:
                continue
            result[eid] = eid not in blocked
        return result

    def _evidence_visible_in(self, evidence_id, plan_id):
        """证据是否沿版本链属于该方案族（能否被某版本看到）。"""
        ev = self.evidence[evidence_id]
        plan = self._get_plan(plan_id)
        if ev["family_id"] != plan["family_id"]:
            return False
        cur = plan
        while True:
            if ev["plan_id"] == cur["id"]:
                return True
            if not cur.get("based_on"):
                return False
            cur = self.plans[cur["based_on"]]

    def _usable_evidence(self, plan_id):
        """目标版本上实际可用于评审的证据（可复用 + 同意有效 + 未被参与者撤回）。"""
        reuse = self._reuse_chain(plan_id)
        out = []
        for eid, reusable in reuse.items():
            ev = self.evidence[eid]
            if reusable and self._consent_valid(ev["participant_id"]):
                out.append(ev)
        return out

    def reuse_report(self, plan_id):
        self._get_plan(plan_id)
        reuse = self._reuse_chain(plan_id)
        reusable, retest = [], []
        for eid, ok in reuse.items():
            ev = self.evidence[eid]
            item = {
                "evidence_id": eid,
                "stratum": ev["stratum"],
                "capability": ev["capability"],
                "from_version": self.plans[ev["plan_id"]]["version"],
                "consent_valid": self._consent_valid(ev["participant_id"]),
            }
            (reusable if ok else retest).append(item)
        needs = {}
        for item in retest:
            needs.setdefault(item["stratum"], [])
            if item["capability"] not in needs[item["stratum"]]:
                needs[item["stratum"]].append(item["capability"])
        return {
            "plan_id": plan_id,
            "reusable_evidence": reusable,
            "retest_required": retest,
            "retest_cells": needs,
        }

    # ---- 5. 认证评审：代表性覆盖 + 例外决定 -------------------------------

    def _coverage(self, plan_id, claimed_strata, claimed_capabilities):
        evidence = self._usable_evidence(plan_id)
        per_stratum = {}
        cells = {}
        critical = []
        for ev in evidence:
            if ev["stratum"] in claimed_strata:
                per_stratum.setdefault(ev["stratum"], set()).add(ev["participant_id"])
            if ev["stratum"] in claimed_strata and ev["capability"] in claimed_capabilities:
                bucket = cells.setdefault((ev["stratum"], ev["capability"]), [])
                bucket.append(ev)
                for b in ev["barriers"]:
                    if b["severity"] == "critical":
                        critical.append({
                            "stratum": ev["stratum"], "capability": ev["capability"],
                            "category": b.get("category"), "evidence_id": ev["id"],
                        })
        return per_stratum, cells, critical

    def decide_certification(self, actor, *, plan_id, claimed_strata,
                             claimed_capabilities, valid_days=DEFAULT_VALID_DAYS,
                             exceptions=None):
        _require(actor, ROLE_CERTIFIER)
        plan = self._get_plan(plan_id)
        exceptions = exceptions or []
        for ex in exceptions:
            if not ex.get("reason"):
                raise PlatformError("例外决定必须写明理由")

        excepted_cells = {(e["stratum"], e["capability"]) for e in exceptions
                          if e.get("capability")}
        excepted_strata = {e["stratum"] for e in exceptions if not e.get("capability")}

        per_stratum, cells, critical_blockers = self._coverage(
            plan_id, set(claimed_strata), set(claimed_capabilities))

        gaps = []
        approved_strata, approved_cells = set(), set()
        for s in claimed_strata:
            if s in excepted_strata:
                continue
            n = len(per_stratum.get(s, set()))
            if n < MIN_PARTICIPANTS_PER_STRATUM:
                gaps.append({"type": "underrepresented_stratum", "stratum": s,
                             "participants": n,
                             "required": MIN_PARTICIPANTS_PER_STRATUM})
                continue
            approved_strata.add(s)
            for c in claimed_capabilities:
                if (s, c) in excepted_cells:
                    continue
                cell_evs = [ev for ev in cells.get((s, c), [])
                            if ev["outcome"] == "completed"]
                if not cell_evs:
                    gaps.append({"type": "missing_capability_evidence",
                                 "stratum": s, "capability": c})
                    continue
                approved_cells.add((s, c))

        # 关键障碍不可用例外豁免：只要落在拟通过分层内即驳回
        # （整组排除该分层才算合法缩小范围，单元格级"豁免"不允许）。
        hard_blockers = [b for b in critical_blockers
                         if b["stratum"] in approved_strata]

        family_suspended = any(
            s["active"] and self.certifications[s["cert_id"]]["family_id"] == plan["family_id"]
            and self.certifications[s["cert_id"]]["tier"] == plan["tier"]
            for s in self.suspensions)

        did = _new_id("dec")
        decision = {
            "id": did,
            "plan_id": plan_id,
            "family_id": plan["family_id"],
            "tier": plan["tier"],
            "reviewer_ids": [actor["id"]],
            "at": _now_iso(self._ts()),
            "claimed_strata": list(claimed_strata),
            "claimed_capabilities": list(claimed_capabilities),
            "coverage_gaps": gaps,
            "critical_blockers": hard_blockers,
            "exceptions": exceptions,
            "tier_suspended": family_suspended,
        }

        no_verified_cells = not approved_cells
        if hard_blockers or family_suspended or not approved_strata or gaps or no_verified_cells:
            decision["result"] = "rejected"
            decision["reasons"] = self._reject_reasons(gaps, hard_blockers,
                                                       family_suspended,
                                                       not approved_strata,
                                                       no_verified_cells)
            self.decisions[did] = decision
            return {"decision_id": did, "result": "rejected",
                    "reasons": decision["reasons"], "gaps": gaps,
                    "critical_blockers": hard_blockers}

        now = self._ts()
        cert_id = _new_id("cert")
        cap_by_stratum = {}
        for (s, c) in sorted(approved_cells):
            cap_by_stratum.setdefault(s, []).append(c)
        cert = {
            "id": cert_id,
            "decision_id": did,
            "family_id": plan["family_id"],
            "plan_id": plan_id,
            "provider_id": plan["provider_id"],
            "provider_name": plan["provider_name"],
            "service_name": plan["service_name"],
            "tier": plan["tier"],
            "plan_version": plan["version"],
            "applicable_strata": sorted(approved_strata),
            "verified_capabilities": cap_by_stratum,
            "exceptions": exceptions,
            "reviewer_ids": [actor["id"]],
            "issued_at": _now_iso(now),
            "valid_from": now,
            "valid_until": now + timedelta(days=valid_days).total_seconds(),
        }
        decision["result"] = "approved"
        decision["certification_id"] = cert_id
        self.decisions[did] = decision
        self.certifications[cert_id] = cert
        return {"decision_id": did, "result": "approved",
                "certification_id": cert_id,
                "applicable_strata": cert["applicable_strata"],
                "verified_capabilities": cap_by_stratum,
                "valid_until": _now_iso(cert["valid_until"])}

    @staticmethod
    def _reject_reasons(gaps, blockers, suspended, no_strata, no_cells):
        reasons = []
        if blockers:
            reasons.append("拟通过范围内存在关键可达性障碍，不可豁免")
        if suspended:
            reasons.append("该等级因严重事件处于暂停状态，须复验通过")
        if no_strata or no_cells:
            reasons.append("没有任何分层×能力单元达到代表性证据要求")
        if any(g["type"] == "underrepresented_stratum" for g in gaps):
            reasons.append("部分声称覆盖的分层代表性人数不足，请缩窄适用范围或补测")
        if any(g["type"] == "missing_capability_evidence" for g in gaps):
            reasons.append("部分分层×能力单元缺少完成关键任务的证据")
        return reasons or ["代表性覆盖不足"]

    # ---- 6. 严重事件：立即暂停相关等级 ------------------------------------

    def _active_suspension(self, cert_id):
        return next((s for s in self.suspensions
                     if s["cert_id"] == cert_id and s["active"]), None)

    def cert_status(self, cert, at=None):
        at = self._ts() if at is None else at
        if at > cert["valid_until"]:
            return "expired"
        if self._active_suspension(cert["id"]):
            return "suspended"
        return "active"

    def report_incident(self, actor, *, cert_id, description, scope="certification"):
        if actor.get("role") not in (ROLE_CERTIFIER, ROLE_TESTER, ROLE_PROVIDER):
            raise PlatformError("无权上报事件", status=403, code="forbidden")
        cert = self._get_cert(cert_id)
        iid = _new_id("inc")
        incident = {
            "id": iid, "cert_id": cert_id, "family_id": cert["family_id"],
            "tier": cert["tier"], "scope": scope,
            "description": description, "reported_by": actor["id"],
            "at": _now_iso(self._ts()),
        }
        self.incidents[iid] = incident

        targets = [cert]
        if scope == "tier":
            targets = [c for c in self.certifications.values()
                       if c["family_id"] == cert["family_id"]
                       and c["tier"] == cert["tier"]
                       and self.cert_status(c) == "active"]
        for c in targets:  # 立即暂停，不等整改
            if not self._active_suspension(c["id"]):
                self.suspensions.append({
                    "cert_id": c["id"], "incident_id": iid,
                    "at": incident["at"], "active": True,
                })
        return {"incident_id": iid, "suspended_certifications":
                [c["id"] for c in targets]}

    # ---- 7. 整改与复验 ----------------------------------------------------

    def submit_rectification(self, actor, *, cert_id, action):
        _require(actor, ROLE_PROVIDER)
        cert = self._get_cert(cert_id)
        if actor["id"] != cert["provider_id"]:
            raise PlatformError("只有供应商可提交整改", status=403,
                                code="forbidden")
        if not self._active_suspension(cert_id):
            raise PlatformError("该认证当前未被暂停，无需整改流程")
        rid = _new_id("rec")
        self.rectifications[rid] = {
            "id": rid, "cert_id": cert_id, "action": action,
            "by": actor["id"], "at": _now_iso(self._ts()),
        }
        return {"rectification_id": rid}

    def reverify(self, actor, *, cert_id, passed, notes="", new_evidence_ids=None):
        _require(actor, ROLE_CERTIFIER)
        cert = self._get_cert(cert_id)
        suspension = self._active_suspension(cert_id)
        if not suspension:
            raise PlatformError("该认证未处于暂停状态")
        open_rect = any(r["cert_id"] == cert_id for r in self.rectifications.values())
        if not open_rect:
            raise PlatformError("复验前必须有供应商整改记录")
        vid = _new_id("rv")
        record = {
            "id": vid, "cert_id": cert_id, "passed": bool(passed),
            "notes": notes, "new_evidence": new_evidence_ids or [],
            "by": actor["id"], "at": _now_iso(self._ts()),
        }
        self.reverifications[vid] = record
        if passed:
            suspension["active"] = False
            suspension["lifted_by"] = vid
            suspension["lifted_at"] = record["at"]
        return {"reverification_id": vid, "passed": bool(passed),
                "resumed": bool(passed)}

    # ---- 8. 申诉（回避原评审人员） ----------------------------------------

    def appeal(self, actor, *, reason, cert_id=None, decision_id=None):
        """供应商对暂停措施或驳回决定提出申诉。"""
        _require(actor, ROLE_PROVIDER)
        if cert_id:
            cert = self._get_cert(cert_id)
            if actor["id"] != cert["provider_id"]:
                raise PlatformError("只有该服务供应商可申诉", status=403,
                                    code="forbidden")
            suspension = self._active_suspension(cert_id)
            if not suspension:
                raise PlatformError("认证处于有效状态，没有可申诉的事项")
            subject = "suspension"
            target = cert_id
            original = set(cert["reviewer_ids"])
            incident = self.incidents[suspension["incident_id"]]
            original.add(incident["reported_by"])
        elif decision_id:
            decision = self.decisions.get(decision_id)
            if not decision:
                raise PlatformError("评审决定不存在", status=404,
                                    code="not_found")
            plan = self.plans[decision["plan_id"]]
            if actor["id"] != plan["provider_id"]:
                raise PlatformError("只有该服务供应商可申诉", status=403,
                                    code="forbidden")
            if decision["result"] != "rejected":
                raise PlatformError("只能对驳回决定提出申诉")
            subject = "rejection"
            target = decision_id
            original = set(decision["reviewer_ids"])
        else:
            raise PlatformError("必须提供 cert_id 或 decision_id")
        aid = _new_id("apl")
        self.appeals[aid] = {
            "id": aid, "subject": subject, "target_id": target,
            "reason": reason, "by": actor["id"],
            "at": _now_iso(self._ts()),
            "original_reviewer_ids": sorted(original),
            "status": "pending",
        }
        return {"appeal_id": aid, "subject": subject, "status": "pending"}

    def decide_appeal(self, actor, *, appeal_id, outcome, reason):
        _require(actor, ROLE_CERTIFIER)
        apl = self.appeals.get(appeal_id)
        if not apl:
            raise PlatformError("申诉不存在", status=404, code="not_found")
        if apl["status"] != "pending":
            raise PlatformError("该申诉已处理")
        if actor["id"] in apl["original_reviewer_ids"]:
            raise PlatformError("申诉处理人不得是原评审参与人（回避）",
                                status=403, code="recusal_required")
        if outcome not in ("uphold", "quash"):
            raise PlatformError("outcome 须为 uphold 或 quash")
        apl["status"] = "upheld" if outcome == "uphold" else "quashed"
        apl["decision"] = {"by": actor["id"], "at": _now_iso(self._ts()),
                           "outcome": outcome, "reason": reason}
        if outcome == "quash" and apl["subject"] == "suspension":
            suspension = self._active_suspension(apl["target_id"])
            if suspension:
                suspension["active"] = False
                suspension["lifted_by"] = f"appeal:{apl['id']}"
                suspension["lifted_at"] = apl["decision"]["at"]
        if outcome == "quash" and apl["subject"] == "rejection":
            # 驳回被独立复核推翻：原决定标记作废，供应商可重新送审。
            self.decisions[apl["target_id"]]["appeal"] = "quashed"
        return {"appeal_id": appeal_id, "status": apl["status"],
                "handler": actor["id"]}

    # ---- 9. 三视图 --------------------------------------------------------

    @staticmethod
    def _stratum_label(code):
        age, health, living, assistance = code.split("|")
        return " / ".join([AGE_BANDS[age], HEALTH[health],
                           LIVING[living], ASSISTANCE[assistance]])

    def consumer_view(self, family_id=None):
        """消费者：只看适用人群、有效期、已验证能力与暂停状态。"""
        items = []
        now = self._ts()
        for cert in self.certifications.values():
            if family_id and cert["family_id"] != family_id:
                continue
            status = self.cert_status(cert, now)
            items.append({
                "provider": cert["provider_name"],
                "service": cert["service_name"],
                "tier": cert["tier"],
                "status": status,
                "applicable_population": [
                    self._stratum_label(s) for s in cert["applicable_strata"]],
                "verified_capabilities": cert["verified_capabilities"],
                "issued_at": cert["issued_at"],
                "valid_until": _now_iso(cert["valid_until"]),
                "plan_version": cert["plan_version"],
                "suspension_notice": "该等级已因安全事件暂停" if status == "suspended" else None,
            })
        return {"certifications": items}

    def enterprise_view(self, actor):
        """企业：只拿到自己服务的脱敏缺陷清单，不见任何参与者身份与逐字反馈。"""
        _require(actor, ROLE_PROVIDER)
        out = []
        for cert in self.certifications.values():
            if cert["provider_id"] != actor["id"]:
                continue
            evidence = self._usable_evidence(cert["plan_id"])
            wanted = set()
            for s, caps in cert["verified_capabilities"].items():
                for c in caps:
                    wanted.add((s, c))
            defects = {}
            for ev in evidence:
                if (ev["stratum"], ev["capability"]) not in wanted:
                    continue
                bucket = defects.setdefault(ev["stratum"], {
                    "barriers": {}, "price_disclosure": {"full": 0, "partial": 0, "none": 0},
                    "abandoned": 0, "staff_response_minutes": [],
                    "staff_rating": [], "tasks": 0,
                })
                bucket["tasks"] += 1
                for b in ev["barriers"]:
                    key = f"{b.get('category', '未分类')}:{b['severity']}"
                    bucket["barriers"][key] = bucket["barriers"].get(key, 0) + 1
                bucket["price_disclosure"][ev["price_disclosure"]] += 1
                if ev["outcome"] == "abandoned":
                    bucket["abandoned"] += 1
                sr = ev["staff_response"]
                if isinstance(sr.get("response_minutes"), (int, float)):
                    bucket["staff_response_minutes"].append(sr["response_minutes"])
                if isinstance(sr.get("rating"), (int, float)):
                    bucket["staff_rating"].append(sr["rating"])
            for s, b in defects.items():
                b["avg_response_minutes"] = (round(sum(b["staff_response_minutes"]) /
                                                   len(b["staff_response_minutes"]), 1)
                                             if b["staff_response_minutes"] else None)
                b["avg_staff_rating"] = (round(sum(b["staff_rating"]) /
                                               len(b["staff_rating"]), 1)
                                         if b["staff_rating"] else None)
                del b["staff_response_minutes"]
                del b["staff_rating"]
            out.append({
                "certification_id": cert["id"],
                "service": cert["service_name"],
                "tier": cert["tier"],
                "plan_version": cert["plan_version"],
                "status": self.cert_status(cert),
                "desensitized_defects": defects,
            })
        return {"services": out}

    def certifier_view(self, cert_id):
        """认证人员：从等级一路追到版本、覆盖、证据、例外、事件、整改、申诉。"""
        cert = self._get_cert(cert_id)
        evidence = self._usable_evidence(cert["plan_id"])
        coverage = {}
        for ev in evidence:
            coverage.setdefault(ev["stratum"], {})
            cell = coverage[ev["stratum"]].setdefault(ev["capability"], [])
            cell.append({
                "evidence_id": ev["id"], "outcome": ev["outcome"],
                "on_version": self.plans[ev["plan_id"]]["version"],
                "reused": ev["plan_id"] != cert["plan_id"],
                "barrier_severities": [b.get("severity") for b in ev["barriers"]],
                "price_disclosure": ev["price_disclosure"],
                "log_hash": ev.get("log_hash"),
            })
        chain = []
        cur = self.plans[cert["plan_id"]]
        while True:
            chain.append({"plan_id": cur["id"], "version": cur["version"],
                          "changed_facets": cur.get("changed_facets", []),
                          "based_on": cur.get("based_on")})
            if not cur.get("based_on"):
                break
            cur = self.plans[cur["based_on"]]
        cid = cert["id"]
        return {
            "certification": {
                "id": cid, "tier": cert["tier"],
                "status": self.cert_status(cert),
                "issued_at": cert["issued_at"],
                "valid_until": _now_iso(cert["valid_until"]),
                "reviewer_ids": cert["reviewer_ids"],
                "applicable_strata": cert["applicable_strata"],
                "verified_capabilities": cert["verified_capabilities"],
            },
            "plan_version_chain": list(reversed(chain)),
            "reuse_report": self.reuse_report(cert["plan_id"]),
            "representative_coverage": coverage,
            "exceptions": cert["exceptions"],
            "decision": self.decisions.get(cert["decision_id"]),
            "incidents": [i for i in self.incidents.values() if i["cert_id"] == cid],
            "suspensions": [s for s in self.suspensions if s["cert_id"] == cid],
            "rectifications": [r for r in self.rectifications.values()
                               if r["cert_id"] == cid],
            "reverifications": [r for r in self.reverifications.values()
                                if r["cert_id"] == cid],
            "appeals": [a for a in self.appeals.values()
                        if a["target_id"] in (cid, cert["decision_id"])],
            "evidence_detail": [self._evidence_detail(e) for e in evidence
                                 if (e["stratum"], e["capability"]) in
                                 {(s, c) for s, cs in cert["verified_capabilities"].items()
                                  for c in cs}],
            "log_integrity": self.verify_log(),
            "evidence_integrity": self.verify_evidence_integrity(),
        }

    def _evidence_detail(self, ev):
        return {
            "evidence_id": ev["id"],
            "stratum": ev["stratum"],
            "participant_pseudonym": ev["participant_id"],
            "capability": ev["capability"],
            "task": ev["task"],
            "barriers": ev["barriers"],
            "staff_response": ev["staff_response"],
            "price_disclosure": ev["price_disclosure"],
            "outcome": ev["outcome"],
            "exit_reason": ev["exit_reason"],
            "raw_feedback": ev["raw_feedback"],
            "amendments": ev["amendments"],
            "facet_dependencies": ev["facet_dependencies"],
            "recorded_by": ev["recorded_by"],
            "created_at": ev["created_at"],
            "log_hash": ev.get("log_hash"),
        }

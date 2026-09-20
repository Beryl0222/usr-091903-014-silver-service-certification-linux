"""银发服务体验证据平台的领域规则。

设计要点（对应认证流程要求）：

* 测试分层固定为四个维度：年龄阶段、健康状况、居住状态、辅助需求；
  参与者必须存在针对该服务方案的有效同意，证据才计入覆盖。
* 证据记录五个关键信号：关键任务、可达性障碍、人员响应、价格披露、中途退出。
  证据与原始反馈只允许追加（append-only），并进入按方案串联的哈希链，
  供应商没有任何改写入口。
* 方案/场地产生新版本时按“受影响维度”判定旧证据可否复用；
  未受影响且同意仍有效、分层仍被声明的证据标记为复用证据，
  其余分层/维度明确列入复测清单。
* 严重事件立即暂停相关等级；暂停只能由整改后的复验通过解除，申诉不能替代复验。
* 申诉裁决人不得是原评审/原记录人，服务端强制回避。
* 三类视图：消费者只看适用人群、有效期、已验证能力；
  供应商只看脱敏缺陷清单；认证人员可从等级追溯到方案版本、代表性覆盖、
  证据来源版本、例外决定、事件与整改复验全链路。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

AGE_BANDS = {
    "low_60_69": "低龄老人(60-69岁)",
    "mid_70_79": "中龄老人(70-79岁)",
    "high_80_plus": "高龄老人(80岁及以上)",
}
HEALTH_STATUSES = {
    "healthy": "健康",
    "chronic": "慢病",
    "mobility_limited": "行动受限",
}
LIVING_STATUSES = {
    "with_family": "与家人同住",
    "alone": "独居",
    "in_facility": "机构居住",
}
ASSISTANCE_LEVELS = {
    "none": "无需辅助",
    "partial": "部分辅助(助行器具/服药提醒)",
    "full": "全程辅助(轮椅/专人照护)",
}

STRATUM_FIELDS = ("age_band", "health_status", "living_status", "assistance")
_STRATUM_DOMAINS = (
    AGE_BANDS,
    HEALTH_STATUSES,
    LIVING_STATUSES,
    ASSISTANCE_LEVELS,
)

# 需要覆盖的四个可验证维度；中途退出是每次试用必须记录的安全信号，不单列覆盖维度。
DIMENSION_TASK = "task"
DIMENSION_ACCESSIBILITY = "accessibility"
DIMENSION_STAFF_RESPONSE = "staff_response"
DIMENSION_PRICE = "price"
CORE_DIMENSIONS = (
    DIMENSION_TASK,
    DIMENSION_ACCESSIBILITY,
    DIMENSION_STAFF_RESPONSE,
    DIMENSION_PRICE,
)
DIMENSION_LABELS = {
    DIMENSION_TASK: "关键任务",
    DIMENSION_ACCESSIBILITY: "可达性",
    DIMENSION_STAFF_RESPONSE: "人员响应",
    DIMENSION_PRICE: "价格披露",
    "dropout": "中途退出",
}

BARRIER_SEVERITIES = ("low", "medium", "high")
DEFECT_SEVERITIES = ("minor", "major", "blocking")
DROPOUT_REASONS = ("safety", "accessibility", "price", "personal", "other")

# 变更类型默认影响的维度；创建新版本时可用 affected_dimensions 显式扩大。
CHANGE_TYPE_IMPACT = {
    "venue": {DIMENSION_ACCESSIBILITY},          # 场地变化：可达性须复测
    "plan_content": {DIMENSION_TASK},            # 服务内容/课程变化：关键任务须复测
    "staffing": {DIMENSION_STAFF_RESPONSE},      # 人员配置变化：响应须复测
    "pricing": {DIMENSION_PRICE},                # 价格变化：披露须复测
}

VALID_CHANGE_TYPES = tuple(CHANGE_TYPE_IMPACT)
CERTIFICATE_VALIDITY_DAYS = 365
EXCEPTION_DEFAULT_DAYS = 90
DEFAULT_MIN_PARTICIPANTS = 2
DEFAULT_RESPONSE_LIMIT_SECONDS = 300


class DomainError(Exception):
    """携带 HTTP 状态码的领域错误。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_stratum(stratum: dict) -> tuple:
    """把分层字典归一化为四元组，并校验取值合法。"""
    if not isinstance(stratum, dict):
        raise DomainError("分层必须是包含四个维度的对象")
    key = []
    for field, domain in zip(STRATUM_FIELDS, _STRATUM_DOMAINS):
        value = stratum.get(field)
        if value not in domain:
            raise DomainError(f"分层维度 {field} 取值非法：{value!r}")
        key.append(value)
    return tuple(key)


def stratum_dict(key: tuple) -> dict:
    return dict(zip(STRATUM_FIELDS, key))


def stratum_label(key: tuple) -> str:
    return "·".join(domain[value] for domain, value in zip(_STRATUM_DOMAINS, key))


# ---------------------------------------------------------------------------
# 存储与核心服务
# ---------------------------------------------------------------------------


class EvidenceStore:
    def __init__(self):
        self.reset()

    def reset(self):
        self.plans = {}
        self.participants = {}
        self.consents = {}
        self.sessions = {}
        self.feedback = {}
        self.defects = {}
        self.exceptions = {}
        self.incidents = {}
        self.rectifications = {}
        self.reverifications = {}
        self.decisions = {}
        self.revocations = {}
        self.appeals = {}
        # 按方案追加的哈希链：每条只记录类型、引用和哈希，校验时回算活动对象。
        self.ledger = []
        self._counters = {}

    def _next_id(self, prefix: str) -> str:
        count = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = count
        return f"{prefix}{count}"

    # -- 哈希链 -------------------------------------------------------------

    def _append_ledger(self, plan_id: str, kind: str, ref: str, obj: dict):
        prev_hash = self.ledger[-1]["hash"] if self.ledger else ""
        digest = hashlib.sha256(
            (prev_hash + "|" + _canonical_dumps(obj)).encode("utf-8")
        ).hexdigest()
        entry = {
            "seq": len(self.ledger) + 1,
            "plan_id": plan_id,
            "kind": kind,
            "ref": ref,
            "prev_hash": prev_hash,
            "hash": digest,
        }
        self.ledger.append(entry)

    def verify_ledger(self, plan_id: str) -> dict:
        """重算某方案的追加链；任何原始对象被改写都会导致校验失败。"""
        prev_hash = ""
        for entry in self.ledger:
            if entry["plan_id"] != plan_id:
                continue
            if entry["prev_hash"] != prev_hash:
                return {"ok": False, "broken_at": entry["seq"], "reason": "链断裂"}
            obj = self._ledger_target(entry)
            if obj is None:
                return {
                    "ok": False,
                    "broken_at": entry["seq"],
                    "reason": f"{entry['kind']} {entry['ref']} 已缺失",
                }
            digest = hashlib.sha256(
                (prev_hash + "|" + _canonical_dumps(obj)).encode("utf-8")
            ).hexdigest()
            if digest != entry["hash"]:
                return {
                    "ok": False,
                    "broken_at": entry["seq"],
                    "reason": "原始记录内容与哈希不一致（疑似被改写）",
                }
            prev_hash = entry["hash"]
        return {"ok": True, "entries": sum(1 for e in self.ledger if e["plan_id"] == plan_id)}

    def _ledger_target(self, entry: dict):
        collection = {
            "evidence": self.sessions,
            "feedback": self.feedback,
            "incident": self.incidents,
            "decision": self.decisions,
            "revocation": self.revocations,
        }.get(entry["kind"])
        return collection.get(entry["ref"]) if collection else None

    # -- 查询辅助 -----------------------------------------------------------

    def _get(self, collection: dict, obj_id: str, label: str):
        obj = collection.get(obj_id)
        if obj is None:
            raise DomainError(f"{label}不存在：{obj_id}", status=404)
        return obj

    def _plan(self, plan_id: str) -> dict:
        return self._get(self.plans, plan_id, "服务方案")

    def _version(self, plan: dict, version: int) -> dict:
        versions = plan["versions"]
        if not (1 <= version <= len(versions)):
            raise DomainError(f"方案版本不存在：v{version}", status=404)
        return versions[version - 1]

    def _level(self, plan: dict, version: int, level_id: str) -> dict:
        ver = self._version(plan, version)
        for level in ver["levels"]:
            if level["id"] == level_id:
                return level
        raise DomainError(f"等级不存在：{level_id}（v{version}）", status=404)

    def _require_owner(self, plan: dict, user_id: str):
        if user_id != plan["supplier_id"]:
            raise DomainError("只有该方案的供应商可以执行此操作", status=403)

    def _valid_consent(self, participant_id: str, plan_id: str):
        for consent in self.consents.values():
            if (
                consent["participant_id"] == participant_id
                and consent["plan_id"] == plan_id
                and not consent["withdrawn"]
            ):
                return consent
        return None

    # -- 方案与版本 ---------------------------------------------------------

    def _normalize_level(self, raw: dict) -> dict:
        if not isinstance(raw, dict) or not raw.get("name"):
            raise DomainError("等级必须包含名称")
        strata = raw.get("claimed_strata", [])
        if not isinstance(strata, list) or not strata:
            raise DomainError("等级必须声明至少一个适用分层")
        normalized = []
        seen = set()
        for stratum in strata:
            key = canonical_stratum(stratum)
            if key in seen:
                raise DomainError(f"等级内分层重复：{stratum_label(key)}")
            seen.add(key)
            normalized.append(stratum_dict(key))
        min_participants = raw.get("min_participants", DEFAULT_MIN_PARTICIPANTS)
        if not isinstance(min_participants, int) or min_participants < 1:
            raise DomainError("每分层最少参与者人数必须是正整数")
        limit = raw.get("staff_response_limit_seconds", DEFAULT_RESPONSE_LIMIT_SECONDS)
        if not isinstance(limit, int) or limit < 1:
            raise DomainError("人员响应时限必须是正整数（秒）")
        return {
            "id": raw.get("id") or self._next_id("l"),
            "name": raw["name"],
            "claimed_strata": normalized,
            "min_participants": min_participants,
            "staff_response_limit_seconds": limit,
        }

    def create_plan(self, supplier_id: str, name: str, levels: list) -> dict:
        if not name:
            raise DomainError("方案名称不能为空")
        if not levels:
            raise DomainError("方案至少包含一个认证等级")
        normalized = [self._normalize_level(level) for level in levels]
        plan_id = self._next_id("p")
        plan = {
            "id": plan_id,
            "supplier_id": supplier_id,
            "name": name,
            "created_at": now_iso(),
            "current_version": 1,
            "versions": [
                {
                    "version": 1,
                    "created_at": now_iso(),
                    "change_summary": "首次登记",
                    "change_types": [],
                    "affected_dimensions": [],
                    "levels": normalized,
                }
            ],
            "suspensions": {},  # level_id -> 暂停信息
        }
        self.plans[plan_id] = plan
        return plan

    def create_version(
        self,
        supplier_id: str,
        plan_id: str,
        change_summary: str,
        change_types: list | None = None,
        affected_dimensions: list | None = None,
        levels: list | None = None,
    ) -> dict:
        plan = self._plan(plan_id)
        self._require_owner(plan, supplier_id)
        old_version = plan["versions"][-1]

        change_types = change_types or []
        affected = set()
        for change_type in change_types:
            if change_type not in CHANGE_TYPE_IMPACT:
                raise DomainError(f"未知变更类型：{change_type}")
            affected.update(CHANGE_TYPE_IMPACT[change_type])
        for dimension in affected_dimensions or []:
            if dimension not in CORE_DIMENSIONS:
                raise DomainError(f"受影响维度非法：{dimension}")
            affected.add(dimension)
        if not change_summary:
            raise DomainError("变更说明不能为空")

        if levels is None:
            new_levels = json.loads(json.dumps(old_version["levels"]))
        else:
            new_levels = [self._normalize_level(level) for level in levels]
            old_ids = {level["id"] for level in old_version["levels"]}
            new_ids = {level["id"] for level in new_levels}
            if old_ids != new_ids:
                raise DomainError("新版本只能调整既有等级的声明，不能增删等级")

        version = {
            "version": old_version["version"] + 1,
            "created_at": now_iso(),
            "change_summary": change_summary,
            "change_types": change_types,
            "affected_dimensions": sorted(affected),
            "levels": new_levels,
        }
        plan["versions"].append(version)
        plan["current_version"] = version["version"]
        return version

    # -- 参与者与同意 -------------------------------------------------------

    def create_participant(self, stratum: dict) -> dict:
        key = canonical_stratum(stratum)
        participant_id = self._next_id("u")
        participant = {
            "id": participant_id,
            "stratum": stratum_dict(key),
            "registered_at": now_iso(),
        }
        self.participants[participant_id] = participant
        return participant

    def grant_consent(self, participant_id: str, plan_id: str) -> dict:
        participant = self._get(self.participants, participant_id, "参与者")
        self._plan(plan_id)
        existing = self._valid_consent(participant_id, plan_id)
        if existing:
            raise DomainError("该参与者对本方案已有有效同意", status=409)
        consent = {
            "id": self._next_id("c"),
            "participant_id": participant_id,
            "plan_id": plan_id,
            "scope": "experience_evidence",
            "granted_at": now_iso(),
            "withdrawn": False,
            "withdrawn_at": None,
        }
        self.consents[consent["id"]] = consent
        participant_ref = dict(participant)
        participant_ref["consent"] = consent["id"]
        return consent

    def withdraw_consent(self, consent_id: str) -> dict:
        consent = self._get(self.consents, consent_id, "同意记录")
        if consent["withdrawn"]:
            raise DomainError("同意已撤回", status=409)
        consent["withdrawn"] = True
        consent["withdrawn_at"] = now_iso()
        return consent

    # -- 证据记录（只增不改） -----------------------------------------------

    def add_evidence(
        self,
        plan_id: str,
        version: int | None,
        level_id: str,
        participant_id: str,
        dimensions: list | None,
        key_tasks: list | None,
        accessibility_barriers: list | None,
        staff_responses: list | None,
        price: dict | None,
        dropout: dict | None,
    ) -> dict:
        plan = self._plan(plan_id)
        version = version or plan["current_version"]
        level = self._level(plan, version, level_id)
        participant = self._get(self.participants, participant_id, "参与者")
        if not self._valid_consent(participant_id, plan_id):
            raise DomainError("缺少参与者针对本方案的有效同意，不得开展测试或计入证据")

        stratum_key = canonical_stratum(participant["stratum"])
        claimed = {canonical_stratum(s) for s in level["claimed_strata"]}
        if stratum_key not in claimed:
            raise DomainError(
                f"参与者分层 {stratum_label(stratum_key)} 不在等级「{level['name']}」声明范围内"
            )

        assessed = set(dimensions) if dimensions else set(CORE_DIMENSIONS)
        unknown = assessed - set(CORE_DIMENSIONS)
        if unknown:
            raise DomainError(f"未知评估维度：{sorted(unknown)}")
        if not assessed:
            raise DomainError("至少评估一个维度")

        record = {
            "id": self._next_id("e"),
            "plan_id": plan_id,
            "version": version,
            "level_id": level_id,
            "participant_id": participant_id,
            "stratum": participant["stratum"],
            "dimensions": sorted(assessed),
            "recorded_at": now_iso(),
        }

        if DIMENSION_TASK in assessed:
            tasks = key_tasks or []
            if not isinstance(tasks, list) or not tasks:
                raise DomainError("关键任务记录不能为空")
            normalized_tasks = []
            for task in tasks:
                if not task.get("name") or "completed" not in task:
                    raise DomainError("每个关键任务需要名称和 completed 结果")
                normalized_tasks.append(
                    {
                        "name": task["name"],
                        "capability": task.get("capability", task["name"]),
                        "completed": bool(task["completed"]),
                    }
                )
            record["key_tasks"] = normalized_tasks

        if DIMENSION_ACCESSIBILITY in assessed:
            barriers = accessibility_barriers or []
            for barrier in barriers:
                if barrier.get("severity") not in BARRIER_SEVERITIES:
                    raise DomainError("可达性障碍等级必须是 low/medium/high")
                if not barrier.get("description"):
                    raise DomainError("可达性障碍描述不能为空")
            record["accessibility_barriers"] = [
                {"description": b["description"], "severity": b["severity"]}
                for b in barriers
            ]

        if DIMENSION_STAFF_RESPONSE in assessed:
            responses = staff_responses or []
            if not isinstance(responses, list) or not responses:
                raise DomainError("人员响应记录不能为空")
            normalized_responses = []
            for response in responses:
                seconds = response.get("response_seconds")
                if not isinstance(seconds, int) or seconds < 0:
                    raise DomainError("响应时长必须是非负整数秒")
                if not response.get("request"):
                    raise DomainError("响应记录必须说明呼叫/请求事项")
                normalized_responses.append(
                    {"request": response["request"], "response_seconds": seconds}
                )
            record["staff_responses"] = normalized_responses

        if DIMENSION_PRICE in assessed:
            if not isinstance(price, dict) or "disclosed_upfront" not in price:
                raise DomainError("价格披露必须明确 disclosed_upfront")
            record["price"] = {
                "disclosed_upfront": bool(price["disclosed_upfront"]),
                "detail": price.get("detail", ""),
            }

        if not isinstance(dropout, dict) or "dropped_out" not in dropout:
            raise DomainError("每次试用必须记录中途退出情况")
        reason = dropout.get("reason")
        if dropout["dropped_out"]:
            if reason not in DROPOUT_REASONS:
                raise DomainError("中途退出原因必须是 safety/accessibility/price/personal/other")
        record["dropout"] = {
            "dropped_out": bool(dropout["dropped_out"]),
            "reason": reason,
            "stage": dropout.get("stage", ""),
            "note": dropout.get("note", ""),
        }

        self.sessions[record["id"]] = record
        self._append_ledger(plan_id, "evidence", record["id"], record)
        return record

    def add_feedback(
        self,
        session_id: str,
        participant_id: str,
        text: str,
        correction_ref: str | None = None,
    ) -> dict:
        session = self._get(self.sessions, session_id, "证据记录")
        participant = self._get(self.participants, participant_id, "参与者")
        if participant_id != session["participant_id"]:
            raise DomainError("反馈必须来自参与该次试用的本人", status=403)
        if not text or not text.strip():
            raise DomainError("原始反馈内容不能为空")
        if correction_ref:
            original = self._get(self.feedback, correction_ref, "原反馈")
            if original["session_id"] != session_id:
                raise DomainError("更正反馈必须对应同一次试用")
        record = {
            "id": self._next_id("f"),
            "session_id": session_id,
            "plan_id": session["plan_id"],
            "participant_id": participant_id,
            "text": text.strip(),
            "correction_ref": correction_ref,
            "created_at": now_iso(),
        }
        self.feedback[record["id"]] = record
        self._append_ledger(session["plan_id"], "feedback", record["id"], record)
        return record

    # -- 缺陷与例外 ---------------------------------------------------------

    def add_defect(
        self,
        reporter_id: str,
        plan_id: str,
        level_id: str,
        dimension: str,
        description: str,
        severity: str,
        stratum: dict | None = None,
        version: int | None = None,
        required_action: str = "",
        session_id: str | None = None,
    ) -> dict:
        plan = self._plan(plan_id)
        version = version or plan["current_version"]
        self._level(plan, version, level_id)
        if dimension not in CORE_DIMENSIONS and dimension != "dropout":
            raise DomainError("缺陷维度非法")
        if severity not in DEFECT_SEVERITIES:
            raise DomainError("缺陷等级必须是 minor/major/blocking")
        if not description:
            raise DomainError("缺陷描述不能为空")
        stratum_key = canonical_stratum(stratum) if stratum else None
        if session_id:
            session = self._get(self.sessions, session_id, "证据记录")
            if session["plan_id"] != plan_id or session["level_id"] != level_id:
                raise DomainError("关联证据与方案/等级不一致")
        defect = {
            "id": self._next_id("d"),
            "plan_id": plan_id,
            "level_id": level_id,
            "version": version,
            "dimension": dimension,
            "severity": severity,
            "description": description,
            "required_action": required_action,
            "stratum": stratum_dict(stratum_key) if stratum_key else None,
            "session_id": session_id,
            "reported_by": reporter_id,
            "created_at": now_iso(),
            "status": "open",
            "resolved_at": None,
            "resolved_by_reverification": None,
        }
        self.defects[defect["id"]] = defect
        return defect

    def add_exception(
        self,
        decider_id: str,
        defect_id: str,
        justification: str,
        compensating_measure: str,
        valid_until: str | None = None,
    ) -> dict:
        defect = self._get(self.defects, defect_id, "缺陷")
        if defect["status"] != "open":
            raise DomainError("只能对未关闭缺陷作出例外决定", status=409)
        if defect["severity"] == "blocking":
            raise DomainError("阻断性缺陷涉及安全与诚信，不得通过例外放行")
        if not justification or not compensating_measure:
            raise DomainError("例外决定必须说明理由与补偿措施")
        for existing in self.exceptions.values():
            if existing["defect_id"] == defect_id and existing["status"] == "active":
                raise DomainError("该缺陷已有有效例外决定", status=409)
        if valid_until is None:
            valid_until = (
                datetime.now(timezone.utc) + timedelta(days=EXCEPTION_DEFAULT_DAYS)
            ).isoformat()
        exception = {
            "id": self._next_id("x"),
            "plan_id": defect["plan_id"],
            "level_id": defect["level_id"],
            "defect_id": defect_id,
            "decider_id": decider_id,
            "justification": justification,
            "compensating_measure": compensating_measure,
            "created_at": now_iso(),
            "valid_until": valid_until,
            "status": "active",
        }
        self.exceptions[exception["id"]] = exception
        return exception

    def _exception_covers(self, defect: dict) -> dict | None:
        for exception in self.exceptions.values():
            if (
                exception["defect_id"] == defect["id"]
                and exception["status"] == "active"
                and exception["valid_until"] >= now_iso()
            ):
                return exception
        return None

    # -- 严重事件、暂停、整改复验 -------------------------------------------

    def _revoke(self, decision_id: str, reason: str, ref_id: str):
        """以追加记录撤销证书效力，绝不改写原决定。"""
        decision = self.decisions[decision_id]
        if any(
            r["decision_id"] == decision_id for r in self.revocations.values()
        ):
            return
        record = {
            "id": self._next_id("rvk"),
            "plan_id": decision["plan_id"],
            "level_id": decision["level_id"],
            "decision_id": decision_id,
            "reason": reason,
            "ref_id": ref_id,
            "created_at": now_iso(),
        }
        self.revocations[record["id"]] = record
        self._append_ledger(decision["plan_id"], "revocation", record["id"], record)

    def report_incident(
        self, reporter_id: str, plan_id: str, level_id: str, description: str
    ) -> dict:
        plan = self._plan(plan_id)
        self._level(plan, plan["current_version"], level_id)
        if not description:
            raise DomainError("严重事件描述不能为空")
        incident = {
            "id": self._next_id("i"),
            "plan_id": plan_id,
            "level_id": level_id,
            "description": description,
            "reported_by": reporter_id,
            "created_at": now_iso(),
            "status": "open",
            "resolved_at": None,
            "resolved_by_reverification": None,
        }
        self.incidents[incident["id"]] = incident
        # 立即暂停相关等级，与事件记录在同一操作内生效。
        plan["suspensions"][level_id] = {
            "incident_id": incident["id"],
            "since": now_iso(),
            "lifted_at": None,
            "lifted_by_reverification": None,
        }
        # 现有证书随即失效：即便整改复验解除暂停，也须重新作出认证决定，
        # 不能把暂停前的证书自动恢复。撤销以追加记录留痕。
        for decision in list(self.decisions.values()):
            if (
                decision["plan_id"] == plan_id
                and decision["level_id"] == level_id
                and decision["outcome"] == "certified"
            ):
                self._revoke(decision["id"], "incident", incident["id"])
        self._append_ledger(plan_id, "incident", incident["id"], incident)
        return incident

    def create_rectification(
        self,
        supplier_id: str,
        plan_id: str,
        level_id: str,
        summary: str,
        defect_ids: list | None = None,
        incident_id: str | None = None,
    ) -> dict:
        plan = self._plan(plan_id)
        self._require_owner(plan, supplier_id)
        self._level(plan, plan["current_version"], level_id)
        if not summary:
            raise DomainError("整改说明不能为空")
        defect_ids = defect_ids or []
        for defect_id in defect_ids:
            defect = self._get(self.defects, defect_id, "缺陷")
            if defect["plan_id"] != plan_id or defect["level_id"] != level_id:
                raise DomainError(f"缺陷 {defect_id} 与本等级不一致")
            if defect["status"] != "open":
                raise DomainError(f"缺陷 {defect_id} 已关闭", status=409)
        if incident_id:
            incident = self._get(self.incidents, incident_id, "严重事件")
            if incident["plan_id"] != plan_id or incident["level_id"] != level_id:
                raise DomainError("严重事件与本等级不一致")
            if incident["status"] != "open":
                raise DomainError("该严重事件已关闭", status=409)
        record = {
            "id": self._next_id("r"),
            "plan_id": plan_id,
            "level_id": level_id,
            "summary": summary,
            "defect_ids": defect_ids,
            "incident_id": incident_id,
            "submitted_by": supplier_id,
            "created_at": now_iso(),
            "status": "pending_reverification",
        }
        self.rectifications[record["id"]] = record
        return record

    def add_reverification(
        self, reviewer_id: str, rectification_id: str, passed: bool, notes: str
    ) -> dict:
        rectification = self._get(self.rectifications, rectification_id, "整改报告")
        if rectification["status"] != "pending_reverification":
            raise DomainError("该整改已经完成复验", status=409)
        record = {
            "id": self._next_id("rv"),
            "rectification_id": rectification_id,
            "plan_id": rectification["plan_id"],
            "level_id": rectification["level_id"],
            "passed": bool(passed),
            "notes": notes or "",
            "reviewed_by": reviewer_id,
            "created_at": now_iso(),
        }
        self.reverifications[record["id"]] = record
        if not passed:
            rectification["status"] = "reverification_failed"
            return record

        rectification["status"] = "reverification_passed"
        for defect_id in rectification["defect_ids"]:
            defect = self.defects[defect_id]
            defect["status"] = "resolved"
            defect["resolved_at"] = now_iso()
            defect["resolved_by_reverification"] = record["id"]
        if rectification["incident_id"]:
            incident = self.incidents[rectification["incident_id"]]
            incident["status"] = "resolved"
            incident["resolved_at"] = now_iso()
            incident["resolved_by_reverification"] = record["id"]
            suspension = self.plans[rectification["plan_id"]]["suspensions"].get(
                rectification["level_id"]
            )
            # 只有对应事件的复验通过才能解除暂停。
            if suspension and suspension["incident_id"] == incident["id"]:
                suspension["lifted_at"] = now_iso()
                suspension["lifted_by_reverification"] = record["id"]
                self.plans[rectification["plan_id"]]["suspensions"].pop(
                    rectification["level_id"]
                )
        return record

    # -- 覆盖计算与证据复用 --------------------------------------------------

    def _dimension_passes(self, session: dict, dimension: str, limit: int) -> bool:
        if dimension not in session["dimensions"]:
            return False
        if dimension == DIMENSION_TASK:
            return all(task["completed"] for task in session["key_tasks"])
        if dimension == DIMENSION_ACCESSIBILITY:
            return all(b["severity"] != "high" for b in session["accessibility_barriers"])
        if dimension == DIMENSION_STAFF_RESPONSE:
            return all(
                response["response_seconds"] <= limit
                for response in session["staff_responses"]
            )
        if dimension == DIMENSION_PRICE:
            return session["price"]["disclosed_upfront"]
        return False

    def _affected_between(self, plan: dict, from_version: int, to_version: int) -> set:
        affected = set()
        for ver in plan["versions"]:
            if from_version < ver["version"] <= to_version:
                affected.update(ver["affected_dimensions"])
        return affected

    def compute_coverage(self, plan_id: str, level_id: str, version: int | None = None) -> dict:
        plan = self._plan(plan_id)
        version = version or plan["current_version"]
        level = self._level(plan, version, level_id)
        limit = level["staff_response_limit_seconds"]
        min_participants = level["min_participants"]
        sessions = [
            s for s in self.sessions.values()
            if s["plan_id"] == plan_id and s["level_id"] == level_id
        ]

        strata_matrix = []
        for raw_stratum in level["claimed_strata"]:
            stratum_key = canonical_stratum(raw_stratum)
            dimension_results = {}
            for dimension in CORE_DIMENSIONS:
                entries = []
                for session in sessions:
                    session_key = canonical_stratum(session["stratum"])
                    if session_key != stratum_key:
                        continue
                    if not self._valid_consent(session["participant_id"], plan_id):
                        continue  # 同意撤回后证据保留可查，但不再计入覆盖或复用
                    if dimension not in session["dimensions"]:
                        continue
                    if not self._dimension_passes(session, dimension, limit):
                        continue
                    if session["version"] == version:
                        reused = False
                    elif session["version"] < version:
                        if dimension in self._affected_between(plan, session["version"], version):
                            continue
                        reused = True
                    else:
                        continue
                    entries.append({
                        "session_id": session["id"],
                        "participant_id": session["participant_id"],
                        "source_version": session["version"],
                        "reused": reused,
                    })
                participant_ids = {e["participant_id"] for e in entries}
                dimension_results[dimension] = {
                    "label": DIMENSION_LABELS[dimension],
                    "passing_participants": len(participant_ids),
                    "required_participants": min_participants,
                    "passed": len(participant_ids) >= min_participants,
                    "entries": sorted(entries, key=lambda e: (not e["reused"], e["session_id"])),
                }
            safety_dropouts = [
                {
                    "session_id": s["id"],
                    "participant_id": s["participant_id"],
                    "version": s["version"],
                    "stage": s["dropout"].get("stage", ""),
                    "note": s["dropout"].get("note", ""),
                }
                for s in sessions
                if canonical_stratum(s["stratum"]) == stratum_key
                and s["version"] == version
                and s["dropout"]["dropped_out"]
                and s["dropout"]["reason"] == "safety"
            ]
            strata_matrix.append({
                "stratum": stratum_dict(stratum_key),
                "stratum_label": stratum_label(stratum_key),
                "dimensions": dimension_results,
                "safety_dropouts": safety_dropouts,
            })
        return {
            "plan_id": plan_id,
            "level_id": level_id,
            "version": version,
            "required_participants": min_participants,
            "strata": strata_matrix,
        }

    def reuse_report(self, plan_id: str, level_id: str, version: int | None = None) -> dict:
        plan = self._plan(plan_id)
        version = version or plan["current_version"]
        coverage = self.compute_coverage(plan_id, level_id, version)
        affected = sorted(self._affected_between(plan, version - 1, version)) if version > 1 else []
        strata_out = []
        for stratum_result in coverage["strata"]:
            dims_out = {}
            retest = []
            for dimension, result in stratum_result["dimensions"].items():
                reusable = [e for e in result["entries"] if e["reused"]]
                fresh = [e for e in result["entries"] if not e["reused"]]
                needs_retest = not result["passed"]
                if needs_retest:
                    retest.append(DIMENSION_LABELS[dimension])
                dims_out[dimension] = {
                    "label": DIMENSION_LABELS[dimension],
                    "reused": reusable,
                    "new_evidence": fresh,
                    "required_participants": result["required_participants"],
                    "passing_participants": result["passing_participants"],
                    "retest_required": needs_retest,
                }
            strata_out.append({
                "stratum": stratum_result["stratum"],
                "stratum_label": stratum_result["stratum_label"],
                "needs_retest_dimensions": retest,
                "dimensions": dims_out,
            })
        return {
            "plan_id": plan_id,
            "level_id": level_id,
            "version": version,
            "affected_dimensions": affected,
            "strata": strata_out,
        }

    def _verified_capabilities(self, plan: dict, level_id: str, version: int) -> list:
        """能力只有在每个适用分层都有足够参与者独立完成时才算“已验证”。"""
        coverage = self.compute_coverage(plan["id"], level_id, version)
        level = self._level(plan, version, level_id)
        sessions_by_id = self.sessions
        capabilities = {}
        for stratum_result in coverage["strata"]:
            key = canonical_stratum(stratum_result["stratum"])
            task_entries = stratum_result["dimensions"][DIMENSION_TASK]["entries"]
            completed = {}  # capability -> set(participant)
            for entry in task_entries:
                session = sessions_by_id[entry["session_id"]]
                if DIMENSION_TASK not in session["dimensions"]:
                    continue
                for task in session["key_tasks"]:
                    if task["completed"]:
                        completed.setdefault(task["capability"], set()).add(
                            entry["participant_id"]
                        )
            for capability, participants in completed.items():
                if len(participants) >= level["min_participants"]:
                    capabilities.setdefault(capability, set()).add(key)
        claimed = {canonical_stratum(s) for s in level["claimed_strata"]}
        result = []
        for capability, covered_strata in sorted(capabilities.items()):
            if covered_strata >= claimed:
                result.append(capability)
        return result

    # -- 认证决定 -----------------------------------------------------------

    def _active_certificate(self, plan: dict, level_id: str) -> dict | None:
        revoked = {
            r["decision_id"] for r in self.revocations.values()
            if r["plan_id"] == plan["id"] and r["level_id"] == level_id
        }
        candidates = [
            d for d in self.decisions.values()
            if d["plan_id"] == plan["id"]
            and d["level_id"] == level_id
            and d["outcome"] == "certified"
            and d["id"] not in revoked
        ]
        return candidates[-1] if candidates else None

    def evaluate_decision(
        self, plan_id: str, level_id: str, reviewer_ids: list, version: int | None = None
    ) -> dict:
        plan = self._plan(plan_id)
        version = version or plan["current_version"]
        level = self._level(plan, version, level_id)
        if not reviewer_ids or any(not r for r in reviewer_ids):
            raise DomainError("认证决定必须记录评审人")

        coverage = self.compute_coverage(plan_id, level_id, version)
        reasons = []

        if level_id in plan["suspensions"]:
            incident_id = plan["suspensions"][level_id]["incident_id"]
            reasons.append(f"该等级因严重事件 {incident_id} 已暂停，须整改复验通过后方可认证")

        for stratum_result in coverage["strata"]:
            label = stratum_result["stratum_label"]
            for dimension, result in stratum_result["dimensions"].items():
                if not result["passed"]:
                    reasons.append(
                        f"分层「{label}」缺少{result['label']}有效证据："
                        f"{result['passing_participants']}/{result['required_participants']} 名参与者"
                    )
            for dropout in stratum_result["safety_dropouts"]:
                reasons.append(
                    f"分层「{label}」发生因安全原因中途退出（证据 {dropout['session_id']}），不得通过"
                )

        open_defects = [
            d for d in self.defects.values()
            if d["plan_id"] == plan_id
            and d["level_id"] == level_id
            and d["status"] == "open"
        ]
        blocking = [d for d in open_defects if d["severity"] == "blocking"]
        major_unexcepted = []
        accepted_exceptions = []
        for defect in open_defects:
            if defect["severity"] == "major":
                exception = self._exception_covers(defect)
                if exception:
                    accepted_exceptions.append({
                        "defect_id": defect["id"],
                        "exception_id": exception["id"],
                        "decider_id": exception["decider_id"],
                        "justification": exception["justification"],
                        "valid_until": exception["valid_until"],
                    })
                else:
                    major_unexcepted.append(defect)
        for defect in blocking:
            reasons.append(f"存在未关闭的阻断性缺陷 {defect['id']}：{defect['description']}")
        for defect in major_unexcepted:
            reasons.append(f"存在未关闭且无有效例外的重要缺陷 {defect['id']}：{defect['description']}")

        outcome = "certified" if not reasons else "denied"
        decision = {
            "id": self._next_id("dc"),
            "plan_id": plan_id,
            "level_id": level_id,
            "version": version,
            "outcome": outcome,
            "reasons": reasons,
            "reviewer_ids": list(reviewer_ids),
            "coverage": coverage,
            "accepted_exceptions": accepted_exceptions,
            "open_blocking_defect_ids": [d["id"] for d in blocking],
            "created_at": now_iso(),
            "certificate": None,
        }
        if outcome == "certified":
            valid_from = datetime.now(timezone.utc)
            certificate = {
                "id": self._next_id("cert"),
                "version": version,
                "level_id": level_id,
                "valid_from": valid_from.isoformat(),
                "valid_until": (
                    valid_from + timedelta(days=CERTIFICATE_VALIDITY_DAYS)
                ).isoformat(),
                "applicable_strata": [
                    s["stratum_label"] for s in coverage["strata"]
                ],
                "verified_capabilities": self._verified_capabilities(plan, level_id, version),
            }
            decision["certificate"] = certificate
            # 同一等级旧版本或旧决定的证书即被取代（追加撤销记录）。
            previous_active = self._active_certificate(plan, level_id)
            if previous_active:
                self._revoke(previous_active["id"], "new_decision", decision["id"])
        self.decisions[decision["id"]] = decision
        self._append_ledger(plan_id, "decision", decision["id"], decision)
        return decision

    # -- 申诉（强制回避） ----------------------------------------------------

    def create_appeal(
        self, appellant_id: str, subject_type: str, subject_id: str, reason: str
    ) -> dict:
        if subject_type not in ("decision", "incident", "defect"):
            raise DomainError("申诉对象必须是 decision/incident/defect")
        if not reason:
            raise DomainError("申诉理由不能为空")
        if subject_type == "decision":
            subject = self._get(self.decisions, subject_id, "认证决定")
        elif subject_type == "incident":
            subject = self._get(self.incidents, subject_id, "严重事件")
        else:
            subject = self._get(self.defects, subject_id, "缺陷")
        plan = self._plan(subject["plan_id"])
        self._require_owner(plan, appellant_id)
        appeal = {
            "id": self._next_id("ap"),
            "plan_id": subject["plan_id"],
            "subject_type": subject_type,
            "subject_id": subject_id,
            "appellant_id": appellant_id,
            "reason": reason,
            "status": "open",
            "ruling_by": None,
            "ruling": None,
            "ruling_notes": None,
            "ruled_at": None,
            "created_at": now_iso(),
        }
        self.appeals[appeal["id"]] = appeal
        return appeal

    def _original_reviewer_ids(self, appeal: dict) -> set:
        if appeal["subject_type"] == "decision":
            return set(self.decisions[appeal["subject_id"]]["reviewer_ids"])
        if appeal["subject_type"] == "incident":
            return {self.incidents[appeal["subject_id"]]["reported_by"]}
        return {self.defects[appeal["subject_id"]]["reported_by"]}

    def rule_appeal(self, reviewer_id: str, appeal_id: str, upheld: bool, notes: str) -> dict:
        appeal = self._get(self.appeals, appeal_id, "申诉")
        if appeal["status"] != "open":
            raise DomainError("该申诉已经裁决", status=409)
        forbidden = self._original_reviewer_ids(appeal)
        if reviewer_id in forbidden:
            raise DomainError(
                "裁决人参与过原评审/原记录，必须回避，请换未参与原评审的人员处理",
                status=409,
            )
        appeal["status"] = "upheld" if upheld else "rejected"
        appeal["ruling_by"] = reviewer_id
        appeal["ruling"] = "upheld" if upheld else "rejected"
        appeal["ruling_notes"] = notes or ""
        appeal["ruled_at"] = now_iso()
        # 安全底线：即便申诉成立，暂停仍须以整改复验通过为解除条件。
        return appeal

    # -- 三类视图 -----------------------------------------------------------

    def consumer_view(self, plan_id: str) -> dict:
        plan = self._plan(plan_id)
        current = plan["current_version"]
        levels_out = []
        for level in self._version(plan, current)["levels"]:
            level_id = level["id"]
            entry = {
                "level_id": level_id,
                "name": level["name"],
                "status": "uncertified",
                "applicable_strata": None,
                "valid_until": None,
                "verified_capabilities": [],
            }
            if level_id in plan["suspensions"]:
                entry["status"] = "suspended"
            else:
                certificate_decision = self._active_certificate(plan, level_id)
                if (
                    certificate_decision
                    and certificate_decision["version"] == current
                    and certificate_decision["certificate"]
                ):
                    cert = certificate_decision["certificate"]
                    if cert["valid_until"] < now_iso():
                        entry["status"] = "expired"
                    else:
                        entry["status"] = "certified"
                        entry["applicable_strata"] = cert["applicable_strata"]
                        entry["valid_until"] = cert["valid_until"]
                        entry["verified_capabilities"] = cert["verified_capabilities"]
            levels_out.append(entry)
        return {
            "plan_id": plan_id,
            "name": plan["name"],
            "current_version": current,
            "levels": levels_out,
            "notice": "本信息仅反映分层体验证据的认证结论，不等于一次活动的满意度评价",
        }

    def supplier_view(self, plan_id: str, user_id: str) -> dict:
        plan = self._plan(plan_id)
        self._require_owner(plan, user_id)
        current = plan["current_version"]
        defects_out = []
        for defect in self.defects.values():
            if defect["plan_id"] != plan_id:
                continue
            exception = self._exception_covers(defect)
            defects_out.append({
                # 脱敏：不提供参与者、证据、评审人等可回溯到个人的标识。
                "id": defect["id"],
                "level_id": defect["level_id"],
                "version": defect["version"],
                "dimension": DIMENSION_LABELS.get(defect["dimension"], defect["dimension"]),
                "stratum": stratum_label(canonical_stratum(defect["stratum"]))
                if defect["stratum"] else None,
                "severity": defect["severity"],
                "description": defect["description"],
                "required_action": defect["required_action"],
                "status": defect["status"],
                "exception_active": bool(exception),
            })
        rectifications_out = [
            {
                "id": r["id"],
                "level_id": r["level_id"],
                "summary": r["summary"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in self.rectifications.values()
            if r["plan_id"] == plan_id
        ]
        return {
            "plan_id": plan_id,
            "name": plan["name"],
            "current_version": current,
            "suspended_levels": sorted(plan["suspensions"].keys()),
            "defects": defects_out,
            "rectifications": rectifications_out,
        }

    def certifier_view(self, plan_id: str) -> dict:
        plan = self._plan(plan_id)
        current = plan["current_version"]
        levels_out = []
        for level in self._version(plan, current)["levels"]:
            coverage = self.compute_coverage(plan_id, level["id"], current)
            certificate_decision = self._active_certificate(plan, level["id"])
            levels_out.append({
                "level_id": level["id"],
                "name": level["name"],
                "plan_version": current,
                "suspended": level["id"] in plan["suspensions"],
                "suspension": plan["suspensions"].get(level["id"]),
                "representative_coverage": coverage,
                "reuse_report": self.reuse_report(plan_id, level["id"], current),
                "active_certificate_decision_id": certificate_decision["id"]
                if certificate_decision else None,
            })
        return {
            "plan_id": plan_id,
            "name": plan["name"],
            "supplier_id": plan["supplier_id"],
            "current_version": current,
            "versions": plan["versions"],
            "levels": levels_out,
            "participants": [p for p in self.participants.values()],
            "consents": [c for c in self.consents.values() if c["plan_id"] == plan_id],
            "evidence": [s for s in self.sessions.values() if s["plan_id"] == plan_id],
            "feedback": [f for f in self.feedback.values() if f["plan_id"] == plan_id],
            "defects": [d for d in self.defects.values() if d["plan_id"] == plan_id],
            "exceptions": [e for e in self.exceptions.values() if e["plan_id"] == plan_id],
            "incidents": [i for i in self.incidents.values() if i["plan_id"] == plan_id],
            "rectifications": [
                r for r in self.rectifications.values() if r["plan_id"] == plan_id
            ],
            "reverifications": [
                v for v in self.reverifications.values() if v["plan_id"] == plan_id
            ],
            "decisions": [d for d in self.decisions.values() if d["plan_id"] == plan_id],
            "revocations": [r for r in self.revocations.values() if r["plan_id"] == plan_id],
            "appeals": [a for a in self.appeals.values() if a["plan_id"] == plan_id],
            "ledger_verification": self.verify_ledger(plan_id),
        }

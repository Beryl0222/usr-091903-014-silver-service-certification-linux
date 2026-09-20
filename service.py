"""银发服务体验认证的 HTTP 入口。

除保留稳定的 ``/health`` 身份检查外，``/api/*`` 暴露证据平台的领域接口。
调用方通过请求头表明身份：

* ``X-User-Id``：操作人标识（供应商/认证人员/参与者）；
* ``X-User-Role``：``supplier`` / ``certifier`` / ``participant``。

原始反馈只允许参与者本人追加；供应商对证据与反馈没有任何写入入口。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from certification import DomainError, EvidenceStore

SERVICE_ID = "silver-service-certification"
SERVICE_NAME = "银发服务体验认证"

STORE = EvidenceStore()
ALLOW_RESET = os.environ.get("ALLOW_RESET", "") == "1"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """提供健康检查与证据平台领域接口。"""

    store = STORE

    # -- 基础收发 -----------------------------------------------------------

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("请求体必须是 UTF-8 JSON")
        if not isinstance(payload, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return payload

    def _identity(self):
        user_id = self.headers.get("X-User-Id", "").strip()
        role = self.headers.get("X-User-Role", "").strip()
        if not user_id:
            raise DomainError("缺少操作人标识 X-User-Id", status=401)
        if role not in ("supplier", "certifier", "participant"):
            raise DomainError("X-User-Role 必须是 supplier/certifier/participant", status=401)
        return user_id, role

    def _require_role(self, *roles):
        user_id, role = self._identity()
        if role not in roles:
            raise DomainError(f"当前角色 {role} 无权执行此操作", status=403)
        return user_id, role

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                self._send_json(health_payload())
                return
            match = _match(parsed.path)
            if not match:
                self.send_error(404)
                return
            name, kwargs = match
            if name == "view":
                if kwargs["view"] == "consumer":
                    self._identity()
                    self._send_json(self.store.consumer_view(kwargs["plan_id"]))
                elif kwargs["view"] == "supplier":
                    user_id, _ = self._identity()
                    self._send_json(self.store.supplier_view(kwargs["plan_id"], user_id))
                elif kwargs["view"] == "certifier":
                    self._require_role("certifier")
                    self._send_json(self.store.certifier_view(kwargs["plan_id"]))
                else:
                    self.send_error(404)
            elif name in ("coverage", "reuse"):
                query = parse_qs(parsed.query)
                version = _query_int(query, "version")
                self._require_role("certifier")
                if name == "coverage":
                    self._send_json(
                        self.store.compute_coverage(
                            kwargs["plan_id"], kwargs["level_id"], version
                        )
                    )
                else:
                    self._send_json(
                        self.store.reuse_report(
                            kwargs["plan_id"], kwargs["level_id"], version
                        )
                    )
            else:
                raise DomainError("该路径不支持 GET", status=405)
        except DomainError as error:
            self._send_json({"error": str(error)}, status=error.status)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/internal/reset":
                if not ALLOW_RESET:
                    raise DomainError("重置接口未启用", status=404)
                self.store.reset()
                self._send_json({"status": "reset"})
                return
            match = _match(parsed.path)
            if not match:
                self.send_error(404)
                return
            name, kwargs = match
            data = self._read_json()
            self._send_json(self._dispatch(name, kwargs, data))
        except DomainError as error:
            self._send_json({"error": str(error)}, status=error.status)

    def _dispatch(self, name, kwargs, data):
        store = self.store
        if name == "create_plan":
            user_id, _ = self._require_role("supplier")
            return store.create_plan(
                supplier_id=user_id,
                name=data.get("name", ""),
                levels=data.get("levels", []),
            )
        if name == "create_version":
            user_id, _ = self._require_role("supplier")
            return store.create_version(
                supplier_id=user_id,
                plan_id=kwargs["plan_id"],
                change_summary=data.get("change_summary", ""),
                change_types=data.get("change_types", []),
                affected_dimensions=data.get("affected_dimensions", []),
                levels=data.get("levels"),
            )
        if name == "create_participant":
            self._require_role("certifier", "participant")
            return store.create_participant(stratum=data["stratum"])
        if name == "grant_consent":
            self._identity()
            return store.grant_consent(
                participant_id=kwargs["participant_id"],
                plan_id=data["plan_id"],
            )
        if name == "withdraw_consent":
            self._identity()
            return store.withdraw_consent(consent_id=kwargs["consent_id"])
        if name == "add_evidence":
            self._require_role("certifier")
            return store.add_evidence(
                plan_id=kwargs["plan_id"],
                version=data.get("version"),
                level_id=data["level_id"],
                participant_id=data["participant_id"],
                dimensions=data.get("dimensions"),
                key_tasks=data.get("key_tasks"),
                accessibility_barriers=data.get("accessibility_barriers"),
                staff_responses=data.get("staff_responses"),
                price=data.get("price"),
                dropout=data.get("dropout"),
            )
        if name == "add_feedback":
            user_id, role = self._identity()
            claimed_id = data.get("participant_id", user_id)
            if role != "participant" or user_id != claimed_id:
                raise DomainError("原始反馈只能由参与者本人提交", status=403)
            return store.add_feedback(
                session_id=kwargs["session_id"],
                participant_id=user_id,
                text=data.get("text", ""),
                correction_ref=data.get("correction_ref"),
            )
        if name == "add_defect":
            user_id, _ = self._require_role("certifier")
            return store.add_defect(
                reporter_id=user_id,
                plan_id=kwargs["plan_id"],
                level_id=data["level_id"],
                dimension=data["dimension"],
                description=data.get("description", ""),
                severity=data["severity"],
                stratum=data.get("stratum"),
                version=data.get("version"),
                required_action=data.get("required_action", ""),
                session_id=data.get("session_id"),
            )
        if name == "add_exception":
            user_id, _ = self._require_role("certifier")
            return store.add_exception(
                decider_id=user_id,
                defect_id=data["defect_id"],
                justification=data.get("justification", ""),
                compensating_measure=data.get("compensating_measure", ""),
                valid_until=data.get("valid_until"),
            )
        if name == "report_incident":
            user_id, _ = self._require_role("certifier")
            return store.report_incident(
                reporter_id=user_id,
                plan_id=kwargs["plan_id"],
                level_id=data["level_id"],
                description=data.get("description", ""),
            )
        if name == "create_rectification":
            user_id, _ = self._require_role("supplier")
            return store.create_rectification(
                supplier_id=user_id,
                plan_id=kwargs["plan_id"],
                level_id=data["level_id"],
                summary=data.get("summary", ""),
                defect_ids=data.get("defect_ids", []),
                incident_id=data.get("incident_id"),
            )
        if name == "add_reverification":
            user_id, _ = self._require_role("certifier")
            return store.add_reverification(
                reviewer_id=user_id,
                rectification_id=data["rectification_id"],
                passed=bool(data.get("passed")),
                notes=data.get("notes", ""),
            )
        if name == "evaluate_decision":
            user_id, _ = self._require_role("certifier")
            reviewer_ids = data.get("reviewer_ids")
            if not isinstance(reviewer_ids, list) or not reviewer_ids:
                raise DomainError("必须提供评审人列表 reviewer_ids")
            if user_id not in reviewer_ids:
                reviewer_ids = [user_id] + list(reviewer_ids)
            return store.evaluate_decision(
                plan_id=kwargs["plan_id"],
                level_id=data["level_id"],
                reviewer_ids=reviewer_ids,
                version=data.get("version"),
            )
        if name == "create_appeal":
            user_id, _ = self._require_role("supplier")
            return store.create_appeal(
                appellant_id=user_id,
                subject_type=data["subject_type"],
                subject_id=data["subject_id"],
                reason=data.get("reason", ""),
            )
        if name == "rule_appeal":
            user_id, _ = self._require_role("certifier")
            return store.rule_appeal(
                reviewer_id=user_id,
                appeal_id=kwargs["appeal_id"],
                upheld=bool(data.get("upheld")),
                notes=data.get("notes", ""),
            )
        raise DomainError("该路径不支持 POST", status=405)

    def log_message(self, *_args):
        return


# 固定路由，避免引入第三方 Web 框架。
_ROUTES = (
    ("POST", "/api/plans", "create_plan"),
    ("POST", "/api/plans/{plan_id}/versions", "create_version"),
    ("POST", "/api/participants", "create_participant"),
    ("POST", "/api/participants/{participant_id}/consent", "grant_consent"),
    ("POST", "/api/consents/{consent_id}/withdraw", "withdraw_consent"),
    ("POST", "/api/plans/{plan_id}/evidence", "add_evidence"),
    ("POST", "/api/evidence/{session_id}/feedback", "add_feedback"),
    ("POST", "/api/plans/{plan_id}/defects", "add_defect"),
    ("POST", "/api/exceptions", "add_exception"),
    ("POST", "/api/plans/{plan_id}/incidents", "report_incident"),
    ("POST", "/api/plans/{plan_id}/rectifications", "create_rectification"),
    ("POST", "/api/reverifications", "add_reverification"),
    ("POST", "/api/plans/{plan_id}/decisions", "evaluate_decision"),
    ("POST", "/api/appeals", "create_appeal"),
    ("POST", "/api/appeals/{appeal_id}/rule", "rule_appeal"),
    ("GET", "/api/plans/{plan_id}/{view}", "view"),
    (
        "GET",
        "/api/plans/{plan_id}/levels/{level_id}/coverage",
        "coverage",
    ),
    (
        "GET",
        "/api/plans/{plan_id}/levels/{level_id}/reuse",
        "reuse",
    ),
)


def _match(path: str):
    segments = [s for s in path.split("/") if s]
    for method, pattern, name in _ROUTES:
        pattern_segments = [s for s in pattern.split("/") if s]
        if len(segments) != len(pattern_segments):
            continue
        kwargs = {}
        for actual, expected in zip(segments, pattern_segments):
            if expected.startswith("{") and expected.endswith("}"):
                kwargs[expected[1:-1]] = actual
            elif actual != expected:
                break
        else:
            return name, kwargs
    return None


def _query_int(query, key):
    if key not in query:
        return None
    try:
        return int(query[key][0])
    except ValueError:
        raise DomainError(f"查询参数 {key} 必须是整数")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert isinstance(STORE, EvidenceStore)
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

"""银发服务体验认证的运行入口。

提供 /health 稳定身份检查，以及服务体验证据平台的领域接口。
领域规则见 evidence_platform.py。
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evidence_platform import (
    AGE_BANDS, ASSISTANCE, FACETS, HEALTH, LIVING, EvidencePlatform,
    PlatformError,
)

SERVICE_ID = "silver-service-certification"
SERVICE_NAME = "银发服务体验认证"

# 演示/联调用单例存储；进程内加锁，重启即清空。
STORE_LOCK = threading.Lock()
PLATFORM = EvidencePlatform()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def reference_payload():
    """分层与方案面词表，供接入方核对取值。"""
    return {
        "age_bands": AGE_BANDS, "health_status": HEALTH,
        "living": LIVING, "assistance": ASSISTANCE, "facets": FACETS,
    }


# method, path-prefix -> (handler_name, 是否带尾部 id)
class Handler(BaseHTTPRequestHandler):
    """健康检查 + 证据平台 JSON 接口。"""

    server_version = "SilverCert/1.0"

    # -- 基础 --------------------------------------------------------------

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise PlatformError("请求体不是合法 JSON", status=400,
                                code="bad_json")
        if not isinstance(data, dict):
            raise PlatformError("请求体必须是 JSON 对象", status=400,
                                code="bad_request")
        return data

    def do_GET(self):
        try:
            self._dispatch_get()
        except PlatformError as exc:
            self._send_json(exc.status,
                            {"error": str(exc), "code": exc.code})

    def _dispatch_get(self):
        if self.path == "/health":
            self._send_json(200, health_payload())
            return
        if self.path == "/reference":
            self._send_json(200, reference_payload())
            return
        if self.path.startswith("/plans/") and self.path.endswith("/reuse"):
            plan_id = self.path[len("/plans/"):-len("/reuse")]
            self._call(lambda: PLATFORM.reuse_report(plan_id))
            return
        if self.path.startswith("/certifications/") and self.path.endswith("/trace"):
            cert_id = self.path[len("/certifications/"):-len("/trace")]
            self._call(lambda: PLATFORM.certifier_view(cert_id))
            return
        if self.path == "/views/consumer":
            self._call(lambda: PLATFORM.consumer_view())
            return
        self.send_error(404)

    def do_POST(self):
        try:
            self._dispatch_post()
        except PlatformError as exc:
            self._send_json(exc.status,
                            {"error": str(exc), "code": exc.code})

    def _dispatch_post(self):
        routes = {
            "/participants": self._op_register_participant,
            "/plans": self._op_create_plan,
            "/evidence": self._op_record_evidence,
            "/certifications/decide": self._op_decide,
            "/incidents": self._op_incident,
            "/rectifications": self._op_rectification,
            "/reverifications": self._op_reverify,
            "/appeals": self._op_appeal,
            "/views/enterprise": self._op_enterprise,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
            return
        # 带路径参数的路由
        if self.path.startswith("/participants/") and self.path.endswith("/withdraw"):
            pid = self.path[len("/participants/"):-len("/withdraw")]
            self._op_withdraw(pid)
            return
        if self.path.startswith("/plans/") and self.path.endswith("/versions"):
            plan_id = self.path[len("/plans/"):-len("/versions")]
            self._op_new_version(plan_id)
            return
        if self.path.startswith("/evidence/") and self.path.endswith("/amend"):
            eid = self.path[len("/evidence/"):-len("/amend")]
            self._op_amend(eid)
            return
        if self.path.startswith("/appeals/") and self.path.endswith("/decide"):
            aid = self.path[len("/appeals/"):-len("/decide")]
            self._op_decide_appeal(aid)
            return
        self.send_error(404)

    def _call(self, fn):
        try:
            with STORE_LOCK:
                result = fn()
            self._send_json(200, result)
        except PlatformError as exc:
            self._send_json(exc.status,
                            {"error": str(exc), "code": exc.code})
        except KeyError as exc:
            self._send_json(400,
                            {"error": f"缺少必填字段: {exc.args[0]}",
                             "code": "missing_field"})

    # -- 具体操作：统一从 body 取 actor 与参数 ------------------------------

    def _body(self):
        return self._read_body()

    def _op_register_participant(self):
        b = self._body()
        self._call(lambda: PLATFORM.register_participant(
            b.get("actor"), age_band=b["age_band"],
            health_status=b["health_status"], living=b["living"],
            assistance=b["assistance"], consent_version=b["consent_version"],
            agreed=b.get("agreed", True)))

    def _op_withdraw(self, pid):
        b = self._body()
        self._call(lambda: PLATFORM.withdraw_consent(
            b.get("actor"), pid, reason=b.get("reason", "")))

    def _op_create_plan(self):
        b = self._body()
        self._call(lambda: PLATFORM.create_plan(
            b.get("actor"), provider_name=b["provider_name"],
            service_name=b["service_name"], version=b["version"],
            tier=b["tier"], facets=b["facets"],
            summary=b.get("summary", "")))

    def _op_new_version(self, plan_id):
        b = self._body()
        self._call(lambda: PLATFORM.new_version(
            b.get("actor"), plan_id, version=b["version"],
            changed_facets=b["changed_facets"], summary=b.get("summary", "")))

    def _op_record_evidence(self):
        b = self._body()
        self._call(lambda: PLATFORM.record_evidence(
            b.get("actor"), plan_id=b["plan_id"],
            participant_id=b["participant_id"], capability=b["capability"],
            task=b["task"], barriers=b.get("barriers", []),
            staff_response=b.get("staff_response", {}),
            price_disclosure=b["price_disclosure"], outcome=b["outcome"],
            raw_feedback=b["raw_feedback"],
            facet_dependencies=b["facet_dependencies"],
            exit_reason=b.get("exit_reason")))

    def _op_amend(self, eid):
        b = self._body()
        self._call(lambda: PLATFORM.amend_evidence(
            b.get("actor"), eid, b["note"]))

    def _op_decide(self):
        b = self._body()
        self._call(lambda: PLATFORM.decide_certification(
            b.get("actor"), plan_id=b["plan_id"],
            claimed_strata=b["claimed_strata"],
            claimed_capabilities=b["claimed_capabilities"],
            valid_days=b.get("valid_days", 365),
            exceptions=b.get("exceptions", [])))

    def _op_incident(self):
        b = self._body()
        self._call(lambda: PLATFORM.report_incident(
            b.get("actor"), cert_id=b["cert_id"],
            description=b["description"], scope=b.get("scope", "certification")))

    def _op_rectification(self):
        b = self._body()
        self._call(lambda: PLATFORM.submit_rectification(
            b.get("actor"), cert_id=b["cert_id"], action=b["action"]))

    def _op_reverify(self):
        b = self._body()
        self._call(lambda: PLATFORM.reverify(
            b.get("actor"), cert_id=b["cert_id"], passed=b["passed"],
            notes=b.get("notes", ""), new_evidence_ids=b.get("new_evidence_ids")))

    def _op_appeal(self):
        b = self._body()
        self._call(lambda: PLATFORM.appeal(
            b.get("actor"), reason=b["reason"],
            cert_id=b.get("cert_id"), decision_id=b.get("decision_id")))

    def _op_decide_appeal(self, aid):
        b = self._body()
        self._call(lambda: PLATFORM.decide_appeal(
            b.get("actor"), appeal_id=aid, outcome=b["outcome"],
            reason=b["reason"]))

    def _op_enterprise(self):
        b = self._body()
        self._call(lambda: PLATFORM.enterprise_view(b.get("actor")))

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert set(reference_payload()["facets"]) == {
            "venue", "curriculum", "care_staff", "pricing", "transport"}
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()

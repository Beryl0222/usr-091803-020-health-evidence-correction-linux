"""健康科普证据纠错的运行入口与 HTTP 接口。"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from domain import (
    Claim,
    ClaimStance,
    ContentKind,
    DomainError,
    Evidence,
    EvidenceType,
    NotFoundError,
    ScopeError,
)
from store import HealthEvidenceService, parse_when

SERVICE_ID = "health-evidence-correction"
SERVICE_NAME = "健康科普证据纠错"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _need(payload: dict, key: str):
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(f"缺少必填字段：{key}")
    return value


def _parse_evidence(item: dict) -> Evidence:
    try:
        etype = EvidenceType(item.get("type"))
    except ValueError:
        raise DomainError(f"未知的资料类型：{item.get('type')!r}")
    return Evidence(
        evidence_id=_need(item, "evidence_id"),
        type=etype,
        title=_need(item, "title"),
        citation=_need(item, "citation"),
        limitations=tuple(item.get("limitations") or ()),
    )


def _parse_claim(item: dict) -> Claim:
    try:
        stance = ClaimStance(item.get("stance"))
    except ValueError:
        raise DomainError(f"未知的结论标记：{item.get('stance')!r}")
    return Claim(
        claim_id=_need(item, "claim_id"),
        text=_need(item, "text"),
        domain=_need(item, "domain"),
        stance=stance,
        applicable_population=_need(item, "applicable_population"),
        evidence_ids=tuple(item.get("evidence_ids") or ()),
    )


def _parse_kind(value) -> ContentKind:
    try:
        return ContentKind(value or "article")
    except ValueError:
        raise DomainError(f"未知的内容类型：{value!r}")


def _parse_optional_time(value):
    return parse_when(value) if value else None


def _query_one(query: dict, key: str) -> str:
    values = query.get(key)
    if not values:
        raise DomainError(f"缺少查询参数：{key}")
    return values[0]


def _content_payload(body: dict) -> dict:
    """从同步载荷中解析内容创建参数。"""
    return {
        "kind": _parse_kind(body.get("kind")),
        "title": _need(body, "title"),
        "editor_id": _need(body, "editor_id"),
        "claims": [_parse_claim(i) for i in body.get("claims") or ()],
        "evidence": [_parse_evidence(i) for i in body.get("evidence") or ()],
        "now": _parse_optional_time(body.get("published_at")),
    }


class Handler(BaseHTTPRequestHandler):
    """健康检查与业务接口的明确入口。"""

    def _service(self) -> HealthEvidenceService:
        service = getattr(self.server, "service", None)
        if service is None:
            service = HealthEvidenceService()
            self.server.service = service
        return service

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("请求体不是合法的 JSON")
        if not isinstance(payload, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return payload

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = parse_qs(split.query)
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(path)
            if not match:
                continue
            try:
                body = self._read_json() if method == "POST" else None
                status, payload = handler(self, match, query, body)
                self._json(status, payload)
            except NotFoundError as error:
                self._json(404, {"error": str(error)})
            except ScopeError as error:
                self._json(403, {"error": str(error)})
            except DomainError as error:
                self._json(400, {"error": str(error)})
            return
        self.send_error(404)

    # ---- 各路由处理 -------------------------------------------------

    def _health(self, _match, _query, _body):
        return 200, health_payload()

    def _create_content(self, _match, _query, body):
        service = self._service()
        content = service.create_content(
            content_uid=_need(body, "content_uid"),
            kind=_parse_kind(body.get("kind")),
            title=_need(body, "title"),
            editor_id=_need(body, "editor_id"),
            claims=[_parse_claim(i) for i in body.get("claims") or ()],
            evidence=[_parse_evidence(i) for i in body.get("evidence") or ()],
            platform=body.get("platform"),
            external_id=body.get("external_id"),
            now=_parse_optional_time(body.get("published_at")),
        )
        return 201, service.content_view(content)

    def _get_content(self, match, _query, _body):
        service = self._service()
        return 200, service.content_view(service._content(match.group(1)))

    def _revise_content(self, match, _query, body):
        service = self._service()
        version, correction = service.revise_content(
            match.group(1),
            editor_id=_need(body, "editor_id"),
            reason=body.get("reason") or "",  # 由领域规则校验“修订必须写明原因”
            claims=[_parse_claim(i) for i in body.get("claims") or ()],
            evidence=[_parse_evidence(i) for i in body.get("evidence") or ()],
            now=_parse_optional_time(body.get("published_at")),
        )
        return 201, {
            "version": service.version_view(version),
            "correction": service.correction_view(correction) if correction else None,
        }

    def _content_at(self, match, query, _body):
        service = self._service()
        content, version = service.content_at(match.group(1), _query_one(query, "date"))
        return 200, {
            "content_uid": content.content_uid,
            "kind": content.kind.value,
            "title": content.title,
            "version": service.version_view(version),
        }

    def _diff_since(self, match, query, _body):
        service = self._service()
        return 200, service.diff_since(match.group(1), _query_one(query, "since"))

    def _sync(self, _match, _query, body):
        service = self._service()
        payload = body.get("content")
        content, created = service.sync_content(
            content_uid=_need(body, "content_uid"),
            platform=_need(body, "platform"),
            external_id=_need(body, "external_id"),
            payload=_content_payload(payload) if payload is not None else None,
        )
        return (201 if created else 200), {
            "content_uid": content.content_uid,
            "created": created,
            "platform_ids": dict(content.platform_ids),
        }

    def _register_reviewer(self, _match, _query, body):
        service = self._service()
        reviewer = service.register_reviewer(
            _need(body, "reviewer_id"), _need(body, "name"), body.get("domains") or ()
        )
        return 201, {
            "reviewer_id": reviewer.reviewer_id,
            "name": reviewer.name,
            "domains": sorted(reviewer.domains),
        }

    def _add_review(self, _match, _query, body):
        service = self._service()
        review = service.add_review(
            review_id=_need(body, "review_id"),
            reviewer_id=_need(body, "reviewer_id"),
            claim_id=_need(body, "claim_id"),
            decision=_need(body, "decision"),
            note=body.get("note") or "",
        )
        return 201, service.review_view(review)

    def _declare_coi(self, _match, _query, body):
        service = self._service()
        declaration = service.declare_coi(
            declaration_id=_need(body, "declaration_id"),
            reviewer_id=_need(body, "reviewer_id"),
            content_uid=_need(body, "content_uid"),
            statement=_need(body, "statement"),
        )
        return 201, service.coi_view(declaration)

    def _add_comment(self, _match, _query, body):
        service = self._service()
        comment = service.add_comment(
            comment_id=_need(body, "comment_id"),
            content_uid=_need(body, "content_uid"),
            author_id=_need(body, "author_id"),
            text=_need(body, "text"),
            contains_personal_health_info=bool(body.get("contains_personal_health_info", False)),
        )
        return 201, service.comment_view(comment)

    def _public_dataset(self, _match, _query, _body):
        return 200, self._service().public_dataset()

    def _register_subscriber(self, _match, _query, body):
        service = self._service()
        subscriber = service.register_subscriber(
            _need(body, "subscriber_id"), body.get("content_uids") or ()
        )
        return 201, {
            "subscriber_id": subscriber.subscriber_id,
            "content_uids": sorted(subscriber.content_uids),
        }

    def _subscriber_corrections(self, match, _query, _body):
        service = self._service()
        corrections = service.corrections_for(match.group(1))
        return 200, [service.correction_view(c) for c in corrections]

    def _events(self, _match, _query, _body):
        return 200, self._service().events

    def log_message(self, *_args):
        return


ROUTES = [
    ("GET", re.compile(r"^/health$"), Handler._health),
    ("POST", re.compile(r"^/contents$"), Handler._create_content),
    ("GET", re.compile(r"^/contents/([^/]+)$"), Handler._get_content),
    ("POST", re.compile(r"^/contents/([^/]+)/revisions$"), Handler._revise_content),
    ("GET", re.compile(r"^/contents/([^/]+)/at$"), Handler._content_at),
    ("GET", re.compile(r"^/contents/([^/]+)/diff$"), Handler._diff_since),
    ("POST", re.compile(r"^/sync$"), Handler._sync),
    ("POST", re.compile(r"^/reviewers$"), Handler._register_reviewer),
    ("POST", re.compile(r"^/reviews$"), Handler._add_review),
    ("POST", re.compile(r"^/coi-declarations$"), Handler._declare_coi),
    ("POST", re.compile(r"^/comments$"), Handler._add_comment),
    ("GET", re.compile(r"^/public-dataset$"), Handler._public_dataset),
    ("POST", re.compile(r"^/subscribers$"), Handler._register_subscriber),
    ("GET", re.compile(r"^/subscribers/([^/]+)/corrections$"), Handler._subscriber_corrections),
    ("GET", re.compile(r"^/events$"), Handler._events),
]


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        HealthEvidenceService()  # 领域模块可正常装配
        print("基础检查通过")
        return
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.service = HealthEvidenceService()
    server.serve_forever()


if __name__ == "__main__":
    main()

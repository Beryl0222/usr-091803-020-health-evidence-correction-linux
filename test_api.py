"""通过 HTTP 接口端到端验证「更年期与脑萎缩」纠错流程。"""

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler
from store import HealthEvidenceService

PAPER = {
    "evidence_id": "paper-gray-matter",
    "type": "paper",
    "title": "绝经期与脑灰质体积的纵向影像研究",
    "citation": "J. Neurol. Imaging, 2026, doi:10.1000/example",
    "limitations": ["样本绝经状态为自报", "未全面检测激素水平"],
}

CLAIM_V1 = {
    "claim_id": "claim-menopause-brain",
    "text": "更年期会加速脑萎缩",
    "domain": "神经内科",
    "stance": "supported",
    "applicable_population": "所有中年女性",
    "evidence_ids": ["paper-gray-matter"],
}

CLAIM_V2 = {
    **CLAIM_V1,
    "text": "现有影像研究未显示更年期女性灰质下降速度显著加快，是否加速脑萎缩尚无定论",
    "stance": "inconclusive",
    "applicable_population": "自报绝经状态的女性（研究未全面检测激素）",
}


class ApiTest(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.service = HealthEvidenceService()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _request(self, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(f"{self.base_url}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            body = json.loads(error.read().decode("utf-8")) if error.headers.get_content_type() == "application/json" else {}
            error.close()
            return error.code, body

    def _get(self, path):
        return self._request("GET", path)

    def _post(self, path, payload):
        return self._request("POST", path, payload)

    def _publish_video(self):
        status, body = self._post("/contents", {
            "content_uid": "video-menopause",
            "kind": "video",
            "title": "更年期会加速脑萎缩？",
            "editor_id": "editor-1",
            "evidence": [PAPER],
            "claims": [CLAIM_V1],
            "platform": "douyin",
            "external_id": "dy-123",
            "published_at": "2026-09-01T10:00:00Z",
        })
        self.assertEqual(status, 201)
        return body

    def test_correction_flow_end_to_end(self):
        created = self._publish_video()
        self.assertEqual(created["current_version"], 1)
        self.assertEqual(created["versions"][0]["claims"][0]["stance_label"], "支持")

        self._post("/subscribers", {"subscriber_id": "sub-1", "content_uids": ["video-menopause"]})

        # 编辑部修订：写明原因，结论改为尚无定论
        status, body = self._post("/contents/video-menopause/revisions", {
            "editor_id": "editor-2",
            "reason": "原始论文仅显示灰质下降速度无显著差异，且样本为自报状态、未全面检测激素",
            "claims": [CLAIM_V2],
            "published_at": "2026-09-10T09:00:00Z",
        })
        self.assertEqual(status, 201)
        self.assertEqual(body["version"]["version"], 2)
        correction = body["correction"]
        self.assertEqual(correction["recipient_ids"], ["sub-1"])
        self.assertIn("尚无定论", correction["changes"][0])
        self.assertIn("未全面检测激素水平", correction["changes"][0])

        # 订阅者收到更正，旧版本仍被保留
        status, inbox = self._get("/subscribers/sub-1/corrections")
        self.assertEqual(status, 200)
        self.assertEqual(len(inbox), 1)
        status, content = self._get("/contents/video-menopause")
        self.assertEqual([v["version"] for v in content["versions"]], [1, 2])

        # 按发布日期还原当时内容
        status, restored = self._get("/contents/video-menopause/at?date=2026-09-01")
        self.assertEqual(status, 200)
        self.assertEqual(restored["version"]["claims"][0]["stance"], "supported")

        # 差异摘要带证据出处与研究限制
        status, diff = self._get("/contents/video-menopause/diff?since=2026-09-01")
        self.assertEqual(status, 200)
        self.assertEqual(diff["changes"][0]["change"], "modified")
        citation = diff["changes"][0]["after"]["evidence"][0]["citation_text"]
        self.assertIn("doi:10.1000/example", citation)
        self.assertIn("样本绝经状态为自报", citation)
        self.assertEqual(len(diff["corrections"]), 1)

    def test_revision_without_reason_is_rejected(self):
        self._publish_video()
        status, body = self._post("/contents/video-menopause/revisions", {
            "editor_id": "editor-2", "reason": " ", "claims": [CLAIM_V2],
        })
        self.assertEqual(status, 400)
        self.assertIn("原因", body["error"])

    def test_sync_dedup_by_content_identity(self):
        self._publish_video()
        status, body = self._post("/sync", {
            "content_uid": "video-menopause", "platform": "bilibili", "external_id": "bili-456",
        })
        self.assertEqual(status, 200)
        self.assertFalse(body["created"])
        self.assertEqual(body["platform_ids"], {"douyin": "dy-123", "bilibili": "bili-456"})

    def test_review_scope_and_append_only_rules(self):
        self._publish_video()
        self._post("/reviewers", {"reviewer_id": "rev-1", "name": "某医生", "domains": ["内分泌科"]})
        status, _ = self._post("/reviews", {
            "review_id": "r1", "reviewer_id": "rev-1",
            "claim_id": "claim-menopause-brain", "decision": "同意",
        })
        self.assertEqual(status, 403)  # 审核人只处理获分配的医学领域

        self._post("/reviewers", {"reviewer_id": "rev-2", "name": "另一位医生", "domains": ["神经内科"]})
        status, _ = self._post("/reviews", {
            "review_id": "r2", "reviewer_id": "rev-2",
            "claim_id": "claim-menopause-brain", "decision": "同意修订",
        })
        self.assertEqual(status, 201)
        status, _ = self._post("/reviews", {
            "review_id": "r2", "reviewer_id": "rev-2",
            "claim_id": "claim-menopause-brain", "decision": "改口反对",
        })
        self.assertEqual(status, 400)  # 专家复核不可覆盖

        status, _ = self._post("/coi-declarations", {
            "declaration_id": "coi-1", "reviewer_id": "rev-2",
            "content_uid": "video-menopause", "statement": "无利益冲突",
        })
        self.assertEqual(status, 201)
        status, _ = self._post("/coi-declarations", {
            "declaration_id": "coi-1", "reviewer_id": "rev-2",
            "content_uid": "video-menopause", "statement": "改为有利益冲突",
        })
        self.assertEqual(status, 400)  # 利益冲突声明不可覆盖

    def test_personal_health_info_excluded_from_public_dataset(self):
        self._publish_video()
        self._post("/comments", {
            "comment_id": "cm-1", "content_uid": "video-menopause",
            "author_id": "user-1", "text": "学到了，谢谢科普",
        })
        self._post("/comments", {
            "comment_id": "cm-2", "content_uid": "video-menopause",
            "author_id": "user-2", "text": "我52岁，绝经后在吃替勃龙",
            "contains_personal_health_info": True,
        })
        status, dataset = self._get("/public-dataset")
        self.assertEqual(status, 200)
        exported = {c["comment_id"] for c in dataset["comments"]}
        self.assertEqual(exported, {"cm-1"})
        self.assertNotIn("author_id", dataset["comments"][0])

    def test_events_recorded_for_traceability(self):
        self._publish_video()
        status, events = self._get("/events")
        self.assertEqual(status, 200)
        self.assertIn("content_created", [e["type"] for e in events])


if __name__ == "__main__":
    unittest.main()

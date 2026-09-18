"""围绕「更年期与脑萎缩」纠错场景验证领域规则。"""

import unittest
from datetime import datetime, timezone

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
from store import HealthEvidenceService

T1 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)  # 短视频首次发布
T2 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)  # 编辑部修订

PAPER = Evidence(
    evidence_id="paper-gray-matter",
    type=EvidenceType.PAPER,
    title="绝经期与脑灰质体积的纵向影像研究",
    citation="J. Neurol. Imaging, 2026, doi:10.1000/example",
    limitations=("样本绝经状态为自报", "未全面检测激素水平"),
)

CLAIM_V1 = Claim(
    claim_id="claim-menopause-brain",
    text="更年期会加速脑萎缩",
    domain="神经内科",
    stance=ClaimStance.SUPPORTED,
    applicable_population="所有中年女性",
    evidence_ids=("paper-gray-matter",),
)

CLAIM_V2 = Claim(
    claim_id="claim-menopause-brain",
    text="现有影像研究未显示更年期女性灰质下降速度显著加快，是否加速脑萎缩尚无定论",
    domain="神经内科",
    stance=ClaimStance.INCONCLUSIVE,
    applicable_population="自报绝经状态的女性（研究未全面检测激素）",
    evidence_ids=("paper-gray-matter",),
)

REVISION_REASON = "原始论文仅显示灰质下降速度无显著差异，且样本为自报状态、未全面检测激素，旧表述超出证据范围"


def make_service(with_subscriber=True) -> HealthEvidenceService:
    service = HealthEvidenceService()
    service.register_evidence(PAPER)
    service.create_content(
        content_uid="video-menopause",
        kind=ContentKind.VIDEO,
        title="更年期会加速脑萎缩？",
        editor_id="editor-1",
        claims=[CLAIM_V1],
        platform="douyin",
        external_id="dy-123",
        now=T1,
    )
    if with_subscriber:
        service.register_subscriber("sub-1", ["video-menopause"])
    return service


def revise(service: HealthEvidenceService, claims=None, reason=REVISION_REASON):
    return service.revise_content(
        "video-menopause",
        editor_id="editor-2",
        reason=reason,
        claims=claims if claims is not None else [CLAIM_V2],
        now=T2,
    )


class ClaimEvidenceTest(unittest.TestCase):
    def test_claim_must_link_evidence(self):
        service = HealthEvidenceService()
        with self.assertRaises(DomainError):
            service.create_content(
                "c1", ContentKind.ARTICLE, "t", "e1",
                [Claim("c1-1", "某说法", "神经内科", ClaimStance.SUPPORTED, "人群", ())],
            )

    def test_claim_rejects_unknown_evidence(self):
        service = HealthEvidenceService()
        with self.assertRaises(DomainError):
            service.create_content(
                "c1", ContentKind.ARTICLE, "t", "e1",
                [Claim("c1-1", "某说法", "神经内科", ClaimStance.SUPPORTED, "人群", ("ghost",))],
            )

    def test_claim_requires_population_and_domain(self):
        service = HealthEvidenceService()
        service.register_evidence(PAPER)
        with self.assertRaises(DomainError):
            service.create_content(
                "c1", ContentKind.ARTICLE, "t", "e1",
                [Claim("c1-1", "某说法", "神经内科", ClaimStance.SUPPORTED, "", ("paper-gray-matter",))],
            )


class RevisionTest(unittest.TestCase):
    def test_revision_requires_reason(self):
        service = make_service()
        with self.assertRaises(DomainError):
            revise(service, reason="  ")
        self.assertEqual(len(service.contents["video-menopause"].versions), 1)

    def test_revision_preserves_public_versions(self):
        service = make_service()
        revise(service)
        versions = service.contents["video-menopause"].versions
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].claims[0].text, CLAIM_V1.text)  # 公众曾看到的版本被保留
        self.assertEqual(versions[0].reason, "首次发布")
        self.assertEqual(versions[1].reason, REVISION_REASON)

    def test_correction_sent_to_relevant_subscribers(self):
        service = make_service()
        service.register_subscriber("sub-2", ["other-content"])
        _, correction = revise(service)
        self.assertIsNotNone(correction)
        self.assertEqual(correction.recipient_ids, ("sub-1",))
        self.assertEqual(service.corrections_for("sub-1"), [correction])
        self.assertEqual(service.corrections_for("sub-2"), [])
        text = "\n".join(correction.changes)
        self.assertIn("尚无定论", text)
        self.assertIn("样本绝经状态为自报", text)  # 研究限制随更正呈现
        self.assertIn("未全面检测激素水平", text)

    def test_no_correction_when_public_conclusion_unchanged(self):
        service = make_service()
        extra = Claim(
            "claim-extra", "新增一条补充说法", "神经内科",
            ClaimStance.INCONCLUSIVE, "人群", ("paper-gray-matter",),
        )
        _, correction = revise(service, claims=[CLAIM_V1, extra])
        self.assertIsNone(correction)  # 仅新增说法，无人被旧结论误导
        self.assertEqual(service.corrections_for("sub-1"), [])


class SyncTest(unittest.TestCase):
    def test_sync_dedup_by_content_identity(self):
        service = make_service()
        content, created = service.sync_content("video-menopause", "bilibili", "bili-456")
        self.assertFalse(created)
        self.assertEqual(len(service.contents), 1)
        self.assertEqual(content.platform_ids, {"douyin": "dy-123", "bilibili": "bili-456"})
        content, created = service.sync_content("video-menopause", "bilibili", "bili-456")
        self.assertFalse(created)  # 重复同步保持幂等

    def test_sync_creates_when_identity_unknown(self):
        service = HealthEvidenceService()
        content, created = service.sync_content(
            "video-new",
            "douyin",
            "dy-1",
            payload={
                "kind": ContentKind.VIDEO,
                "title": "新视频",
                "editor_id": "editor-1",
                "claims": [CLAIM_V1],
                "evidence": [PAPER],
            },
            now=T1,
        )
        self.assertTrue(created)
        self.assertEqual(content.platform_ids, {"douyin": "dy-1"})


class ReviewTest(unittest.TestCase):
    def test_reviewer_limited_to_assigned_domains(self):
        service = make_service()
        service.register_reviewer("rev-1", "某医生", ["内分泌科"])
        with self.assertRaises(ScopeError):
            service.add_review("r1", "rev-1", "claim-menopause-brain", "同意")
        service.register_reviewer("rev-2", "另一位医生", ["神经内科", "内分泌科"])
        review = service.add_review("r2", "rev-2", "claim-menopause-brain", "同意修订")
        self.assertEqual(review.domain, "神经内科")

    def test_review_is_append_only(self):
        service = make_service()
        service.register_reviewer("rev-1", "某医生", ["神经内科"])
        service.add_review("r1", "rev-1", "claim-menopause-brain", "同意")
        with self.assertRaises(DomainError):
            service.add_review("r1", "rev-1", "claim-menopause-brain", "改口反对")
        self.assertEqual(len(service.reviews), 1)

    def test_coi_declaration_is_append_only(self):
        service = make_service()
        service.register_reviewer("rev-1", "某医生", ["神经内科"])
        service.declare_coi("coi-1", "rev-1", "video-menopause", "无利益冲突")
        with self.assertRaises(DomainError):
            service.declare_coi("coi-1", "rev-1", "video-menopause", "改为有利益冲突")
        self.assertEqual(len(service.coi_declarations), 1)


class CommentPrivacyTest(unittest.TestCase):
    def test_personal_health_info_stays_out_of_public_dataset(self):
        service = make_service()
        service.add_comment("cm-1", "video-menopause", "user-1", "学到了，谢谢科普")
        service.add_comment(
            "cm-2", "video-menopause", "user-2",
            "我52岁，绝经后在吃替勃龙，剂量要不要调整？",
            contains_personal_health_info=True,
        )
        dataset = service.public_dataset()
        exported = {c["comment_id"]: c for c in dataset["comments"]}
        self.assertIn("cm-1", exported)
        self.assertNotIn("cm-2", exported)  # 个体健康信息不得进入公开资料库
        self.assertNotIn("author_id", exported["cm-1"])  # 公开库不带作者标识


class TemporalQueryTest(unittest.TestCase):
    def test_content_at_restores_historical_version(self):
        service = make_service()
        revise(service)
        _, version = service.content_at("video-menopause", "2026-09-01")
        self.assertEqual(version.version, 1)
        self.assertEqual(version.claims[0].stance, ClaimStance.SUPPORTED)
        _, version = service.content_at("video-menopause", "2026-09-10")
        self.assertEqual(version.version, 2)
        self.assertEqual(version.claims[0].stance, ClaimStance.INCONCLUSIVE)

    def test_content_at_before_first_publish_raises(self):
        service = make_service()
        with self.assertRaises(NotFoundError):
            service.content_at("video-menopause", "2026-08-31")

    def test_diff_summary_carries_evidence_citations(self):
        service = make_service()
        revise(service)
        diff = service.diff_since("video-menopause", "2026-09-01")
        self.assertEqual(diff["from"]["version"], 1)
        self.assertEqual(diff["to"]["version"], 2)
        self.assertEqual(len(diff["changes"]), 1)
        change = diff["changes"][0]
        self.assertEqual(change["change"], "modified")
        self.assertIn("stance", change["fields"])
        citations = change["after"]["evidence"][0]["citation_text"]
        self.assertIn("doi:10.1000/example", citations)
        self.assertIn("样本绝经状态为自报", citations)  # 差异摘要带证据出处与限制
        self.assertEqual(len(diff["corrections"]), 1)
        self.assertEqual(diff["corrections"][0]["recipient_ids"], ["sub-1"])


class EventLogTest(unittest.TestCase):
    def test_mutations_are_recorded_for_traceability(self):
        service = make_service()
        revise(service)
        types = [e["type"] for e in service.events]
        self.assertIn("content_created", types)
        self.assertIn("content_revised", types)
        self.assertIn("correction_sent", types)
        revised = next(e for e in service.events if e["type"] == "content_revised")
        self.assertEqual(revised["actor"], "editor-2")
        self.assertEqual(revised["details"]["reason"], REVISION_REASON)


if __name__ == "__main__":
    unittest.main()

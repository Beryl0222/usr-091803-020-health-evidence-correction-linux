"""健康科普证据纠错的领域规则测试。

每个测试类对应需求中的一组规则：
- 说法与证据关联（指南/论文/个人经验，立场与适用人群）
- 结论推导（研究证据优先于个人经验）
- 修订原因、公众版本保留与更正通知
- 跨平台同步按内容身份去重
- 专家复核与利益冲突声明只增不改、审核人领域限制
- 评论中的个体健康信息不进入公开资料库
- 按发布日期还原内容并给出有证据出处的差异摘要
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from domain import (
    Claim,
    ClaimLink,
    ClaimStatus,
    ClaimWithoutEvidenceError,
    CommentVisibility,
    ContentType,
    CorrectionKind,
    EvidenceType,
    FieldNotAssignedError,
    MissingReasonError,
    NoVersionAtDateError,
    ReviewVerdict,
    Stance,
    UnknownEvidenceError,
    ValidationError,
)
from health_service import HealthEvidenceService

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


class ManualClock:
    """可手动推进的时钟，保证版本时间可控。"""

    def __init__(self, start):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, **kwargs):
        self._now += timedelta(**kwargs)
        return self._now


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock(BASE)
        self.service = HealthEvidenceService(clock=self.clock)

    # -- 场景构件 ------------------------------------------------------

    def register_menopause_paper(self):
        """编辑部查到的研究正文：灰质下降速度无显著差异，限制不可省略。"""
        return self.service.register_evidence(
            type=EvidenceType.PAPER,
            title="绝经过渡期女性灰质体积变化的纵向影像研究",
            citation="《神经影像学期刊》2026年第4期, DOI:10.1234/example",
            limitations=("样本绝经状态为自报", "未全面检测激素水平"),
        )

    def publish_menopause_video(self):
        """原始短视频：仅凭个人经历断言“更年期会加速脑萎缩”。"""
        experience = self.service.register_evidence(
            type=EvidenceType.PERSONAL_EXPERIENCE,
            title="创作者个人经历自述",
            citation="短视频创作者口述",
        )
        claim = Claim(
            claim_id="CL-更年期脑萎缩",
            text="更年期会加速脑萎缩",
            medical_field="神经内科",
            links=(
                ClaimLink(
                    evidence_id=experience.id,
                    stance=Stance.SUPPORTS,
                    applicable_population="围绝经期女性",
                ),
            ),
        )
        view = self.service.sync_content(
            type=ContentType.VIDEO,
            author="某健康账号",
            title="更年期会加速脑萎缩？",
            platform="短视频平台A",
            platform_content_id="vid-001",
            claims=[claim],
            editor="编辑甲",
        )
        return experience, claim, view


class ClaimEvidenceLinkTest(ServiceTestBase):
    def test_claim_must_link_at_least_one_evidence(self):
        claim = Claim(claim_id="C1", text="某说法", medical_field="神经内科", links=())
        with self.assertRaises(ClaimWithoutEvidenceError):
            self.service.sync_content(
                type=ContentType.ARTICLE,
                author="作者",
                title="标题",
                platform="平台",
                platform_content_id="p1",
                claims=[claim],
                editor="编辑",
            )

    def test_link_must_carry_stance_population_and_known_evidence(self):
        paper = self.service.register_evidence(
            type=EvidenceType.PAPER, title="论文", citation="期刊 DOI:10.1/x"
        )
        no_population = Claim(
            claim_id="C1",
            text="某说法",
            medical_field="妇科",
            links=(ClaimLink(evidence_id=paper.id, stance=Stance.SUPPORTS, applicable_population=""),),
        )
        with self.assertRaises(ValidationError):
            self.service.sync_content(
                type=ContentType.ARTICLE,
                author="作者",
                title="标题",
                platform="平台",
                platform_content_id="p1",
                claims=[no_population],
                editor="编辑",
            )
        unknown = Claim(
            claim_id="C2",
            text="某说法",
            medical_field="妇科",
            links=(ClaimLink(evidence_id="EV-9999", stance=Stance.SUPPORTS, applicable_population="成人"),),
        )
        with self.assertRaises(UnknownEvidenceError):
            self.service.sync_content(
                type=ContentType.ARTICLE,
                author="作者",
                title="标题",
                platform="平台",
                platform_content_id="p2",
                claims=[unknown],
                editor="编辑",
            )

    def test_each_claim_links_guideline_paper_or_experience_separately(self):
        guideline = self.service.register_evidence(
            type=EvidenceType.GUIDELINE, title="绝经期健康管理指南", citation="中华医学会 2025"
        )
        paper = self.register_menopause_paper()
        experience = self.service.register_evidence(
            type=EvidenceType.PERSONAL_EXPERIENCE, title="个人经历", citation="讲述者自述"
        )
        claims = [
            Claim(
                claim_id="C-激素",
                text="激素替代治疗需个体化评估",
                medical_field="妇科",
                links=(
                    ClaimLink(evidence_id=guideline.id, stance=Stance.SUPPORTS, applicable_population="围绝经期女性"),
                    ClaimLink(evidence_id=paper.id, stance=Stance.INCONCLUSIVE, applicable_population="45-55岁女性"),
                ),
            ),
            Claim(
                claim_id="C-运动",
                text="规律运动改善睡眠",
                medical_field="全科",
                links=(
                    ClaimLink(evidence_id=experience.id, stance=Stance.SUPPORTS, applicable_population="一般成年人"),
                ),
            ),
        ]
        view = self.service.sync_content(
            type=ContentType.ARTICLE,
            author="编辑部",
            title="更年期健康管理",
            platform="公众号",
            platform_content_id="art-1",
            claims=claims,
            editor="编辑乙",
        )
        snapshots = {c.claim_id: c for c in view.version.claims}
        hormone = snapshots["C-激素"]
        self.assertEqual(
            [link.stance for link in hormone.links], [Stance.SUPPORTS, Stance.INCONCLUSIVE]
        )
        self.assertEqual(
            [link.applicable_population for link in hormone.links],
            ["围绝经期女性", "45-55岁女性"],
        )
        self.assertEqual(snapshots["C-运动"].conclusion, ClaimStatus.INCONCLUSIVE)


class ConclusionDerivationTest(ServiceTestBase):
    def _publish_with_links(self, claim_id, links):
        claim = Claim(claim_id=claim_id, text="说法", medical_field="神经内科", links=links)
        return self.service.sync_content(
            type=ContentType.ARTICLE,
            author="作者",
            title=f"标题-{claim_id}",
            platform="平台",
            platform_content_id=claim_id,
            claims=[claim],
            editor="编辑",
        ).version.claims[0]

    def test_personal_experience_alone_cannot_settle_claim(self):
        experience = self.service.register_evidence(
            type=EvidenceType.PERSONAL_EXPERIENCE, title="个人经历", citation="自述"
        )
        snap = self._publish_with_links(
            "C1",
            (ClaimLink(evidence_id=experience.id, stance=Stance.SUPPORTS, applicable_population="成人"),),
        )
        self.assertEqual(snap.conclusion, ClaimStatus.INCONCLUSIVE)

    def test_research_evidence_outweighs_anecdote(self):
        experience = self.service.register_evidence(
            type=EvidenceType.PERSONAL_EXPERIENCE, title="个人经历", citation="自述"
        )
        paper = self.register_menopause_paper()
        snap = self._publish_with_links(
            "C2",
            (
                ClaimLink(evidence_id=experience.id, stance=Stance.SUPPORTS, applicable_population="成人"),
                ClaimLink(evidence_id=paper.id, stance=Stance.REFUTES, applicable_population="围绝经期女性"),
            ),
        )
        self.assertEqual(snap.conclusion, ClaimStatus.REFUTED)

    def test_consistent_research_supports_or_refutes(self):
        guideline = self.service.register_evidence(
            type=EvidenceType.GUIDELINE, title="指南", citation="学会 2025"
        )
        paper = self.register_menopause_paper()
        supported = self._publish_with_links(
            "C3",
            (
                ClaimLink(evidence_id=guideline.id, stance=Stance.SUPPORTS, applicable_population="成人"),
                ClaimLink(evidence_id=paper.id, stance=Stance.SUPPORTS, applicable_population="成人"),
            ),
        )
        self.assertEqual(supported.conclusion, ClaimStatus.SUPPORTED)

    def test_conflicting_research_is_contested(self):
        guideline = self.service.register_evidence(
            type=EvidenceType.GUIDELINE, title="指南", citation="学会 2025"
        )
        paper = self.register_menopause_paper()
        snap = self._publish_with_links(
            "C4",
            (
                ClaimLink(evidence_id=guideline.id, stance=Stance.SUPPORTS, applicable_population="成人"),
                ClaimLink(evidence_id=paper.id, stance=Stance.REFUTES, applicable_population="成人"),
            ),
        )
        self.assertEqual(snap.conclusion, ClaimStatus.CONTESTED)


class RevisionAndCorrectionTest(ServiceTestBase):
    def test_revision_requires_reason(self):
        _, claim, view = self.publish_menopause_video()
        with self.assertRaises(MissingReasonError):
            self.service.revise_content(
                view.content_id, editor="编辑甲", reason="  ", claims=[claim]
            )
        with self.assertRaises(MissingReasonError):
            self.service.retract_content(view.content_id, editor="编辑甲", reason="")

    def test_public_versions_are_preserved(self):
        experience, claim, view = self.publish_menopause_video()
        paper = self.register_menopause_paper()
        self.clock.advance(days=3)
        revised_claim = Claim(
            claim_id=claim.claim_id,
            text=claim.text,
            medical_field=claim.medical_field,
            links=claim.links
            + (
                ClaimLink(
                    evidence_id=paper.id,
                    stance=Stance.REFUTES,
                    applicable_population="围绝经期女性",
                ),
            ),
        )
        self.service.revise_content(
            view.content_id,
            editor="编辑甲",
            reason="研究正文仅显示灰质下降速度无显著差异，补充论文证据",
            claims=[revised_claim],
        )
        old_view = self.service.content_as_of(view.content_id, BASE + timedelta(days=1))
        self.assertEqual(old_view.version.number, 1)
        self.assertEqual(old_view.version.claims[0].conclusion, ClaimStatus.INCONCLUSIVE)
        self.assertEqual(
            old_view.version.claims[0].links,
            (ClaimLink(evidence_id=experience.id, stance=Stance.SUPPORTS, applicable_population="围绝经期女性"),),
        )
        current = self.service.get_content(view.content_id)
        self.assertEqual(current.version.number, 2)
        self.assertEqual(current.version.claims[0].conclusion, ClaimStatus.REFUTED)

    def test_correction_sent_to_subscribers_who_saw_old_version(self):
        _, claim, view = self.publish_menopause_video()
        self.service.subscribe("订阅者甲", view.content_id)  # 看过第 1 版
        paper = self.register_menopause_paper()
        self.clock.advance(days=2)
        revised_claim = Claim(
            claim_id=claim.claim_id,
            text=claim.text,
            medical_field=claim.medical_field,
            links=claim.links
            + (ClaimLink(evidence_id=paper.id, stance=Stance.REFUTES, applicable_population="围绝经期女性"),),
        )
        self.service.revise_content(
            view.content_id, editor="编辑甲", reason="补充研究正文证据", claims=[revised_claim]
        )
        self.service.subscribe("订阅者乙", view.content_id)  # 修订后才订阅，只见过新版

        corrections = self.service.corrections_for(view.content_id)
        self.assertEqual(len(corrections), 1)
        correction = corrections[0]
        self.assertEqual(correction.kind, CorrectionKind.REVISION)
        self.assertEqual(correction.reason, "补充研究正文证据")
        self.assertEqual(correction.recipients, ("订阅者甲",))

        notified = self.service.notifications_for("订阅者甲")
        self.assertEqual([n.correction_id for n in notified], [correction.id])
        self.assertEqual(self.service.notifications_for("订阅者乙"), ())

        change = correction.claim_changes[0]
        self.assertEqual(change.before, ClaimStatus.INCONCLUSIVE)
        self.assertEqual(change.after, ClaimStatus.REFUTED)
        citations = {c.evidence_id: c for c in change.evidence}
        self.assertIn(paper.id, citations)
        self.assertEqual(citations[paper.id].limitations, ("样本绝经状态为自报", "未全面检测激素水平"))

    def test_no_correction_when_revision_keeps_conclusions(self):
        _, claim, view = self.publish_menopause_video()
        self.service.subscribe("订阅者甲", view.content_id)
        self.clock.advance(days=1)
        self.service.revise_content(
            view.content_id,
            editor="编辑甲",
            reason="修正错别字",
            claims=[
                Claim(
                    claim_id=claim.claim_id,
                    text="更年期会加速脑萎缩（附研究限制说明）",
                    medical_field=claim.medical_field,
                    links=claim.links,
                )
            ],
        )
        self.assertEqual(self.service.corrections_for(view.content_id), ())

    def test_retraction_preserves_record_and_notifies_viewers(self):
        _, _, view = self.publish_menopause_video()
        self.service.subscribe("老读者", view.content_id)
        self.clock.advance(days=1)
        version = self.service.retract_content(
            view.content_id, editor="编辑甲", reason="原说法缺乏研究证据，撤回并更正"
        )
        self.assertTrue(version.retracted)
        # 历史版本仍然可以还原，不是简单删稿
        old_view = self.service.content_as_of(view.content_id, BASE)
        self.assertEqual(old_view.version.number, 1)
        self.assertFalse(old_view.version.retracted)
        corrections = self.service.corrections_for(view.content_id)
        self.assertEqual(corrections[0].kind, CorrectionKind.RETRACTION)
        self.assertEqual(corrections[0].recipients, ("老读者",))


class CrossPlatformSyncTest(ServiceTestBase):
    def test_sync_dedups_by_content_identity(self):
        _, claim, view = self.publish_menopause_video()
        self.clock.advance(hours=6)
        # 同一内容（同作者、标题、首次发布时间）同步到第二个平台
        dup = self.service.sync_content(
            type=ContentType.VIDEO,
            author="某健康账号",
            title="更年期会加速脑萎缩？",
            platform="短视频平台B",
            platform_content_id="vid-002",
            claims=[claim],
            editor="同步任务",
            first_published_at=BASE,
        )
        self.assertEqual(dup.content_id, view.content_id)
        self.assertEqual(len(dup.platforms), 2)
        self.assertEqual({p.platform for p in dup.platforms}, {"短视频平台A", "短视频平台B"})
        # 去重不产生新版本
        self.assertEqual(dup.version.number, 1)
        kinds = [e.kind for e in self.service.events()]
        self.assertIn("content.deduped", kinds)

    def test_different_identity_creates_separate_content(self):
        _, claim, view = self.publish_menopause_video()
        other = self.service.sync_content(
            type=ContentType.VIDEO,
            author="另一个账号",
            title="更年期会加速脑萎缩？",
            platform="短视频平台B",
            platform_content_id="vid-003",
            claims=[
                Claim(
                    claim_id="CL-另一说法",
                    text=claim.text,
                    medical_field=claim.medical_field,
                    links=claim.links,
                )
            ],
            editor="同步任务",
        )
        self.assertNotEqual(other.content_id, view.content_id)


class ReviewAndConflictTest(ServiceTestBase):
    def test_reviewer_only_handles_assigned_fields(self):
        _, _, view = self.publish_menopause_video()
        neurologist = self.service.register_reviewer(name="医生甲", fields={"神经内科", "内分泌科"})
        dermatologist = self.service.register_reviewer(name="医生乙", fields={"皮肤科"})
        review = self.service.submit_review(
            neurologist.id, "CL-更年期脑萎缩", ReviewVerdict.CHANGES_REQUESTED, notes="需补充研究限制"
        )
        self.assertEqual(review.claim_id, "CL-更年期脑萎缩")
        with self.assertRaises(FieldNotAssignedError):
            self.service.submit_review(
                dermatologist.id, "CL-更年期脑萎缩", ReviewVerdict.APPROVED
            )

    def test_reviews_are_append_only_and_survive_revision(self):
        _, claim, view = self.publish_menopause_video()
        neurologist = self.service.register_reviewer(name="医生甲", fields={"神经内科"})
        first = self.service.submit_review(
            neurologist.id, "CL-更年期脑萎缩", ReviewVerdict.CHANGES_REQUESTED, notes="初核"
        )
        self.clock.advance(days=1)
        # 内容修订后复核记录不丢失、不覆盖
        self.service.revise_content(
            view.content_id, editor="编辑甲", reason="补充证据", claims=[claim]
        )
        second = self.service.submit_review(
            neurologist.id, "CL-更年期脑萎缩", ReviewVerdict.APPROVED, notes="复核通过"
        )
        history = self.service.reviews_for("CL-更年期脑萎缩")
        self.assertEqual([r.id for r in history], [first.id, second.id])
        self.assertEqual(history[0].verdict, ReviewVerdict.CHANGES_REQUESTED)
        self.assertEqual(history[1].verdict, ReviewVerdict.APPROVED)

    def test_conflict_declarations_are_append_only(self):
        reviewer = self.service.register_reviewer(name="医生甲", fields={"神经内科"})
        first = self.service.declare_conflict_of_interest(reviewer.id, "无利益冲突")
        self.clock.advance(days=30)
        second = self.service.declare_conflict_of_interest(
            reviewer.id, "2026年10月起担任某药企顾问"
        )
        declarations = self.service.declarations_for(reviewer.id)
        self.assertEqual([d.id for d in declarations], [first.id, second.id])
        self.assertEqual(declarations[0].statement, "无利益冲突")
        self.assertEqual(declarations[1].statement, "2026年10月起担任某药企顾问")
        with self.assertRaises(ValidationError):
            self.service.declare_conflict_of_interest(reviewer.id, "  ")


class CommentPrivacyTest(ServiceTestBase):
    def test_personal_health_info_stays_out_of_public_repo(self):
        _, _, view = self.publish_menopause_video()
        phi = self.service.ingest_comment(
            view.content_id, author="读者甲", text="我被诊断出卵巢早衰，在服用激素，怎么办？"
        )
        flagged = self.service.ingest_comment(
            view.content_id,
            author="读者乙",
            text="普通的一句话",
            contains_personal_health=True,  # 人工标记同样受限
        )
        safe = self.service.ingest_comment(view.content_id, author="读者丙", text="感谢科普，学到了")

        self.assertEqual(phi.visibility, CommentVisibility.RESTRICTED)
        self.assertEqual(flagged.visibility, CommentVisibility.RESTRICTED)
        self.assertEqual(safe.visibility, CommentVisibility.PUBLIC)

        public_texts = [c.text for c in self.service.public_comments()]
        self.assertEqual(public_texts, ["感谢科普，学到了"])
        restricted_ids = {c.id for c in self.service.restricted_comments()}
        self.assertEqual(restricted_ids, {phi.id, flagged.id})


class TimeTravelTest(ServiceTestBase):
    def _revised_scenario(self):
        experience, claim, view = self.publish_menopause_video()
        paper = self.register_menopause_paper()
        self.clock.advance(days=5)
        revised_claim = Claim(
            claim_id=claim.claim_id,
            text=claim.text,
            medical_field=claim.medical_field,
            links=claim.links
            + (ClaimLink(evidence_id=paper.id, stance=Stance.REFUTES, applicable_population="围绝经期女性"),),
        )
        self.service.revise_content(
            view.content_id,
            editor="编辑甲",
            reason="研究正文仅显示灰质下降速度无显著差异，补充论文与限制说明",
            claims=[revised_claim],
        )
        return paper, view

    def test_content_as_of_restores_historical_version(self):
        _, view = self._revised_scenario()
        between = BASE + timedelta(days=2)
        historical = self.service.content_as_of(view.content_id, between)
        self.assertEqual(historical.version.number, 1)
        self.assertEqual(historical.version.claims[0].conclusion, ClaimStatus.INCONCLUSIVE)
        current = self.service.content_as_of(view.content_id, BASE + timedelta(days=30))
        self.assertEqual(current.version.number, 2)
        with self.assertRaises(NoVersionAtDateError):
            self.service.content_as_of(view.content_id, BASE - timedelta(seconds=1))

    def test_diff_summary_cites_evidence_on_both_sides(self):
        paper, view = self._revised_scenario()
        summary = self.service.diff_summary(view.content_id, BASE + timedelta(days=2))
        self.assertEqual(summary.as_of_version, 1)
        self.assertEqual(summary.current_version, 2)
        self.assertEqual(summary.changed_count, 1)

        diff = summary.claim_diffs[0]
        self.assertEqual(diff.claim_id, "CL-更年期脑萎缩")
        self.assertEqual(diff.before, ClaimStatus.INCONCLUSIVE)
        self.assertEqual(diff.after, ClaimStatus.REFUTED)
        self.assertEqual([c.type for c in diff.evidence_then], [EvidenceType.PERSONAL_EXPERIENCE])

        now_by_id = {c.evidence_id: c for c in diff.evidence_now}
        paper_citation = now_by_id[paper.id]
        self.assertEqual(paper_citation.stance, Stance.REFUTES)
        self.assertEqual(paper_citation.applicable_population, "围绝经期女性")
        self.assertEqual(paper_citation.citation, "《神经影像学期刊》2026年第4期, DOI:10.1234/example")
        self.assertEqual(paper_citation.limitations, ("样本绝经状态为自报", "未全面检测激素水平"))

        # 差异摘要可序列化，便于接口输出
        payload = summary.to_dict()
        self.assertEqual(payload["claim_diffs"][0]["after"], "证据反驳")
        json.dumps(payload, ensure_ascii=False)


class MenopauseScenarioTest(ServiceTestBase):
    """端到端还原需求场景：短视频误读研究，编辑部修订并向看过旧说法的订阅者更正。"""

    def test_menopause_brain_atrophy_correction_flow(self):
        experience, claim, view = self.publish_menopause_video()
        content_id = view.content_id

        # 仅凭个人经验，系统判定“尚无定论”
        self.assertEqual(view.version.claims[0].conclusion, ClaimStatus.INCONCLUSIVE)

        # 订阅者看过旧说法
        self.service.subscribe("老订阅者", content_id)

        # 编辑部查到研究正文，登记论文及限制
        paper = self.register_menopause_paper()
        neurologist = self.service.register_reviewer(name="神经科专家", fields={"神经内科"})
        self.service.declare_conflict_of_interest(neurologist.id, "与所涉研究无任何利益关系")

        # 专家复核（限本领域）后修订：写明原因、保留旧版本
        self.service.submit_review(
            neurologist.id, claim.claim_id, ReviewVerdict.CHANGES_REQUESTED, notes="必须保留研究限制"
        )
        self.clock.advance(days=2)
        revised_claim = Claim(
            claim_id=claim.claim_id,
            text="更年期会加速脑萎缩",
            medical_field=claim.medical_field,
            links=claim.links
            + (ClaimLink(evidence_id=paper.id, stance=Stance.REFUTES, applicable_population="围绝经期女性"),),
        )
        self.service.revise_content(
            content_id,
            editor="编辑甲",
            reason="研究正文仅显示灰质下降速度无显著差异，且样本自报状态、未全面检测激素，补充证据与限制",
            claims=[revised_claim],
        )

        # 看过旧说法的订阅者收到更正，更正附研究出处与限制
        corrections = self.service.corrections_for(content_id)
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0].recipients, ("老订阅者",))
        change = corrections[0].claim_changes[0]
        self.assertEqual((change.before, change.after), (ClaimStatus.INCONCLUSIVE, ClaimStatus.REFUTED))
        paper_citation = {c.evidence_id: c for c in change.evidence}[paper.id]
        self.assertIn("未全面检测激素水平", paper_citation.limitations)

        # 按发布日期还原：旧版本仍在，差异摘要给出证据出处
        historical = self.service.content_as_of(content_id, BASE + timedelta(days=1))
        self.assertEqual(historical.version.number, 1)
        summary = self.service.diff_summary(content_id, BASE + timedelta(days=1))
        self.assertEqual(summary.changed_count, 1)
        self.assertEqual(summary.claim_diffs[0].after, ClaimStatus.REFUTED)

        # 评论区：个体健康信息不进入公开资料库
        phi = self.service.ingest_comment(content_id, author="读者", text="我确诊早发性更年期，在服用激素")
        self.assertEqual(phi.visibility, CommentVisibility.RESTRICTED)
        self.assertEqual(self.service.public_comments(content_id), ())

        # 复核与声明只增不改
        self.assertEqual(len(self.service.reviews_for(claim.claim_id)), 1)
        self.assertEqual(len(self.service.declarations_for(neurologist.id)), 1)


class AuditTrailTest(ServiceTestBase):
    def test_events_record_actor_for_responsibility_tracing(self):
        _, _, view = self.publish_menopause_video()
        reviewer = self.service.register_reviewer(name="医生甲", fields={"神经内科"})
        self.service.submit_review(reviewer.id, "CL-更年期脑萎缩", ReviewVerdict.APPROVED)

        events = self.service.events()
        self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)))
        by_kind = {}
        for event in events:
            by_kind.setdefault(event.kind, event)
        self.assertEqual(by_kind["content.published"].actor, "编辑甲")
        self.assertEqual(by_kind["review.submitted"].actor, reviewer.id)
        self.assertEqual(by_kind["reviewer.registered"].actor, "编辑部")


if __name__ == "__main__":
    unittest.main()

"""健康科普证据纠错的业务规则与责任追溯。

所有公开内容的变更都会生成新的不可变版本并记录事件：
- 公众曾看到的版本、专家复核与利益冲突声明一律只增不改；
- 修订/撤稿必须写明原因，并向看过旧版本的订阅者发送更正；
- 跨平台同步按内容身份去重；
- 含个体健康信息的评论只进入受限队列，不进入公开资料库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from domain import (
    Claim,
    ClaimChange,
    ClaimDiff,
    ClaimSnapshot,
    ClaimWithoutEvidenceError,
    Comment,
    CommentVisibility,
    ConflictOfInterestDeclaration,
    ContentType,
    ContentVersion,
    ContentView,
    Correction,
    CorrectionKind,
    DiffSummary,
    Event,
    EvidenceCitation,
    EvidenceSource,
    EvidenceType,
    ExpertReview,
    FieldNotAssignedError,
    MissingReasonError,
    NoVersionAtDateError,
    Notification,
    PlatformPublication,
    Reviewer,
    ReviewVerdict,
    Subscription,
    UnknownClaimError,
    UnknownContentError,
    UnknownEvidenceError,
    UnknownReviewerError,
    UnknownSubscriptionError,
    ValidationError,
    contains_personal_health_info,
    content_identity,
    derive_claim_status,
)


@dataclass
class _ContentRecord:
    """内容的内部可变记录；对外只暴露不可变视图。"""

    id: str
    identity: str
    type: ContentType
    author: str
    platforms: list[PlatformPublication] = field(default_factory=list)
    versions: list[ContentVersion] = field(default_factory=list)


class HealthEvidenceService:
    """健康科普内容与证据审校的应用服务。

    clock 可注入，便于测试与回放；默认使用 UTC 当前时间。
    """

    def __init__(self, clock=None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._evidence: dict[str, EvidenceSource] = {}
        self._contents: dict[str, _ContentRecord] = {}
        self._identity_index: dict[str, str] = {}  # 内容身份 -> 内容 id
        self._claim_index: dict[str, tuple[str, str]] = {}  # 说法 id -> (内容 id, 医学领域)
        self._reviewers: dict[str, Reviewer] = {}
        self._reviews: list[ExpertReview] = []
        self._declarations: list[ConflictOfInterestDeclaration] = []
        self._subscriptions: dict[tuple[str, str], Subscription] = {}
        self._corrections: list[Correction] = []
        self._notifications: list[Notification] = []
        self._public_comments: list[Comment] = []
        self._restricted_comments: list[Comment] = []
        self._events: list[Event] = []
        self._counters: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    def _record(self, kind: str, actor: str, detail: dict) -> None:
        self._events.append(
            Event(seq=len(self._events) + 1, kind=kind, actor=actor, detail=detail, at=self._clock())
        )

    @staticmethod
    def _require_reason(reason: str) -> str:
        if not reason or not reason.strip():
            raise MissingReasonError("修订或撤稿必须写明原因")
        return reason.strip()

    def _require_content(self, content_id: str) -> _ContentRecord:
        record = self._contents.get(content_id)
        if record is None:
            raise UnknownContentError(f"未知内容：{content_id}")
        return record

    def _view(self, record: _ContentRecord) -> ContentView:
        return ContentView(
            content_id=record.id,
            identity=record.identity,
            type=record.type,
            author=record.author,
            platforms=tuple(record.platforms),
            version=record.versions[-1],
        )

    # ------------------------------------------------------------------
    # 证据与审核人登记
    # ------------------------------------------------------------------

    def register_evidence(
        self,
        *,
        type: EvidenceType,
        title: str,
        citation: str,
        published_at: datetime | None = None,
        limitations: tuple[str, ...] = (),
        actor: str = "编辑部",
    ) -> EvidenceSource:
        """登记一条引用资料（指南、论文或个人经验），限制说明随资料保存。"""
        if not title.strip() or not citation.strip():
            raise ValidationError("资料必须标明标题与出处")
        source = EvidenceSource(
            id=self._next_id("EV"),
            type=type,
            title=title.strip(),
            citation=citation.strip(),
            published_at=published_at,
            limitations=tuple(limitations),
        )
        self._evidence[source.id] = source
        self._record("evidence.registered", actor, {"evidence_id": source.id, "type": type.value})
        return source

    def register_reviewer(self, *, name: str, fields, actor: str = "编辑部") -> Reviewer:
        """登记审核人及其获分配的医学领域。"""
        assigned = frozenset(fields)
        if not name.strip() or not assigned:
            raise ValidationError("审核人必须有姓名且至少获分配一个医学领域")
        reviewer = Reviewer(id=self._next_id("RV"), name=name.strip(), assigned_fields=assigned)
        self._reviewers[reviewer.id] = reviewer
        self._record(
            "reviewer.registered", actor, {"reviewer_id": reviewer.id, "fields": sorted(assigned)}
        )
        return reviewer

    # ------------------------------------------------------------------
    # 内容发布与跨平台同步
    # ------------------------------------------------------------------

    def _snapshot_claims(self, claims) -> tuple[ClaimSnapshot, ...]:
        claims = tuple(claims)
        seen: set[str] = set()
        snapshots: list[ClaimSnapshot] = []
        for claim in claims:
            if not isinstance(claim, Claim):
                raise ValidationError("说法必须以 Claim 形式提交")
            if not claim.claim_id.strip():
                raise ValidationError("说法必须有编号")
            if claim.claim_id in seen:
                raise ValidationError(f"说法编号重复：{claim.claim_id}")
            seen.add(claim.claim_id)
            if not claim.text.strip():
                raise ValidationError("说法文本不能为空")
            if not claim.medical_field.strip():
                raise ValidationError("说法必须标明医学领域")
            if not claim.links:
                raise ClaimWithoutEvidenceError(f"说法“{claim.text}”未关联任何证据")
            for link in claim.links:
                if link.evidence_id not in self._evidence:
                    raise UnknownEvidenceError(f"未知证据：{link.evidence_id}")
                if not link.applicable_population.strip():
                    raise ValidationError("证据关联必须标明适用人群")
            conclusion = derive_claim_status(claim.links, self._evidence)
            snapshots.append(
                ClaimSnapshot(
                    claim_id=claim.claim_id,
                    text=claim.text.strip(),
                    medical_field=claim.medical_field.strip(),
                    links=tuple(claim.links),
                    conclusion=conclusion,
                )
            )
        return tuple(snapshots)

    def _check_claim_ownership(self, content_id: str | None, snapshots: tuple[ClaimSnapshot, ...]) -> None:
        for snap in snapshots:
            owner = self._claim_index.get(snap.claim_id)
            if owner is not None and owner[0] != content_id:
                raise ValidationError(f"说法编号已被其他内容使用：{snap.claim_id}")

    def _index_claims(self, content_id: str, snapshots: tuple[ClaimSnapshot, ...]) -> None:
        self._check_claim_ownership(content_id, snapshots)
        for snap in snapshots:
            self._claim_index[snap.claim_id] = (content_id, snap.medical_field)

    def sync_content(
        self,
        *,
        type: ContentType,
        author: str,
        title: str,
        platform: str,
        platform_content_id: str,
        claims,
        editor: str,
        reason: str = "首次发布",
        identity: str | None = None,
        first_published_at: datetime | None = None,
    ) -> ContentView:
        """发布或同步一条内容。

        跨平台同步按内容身份去重：身份已存在时只登记新的平台发布记录，
        不产生重复内容或重复版本。
        """
        now = self._clock()
        identity = identity or content_identity(author, title, first_published_at or now)
        existing_id = self._identity_index.get(identity)
        if existing_id is not None:
            record = self._contents[existing_id]
            known = any(
                p.platform == platform and p.platform_content_id == platform_content_id
                for p in record.platforms
            )
            if not known:
                record.platforms.append(PlatformPublication(platform, platform_content_id, now))
            self._record(
                "content.deduped",
                editor,
                {"content_id": record.id, "platform": platform, "identity": identity},
            )
            return self._view(record)

        snapshots = self._snapshot_claims(claims)
        self._check_claim_ownership(None, snapshots)
        content_id = self._next_id("CT")
        record = _ContentRecord(
            id=content_id,
            identity=identity,
            type=type,
            author=author.strip(),
            platforms=[PlatformPublication(platform, platform_content_id, now)],
        )
        version = ContentVersion(
            number=1,
            editor=editor,
            reason=self._require_reason(reason),
            published_at=now,
            title=title.strip(),
            claims=snapshots,
        )
        record.versions.append(version)
        self._contents[content_id] = record
        self._identity_index[identity] = content_id
        self._index_claims(content_id, snapshots)
        self._record(
            "content.published",
            editor,
            {"content_id": content_id, "platform": platform, "version": 1},
        )
        return self._view(record)

    # ------------------------------------------------------------------
    # 修订、撤稿与更正
    # ------------------------------------------------------------------

    def revise_content(
        self, content_id: str, *, editor: str, reason: str, claims, title: str | None = None
    ) -> ContentVersion:
        """修订内容：必须写明原因，旧版本永久保留。

        若说法集合或结论发生变化，自动向看过旧版本的订阅者发送更正。
        """
        record = self._require_content(content_id)
        reason = self._require_reason(reason)
        now = self._clock()
        snapshots = self._snapshot_claims(claims)
        self._check_claim_ownership(content_id, snapshots)
        previous = record.versions[-1]
        version = ContentVersion(
            number=previous.number + 1,
            editor=editor,
            reason=reason,
            published_at=now,
            title=title.strip() if title is not None else previous.title,
            claims=snapshots,
        )
        record.versions.append(version)
        self._index_claims(content_id, snapshots)
        self._record(
            "content.revised",
            editor,
            {"content_id": content_id, "version": version.number, "reason": reason},
        )
        changes = self._claim_changes(previous.claims, snapshots)
        if changes:
            self._issue_correction(
                record, editor, previous, version, reason, changes, CorrectionKind.REVISION
            )
        return version

    def retract_content(self, content_id: str, *, editor: str, reason: str) -> ContentVersion:
        """撤稿：不删除任何历史版本，追加一个撤稿版本并向看过旧版本的订阅者发送更正。"""
        record = self._require_content(content_id)
        reason = self._require_reason(reason)
        now = self._clock()
        previous = record.versions[-1]
        version = ContentVersion(
            number=previous.number + 1,
            editor=editor,
            reason=reason,
            published_at=now,
            title=previous.title,
            claims=previous.claims,
            retracted=True,
        )
        record.versions.append(version)
        self._record(
            "content.retracted",
            editor,
            {"content_id": content_id, "version": version.number, "reason": reason},
        )
        self._issue_correction(
            record, editor, previous, version, reason, (), CorrectionKind.RETRACTION
        )
        return version

    def _citations(self, snapshot: ClaimSnapshot) -> tuple[EvidenceCitation, ...]:
        citations = []
        for link in snapshot.links:
            source = self._evidence[link.evidence_id]
            citations.append(
                EvidenceCitation(
                    evidence_id=source.id,
                    type=source.type,
                    title=source.title,
                    citation=source.citation,
                    stance=link.stance,
                    applicable_population=link.applicable_population,
                    limitations=source.limitations,
                )
            )
        return tuple(citations)

    def _claim_changes(
        self, before: tuple[ClaimSnapshot, ...], after: tuple[ClaimSnapshot, ...]
    ) -> list[ClaimChange]:
        before_map = {snap.claim_id: snap for snap in before}
        after_map = {snap.claim_id: snap for snap in after}
        changes: list[ClaimChange] = []
        for claim_id, after_snap in after_map.items():
            before_snap = before_map.get(claim_id)
            if before_snap is None:
                changes.append(
                    ClaimChange(claim_id, after_snap.text, None, after_snap.conclusion, self._citations(after_snap))
                )
            elif before_snap.conclusion != after_snap.conclusion or before_snap.links != after_snap.links:
                changes.append(
                    ClaimChange(
                        claim_id,
                        after_snap.text,
                        before_snap.conclusion,
                        after_snap.conclusion,
                        self._citations(after_snap),
                    )
                )
        for claim_id, before_snap in before_map.items():
            if claim_id not in after_map:
                changes.append(
                    ClaimChange(claim_id, before_snap.text, before_snap.conclusion, None, ())
                )
        return changes

    def _issue_correction(
        self,
        record: _ContentRecord,
        editor: str,
        previous: ContentVersion,
        version: ContentVersion,
        reason: str,
        changes,
        kind: CorrectionKind,
    ) -> Correction:
        """向看过旧版本的相关订阅者发送更正。"""
        recipients = tuple(
            sorted(
                sub.subscriber_id
                for (subscriber_id, cid), sub in self._subscriptions.items()
                if cid == record.id and 0 < sub.seen_version < version.number
            )
        )
        correction = Correction(
            id=self._next_id("COR"),
            content_id=record.id,
            kind=kind,
            from_version=previous.number,
            to_version=version.number,
            reason=reason,
            claim_changes=tuple(changes),
            recipients=recipients,
            issued_at=version.published_at,
        )
        self._corrections.append(correction)
        self._record(
            "correction.issued",
            editor,
            {
                "content_id": record.id,
                "correction_id": correction.id,
                "kind": kind.value,
                "recipients": len(recipients),
            },
        )
        for subscriber_id in recipients:
            self._notifications.append(
                Notification(
                    subscriber_id=subscriber_id,
                    correction_id=correction.id,
                    content_id=record.id,
                    sent_at=version.published_at,
                )
            )
            self._record(
                "notification.sent",
                "system",
                {"correction_id": correction.id, "subscriber_id": subscriber_id},
            )
        return correction

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    def subscribe(self, subscriber_id: str, content_id: str) -> Subscription:
        """订阅内容；视为已看到当前最新公众版本。"""
        record = self._require_content(content_id)
        subscription = Subscription(
            subscriber_id=subscriber_id,
            content_id=content_id,
            seen_version=record.versions[-1].number,
        )
        self._subscriptions[(subscriber_id, content_id)] = subscription
        self._record(
            "subscription.added",
            subscriber_id,
            {"content_id": content_id, "seen_version": subscription.seen_version},
        )
        return subscription

    def mark_seen(self, subscriber_id: str, content_id: str, version: int) -> Subscription:
        """记录订阅者实际看到的公众版本。"""
        record = self._require_content(content_id)
        key = (subscriber_id, content_id)
        subscription = self._subscriptions.get(key)
        if subscription is None:
            raise UnknownSubscriptionError(f"订阅者未订阅该内容：{subscriber_id}")
        if not 1 <= version <= record.versions[-1].number:
            raise ValidationError(f"版本号超出范围：{version}")
        subscription.seen_version = version
        return subscription

    # ------------------------------------------------------------------
    # 专家复核与利益冲突（只增不改）
    # ------------------------------------------------------------------

    def submit_review(
        self, reviewer_id: str, claim_id: str, verdict: ReviewVerdict, notes: str = ""
    ) -> ExpertReview:
        """提交专家复核。审核人只能处理获分配的医学领域；复核记录只增不改。"""
        reviewer = self._reviewers.get(reviewer_id)
        if reviewer is None:
            raise UnknownReviewerError(f"未知审核人：{reviewer_id}")
        owner = self._claim_index.get(claim_id)
        if owner is None:
            raise UnknownClaimError(f"未知说法：{claim_id}")
        _, medical_field = owner
        if medical_field not in reviewer.assigned_fields:
            raise FieldNotAssignedError(
                f"审核人 {reviewer.name} 未获分配领域“{medical_field}”，不能复核该说法"
            )
        review = ExpertReview(
            id=self._next_id("ER"),
            claim_id=claim_id,
            reviewer_id=reviewer_id,
            verdict=verdict,
            notes=notes,
            created_at=self._clock(),
        )
        self._reviews.append(review)
        self._record(
            "review.submitted",
            reviewer_id,
            {"claim_id": claim_id, "verdict": verdict.value, "review_id": review.id},
        )
        return review

    def declare_conflict_of_interest(
        self, reviewer_id: str, statement: str
    ) -> ConflictOfInterestDeclaration:
        """登记利益冲突声明。声明只增不改，新声明不覆盖旧声明。"""
        if reviewer_id not in self._reviewers:
            raise UnknownReviewerError(f"未知审核人：{reviewer_id}")
        if not statement.strip():
            raise ValidationError("利益冲突声明不能为空")
        declaration = ConflictOfInterestDeclaration(
            id=self._next_id("CI"),
            reviewer_id=reviewer_id,
            statement=statement.strip(),
            created_at=self._clock(),
        )
        self._declarations.append(declaration)
        self._record(
            "coi.declared", reviewer_id, {"declaration_id": declaration.id}
        )
        return declaration

    def reviews_for(self, claim_id: str) -> tuple[ExpertReview, ...]:
        """某条说法的全部复核历史，按提交顺序。"""
        return tuple(review for review in self._reviews if review.claim_id == claim_id)

    def declarations_for(self, reviewer_id: str) -> tuple[ConflictOfInterestDeclaration, ...]:
        """某位审核人的全部利益冲突声明，按提交顺序。"""
        return tuple(d for d in self._declarations if d.reviewer_id == reviewer_id)

    # ------------------------------------------------------------------
    # 评论与隐私
    # ------------------------------------------------------------------

    def ingest_comment(
        self, content_id: str, *, author: str, text: str, contains_personal_health: bool = False
    ) -> Comment:
        """接收评论。含个体健康信息的评论只进入受限队列，不进入公开资料库。"""
        self._require_content(content_id)
        restricted = bool(contains_personal_health) or contains_personal_health_info(text)
        comment = Comment(
            id=self._next_id("CM"),
            content_id=content_id,
            author=author,
            text=text,
            created_at=self._clock(),
            visibility=CommentVisibility.RESTRICTED if restricted else CommentVisibility.PUBLIC,
        )
        if restricted:
            self._restricted_comments.append(comment)
            self._record("comment.restricted", author, {"comment_id": comment.id})
        else:
            self._public_comments.append(comment)
            self._record("comment.public", author, {"comment_id": comment.id})
        return comment

    def public_comments(self, content_id: str | None = None) -> tuple[Comment, ...]:
        """公开资料库中的评论；保证不含个体健康信息。"""
        return tuple(
            comment
            for comment in self._public_comments
            if content_id is None or comment.content_id == content_id
        )

    def restricted_comments(self) -> tuple[Comment, ...]:
        """受限队列中的评论（含个体健康信息），仅供内部审核。"""
        return tuple(self._restricted_comments)

    # ------------------------------------------------------------------
    # 时间回溯与差异摘要
    # ------------------------------------------------------------------

    def version_as_of(self, content_id: str, when: datetime) -> ContentVersion:
        """指定日期公众实际看到的版本。"""
        record = self._require_content(content_id)
        candidates = [v for v in record.versions if v.published_at <= when]
        if not candidates:
            raise NoVersionAtDateError(f"内容 {content_id} 在 {when.isoformat()} 尚未发布")
        return candidates[-1]

    def content_as_of(self, content_id: str, when: datetime) -> ContentView:
        """按任意发布日期还原当时的内容。"""
        record = self._require_content(content_id)
        version = self.version_as_of(content_id, when)
        return ContentView(
            content_id=record.id,
            identity=record.identity,
            type=record.type,
            author=record.author,
            platforms=tuple(record.platforms),
            version=version,
        )

    def diff_summary(self, content_id: str, when: datetime) -> DiffSummary:
        """历史版本与当前结论之间的差异摘要，两侧均附证据出处。"""
        record = self._require_content(content_id)
        then = self.version_as_of(content_id, when)
        now = record.versions[-1]
        then_map = {snap.claim_id: snap for snap in then.claims}
        now_map = {snap.claim_id: snap for snap in now.claims}
        diffs: list[ClaimDiff] = []
        for claim_id in sorted(set(then_map) | set(now_map)):
            before = then_map.get(claim_id)
            after = now_map.get(claim_id)
            changed = (before is None) != (after is None) or (
                before is not None
                and after is not None
                and (before.conclusion != after.conclusion or before.links != after.links)
            )
            anchor = after or before
            diffs.append(
                ClaimDiff(
                    claim_id=claim_id,
                    text=anchor.text,
                    medical_field=anchor.medical_field,
                    before=before.conclusion if before else None,
                    after=after.conclusion if after else None,
                    changed=changed,
                    evidence_then=self._citations(before) if before else (),
                    evidence_now=self._citations(after) if after else (),
                )
            )
        return DiffSummary(
            content_id=content_id,
            as_of_version=then.number,
            as_of_published_at=then.published_at,
            current_version=now.number,
            current_published_at=now.published_at,
            claim_diffs=tuple(diffs),
        )

    # ------------------------------------------------------------------
    # 查询与追溯
    # ------------------------------------------------------------------

    def get_content(self, content_id: str) -> ContentView:
        """内容的当前视图。"""
        return self._view(self._require_content(content_id))

    def corrections_for(self, content_id: str) -> tuple[Correction, ...]:
        return tuple(c for c in self._corrections if c.content_id == content_id)

    def notifications_for(self, subscriber_id: str) -> tuple[Notification, ...]:
        return tuple(n for n in self._notifications if n.subscriber_id == subscriber_id)

    def events(self) -> tuple[Event, ...]:
        """全部事件，按发生顺序，用于责任追溯。"""
        return tuple(self._events)

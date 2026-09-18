"""健康科普证据纠错的内存存储与业务规则。

所有改变公众可见结论的操作都会写入事件记录，便于责任追溯；
历史版本、专家复核与利益冲突声明仅可追加，不可覆盖。
"""

from __future__ import annotations

import copy
from datetime import date, datetime, time, timezone

from domain import (
    STANCE_LABELS,
    Claim,
    CoiDeclaration,
    Comment,
    Content,
    ContentVersion,
    Correction,
    DomainError,
    Evidence,
    NotFoundError,
    Review,
    Reviewer,
    ScopeError,
    Subscriber,
)


def parse_when(value) -> datetime:
    """把日期或日期时间解析为 UTC 时间；仅日期时视为当日结束（含当天发布的内容）。"""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip()
        if len(text) == 10:
            moment = datetime.combine(date.fromisoformat(text), time.max)
        else:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise DomainError(f"无法解析的日期：{text}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _correction_lines(changes) -> list[str]:
    """把说法差异写成给订阅者看的更正说明，每条都带证据出处。"""
    lines = []
    for change in changes:
        if change["change"] == "modified":
            before, after = change["before"], change["after"]
            cites = "；".join(e["citation_text"] for e in after["evidence"]) or "未登记资料"
            lines.append(
                f"说法「{before['text']}」修订为「{after['text']}」"
                f"（结论：{before['stance_label']}→{after['stance_label']}；依据：{cites}）"
            )
        elif change["change"] == "removed":
            before = change["before"]
            cites = "；".join(e["citation_text"] for e in before["evidence"]) or "未登记资料"
            lines.append(
                f"说法「{before['text']}」已删除"
                f"（原结论：{before['stance_label']}；原依据：{cites}）"
            )
        else:
            after = change["after"]
            cites = "；".join(e["citation_text"] for e in after["evidence"]) or "未登记资料"
            lines.append(
                f"新增说法「{after['text']}」（结论：{after['stance_label']}；依据：{cites}）"
            )
    return lines


class HealthEvidenceService:
    """健康科普内容、证据、复核与更正的核心服务。"""

    def __init__(self):
        self.evidence: dict[str, Evidence] = {}
        self.contents: dict[str, Content] = {}
        self.claims: dict[str, Claim] = {}  # 说法标识 -> 最新已知状态（含已下线说法）
        self.claim_content: dict[str, str] = {}  # 说法标识 -> 所属内容身份
        self.reviewers: dict[str, Reviewer] = {}
        self.reviews: list[Review] = []  # 仅追加
        self.coi_declarations: list[CoiDeclaration] = []  # 仅追加
        self.comments: list[Comment] = []
        self.subscribers: dict[str, Subscriber] = {}
        self.corrections: list[Correction] = []
        self.events: list[dict] = []  # 事件记录，责任追溯
        self._seq = 0

    # ---- 内部工具 -------------------------------------------------

    def _record(self, event_type: str, actor: str, details: dict) -> None:
        self._seq += 1
        self.events.append(
            {
                "seq": self._seq,
                "at": datetime.now(timezone.utc).isoformat(),
                "type": event_type,
                "actor": actor,
                "details": details,
            }
        )

    def _content(self, content_uid: str) -> Content:
        content = self.contents.get(content_uid)
        if content is None:
            raise NotFoundError(f"内容不存在：{content_uid}")
        return content

    def _validate_claims(self, claims, content_uid: str) -> list[Claim]:
        known = set(self.evidence)
        seen = set()
        for claim in claims:
            claim.validate(known)
            if claim.claim_id in seen:
                raise DomainError(f"说法标识重复：{claim.claim_id}")
            seen.add(claim.claim_id)
            owner = self.claim_content.get(claim.claim_id)
            if owner is not None and owner != content_uid:
                raise DomainError(f"说法标识已被其他内容使用：{claim.claim_id}")
        return list(claims)

    def _index_claims(self, content_uid: str, claims) -> None:
        for claim in claims:
            self.claims[claim.claim_id] = claim
            self.claim_content[claim.claim_id] = content_uid

    # ---- 证据资料 -------------------------------------------------

    def register_evidence(self, evidence: Evidence) -> Evidence:
        existing = self.evidence.get(evidence.evidence_id)
        if existing is not None:
            if existing != evidence:
                raise DomainError(f"资料标识已存在且内容不一致：{evidence.evidence_id}")
            return existing
        self.evidence[evidence.evidence_id] = evidence
        self._record(
            "evidence_registered",
            "system",
            {"evidence_id": evidence.evidence_id, "type": evidence.type.value},
        )
        return evidence

    # ---- 内容发布与修订 --------------------------------------------

    def create_content(
        self,
        content_uid: str,
        kind,
        title: str,
        editor_id: str,
        claims,
        evidence=(),
        platform: str | None = None,
        external_id: str | None = None,
        reason: str = "首次发布",
        now: datetime | None = None,
    ) -> Content:
        if content_uid in self.contents:
            raise DomainError(f"内容身份已存在：{content_uid}")
        for item in evidence:
            self.register_evidence(item)
        checked = self._validate_claims(claims, content_uid)
        at = now or datetime.now(timezone.utc)
        content = Content(content_uid=content_uid, kind=kind, title=title)
        if platform:
            if not external_id:
                raise DomainError("登记平台身份时必须提供外部标识")
            content.platform_ids[platform] = external_id
        version = ContentVersion(
            version=1,
            editor_id=editor_id,
            reason=reason,
            published_at=at,
            claims=tuple(copy.deepcopy(c) for c in checked),
        )
        content.versions.append(version)
        self.contents[content_uid] = content
        self._index_claims(content_uid, version.claims)
        self._record(
            "content_created",
            editor_id,
            {"content_uid": content_uid, "version": 1, "kind": content.kind.value},
        )
        return content

    def revise_content(
        self,
        content_uid: str,
        editor_id: str,
        reason: str,
        claims,
        evidence=(),
        now: datetime | None = None,
    ):
        """修订内容：必须写明原因，保留公众曾看到的版本，并向相关订阅者发送更正。"""
        content = self._content(content_uid)
        if not reason or not reason.strip():
            raise DomainError("编辑修订必须写明原因")
        for item in evidence:
            self.register_evidence(item)
        checked = self._validate_claims(claims, content_uid)
        at = now or datetime.now(timezone.utc)
        previous = content.current
        version = ContentVersion(
            version=previous.version + 1,
            editor_id=editor_id,
            reason=reason.strip(),
            published_at=at,
            claims=tuple(copy.deepcopy(c) for c in checked),
        )
        content.versions.append(version)
        self._index_claims(content_uid, version.claims)
        changes = self._claim_changes(previous.claims, version.claims)
        correction = None
        if any(c["change"] in ("modified", "removed") for c in changes):
            correction = self._send_correction(content, previous, version, changes, at)
        self._record(
            "content_revised",
            editor_id,
            {
                "content_uid": content_uid,
                "version": version.version,
                "reason": version.reason,
                "correction": correction.correction_id if correction else None,
            },
        )
        return version, correction

    def _send_correction(
        self, content: Content, previous: ContentVersion, version: ContentVersion, changes, at: datetime
    ) -> Correction:
        recipients = sorted(
            sid for sid, sub in self.subscribers.items() if content.content_uid in sub.content_uids
        )
        correction = Correction(
            correction_id=f"corr-{len(self.corrections) + 1:04d}",
            content_uid=content.content_uid,
            from_version=previous.version,
            to_version=version.version,
            reason=version.reason,
            changes=tuple(_correction_lines(changes)),
            recipient_ids=tuple(recipients),
            created_at=at,
        )
        self.corrections.append(correction)
        for sid in recipients:
            self.subscribers[sid].inbox.append(correction)
        self._record(
            "correction_sent",
            "system",
            {
                "correction_id": correction.correction_id,
                "content_uid": content.content_uid,
                "recipients": recipients,
            },
        )
        return correction

    # ---- 跨平台同步 -------------------------------------------------

    def sync_content(
        self,
        content_uid: str,
        platform: str,
        external_id: str,
        payload: dict | None = None,
        now: datetime | None = None,
    ):
        """按内容身份去重：已知身份只登记平台标识，未知身份按载荷创建。"""
        content = self.contents.get(content_uid)
        if content is None:
            if payload is None:
                raise NotFoundError(f"同步的内容身份未知且未提供内容载荷：{content_uid}")
            content = self.create_content(
                content_uid=content_uid,
                platform=platform,
                external_id=external_id,
                now=now,
                **payload,
            )
            return content, True
        if content.platform_ids.get(platform) != external_id:
            content.platform_ids[platform] = external_id
            self._record(
                "platform_linked",
                "system",
                {"content_uid": content_uid, "platform": platform, "external_id": external_id},
            )
        return content, False

    # ---- 专家复核与利益冲突 ------------------------------------------

    def register_reviewer(self, reviewer_id: str, name: str, domains) -> Reviewer:
        if reviewer_id in self.reviewers:
            raise DomainError(f"审核人已存在：{reviewer_id}")
        reviewer = Reviewer(reviewer_id=reviewer_id, name=name, domains=frozenset(domains))
        self.reviewers[reviewer_id] = reviewer
        self._record("reviewer_registered", reviewer_id, {"domains": sorted(reviewer.domains)})
        return reviewer

    def add_review(
        self,
        review_id: str,
        reviewer_id: str,
        claim_id: str,
        decision: str,
        note: str = "",
        now: datetime | None = None,
    ) -> Review:
        if any(r.review_id == review_id for r in self.reviews):
            raise DomainError("专家复核仅可追加，不可覆盖已有记录")
        reviewer = self.reviewers.get(reviewer_id)
        if reviewer is None:
            raise NotFoundError(f"审核人不存在：{reviewer_id}")
        claim = self.claims.get(claim_id)
        if claim is None:
            raise NotFoundError(f"说法不存在：{claim_id}")
        if claim.domain not in reviewer.domains:
            raise ScopeError(
                f"审核人仅获分配领域 {sorted(reviewer.domains)}，不能复核「{claim.domain}」领域的说法"
            )
        review = Review(
            review_id=review_id,
            reviewer_id=reviewer_id,
            claim_id=claim_id,
            domain=claim.domain,
            decision=decision,
            note=note,
            created_at=now or datetime.now(timezone.utc),
        )
        self.reviews.append(review)
        self._record(
            "review_added",
            reviewer_id,
            {"review_id": review_id, "claim_id": claim_id, "decision": decision},
        )
        return review

    def declare_coi(
        self,
        declaration_id: str,
        reviewer_id: str,
        content_uid: str,
        statement: str,
        now: datetime | None = None,
    ) -> CoiDeclaration:
        if any(d.declaration_id == declaration_id for d in self.coi_declarations):
            raise DomainError("利益冲突声明仅可追加，不可覆盖已有记录")
        if reviewer_id not in self.reviewers:
            raise NotFoundError(f"审核人不存在：{reviewer_id}")
        self._content(content_uid)
        declaration = CoiDeclaration(
            declaration_id=declaration_id,
            reviewer_id=reviewer_id,
            content_uid=content_uid,
            statement=statement,
            created_at=now or datetime.now(timezone.utc),
        )
        self.coi_declarations.append(declaration)
        self._record(
            "coi_declared",
            reviewer_id,
            {"declaration_id": declaration_id, "content_uid": content_uid},
        )
        return declaration

    # ---- 评论与公开资料库 --------------------------------------------

    def add_comment(
        self,
        comment_id: str,
        content_uid: str,
        author_id: str,
        text: str,
        contains_personal_health_info: bool = False,
        now: datetime | None = None,
    ) -> Comment:
        self._content(content_uid)
        if any(c.comment_id == comment_id for c in self.comments):
            raise DomainError(f"评论标识已存在：{comment_id}")
        comment = Comment(
            comment_id=comment_id,
            content_uid=content_uid,
            author_id=author_id,
            text=text,
            contains_personal_health_info=contains_personal_health_info,
            created_at=now or datetime.now(timezone.utc),
        )
        self.comments.append(comment)
        self._record(
            "comment_added",
            author_id,
            {"comment_id": comment_id, "content_uid": content_uid},
        )
        return comment

    def public_dataset(self) -> dict:
        """公开资料库：含个体健康信息的评论一律排除，且不带作者标识。"""
        return {
            "contents": [self.content_view(c) for c in self.contents.values()],
            "comments": [
                {
                    "comment_id": c.comment_id,
                    "content_uid": c.content_uid,
                    "text": c.text,
                    "created_at": c.created_at.isoformat(),
                }
                for c in self.comments
                if not c.contains_personal_health_info
            ],
        }

    # ---- 订阅与更正 ---------------------------------------------------

    def register_subscriber(self, subscriber_id: str, content_uids=()) -> Subscriber:
        subscriber = self.subscribers.get(subscriber_id)
        if subscriber is None:
            subscriber = Subscriber(subscriber_id=subscriber_id)
            self.subscribers[subscriber_id] = subscriber
        subscriber.content_uids = set(content_uids)
        self._record(
            "subscriber_registered",
            subscriber_id,
            {"content_uids": sorted(subscriber.content_uids)},
        )
        return subscriber

    def corrections_for(self, subscriber_id: str) -> list[Correction]:
        subscriber = self.subscribers.get(subscriber_id)
        if subscriber is None:
            raise NotFoundError(f"订阅者不存在：{subscriber_id}")
        return list(subscriber.inbox)

    # ---- 时间还原与差异摘要 ------------------------------------------

    def content_at(self, content_uid: str, when) -> tuple[Content, ContentVersion]:
        """按任意发布日期还原公众当时看到的内容版本。"""
        content = self._content(content_uid)
        at = parse_when(when)
        eligible = [v for v in content.versions if v.published_at <= at]
        if not eligible:
            raise NotFoundError("该日期之前没有已发布的版本")
        return content, eligible[-1]

    def diff_since(self, content_uid: str, since) -> dict:
        """当时内容与当前结论之间的差异摘要，每条差异都带证据出处。"""
        content, base = self.content_at(content_uid, since)
        current = content.current
        changes = self._claim_changes(base.claims, current.claims)
        corrections = [
            c
            for c in self.corrections
            if c.content_uid == content_uid and c.to_version > base.version
        ]
        return {
            "content_uid": content_uid,
            "from": self.version_view(base),
            "to": self.version_view(current),
            "changes": changes,
            "corrections": [self.correction_view(c) for c in corrections],
        }

    def _claim_changes(self, before, after) -> list[dict]:
        old = {c.claim_id: c for c in before}
        new = {c.claim_id: c for c in after}
        ordered = [c.claim_id for c in after] + [c.claim_id for c in before if c.claim_id not in new]
        changes = []
        for claim_id in ordered:
            previous, current = old.get(claim_id), new.get(claim_id)
            if previous is None:
                changes.append(
                    {"change": "added", "claim_id": claim_id, "after": self.claim_view(current)}
                )
            elif current is None:
                changes.append(
                    {"change": "removed", "claim_id": claim_id, "before": self.claim_view(previous)}
                )
            else:
                fields = [
                    name
                    for name in ("text", "domain", "stance", "applicable_population", "evidence_ids")
                    if getattr(previous, name) != getattr(current, name)
                ]
                if fields:
                    changes.append(
                        {
                            "change": "modified",
                            "claim_id": claim_id,
                            "fields": fields,
                            "before": self.claim_view(previous),
                            "after": self.claim_view(current),
                        }
                    )
        return changes

    # ---- 视图（可 JSON 序列化） ---------------------------------------

    def evidence_view(self, evidence: Evidence) -> dict:
        return {
            "evidence_id": evidence.evidence_id,
            "type": evidence.type.value,
            "title": evidence.title,
            "citation": evidence.citation,
            "limitations": list(evidence.limitations),
            "citation_text": evidence.citation_text(),
        }

    def claim_view(self, claim: Claim) -> dict:
        return {
            "claim_id": claim.claim_id,
            "text": claim.text,
            "domain": claim.domain,
            "stance": claim.stance.value,
            "stance_label": STANCE_LABELS[claim.stance.value],
            "applicable_population": claim.applicable_population,
            "evidence": [
                self.evidence_view(self.evidence[e])
                for e in claim.evidence_ids
                if e in self.evidence
            ],
        }

    def version_view(self, version: ContentVersion) -> dict:
        return {
            "version": version.version,
            "editor_id": version.editor_id,
            "reason": version.reason,
            "published_at": version.published_at.isoformat(),
            "claims": [self.claim_view(c) for c in version.claims],
        }

    def content_view(self, content: Content) -> dict:
        return {
            "content_uid": content.content_uid,
            "kind": content.kind.value,
            "title": content.title,
            "platform_ids": dict(content.platform_ids),
            "current_version": content.current.version,
            "versions": [self.version_view(v) for v in content.versions],
        }

    def correction_view(self, correction: Correction) -> dict:
        return {
            "correction_id": correction.correction_id,
            "content_uid": correction.content_uid,
            "from_version": correction.from_version,
            "to_version": correction.to_version,
            "reason": correction.reason,
            "changes": list(correction.changes),
            "recipient_ids": list(correction.recipient_ids),
            "created_at": correction.created_at.isoformat(),
        }

    def review_view(self, review: Review) -> dict:
        return {
            "review_id": review.review_id,
            "reviewer_id": review.reviewer_id,
            "claim_id": review.claim_id,
            "domain": review.domain,
            "decision": review.decision,
            "note": review.note,
            "created_at": review.created_at.isoformat(),
        }

    def coi_view(self, declaration: CoiDeclaration) -> dict:
        return {
            "declaration_id": declaration.declaration_id,
            "reviewer_id": declaration.reviewer_id,
            "content_uid": declaration.content_uid,
            "statement": declaration.statement,
            "created_at": declaration.created_at.isoformat(),
        }

    def comment_view(self, comment: Comment) -> dict:
        return {
            "comment_id": comment.comment_id,
            "content_uid": comment.content_uid,
            "contains_personal_health_info": comment.contains_personal_health_info,
            "created_at": comment.created_at.isoformat(),
        }

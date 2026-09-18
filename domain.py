"""健康科普证据纠错的领域对象。

每条医学说法分别关联指南、论文或个人经验，并标记支持、反驳、
尚无定论与适用人群；历史版本、专家复核与利益冲突声明仅可追加，
不可覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DomainError(Exception):
    """违反领域规则。"""


class NotFoundError(DomainError):
    """对象不存在。"""


class ScopeError(DomainError):
    """审核人超出获分配的医学领域。"""


class EvidenceType(str, Enum):
    GUIDELINE = "guideline"  # 指南
    PAPER = "paper"  # 论文
    PERSONAL_EXPERIENCE = "personal_experience"  # 个人经验


class ClaimStance(str, Enum):
    SUPPORTED = "supported"  # 支持
    REFUTED = "refuted"  # 反驳
    INCONCLUSIVE = "inconclusive"  # 尚无定论


STANCE_LABELS = {
    ClaimStance.SUPPORTED.value: "支持",
    ClaimStance.REFUTED.value: "反驳",
    ClaimStance.INCONCLUSIVE.value: "尚无定论",
}


class ContentKind(str, Enum):
    ARTICLE = "article"
    VIDEO = "video"


@dataclass(frozen=True)
class Evidence:
    """一条可作为说法依据的资料；研究限制随引用展示，不可省略。"""

    evidence_id: str
    type: EvidenceType
    title: str
    citation: str  # 出处：期刊、机构或链接
    limitations: tuple[str, ...] = ()

    def citation_text(self) -> str:
        """带出处的引用文本，研究限制一并呈现。"""
        text = f"{self.title}（{self.citation}）"
        if self.limitations:
            text += "；限制：" + "、".join(self.limitations)
        return text


@dataclass
class Claim:
    """一条医学说法，必须分别关联至少一条证据资料。"""

    claim_id: str
    text: str
    domain: str  # 医学领域，决定可由哪些审核人复核
    stance: ClaimStance
    applicable_population: str  # 适用人群
    evidence_ids: tuple[str, ...]

    def validate(self, known_evidence: set[str]) -> None:
        if not self.text.strip():
            raise DomainError("医学说法的内容不能为空")
        if not self.domain.strip():
            raise DomainError("医学说法必须标明所属医学领域")
        if not self.applicable_population.strip():
            raise DomainError("医学说法必须标明适用人群")
        if not self.evidence_ids:
            raise DomainError("每条医学说法必须分别关联指南、论文或个人经验")
        missing = [e for e in self.evidence_ids if e not in known_evidence]
        if missing:
            raise DomainError(f"说法引用了未登记的资料：{missing}")


@dataclass(frozen=True)
class ContentVersion:
    """公众曾看到的一个内容版本，发布后不可更改。"""

    version: int
    editor_id: str
    reason: str  # 修订原因，必填
    published_at: datetime
    claims: tuple[Claim, ...]


@dataclass
class Content:
    """一篇文章或视频；content_uid 是跨平台去重所依据的内容身份。"""

    content_uid: str
    kind: ContentKind
    title: str
    platform_ids: dict[str, str] = field(default_factory=dict)  # 平台 -> 外部标识
    versions: list[ContentVersion] = field(default_factory=list)

    @property
    def current(self) -> ContentVersion:
        return self.versions[-1]


@dataclass(frozen=True)
class Correction:
    """一次向公众可见结论变更发出的更正。"""

    correction_id: str
    content_uid: str
    from_version: int
    to_version: int
    reason: str
    changes: tuple[str, ...]  # 人类可读的差异描述，含证据出处
    recipient_ids: tuple[str, ...]  # 收到更正的相关订阅者
    created_at: datetime


@dataclass(frozen=True)
class Reviewer:
    reviewer_id: str
    name: str
    domains: frozenset[str]  # 获分配的医学领域


@dataclass(frozen=True)
class Review:
    """专家复核记录，仅可追加，不可覆盖。"""

    review_id: str
    reviewer_id: str
    claim_id: str
    domain: str
    decision: str
    note: str
    created_at: datetime


@dataclass(frozen=True)
class CoiDeclaration:
    """利益冲突声明，仅可追加，不可覆盖。"""

    declaration_id: str
    reviewer_id: str
    content_uid: str
    statement: str
    created_at: datetime


@dataclass(frozen=True)
class Comment:
    comment_id: str
    content_uid: str
    author_id: str
    text: str
    contains_personal_health_info: bool  # 含个体健康信息，不得进入公开资料库
    created_at: datetime


@dataclass
class Subscriber:
    subscriber_id: str
    content_uids: set[str] = field(default_factory=set)
    inbox: list[Correction] = field(default_factory=list)

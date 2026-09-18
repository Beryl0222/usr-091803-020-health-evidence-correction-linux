"""健康科普证据纠错的领域对象与纯规则。

本模块只包含不可变数据与派生逻辑，不依赖任何外部存储；
发布、修订、更正、审校等业务规则见 health_service.py。

核心约定：
- 每条医学说法（Claim）必须关联至少一条证据（指南、论文或个人经验），
  关联上标明立场（支持/反驳/尚无定论）与适用人群；
- 说法结论由证据关联推导：指南与论文优先，仅有个人经验时一律“尚无定论”；
- 公众曾看到的每个内容版本都是不可变快照，永久保留；
- 专家复核与利益冲突声明只增不改。
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from datetime import datetime


class EvidenceType(enum.Enum):
    """引用资料类型。"""

    GUIDELINE = "指南"
    PAPER = "论文"
    PERSONAL_EXPERIENCE = "个人经验"


class Stance(enum.Enum):
    """一条证据对说法的立场。"""

    SUPPORTS = "支持"
    REFUTES = "反驳"
    INCONCLUSIVE = "尚无定论"


class ClaimStatus(enum.Enum):
    """由证据关联推导出的说法结论。"""

    SUPPORTED = "证据支持"
    REFUTED = "证据反驳"
    INCONCLUSIVE = "尚无定论"
    CONTESTED = "证据分歧"
    UNVERIFIED = "未评估"


class ContentType(enum.Enum):
    ARTICLE = "文章"
    VIDEO = "视频"


class ReviewVerdict(enum.Enum):
    APPROVED = "通过"
    CHANGES_REQUESTED = "需修改"


class CommentVisibility(enum.Enum):
    PUBLIC = "公开"
    RESTRICTED = "受限"


class CorrectionKind(enum.Enum):
    REVISION = "修订"
    RETRACTION = "撤稿"


# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """领域规则错误基类。"""


class ValidationError(DomainError):
    """输入不满足领域约束（如字段为空、编号重复）。"""


class MissingReasonError(DomainError):
    """修订或撤稿必须写明原因。"""


class ClaimWithoutEvidenceError(DomainError):
    """每条医学说法必须关联至少一条证据。"""


class UnknownContentError(DomainError):
    pass


class UnknownClaimError(DomainError):
    pass


class UnknownEvidenceError(DomainError):
    pass


class UnknownReviewerError(DomainError):
    pass


class UnknownSubscriptionError(DomainError):
    pass


class FieldNotAssignedError(DomainError):
    """审核人只能处理获分配的医学领域。"""


class NoVersionAtDateError(DomainError):
    """指定日期早于内容的首次发布。"""


# ---------------------------------------------------------------------------
# 证据与说法
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSource:
    """一条可引用的资料来源，限制说明不可省略。"""

    id: str
    type: EvidenceType
    title: str
    citation: str  # 出处：期刊/DOI/链接或讲述者
    published_at: datetime | None = None
    limitations: tuple[str, ...] = ()  # 例如“样本绝经状态为自报”“未全面检测激素”


@dataclass(frozen=True)
class ClaimLink:
    """说法与证据的关联，含立场与适用人群。"""

    evidence_id: str
    stance: Stance
    applicable_population: str


@dataclass(frozen=True)
class Claim:
    """发布或修订时提交的一条医学说法。"""

    claim_id: str
    text: str
    medical_field: str  # 医学领域，用于审核分配
    links: tuple[ClaimLink, ...]


@dataclass(frozen=True)
class ClaimSnapshot:
    """某一版本中说法的不可变快照，结论在发布时推导并固化。"""

    claim_id: str
    text: str
    medical_field: str
    links: tuple[ClaimLink, ...]
    conclusion: ClaimStatus


_RESEARCH_TYPES = frozenset({EvidenceType.GUIDELINE, EvidenceType.PAPER})


def derive_claim_status(
    links: tuple[ClaimLink, ...], evidence_by_id: dict[str, EvidenceSource]
) -> ClaimStatus:
    """由证据关联推导说法结论。

    规则：指南与论文优先于个人经验；仅有个人经验时一律“尚无定论”，
    因为个案不能为医学说法定论；研究证据立场不一致时为“证据分歧”。
    """
    if not links:
        return ClaimStatus.UNVERIFIED
    research = [link for link in links if evidence_by_id[link.evidence_id].type in _RESEARCH_TYPES]
    if not research:
        return ClaimStatus.INCONCLUSIVE
    stances = {link.stance for link in research}
    if len(stances) > 1:
        return ClaimStatus.CONTESTED
    only = next(iter(stances))
    return {
        Stance.SUPPORTS: ClaimStatus.SUPPORTED,
        Stance.REFUTES: ClaimStatus.REFUTED,
        Stance.INCONCLUSIVE: ClaimStatus.INCONCLUSIVE,
    }[only]


# ---------------------------------------------------------------------------
# 内容版本与视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentVersion:
    """公众曾看到的一个内容版本，永久保留、不可修改。"""

    number: int
    editor: str
    reason: str  # 修订原因（首版为“首次发布”）
    published_at: datetime
    title: str
    claims: tuple[ClaimSnapshot, ...]
    retracted: bool = False


@dataclass(frozen=True)
class PlatformPublication:
    """内容在某个平台上的发布记录。"""

    platform: str
    platform_content_id: str
    synced_at: datetime


@dataclass(frozen=True)
class ContentView:
    """内容在某一时刻的还原视图。"""

    content_id: str
    identity: str
    type: ContentType
    author: str
    platforms: tuple[PlatformPublication, ...]
    version: ContentVersion


def content_identity(author: str, title: str, first_published_at: datetime) -> str:
    """跨平台去重使用的内容身份。

    同一作者、标题与首次发布时间视为同一内容；平台若已有统一标识，
    可在同步时显式传入。
    """
    raw = f"{author.strip()}|{title.strip()}|{first_published_at.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 更正与通知
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceCitation:
    """差异摘要中一条说法的证据出处。"""

    evidence_id: str
    type: EvidenceType
    title: str
    citation: str
    stance: Stance
    applicable_population: str
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class ClaimChange:
    """一次修订中某条说法的结论变化。"""

    claim_id: str
    text: str
    before: ClaimStatus | None  # None 表示新增说法
    after: ClaimStatus | None  # None 表示移除说法
    evidence: tuple[EvidenceCitation, ...]  # 当前结论的证据出处


@dataclass(frozen=True)
class Correction:
    """一次向公众发出的更正。"""

    id: str
    content_id: str
    kind: CorrectionKind
    from_version: int
    to_version: int
    reason: str
    claim_changes: tuple[ClaimChange, ...]
    recipients: tuple[str, ...]  # 看过旧版本、被送达更正的订阅者
    issued_at: datetime


@dataclass(frozen=True)
class Notification:
    """送达给单个订阅者的更正通知。"""

    subscriber_id: str
    correction_id: str
    content_id: str
    sent_at: datetime


@dataclass
class Subscription:
    """订阅关系；seen_version 记录订阅者最后看到的公众版本。"""

    subscriber_id: str
    content_id: str
    seen_version: int


# ---------------------------------------------------------------------------
# 专家复核、利益冲突与评论
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reviewer:
    """审核人，只处理获分配的医学领域。"""

    id: str
    name: str
    assigned_fields: frozenset[str]


@dataclass(frozen=True)
class ExpertReview:
    """一次专家复核。记录只增不改，历史复核全部保留。"""

    id: str
    claim_id: str
    reviewer_id: str
    verdict: ReviewVerdict
    notes: str
    created_at: datetime


@dataclass(frozen=True)
class ConflictOfInterestDeclaration:
    """一条利益冲突声明。只增不改，新声明不覆盖旧声明。"""

    id: str
    reviewer_id: str
    statement: str
    created_at: datetime


@dataclass(frozen=True)
class Comment:
    """一条评论。含个体健康信息的评论只进入受限队列，不进入公开资料库。"""

    id: str
    content_id: str
    author: str
    text: str
    created_at: datetime
    visibility: CommentVisibility


_PERSONAL_HEALTH_MARKERS = (
    "病史",
    "病历",
    "确诊",
    "被诊断",
    "在服用",
    "在吃药",
    "医生给我开",
    "我的激素",
    "我的检查",
    "我的指标",
    "我的手术",
    "我的症状",
    "我的处方",
    "我的化验",
)


def contains_personal_health_info(text: str) -> bool:
    """识别评论中的个体健康信息。

    这里用确定性的关键词规则代替分类器，保证行为可测试；
    接入更完善的模型时只需替换本函数，公开资料库的隔离不变量不变。
    """
    return any(marker in text for marker in _PERSONAL_HEALTH_MARKERS)


# ---------------------------------------------------------------------------
# 时间回溯与差异摘要
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimDiff:
    """某条说法在历史版本与当前版本之间的差异，两侧均附证据出处。"""

    claim_id: str
    text: str
    medical_field: str
    before: ClaimStatus | None
    after: ClaimStatus | None
    changed: bool
    evidence_then: tuple[EvidenceCitation, ...]
    evidence_now: tuple[EvidenceCitation, ...]


@dataclass(frozen=True)
class DiffSummary:
    """按发布日期还原的内容与当前结论之间的差异摘要。"""

    content_id: str
    as_of_version: int
    as_of_published_at: datetime
    current_version: int
    current_published_at: datetime
    claim_diffs: tuple[ClaimDiff, ...]

    @property
    def changed_count(self) -> int:
        return sum(1 for diff in self.claim_diffs if diff.changed)

    def to_dict(self) -> dict:
        def citation_dict(c: EvidenceCitation) -> dict:
            return {
                "evidence_id": c.evidence_id,
                "type": c.type.value,
                "title": c.title,
                "citation": c.citation,
                "stance": c.stance.value,
                "applicable_population": c.applicable_population,
                "limitations": list(c.limitations),
            }

        def status_value(status: ClaimStatus | None) -> str | None:
            return status.value if status is not None else None

        return {
            "content_id": self.content_id,
            "as_of_version": self.as_of_version,
            "as_of_published_at": self.as_of_published_at.isoformat(),
            "current_version": self.current_version,
            "current_published_at": self.current_published_at.isoformat(),
            "changed_count": self.changed_count,
            "claim_diffs": [
                {
                    "claim_id": d.claim_id,
                    "text": d.text,
                    "medical_field": d.medical_field,
                    "before": status_value(d.before),
                    "after": status_value(d.after),
                    "changed": d.changed,
                    "evidence_then": [citation_dict(c) for c in d.evidence_then],
                    "evidence_now": [citation_dict(c) for c in d.evidence_now],
                }
                for d in self.claim_diffs
            ],
        }


# ---------------------------------------------------------------------------
# 事件追溯
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """一条不可变事件，用于责任追溯。"""

    seq: int
    kind: str
    actor: str
    detail: dict
    at: datetime

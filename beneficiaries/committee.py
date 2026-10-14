"""لجنة المساعدات: عرض دراسات الحالة والمساعدات الكبيرة على اللجنة، وتصويت الأعضاء عن بُعد، وصدور القرار بالنصاب.

لا يُعتمد المستند حتى يصدر قرار اللجنة. أي تعديل عليه بعد العرض يُبطل العرض ويتطلب عرضاً جديداً.
"""
import hashlib

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.utils import flt

ASSESSMENT, DISBURSEMENT = "case-assessment", "aid-disbursement"


def document_value(doc):
    """قيمة المساعدة للمقارنة بحد العرض على اللجنة (العينية بتقدير تكلفة أصنافها)."""
    if doc.doctype_slug != DISBURSEMENT:
        return flt(0)
    if doc.amount:
        return flt(doc.amount)
    from stock.models import Bin

    total = flt(0)
    for row in doc.items.select_related("item"):
        rate = row.item.buying_rate
        if doc.warehouse_id:
            bin_ = Bin.objects.filter(item=row.item, warehouse_id=doc.warehouse_id).first()
            if bin_ and bin_.valuation_rate:
                rate = bin_.valuation_rate
        total += flt(row.qty * rate)
    return total


def committee_for(doc):
    """اللجنة التي يجب أن يُعرض عليها المستند، أو None."""
    from beneficiaries.models import AidCommittee

    committee = AidCommittee.active_for(doc.company)
    if committee is None:
        return None
    if doc.doctype_slug == ASSESSMENT and committee.review_assessments:
        return committee
    if (doc.doctype_slug == DISBURSEMENT and committee.disbursement_threshold
            and document_value(doc) >= committee.disbursement_threshold):
        return committee
    return None


def fingerprint(doc):
    if doc.doctype_slug == DISBURSEMENT:
        return doc.content_fingerprint()
    scores = sorted((s.criterion, s.max_score, s.score) for s in doc.scores.all())
    content = [doc.beneficiary_id, doc.category_id, doc.findings, scores]
    return hashlib.sha256(repr(content).encode()).hexdigest()


def case_for(doc):
    from beneficiaries.models import CommitteeCase

    if not doc.pk:
        return None
    return CommitteeCase.all_objects.filter(company=doc.company, doctype=doc.doctype_slug,
                                            object_id=doc.pk).first()


def title_for(doc):
    if doc.doctype_slug == ASSESSMENT:
        return f"دراسة حالة {doc} — {doc.beneficiary.full_name} (درجة الاستحقاق {doc.score_percent}%)"
    return f"صرف مساعدة {doc} — {doc.beneficiary.full_name} — {doc.program}"


def refer(doc, user):
    """يعرض المستند على اللجنة (ويُبطل أي عرض سابق تغيرت بعده البيانات) وينبه الأعضاء."""
    from beneficiaries.models import CommitteeCase
    from core.notify import notify

    if not doc.is_draft:
        raise ValidationError("يُعرض على اللجنة ما لم يُعتمد بعد.")
    doc.calculate_totals()  # المبلغ الافتراضي للبرنامج يُحسب قبل مقارنته بحد العرض
    doc.save()
    committee = committee_for(doc)
    if committee is None:
        raise ValidationError("هذا المستند لا يحتاج عرضاً على اللجنة.")
    members = list(committee.members.all())
    if len(members) < committee.quorum:
        raise ValidationError(f"عدد أعضاء اللجنة ({len(members)}) أقل من النصاب ({committee.quorum}). "
                              "أكمل الأعضاء من إعدادات لجنة المساعدات.")
    if doc.doctype_slug == ASSESSMENT:
        doc.validate_scores()  # الدرجات مكتملة قبل العرض
    else:
        doc.check_eligibility()
    with transaction.atomic():
        CommitteeCase.all_objects.filter(company=doc.company, doctype=doc.doctype_slug, object_id=doc.pk).delete()
        case = CommitteeCase.all_objects.create(
            company=doc.company, committee=committee, doctype=doc.doctype_slug, object_id=doc.pk,
            title=title_for(doc)[:255], amount=document_value(doc), fingerprint=fingerprint(doc), referred_by=user)
    notify(doc.company, members, f"معروض على {committee}: {case.title}", doc.get_absolute_url())
    return case


def tally(case):
    approve = case.votes.filter(vote="approve").count()
    reject = case.votes.filter(vote="reject").count()
    return approve, reject


def _decision(case):
    """القرار بعد آخر صوت: approved أو rejected أو None (لم يكتمل)."""
    committee = case.committee
    approve, reject = tally(case)
    members = committee.members.count()
    if committee.decision_rule == "unanimous":
        if reject:
            return "rejected"
        return "approved" if approve >= committee.quorum else None
    if approve + reject < committee.quorum:
        return None
    if approve != reject:
        return "approved" if approve > reject else "rejected"
    # تعادل: يُنتظر بقية الأعضاء، وإن صوّت الجميع وبقي التعادل لا تُعتمد
    return "rejected" if approve + reject >= members else None


def vote(case, user, choice, note=""):
    from beneficiaries.models import CaseStatus, CommitteeVote
    from core.notify import notify

    if choice not in ("approve", "reject"):
        raise ValidationError("اختر موافق أو غير موافق.")
    if case.status != CaseStatus.PENDING:
        raise ValidationError("صدر قرار اللجنة في هذا الموضوع.")
    if not case.committee.members.filter(pk=user.pk).exists():
        raise ValidationError("التصويت لأعضاء اللجنة فقط.")
    doc = document_of(case)
    if doc is None or not doc.is_draft:
        raise ValidationError("المستند لم يعد مسودة.")
    if fingerprint(doc) != case.fingerprint:
        raise ValidationError("تغيّرت بيانات المستند بعد عرضه على اللجنة؛ يجب عرضه من جديد.")
    if choice == "reject" and not (note or "").strip():
        raise ValidationError("اذكر سبب عدم الموافقة.")
    with transaction.atomic():
        CommitteeVote.objects.update_or_create(case=case, user=user,
                                               defaults={"vote": choice, "note": (note or "").strip()[:255]})
        decision = _decision(case)
        if decision:
            case.status, case.decided_at = decision, timezone.now()
            case.save(update_fields=["status", "decided_at"])
    if decision and case.referred_by:
        notify(case.company, [case.referred_by], f"{case.get_status_display()}: {case.title}", doc.get_absolute_url())
    return case


def document_of(case):
    from core.registry import REGISTRY

    dt = REGISTRY.get(case.doctype)
    return dt.model.all_objects.filter(company=case.company, pk=case.object_id).first() if dt else None


def check_before_submit(doc):
    """يمنع اعتماد المستند قبل قرار اللجنة. يعيد الحالة (القرار) أو None إن لم يحتج عرضاً."""
    from beneficiaries.models import CaseStatus

    committee = committee_for(doc)
    if committee is None:
        return None
    case = case_for(doc)
    if case is None:
        raise ValidationError(f"يحتاج هذا المستند قرار {committee} قبل الاعتماد. اضغط «عرض على اللجنة».")
    if case.fingerprint != fingerprint(doc):
        raise ValidationError("تغيّرت بيانات المستند بعد عرضه على اللجنة. اعرضه من جديد.")
    if case.status == CaseStatus.PENDING:
        approve, reject = tally(case)
        raise ValidationError(f"بانتظار قرار {committee}: صوّت {approve + reject} من النصاب {committee.quorum}.")
    if case.status == CaseStatus.REJECTED and doc.doctype_slug == DISBURSEMENT:
        raise ValidationError(f"رفضت {committee} صرف هذه المساعدة؛ لا يمكن اعتمادها.")
    return case


def pending_for(user, company):
    """المواضيع المعروضة على لجنة يكون المستخدم عضواً فيها ولم يصوّت عليها بعد."""
    from beneficiaries.models import CaseStatus, CommitteeCase

    return (CommitteeCase.all_objects.filter(company=company, status=CaseStatus.PENDING, committee__is_active=True,
                                             committee__members=user)
            .exclude(votes__user=user).select_related("committee"))

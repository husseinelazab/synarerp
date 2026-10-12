"""سلاسل الاعتماد متعددة المستويات: يُعتمد المستند بعد موافقة كل مستوى مطلوب بالترتيب.

زر «اعتماد» نفسه يسجل موافقة المستخدم على أدنى مستوى معلق يملكه؛ وحين تكتمل المستويات يُعتمد المستند.
"""
from django.core.exceptions import ValidationError

from core.utils import flt


def document_amount(doc):
    for field in ("base_grand_total", "base_amount", "grand_total", "amount", "total_debit", "total_amount",
                  "total_charges"):
        value = getattr(doc, field, None)
        if value:
            return flt(abs(value))
    return flt(0)


def required_rules(doc):
    """{المستوى: [القواعد]} المطلوبة لهذا المستند حسب نوعه وقيمته."""
    from core.models import ApprovalRule

    amount = document_amount(doc)
    levels = {}
    for rule in ApprovalRule.objects.filter(doctype=doc.doctype_slug, disabled=False, min_amount__lte=amount):
        levels.setdefault(rule.level, []).append(rule)
    return dict(sorted(levels.items()))


def approved_levels(doc):
    from core.models import ApprovalRecord

    return set(ApprovalRecord.objects.filter(doctype=doc.doctype_slug, object_id=doc.pk).values_list("level", flat=True))


def status(doc):
    """للعرض: [{level, approvers, record}] أو [] إن لم يحتج المستند اعتماداً."""
    from core.models import ApprovalRecord

    records = {r.level: r for r in ApprovalRecord.objects.filter(doctype=doc.doctype_slug, object_id=doc.pk)
               .select_related("user")}
    return [{"level": level, "approvers": " أو ".join(_approver_label(r) for r in rules), "record": records.get(level)}
            for level, rules in required_rules(doc).items()]


def _approver_label(rule):
    if rule.approver_role == "user" and rule.approver_id:
        return rule.approver.get_full_name() or rule.approver.username
    return rule.get_approver_role_display()


def approve_or_block(doc, membership, user):
    """قبل الاعتماد: يسجل موافقة المستخدم على ما يملكه من المستويات المعلقة بالترتيب.

    تعيد None إن اكتملت المستويات (يُعتمد المستند)، وإلا رسالة الانتظار بعد تنبيه أصحاب المستوى التالي.
    """
    from core.models import ApprovalRecord
    from core.notify import notify

    rules = required_rules(doc)
    if not rules:
        return None
    done = approved_levels(doc)
    approved_now = []
    for level, level_rules in rules.items():
        if level in done:
            continue
        if any(r.can_approve(membership) for r in level_rules):
            ApprovalRecord.objects.create(company=doc.company, doctype=doc.doctype_slug, object_id=doc.pk,
                                          level=level, user=user)
            done.add(level)
            approved_now.append(level)
            continue
        # أول مستوى معلق لا يملكه المستخدم: يُنبَّه أصحابه وينتظر المستند
        notify(doc.company, approvers_for(doc.company, level_rules),
               f"بانتظار اعتمادك: {doc._meta.verbose_name} {doc} ({document_amount(doc):,.2f}) — المستوى {level}",
               doc.get_absolute_url())
        prefix = f"سُجل اعتمادك للمستوى {'، '.join(map(str, approved_now))}. " if approved_now else ""
        return f"{prefix}المستند بانتظار اعتماد المستوى {level} ({' أو '.join(_approver_label(r) for r in level_rules)}). تم تنبيههم."
    return None


def approvers_for(company, rules):
    from core.models import Membership, Role

    users = set()
    for rule in rules:
        if rule.approver_role == "user" and rule.approver_id:
            users.add(rule.approver)
            continue
        roles = [Role.OWNER] if rule.approver_role == "owner" else [Role.OWNER, Role.ADMIN]
        users |= {m.user for m in Membership.objects.filter(company=company, is_active=True, role__in=roles)
                  .select_related("user")}
    return list(users)


def check_before_submit(doc, request):
    """تستدعيها نقاط الاعتماد من الواجهة وواجهة API: ترفع خطأً بالانتظار إن بقيت مستويات معلقة."""
    message = approve_or_block(doc, getattr(request, "membership", None), getattr(request, "user", None))
    if message:
        raise ApprovalPending(message)


def reset(doc):
    """تعديل المسودة يُبطل الموافقات السابقة (قد تغيرت القيمة)."""
    from core.models import ApprovalRecord

    if doc.pk:
        ApprovalRecord.objects.filter(doctype=doc.doctype_slug, object_id=doc.pk).delete()


class ApprovalPending(ValidationError):
    pass

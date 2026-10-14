from django import template

register = template.Library()


@register.simple_tag
def committee_panel(doc, user):
    """بيانات لوحة لجنة المساعدات في صفحة المستند: هل يحتاج عرضاً، وحالة العرض، والأصوات، وصوت المستخدم."""
    from beneficiaries.committee import case_for, committee_for, fingerprint, tally

    committee = committee_for(doc) if doc.is_draft else None
    case = case_for(doc)
    if committee is None and case is None:
        return None
    if case is None:
        return {"committee": committee, "case": None}
    votes = list(case.votes.select_related("user"))
    approve, reject = tally(case)
    return {
        "committee": case.committee, "case": case, "votes": votes, "approve": approve, "reject": reject,
        "stale": doc.is_draft and case.fingerprint != fingerprint(doc),
        "is_member": case.committee.members.filter(pk=user.pk).exists(),
        "my_vote": next((v for v in votes if v.user_id == user.pk), None),
    }

from core.registry import ChildTable, DocType, register
from governance.models import (BoardSeat, BoardTerm, GovernanceAssessment, GovernanceIndicator, GovernanceScore,
                               GovernanceStandard, Meeting, MeetingAttendance, MeetingDecision, Member,
                               MembershipCategory, MembershipFee)

register(DocType(
    slug="member", model=Member, module="members",
    list_fields=["member_number", "full_name", "category", "phone", "join_date", "status"],
    search_fields=["member_number", "full_name", "national_id", "phone"], detail_template="governance/_member_detail.html",
))
register(DocType(
    slug="membership-category", model=MembershipCategory, module="members",
    list_fields=["name", "annual_fee", "has_voting_rights"], search_fields=["name"],
))
register(DocType(
    slug="membership-fee", model=MembershipFee, module="members",
    list_fields=["member", "year", "amount", "collected_amount", "status"],
    search_fields=["member__full_name", "member__member_number"],
))
register(DocType(
    slug="board-term", model=BoardTerm, module="members",
    list_fields=["name", "start_date", "end_date", "approval_reference"], search_fields=["name"],
    child=ChildTable(BoardSeat, ["member", "position"], label="أعضاء المجلس"),
))
register(DocType(
    slug="meeting", model=Meeting, module="members",
    list_fields=["name", "posting_date", "meeting_type", "title", "present_count", "eligible_count"],
    form_fields=["meeting_type", "title", "posting_date", "start_time", "location", "quorum_percent", "agenda",
                 "minutes", "remarks"],
    search_fields=["name", "title", "agenda", "minutes"],
    child=ChildTable(MeetingAttendance, ["member", "present", "mode", "note"], label="الحضور", min_rows=0,
                     help="اتركه فارغاً لتُجلب قائمة من يحق لهم الحضور عند الحفظ: الأعضاء المصوتون للجمعية العمومية، "
                          "وأعضاء الدورة السارية لمجلس الإدارة."),
    detail_template="governance/_meeting_detail.html",
))
register(DocType(
    slug="meeting-decision", model=MeetingDecision, module="members",
    list_fields=["meeting", "text", "responsible", "due_date", "status"], search_fields=["text", "meeting__title"],
))
register(DocType(
    slug="governance-standard", model=GovernanceStandard, module="governance",
    list_fields=["code", "name", "weight_percent", "version"], search_fields=["code", "name"],
))
register(DocType(
    slug="governance-indicator", model=GovernanceIndicator, module="governance",
    list_fields=["code", "standard", "group", "text", "weight", "auto_check", "check_key", "is_active"],
    search_fields=["code", "text", "group"], detail_template="governance/_indicator_detail.html",
))
register(DocType(
    slug="governance-assessment", model=GovernanceAssessment, module="governance",
    list_fields=["name", "year", "framework", "posting_date", "score_percent"],
    form_fields=["year", "framework", "posting_date", "notes", "remarks"],
    child=ChildTable(GovernanceScore, ["indicator", "weight", "status", "note"], label="المؤشرات", min_rows=0,
                     display_fields=["indicator", "weight", "suggested_status", "status", "auto_result", "note"],
                     help="اتركه فارغاً لتُجلب مؤشرات الإطار المختار؛ المؤشرات الآلية تُقيّم من بيانات النظام عند كل حفظ، "
                          "ومؤشرات معيار المركز الوطني تُعرض لها حالة مقترحة تؤكدها أنت."),
    detail_template="governance/_assessment_detail.html",
))

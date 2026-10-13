from beneficiaries.models import (AssessmentCriterion, AssessmentScore, Beneficiary, BeneficiaryCategory,
                                  BeneficiaryEntity, CaseAssessment, EntityRequest, FamilyMember)
from core.registry import ChildTable, DocType, register

register(DocType(
    slug="beneficiary", model=Beneficiary, module="beneficiaries",
    list_fields=["file_number", "full_name", "national_id", "category", "status", "phone", "city", "next_review_date"],
    search_fields=["file_number", "full_name", "national_id", "phone", "members__full_name", "members__national_id"],
    child=ChildTable(FamilyMember, ["full_name", "national_id", "relation", "gender", "birth_date", "education",
                                    "health_status", "monthly_income"],
                     label="أفراد الأسرة", min_rows=0, help="أفراد الأسرة المقيمون مع رب الأسرة ودخل كل منهم."),
    detail_template="beneficiaries/_beneficiary_detail.html",
))
register(DocType(
    slug="case-assessment", model=CaseAssessment, module="beneficiaries",
    list_fields=["name", "beneficiary", "posting_date", "visit_type", "researcher", "score_percent", "is_eligible"],
    form_fields=["beneficiary", "posting_date", "visit_type", "researcher", "category", "findings", "remarks"],
    search_fields=["name", "beneficiary__full_name", "beneficiary__national_id", "beneficiary__file_number"],
    child=ChildTable(AssessmentScore, ["criterion", "max_score", "score", "comment"], label="المعايير والدرجات",
                     min_rows=0, help="اترك الجدول فارغاً لتُجلب المعايير المفعّلة عند الحفظ، ثم أدخل درجة كل معيار."),
))
register(DocType(
    slug="beneficiary-category", model=BeneficiaryCategory, module="beneficiaries",
    list_fields=["name", "description"], search_fields=["name"],
))
register(DocType(
    slug="assessment-criterion", model=AssessmentCriterion, module="beneficiaries",
    list_fields=["name", "max_score", "description", "sort_order", "is_active"], search_fields=["name"],
))
register(DocType(
    slug="beneficiary-entity", model=BeneficiaryEntity, module="entities",
    list_fields=["name", "entity_type", "registration_number", "representative", "representative_phone", "city",
                 "is_active"],
    search_fields=["name", "registration_number", "representative", "representative_phone"],
    detail_template="beneficiaries/_entity_detail.html",
))
register(DocType(
    slug="entity-request", model=EntityRequest, module="entities",
    list_fields=["number", "entity", "request_date", "request_type", "subject", "requested_amount", "approved_amount",
                 "status"],
    search_fields=["number", "subject", "entity__name"],
))

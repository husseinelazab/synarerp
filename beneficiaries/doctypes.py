from beneficiaries.forms import AidCommitteeForm
from beneficiaries.models import (AidCommittee, AidDisbursement, AidDisbursementItem, AidProgram, AidVoucher,
                                  AssessmentCriterion, AssessmentScore, Beneficiary, BeneficiaryCategory,
                                  BeneficiaryEntity, CaseAssessment, EntityRequest, FamilyMember, VoucherMerchant,
                                  VoucherRedemption)
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
    detail_template="beneficiaries/_committee.html",
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
register(DocType(
    slug="aid-program", model=AidProgram, module="beneficiaries",
    list_fields=["name", "aid_type", "expense_account", "fund", "default_amount", "max_per_year", "is_active"],
    search_fields=["name"],
))
register(DocType(
    slug="aid-committee", model=AidCommittee, module="beneficiaries", form_class=AidCommitteeForm,
    list_fields=["name", "is_active", "quorum", "decision_rule", "review_assessments", "disbursement_threshold"],
    search_fields=["name"], detail_template="beneficiaries/_committee_settings.html",
))
register(DocType(
    slug="voucher-merchant", model=VoucherMerchant, module="beneficiaries",
    list_fields=["name", "supplier", "phone", "is_active"], form_fields=["supplier", "name", "phone", "is_active"],
    search_fields=["name", "supplier__name", "phone"], detail_template="beneficiaries/_merchant_detail.html",
))
register(DocType(
    slug="aid-voucher", model=AidVoucher, module="beneficiaries", read_only=True,
    list_fields=["code", "beneficiary", "amount", "balance", "expires_on", "status"],
    search_fields=["code", "beneficiary__full_name", "beneficiary__national_id", "beneficiary__file_number"],
    detail_template="beneficiaries/_voucher_detail.html",
))
register(DocType(
    slug="voucher-redemption", model=VoucherRedemption, module="beneficiaries",
    list_fields=["name", "posting_date", "voucher", "kind", "merchant", "amount", "invoice_no"],
    form_fields=["voucher", "merchant", "posting_date", "amount", "invoice_no", "remarks"],
    search_fields=["name", "voucher__code", "merchant__name", "invoice_no"],
))
register(DocType(
    slug="aid-disbursement", model=AidDisbursement, module="beneficiaries",
    list_fields=["name", "posting_date", "beneficiary", "program", "aid_type", "amount", "received_at"],
    form_fields=["beneficiary", "program", "posting_date", "amount", "payment_account", "warehouse", "fund", "remarks"],
    search_fields=["name", "beneficiary__full_name", "beneficiary__national_id", "beneficiary__file_number"],
    child=ChildTable(AidDisbursementItem, ["item", "qty"], label="أصناف المساعدة العينية", min_rows=0,
                     display_fields=["item", "qty", "amount"],
                     help="للمساعدة العينية فقط؛ تُصرف من المستودع المحدد بتكلفتها."),
    detail_template="beneficiaries/_aid_detail.html",
    detail_exclude=["otp_hash", "otp_attempts", "otp_sent_at", "receipt_fingerprint", "received_at", "receipt_method",
                    "receipt_note", "confirmed_by"],
))

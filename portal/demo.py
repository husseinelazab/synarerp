"""بيانات تجريبية لبوابة المستفيد: طلب تحديث بيانات بانتظار المراجعة وطلب خدمة جديد لأحد المستفيدين."""

DEMO_NOTE = "انتقلت الأسرة إلى سكن جديد بالإيجار (بيانات تجريبية)"


def seed_demo(company):
    """تُنشئ الطلبين مرة واحدة لأول مستفيد معتمد (أو أي مستفيد) في الجمعية. تعيد (طلب التحديث، طلب الخدمة) أو None."""
    from beneficiaries.models import Beneficiary, BeneficiaryStatus, HousingType
    from core.tenancy import tenant_context
    from portal.models import ProfileUpdateRequest, ServiceKind, ServiceRequest

    with tenant_context(company):
        beneficiary = (Beneficiary.objects.filter(company=company, status=BeneficiaryStatus.APPROVED).order_by("id").first()
                       or Beneficiary.objects.filter(company=company).order_by("id").first())
        if beneficiary is None:
            return None
        update = ProfileUpdateRequest.objects.filter(company=company, note=DEMO_NOTE).first()
        if update is None:
            update = ProfileUpdateRequest.objects.create(
                company=company, beneficiary=beneficiary, city=beneficiary.city or "الرياض",
                district="حي النسيم" if beneficiary.district != "حي النسيم" else "حي الروضة",
                housing_type=HousingType.RENTED, note=DEMO_NOTE)
        service = ServiceRequest.objects.filter(company=company, beneficiary=beneficiary,
                                                kind=ServiceKind.RESTUDY).first()
        if service is None:
            service = ServiceRequest.objects.create(
                company=company, beneficiary=beneficiary, kind=ServiceKind.RESTUDY,
                description="تغيّر دخل الأسرة بعد انتهاء عقد عمل رب الأسرة، وأرجو إعادة دراسة الحالة.")
        return update, service

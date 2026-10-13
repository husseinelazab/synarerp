"""بيانات تجريبية لجمعية: لتجربة وحدات الجهات غير الربحية كاملة بعد التأسيس مباشرة."""
import datetime
from decimal import Decimal

from django.utils import timezone


def _submit(doc):
    doc.calculate_totals()
    doc.save()
    doc.submit()
    return doc


def create_nonprofit_demo(company, user=None):
    """تُستدعى داخل tenant_context للجمعية بعد تأسيسها."""
    from accounts.models import Fund
    from awqaf.models import Waqf, WaqfDistribution, WaqfLease
    from beneficiaries.models import (AidDisbursement, AidProgram, Beneficiary, BeneficiaryCategory,
                                      BeneficiaryEntity, CaseAssessment, EntityRequest)
    from funds.models import DonationReceipt, Donor, Sponsorship, SponsorshipType, generate_dues
    from governance.models import BoardTerm, Meeting, MeetingDecision, Member, MembershipCategory, generate_fees
    from office.models import AdminRequest, AdminRequestType, Circular, Correspondence
    from stock.models import UOM, Item, ItemGroup, OpeningStock, Warehouse
    from volunteering.models import Volunteer, VolunteerOpportunity, VolunteerParticipation

    if Beneficiary.objects.exists():
        return
    today = timezone.localdate()
    start = today.replace(day=1)

    def day(n):
        """يوم من الشهر الحالي لا يتجاوز اليوم (لا مستندات بتاريخ مستقبلي)."""
        return min(start + datetime.timedelta(days=n), today)
    cash = company.default_cash_account
    category = {c.name: c for c in BeneficiaryCategory.objects.all()}

    # ── الداعمون والتبرعات ──
    donors = [Donor.objects.create(name="عبدالرحمن السالم", phone="0501112233", city="الرياض"),
              Donor.objects.create(name="مؤسسة الخير القابضة", donor_type="company", phone="0112223344"),
              Donor.objects.create(name="نورة العتيبي", phone="0559998877", city="جدة")]

    def donate(amount, donation_type="general", fund=None, donor=None, n=1):
        receipt = DonationReceipt(donor=donor, donation_type=donation_type, fund=fund, amount=amount, account=cash,
                                  posting_date=day(n),
                                  payment_method="transfer")
        receipt.full_clean()
        receipt.save()
        return _submit(receipt)

    donate(40000, donor=donors[1])
    donate(25000, "zakat", donor=donors[0], n=3)
    donate(5000, donor=donors[2], n=5)
    donate(3000, n=6)

    # ── المستفيدون ودراسات الحالة ──
    families = [
        ("أم عبدالله الحربي", "1000000101", "أرامل", 2500, [("عبدالله", "son"), ("ريم", "daughter"), ("سعد", "son")]),
        ("مريم القحطاني", "1000000102", "أيتام", 1800, [("خالد", "son"), ("لمى", "daughter")]),
        ("أبو فهد الشمري", "1000000103", "أسر محتاجة", 3200, [("فهد", "son"), ("هند", "daughter"), ("زوجته", "spouse")]),
        ("سلمى الدوسري", "1000000104", "مطلقات", 2000, [("يوسف", "son")]),
    ]
    beneficiaries = []
    for i, (name, national_id, cat, income, members) in enumerate(families):
        b = Beneficiary.objects.create(full_name=name, national_id=national_id, phone=f"05500001{i:02d}",
                                       city="الرياض", district="النسيم", category=category[cat],
                                       housing_type="rented", annual_rent=18000, monthly_income=income,
                                       gender="female" if i != 2 else "male", researcher=user)
        for member_name, relation in members:
            b.members.create(full_name=member_name, relation=relation,
                             gender="female" if relation in ("daughter", "spouse") else "male")
        beneficiaries.append(b)
    for b, scores in zip(beneficiaries[:3], ([28, 18, 12, 10, 6, 8], [27, 16, 10, 12, 5, 9], [24, 15, 14, 8, 8, 7])):
        case = CaseAssessment.objects.create(beneficiary=b, researcher=user, category=b.category,
                                             posting_date=start, findings="زيارة ميدانية: الأسرة بحاجة للدعم.")
        case.calculate_totals()
        case.save()
        for row, score in zip(case.scores.all(), scores):
            row.score = score
            row.save()
        _submit(case)

    # ── صرف المساعدات (نقدية وزكاة وعينية) ──
    def give(program, beneficiary, amount=0, **extra):
        aid = AidDisbursement.objects.create(beneficiary=beneficiary, program=program, amount=amount,
                                             posting_date=day(10), **extra)
        aid.calculate_totals()
        aid.save()
        if user is not None:
            aid.confirm_manually(user, "بيانات تجريبية")
        return _submit(aid)

    give(AidProgram.objects.get(name="مساعدة نقدية"), beneficiaries[0], 1500)
    give(AidProgram.objects.get(name="صرف الزكاة"), beneficiaries[2], 2000)

    warehouse = Warehouse.objects.get(name="المستودع الرئيسي")
    group, uom = ItemGroup.objects.first(), UOM.objects.first()
    basket = Item.objects.create(code="BSK-01", name="سلة غذائية متكاملة", item_group=group, uom=uom, buying_rate=250)
    opening = OpeningStock.objects.create(warehouse=warehouse, posting_date=start)  # رصيد افتتاحي للسلال
    opening.items.create(item=basket, qty=40, rate=250)
    _submit(opening)
    aid = AidDisbursement.objects.create(beneficiary=beneficiaries[1], program=AidProgram.objects.get(name="السلة الغذائية"),
                                         warehouse=warehouse, posting_date=day(12))
    aid.items.create(item=basket, qty=2)
    aid.calculate_totals()
    aid.save()
    if user is not None:
        aid.confirm_manually(user, "بيانات تجريبية")
        _submit(aid)

    # ── الكفالات ──
    orphan_type = SponsorshipType.objects.get(name="كفالة يتيم")
    orphan_type.monthly_amount = 400
    orphan_type.save()
    Sponsorship.objects.create(donor=donors[0], beneficiary=beneficiaries[1], sponsorship_type=orphan_type,
                               start_date=start, monthly_amount=400)
    generate_dues(today)
    for due in Sponsorship.objects.get().dues.all():
        _submit(due.collect(user))

    # ── الجهات المستفيدة ──
    school = BeneficiaryEntity.objects.create(name="مدرسة الحي الابتدائية", entity_type="education",
                                              representative="أ. محمد", representative_phone="0501231231")
    EntityRequest.objects.create(entity=school, subject="حقائب مدرسية للطلاب المحتاجين", requested_amount=8000)

    # ── الأعضاء والمجلس والاجتماعات ──
    working = MembershipCategory.objects.get(name="عضو عامل")
    working.annual_fee = 300
    working.save()
    members = [Member.objects.create(full_name=n, phone=f"05400000{i}", category=working)
               for i, n in enumerate(["د. سعد الغامدي", "م. فيصل الزهراني", "أ. هيا المطيري", "أ. ماجد العنزي",
                                      "أ. ريم الشهري"])]
    generate_fees(today.year)
    for member in members[:4]:
        _submit(member.fees.get().collect(user))
    term = BoardTerm.objects.create(name="الدورة الحالية", start_date=today - datetime.timedelta(days=200),
                                    end_date=today + datetime.timedelta(days=1260), approval_reference="خطاب الاعتماد")
    for member, position in zip(members[:3], ("chair", "treasurer", "member")):
        term.seats.create(member=member, position=position)
    meeting = Meeting.objects.create(meeting_type="board", title="الاجتماع الأول للمجلس", posting_date=start,
                                     agenda="1. اعتماد الخطة التشغيلية\n2. مراجعة الميزانية",
                                     minutes="اعتمد المجلس الخطة التشغيلية للعام.")
    meeting.calculate_totals()
    meeting.save()
    meeting.attendance.update(present=True)
    _submit(meeting)
    MeetingDecision.objects.create(meeting=meeting, text="إطلاق حملة الحقيبة المدرسية", responsible=user,
                                   due_date=today + datetime.timedelta(days=20))

    # ── الأوقاف ──
    waqf = Waqf.objects.create(name="عمارة الخير الوقفية", waqif="فاعل خير", deed_number="4310001",
                               asset_value=3500000, conditions="تُصرف الغلة على الأيتام والأسر المحتاجة")
    waqf.shares.create(name="النظارة", percent=10)
    waqf.shares.create(name="كفالة الأيتام", percent=60, target_fund=Fund.objects.get(name="صندوق الكفالات"))
    waqf.shares.create(name="الأسر المحتاجة", percent=30, target_fund=Fund.objects.get(name="صندوق الزكاة"))
    lease = WaqfLease.objects.create(waqf=waqf, unit="معرض رقم 1", tenant_name="مؤسسة الريادة التجارية",
                                     tenant_phone="0505556666", start_date=start,
                                     end_date=start + datetime.timedelta(days=364), annual_rent=60000,
                                     frequency="quarterly")
    lease.activate(user)
    _submit(lease.installments.first().collect(user))
    distribution = WaqfDistribution.objects.create(waqf=waqf, posting_date=day(15))
    _submit(distribution)

    # ── التطوع ──
    opportunity = VolunteerOpportunity.objects.create(
        title="توزيع السلال الغذائية", start_date=today, end_date=today + datetime.timedelta(days=10),
        required_count=10, expected_hours=5, hour_value=40, location="مقر الجمعية")
    for i, name in enumerate(["عبدالعزيز الدوسري", "تركي السبيعي", "سارة الحمدان"]):
        volunteer = Volunteer.objects.create(full_name=name, national_id=f"110000030{i}", phone=f"05300000{i}",
                                             gender="female" if i == 2 else "male")
        VolunteerParticipation.objects.create(opportunity=opportunity, volunteer=volunteer)

    # ── التوثيق والمكتب الإلكتروني ──
    incoming = Correspondence.objects.create(direction="incoming", subject="طلب تحديث بيانات الجمعية",
                                             party="المركز الوطني لتنمية القطاع غير الربحي",
                                             due_date=today + datetime.timedelta(days=7))
    if user is not None:
        incoming.route(user, user, "للاطلاع والإفادة")
    Correspondence.objects.create(direction="outgoing", subject="شكر على الدعم", party="مؤسسة الخير القابضة",
                                  body="نتقدم لكم بخالص الشكر على دعمكم المتواصل لبرامج الجمعية.",
                                  signatory=user, reply_to=None)
    Circular.objects.create(title="مواعيد الدوام", body="يبدأ الدوام من الساعة 8 صباحاً حتى 4 مساءً.")
    AdminRequest.objects.create(request_type=AdminRequestType.objects.get(name="طلب شراء"), subject="أجهزة حاسب للباحثين",
                                amount=Decimal("9000"), created_by=user)

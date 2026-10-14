"""بيانات تجريبية لجمعية: لتجربة وحدات الجهات غير الربحية كاملة بعد التأسيس مباشرة."""
import datetime
from decimal import Decimal

from django.utils import timezone


def _submit(doc):
    doc.calculate_totals()
    doc.save()
    doc.submit()
    return doc


def _demo_logo():
    """شعار تجريبي جاهز (PNG مضمّن) لتظهر ترويسة التقارير كاملة في التجربة."""
    import base64
    from pathlib import Path

    data = (Path(__file__).parent / "data" / "demo_logo.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode()


def _profile(company):
    from core.models import PrintSettings

    for field, value in (("license_number", "1234"), ("headquarters", "الرياض"), ("phone", "0112345678"),
                         ("address", "حي النسيم، الرياض 12345"), ("email", "info@albirr.example")):
        if not getattr(company, field):
            setattr(company, field, value)
    company.save()
    settings = PrintSettings.for_company(company)
    if not settings.logo:
        settings.logo = _demo_logo()
        settings.accent_color = "#1e6e5a"
        settings.name_en = "Al-Birr Charitable Association (Demo)"
        settings.save()


def create_nonprofit_demo(company, user=None):
    """تُستدعى داخل tenant_context للجمعية بعد تأسيسها."""
    from accounts.models import Fund
    from awqaf.models import Waqf, WaqfDistribution, WaqfLease
    from beneficiaries.models import (AidDisbursement, AidProgram, Beneficiary, BeneficiaryCategory,
                                      BeneficiaryEntity, CaseAssessment, EntityRequest)
    from funds.models import (DonationReceipt, Donor, InKindDonation, Pledge, Sponsorship, SponsorshipType,
                              generate_dues)
    from governance.models import BoardTerm, Meeting, MeetingDecision, Member, MembershipCategory, generate_fees
    from office.models import AdminRequest, AdminRequestType, Circular, Correspondence
    from stock.models import UOM, Item, ItemGroup, Warehouse
    from volunteering.models import Volunteer, VolunteerOpportunity, VolunteerParticipation

    if Beneficiary.objects.exists():
        return
    _profile(company)
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
    in_kind = InKindDonation.objects.create(donor=donors[1], donor_name=donors[1].name, warehouse=warehouse,
                                            posting_date=start, remarks="سلال غذائية من مؤسسة الخير")
    in_kind.items.create(item=basket, qty=40, rate=250)  # تبرع عيني مقيد بقيمته العادلة
    _submit(in_kind)
    aid = AidDisbursement.objects.create(beneficiary=beneficiaries[1], program=AidProgram.objects.get(name="السلة الغذائية"),
                                         warehouse=warehouse, posting_date=day(12))
    aid.items.create(item=basket, qty=2)
    aid.calculate_totals()
    aid.save()
    if user is not None:
        aid.confirm_manually(user, "بيانات تجريبية")
        _submit(aid)

    # ── وعود التبرع: وعد غير مشروط حُصّل جزء منه، ووعد مشروط يُفصح عنه ──
    pledge = Pledge.objects.create(donor=donors[1], amount=20000, posting_date=day(3), due_date=day(3) + datetime.timedelta(days=90),
                                   remarks="دعم البرامج العامة للجمعية")
    _submit(pledge)
    if user is not None:
        receipt = pledge.collect(user)
        receipt.amount, receipt.posting_date = Decimal("5000"), day(10)
        _submit(receipt)
    _submit(Pledge.objects.create(donor=donors[0], amount=100000, posting_date=day(4), is_conditional=True,
                                  condition="عند استكمال 50% من تمويل مشروع مركز الأسرة"))

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
                               asset_value=3500000, conditions="تُصرف الغلة على الأيتام والأسر المحتاجة",
                               revenue_restriction="restricted")
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
    _demo_new_modules(company, user, today, day)


def _demo_new_modules(company, user, today, day):
    """البرامج والتطوير، التميز المؤسسي، العلاقات العامة، الحركة والصيانة."""
    from excellence.models import KPI, Initiative, StrategicGoal, StrategicPlan
    from fleet.models import FuelLog, MaintenanceRequest, Vehicle, VehicleRequest
    from programs.models import ProgramEnrollment, SessionAttendance, TrainingProgram
    from relations.models import Partnership, PRContact, PREvent, Survey, SurveyResponse, Visit

    # ── البرامج والتطوير: برنامج مكتمل بقياس أثر وبرنامج مفتوح للتسجيل ──
    done = TrainingProgram.objects.create(title="إدارة الميزانية الأسرية", program_type="course", trainer="أ. نورة القحطاني",
                                          location="قاعة الجمعية", start_date=day(1), end_date=day(3), hours=Decimal("9"),
                                          capacity=20, target_audience="الأسر المستفيدة")
    sessions = [done.sessions.create(date=day(n), topic=topic) for n, topic in
                ((1, "التخطيط المالي"), (2, "الادخار"), (3, "المشاريع المنزلية"))]
    trainees = [("هند العتيبي", "1200000001", 45, 80, 5, 3), ("مها الشمري", "1200000002", 50, 85, 4, 3),
                ("سارة الحربي", "1200000003", 40, 55, 3, 1)]
    for name, national_id, pre, post, rating, present in trainees:
        enrollment = ProgramEnrollment.objects.create(program=done, full_name=name, national_id=national_id,
                                                      phone="0550000000", gender="female", status="accepted",
                                                      pre_score=pre, post_score=post, satisfaction=rating)
        for session in sessions[:present]:
            SessionAttendance.objects.create(enrollment=enrollment, session=session, present=True)
    if user is not None:
        done.status = "running"
        done.save(update_fields=["status"])
        done.complete(user)
    upcoming = TrainingProgram.objects.create(title="مهارات التطوع الفعّال", program_type="workshop",
                                              trainer="م. خالد الدوسري", mode="remote", start_date=today + datetime.timedelta(days=10),
                                              end_date=today + datetime.timedelta(days=11), hours=Decimal("4"), capacity=40)
    upcoming.sessions.create(date=today + datetime.timedelta(days=10), topic="أساسيات العمل التطوعي")
    if user is not None:
        upcoming.open_registration(user)

    # ── التميز المؤسسي: خطة معتمدة بأهداف ومبادرات ومؤشرات ──
    plan = StrategicPlan.objects.create(title=f"الخطة الاستراتيجية {today.year}–{today.year + 2}", start_year=today.year,
                                        end_year=today.year + 2, vision="مجتمع متكافل تُصان فيه كرامة الأسرة",
                                        mission="تمكين الأسر المحتاجة اجتماعياً واقتصادياً بشراكات مستدامة",
                                        values="الأمانة\nالإتقان\nالشراكة")
    care = StrategicGoal.objects.create(plan=plan, code="ه1", title="تمكين الأسر المستفيدة اقتصادياً", weight=60,
                                        vision2030_pillar="society", sdg=1)
    sustain = StrategicGoal.objects.create(plan=plan, code="ه2", title="تنمية الموارد المالية المستدامة", weight=40,
                                           vision2030_pillar="economy", sdg=17)
    Initiative.objects.create(goal=care, title="برنامج الأسر المنتجة", department="البرامج", start_date=day(0),
                              end_date=today + datetime.timedelta(days=120), budget=150000, progress=35)
    Initiative.objects.create(goal=care, title="الدورات التأهيلية للأسر", department="البرامج", start_date=day(0),
                              end_date=day(5), budget=20000, progress=60)
    Initiative.objects.create(goal=sustain, title="إطلاق المتجر الإلكتروني للتبرعات", department="الموارد المالية",
                              start_date=day(0), end_date=today + datetime.timedelta(days=60), budget=40000, progress=20)
    for goal, name, unit, target, direction, value in (
            (care, "عدد الأسر المستفيدة من برامج التمكين", "count", 120, "higher", 95),
            (care, "نسبة رضا المستفيدين", "percent", 90, "higher", 86),
            (sustain, "نمو التبرعات السنوية", "percent", 15, "higher", 9),
            (sustain, "نسبة المصروفات الإدارية إلى الإجمالي", "percent", 15, "lower", 12)):
        kpi = KPI.objects.create(goal=goal, name=name, unit=unit, target=target, direction=direction, frequency="quarterly")
        kpi.readings.create(period_end=day(0), value=value)
    if user is not None:
        plan.approve(user)

    # ── العلاقات العامة والإعلام ──
    contact = PRContact.objects.create(name="أ. فهد المطيري", organization="بنك التنمية الاجتماعية", category="government",
                                       phone="0112223344")
    partnership = Partnership.objects.create(partner="بنك التنمية الاجتماعية", contact=contact, partnership_type="executive",
                                             start_date=day(0) - datetime.timedelta(days=330),
                                             end_date=today + datetime.timedelta(days=25), value=250000,
                                             objectives="تمويل مشاريع الأسر المنتجة وتدريبها")
    if user is not None:
        partnership.activate(user)
    event = PREvent.objects.create(title="ملتقى الأسر المنتجة", event_type="forum", start_date=day(2), location="مقر الجمعية",
                                   partner=partnership, budget=15000, actual_cost=12500, expected_attendees=150,
                                   actual_attendees=180, outcomes="عرض 30 مشروعاً منزلياً وبيع منتجات بـ 40 ألف ريال")
    if user is not None:
        event.mark_done(user)
    Visit.objects.create(direction="incoming", entity="وفد المركز الوطني لتنمية القطاع غير الربحي", contact=None,
                         visit_date=day(4), purpose="الاطلاع على برامج التمكين", status="done",
                         follow_up_date=today + datetime.timedelta(days=14))
    survey = Survey.objects.create(title="رضا المستفيدين عن الخدمات", audience="الأسر المستفيدة")
    rating = survey.questions.create(text="ما مدى رضاك عن سرعة إنجاز طلبك؟", question_type="rating")
    channel = survey.questions.create(text="كيف عرفت عن الجمعية؟", question_type="choice",
                                      choices="الأقارب والجيران\nوسائل التواصل\nجهة حكومية")
    for score, source in ((5, "الأقارب والجيران"), (4, "وسائل التواصل"), (5, "الأقارب والجيران"), (3, "جهة حكومية")):
        SurveyResponse.objects.create(survey=survey, answers={str(rating.pk): score, str(channel.pk): source})

    # ── الحركة والصيانة ──
    van = Vehicle.objects.create(plate_number="أ ب ج 1234", make="تويوتا", model="هايس", year=2022, vehicle_type="bus",
                                 odometer=48200, last_service_km=44000, registration_expiry=today + datetime.timedelta(days=20),
                                 insurance_expiry=today + datetime.timedelta(days=200))
    Vehicle.objects.create(plate_number="د هـ و 5678", make="هيونداي", model="أكسنت", year=2023, odometer=21000,
                           last_service_km=20000, registration_expiry=today + datetime.timedelta(days=300))
    start = timezone.make_aware(datetime.datetime.combine(day(3), datetime.time(9)))
    trip = VehicleRequest.objects.create(purpose="توصيل السلال الغذائية", destination="حي النسيم", vehicle=van,
                                         start_at=start, end_at=start + datetime.timedelta(hours=4), passengers=3,
                                         created_by=user)
    trip.submit()
    if user is not None:
        trip.complete_trip(user, end_odometer=48260)
    MaintenanceRequest.objects.create(target="facility", facility="مستودع الجمعية", category="hvac",
                                      description="مكيف المستودع لا يبرد", priority="urgent", created_by=user)
    FuelLog.objects.create(vehicle=van, date=day(3), liters=Decimal("60"), amount=Decimal("139.20"), odometer=48260,
                           station="محطة الدريس")

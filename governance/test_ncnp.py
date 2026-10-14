import copy
import datetime
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.tenancy import tenant_context
from governance import checks
from governance.checks import CHECKS, run_check
from governance.models import (BoardTerm, FrameworkSource, GovernanceAssessment, GovernanceEvidence,
                               GovernanceIndicator, GovernanceStandard, Meeting, Member, MembershipCategory,
                               MembershipFee, generate_fees)
from governance.ncnp import framework_data, load_framework, rating_band, readiness, resolve_fix

TODAY = timezone.localdate()
YEAR = TODAY.year
TOTAL = sum(len(s["indicators"]) for s in framework_data()["standards"])


class TenantCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية معيار المركز", "ncnp_gov")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)


class FrameworkDataTests(TestCase):
    def test_data_file_is_consistent(self):
        data = framework_data()
        self.assertTrue(data["version"] and data["sources"])
        self.assertEqual(sum(s["weight_percent"] for s in data["standards"]), 100)
        codes = [i["code"] for s in data["standards"] for i in s["indicators"]]
        self.assertEqual(len(codes), len(set(codes)))
        for standard in data["standards"]:
            for indicator in standard["indicators"]:
                if indicator["auto_check"]:
                    self.assertIn(indicator["auto_check"], CHECKS, indicator["code"])
                if not indicator["points_verified"]:
                    self.assertIn("تقديري", indicator["note"], indicator["code"])

    def test_unknown_check_is_ignored(self):
        self.assertIsNone(run_check("no_such_check", YEAR))


class LoaderTests(TenantCase):
    def test_load_is_idempotent_and_keeps_user_edits(self):
        stats = load_framework(self.company)
        self.assertEqual((stats["created"], stats["updated"]), (TOTAL, 0))
        ncnp = GovernanceIndicator.objects.filter(standard__source=FrameworkSource.NCNP)
        self.assertEqual(ncnp.count(), TOTAL)
        self.assertEqual(GovernanceStandard.objects.filter(source=FrameworkSource.NCNP).count(), 3)
        self.assertEqual(GovernanceIndicator.objects.filter(standard__source="").count(), 6)  # المعايير الخاصة باقية
        edited = ncnp.get(code="N2.18")
        self.assertEqual(edited.weight, 13)
        edited.weight = 10
        edited.save()
        stats = load_framework(self.company)
        self.assertEqual((stats["created"], stats["updated"], stats["unchanged"]), (0, 0, TOTAL))
        edited.refresh_from_db()
        self.assertEqual(edited.weight, 10)

    def test_newer_version_updates_without_destroying_assessments(self):
        load_framework(self.company)
        old = GovernanceAssessment.objects.create(framework=FrameworkSource.NCNP, year=YEAR - 1)
        old.calculate_totals()
        old.save()
        self.assertEqual(old.framework_version, framework_data()["version"])
        rows = old.scores.count()
        data = copy.deepcopy(framework_data())
        data["version"] = "2099.1"
        first = data["standards"][0]
        first["indicators"][0]["title"] = "نص محدث"
        first["indicators"][0]["max_points"] = 4
        removed = first["indicators"].pop()["code"]
        first["indicators"].append({"code": "N1.99", "title": "ممارسة جديدة", "max_points": 2,
                                    "points_verified": False, "evidence": "", "auto_check": "unknown_future_check"})
        stats = load_framework(self.company, data)
        self.assertEqual((stats["created"], stats["deactivated"]), (1, 1))
        self.assertEqual(stats["updated"], TOTAL - 1)
        indicator = GovernanceIndicator.objects.get(code="N1.01")
        self.assertEqual((indicator.text, indicator.weight, indicator.version), ("نص محدث", 4, "2099.1"))
        self.assertFalse(GovernanceIndicator.objects.get(code=removed).is_active)
        self.assertEqual(old.scores.count(), rows)  # التقييم السابق لم يُمس
        new = GovernanceAssessment.objects.create(framework=FrameworkSource.NCNP, year=YEAR)
        new.calculate_totals()
        new.save()
        codes = set(new.scores.values_list("indicator__code", flat=True))
        self.assertIn("N1.99", codes)
        self.assertNotIn(removed, codes)
        self.assertEqual(new.scores.get(indicator__code="N1.99").suggested_status, "")  # مفتاح غير معروف يُتجاهل
        self.assertTrue(load_framework(self.company)["skipped_newer"])  # لا رجوع لإصدار أقدم
        self.assertEqual(GovernanceIndicator.objects.get(code="N1.01").text, "نص محدث")

    def test_code_collision_with_custom_standard(self):
        GovernanceStandard.objects.create(code="NCNP-1", name="معيار خاص")
        with self.assertRaises(ValidationError):
            load_framework(self.company)


class ChecksTests(TenantCase):
    def status(self, key, year=YEAR, target=0):
        return run_check(key, year, target).status

    def website(self, **kwargs):
        from website.models import Website

        return Website.objects.create(slug="ncnp-site", **kwargs)

    def doc(self, title, category="policies", year=YEAR, size=10, published=True):
        from website.models import PublishedDocument

        return PublishedDocument.objects.create(title=title, category=category, year=year, size=size,
                                                content=b"x" * size, is_published=published)

    def member(self, name="عضو", **kwargs):
        return Member.objects.create(full_name=name, category=MembershipCategory.objects.first(), **kwargs)

    def test_board_term_current(self):
        self.assertEqual(self.status("board_term_current", target=3), "not_met")
        term = BoardTerm.objects.create(name="الدورة", start_date=TODAY - datetime.timedelta(days=10),
                                        end_date=TODAY + datetime.timedelta(days=1000))
        for i in range(2):
            term.seats.create(member=self.member(f"ع{i}"))
        self.assertEqual(self.status("board_term_current", target=3), "partial")
        term.seats.create(member=self.member("ع3"))
        self.assertEqual(self.status("board_term_current", target=3), "met")

    def meeting(self, meeting_type, date=TODAY, minutes="محضر", submit=True):
        meeting = Meeting.objects.create(meeting_type=meeting_type, title="اجتماع", posting_date=date, minutes=minutes)
        if submit:
            Meeting.objects.filter(pk=meeting.pk).update(docstatus=1)
        return meeting

    def test_board_meetings(self):
        self.assertEqual(self.status("board_meetings", target=2), "not_met")
        self.meeting("board", date=TODAY.replace(month=1, day=1))
        self.assertEqual(self.status("board_meetings", target=2), "partial")
        self.meeting("board", date=TODAY.replace(month=1, day=2))
        self.assertEqual(self.status("board_meetings", target=2), "met")

    def test_ga_meeting_12m(self):
        self.meeting("ga_ordinary", date=TODAY - datetime.timedelta(days=400))
        self.assertEqual(self.status("ga_meeting_12m"), "not_met")
        self.meeting("ga_ordinary", date=TODAY - datetime.timedelta(days=30), submit=False)
        self.assertEqual(self.status("ga_meeting_12m"), "partial")
        self.meeting("ga_ordinary", date=TODAY - datetime.timedelta(days=20), minutes="   ")
        self.assertEqual(self.status("ga_meeting_12m"), "partial")
        self.meeting("ga_ordinary", date=TODAY - datetime.timedelta(days=10))
        self.assertEqual(self.status("ga_meeting_12m"), "met")

    def test_members_register_growth_and_fees(self):
        self.assertEqual(self.status("members_register"), "not_met")
        self.assertEqual(self.status("member_growth"), "not_met")
        self.assertEqual(self.status("member_fees_recorded"), "na")  # لا اشتراك في الفئات الافتراضية
        self.member(join_date=TODAY)
        self.assertEqual(self.status("members_register"), "met")
        self.assertEqual(self.status("member_growth"), "met")
        self.assertEqual(self.status("member_growth", year=YEAR - 5), "not_met")
        MembershipCategory.objects.filter(pk=MembershipCategory.objects.first().pk).update(annual_fee=100)
        self.assertEqual(self.status("member_fees_recorded"), "not_met")
        generate_fees(YEAR)
        self.assertTrue(MembershipFee.objects.exists())
        self.assertEqual(self.status("member_fees_recorded"), "met")

    def test_website_checks(self):
        for key in ("website_published", "membership_open", "website_disclosure", "fundraising_license_valid",
                    "board_published", "impact_stats_published"):
            self.assertEqual(self.status(key), "not_met", key)
        site = self.website()
        self.assertEqual(self.status("website_published"), "partial")
        site.is_published = True
        site.save()
        self.assertEqual(self.status("website_published"), "met")
        self.assertEqual(self.status("membership_open"), "met")
        self.assertEqual(self.status("website_disclosure"), "partial")
        self.doc("أي وثيقة", category="other")
        self.assertEqual(self.status("website_disclosure"), "met")

    def test_fundraising_license(self):
        site = self.website(fundraising_license_number="FR-1", fundraising_license_expiry=TODAY - datetime.timedelta(days=1))
        self.assertEqual(self.status("fundraising_license_valid"), "not_met")
        site.fundraising_license_expiry = TODAY + datetime.timedelta(days=30)
        site.save()
        self.assertEqual(self.status("fundraising_license_valid"), "met")

    def test_year_documents(self):
        for key, category in (("financials_published", "financials"), ("annual_report_published", "annual"),
                              ("ga_minutes_published", "assembly")):
            self.assertEqual(self.status(key), "not_met", key)
            self.doc("بلا ملف", category=category, size=0)
            self.assertEqual(self.status(key), "not_met", key)
            self.doc("السنة السابقة", category=category, year=YEAR - 1)
            self.assertEqual(self.status(key), "partial", key)
            self.doc("السنة الحالية", category=category)
            self.assertEqual(self.status(key), "met", key)

    def test_strategy_and_activity_reports(self):
        from website.models import NewsPost

        self.assertEqual(self.status("strategy_published"), "not_met")
        self.doc("الخطة الاستراتيجية 2025-2027", category="strategy")
        self.assertEqual(self.status("strategy_published"), "met")
        self.assertEqual(self.status("activity_reports_published"), "not_met")
        NewsPost.objects.create(title="خبر نشاط", body="نص", published_on=TODAY)
        self.assertEqual(self.status("activity_reports_published"), "partial")
        self.doc("التقرير السنوي", category="annual")
        self.assertEqual(self.status("activity_reports_published"), "met")

    def test_board_and_impact_published(self):
        from website.models import ImpactCounter

        site = self.website(is_published=True)
        term = BoardTerm.objects.create(name="الدورة", start_date=TODAY, end_date=TODAY + datetime.timedelta(days=900))
        term.seats.create(member=self.member())
        self.assertEqual(self.status("board_published"), "met")
        ImpactCounter.objects.create(label="مستفيد", value=100)
        self.assertEqual(self.status("impact_stats_published"), "met")
        site.is_published = False
        site.save()
        self.assertEqual(self.status("board_published"), "partial")

    def test_policy_keywords(self):
        titles = {"bylaw_published": "اللائحة الأساسية للجمعية", "policy_conflict_of_interest": "سياسة تعارض المصالح",
                  "policy_whistleblowing": "سياسة الإبلاغ عن المخالفات وحماية المبلغين",
                  "policy_privacy": "سياسة خصوصية البيانات", "policy_document_retention": "سياسة الاحتفاظ بالوثائق وإتلافها",
                  "policy_fundraising": "سياسة جمع التبرعات", "policy_remuneration": "سياسة المكافآت والامتيازات",
                  "policy_hr": "لائحة الموارد البشرية وسلم الرواتب", "policy_financial": "لائحة السياسات المالية",
                  "policy_procurement": "لائحة المشتريات"}
        self.assertEqual(set(titles), set(checks.POLICY_KEYWORDS))
        for key in titles:
            self.assertEqual(self.status(key), "not_met", key)
        hidden = self.doc("سياسة تعارض المصالح (مسودة)", published=False)
        self.assertEqual(self.status("policy_conflict_of_interest"), "not_met")
        hidden.delete()
        for key, title in titles.items():
            self.doc(title)
            self.assertEqual(self.status(key), "met", key)
        self.assertIn("تعارض المصالح", run_check("policy_conflict_of_interest", YEAR).text)

    def test_normalize_matches_hamza_and_taa(self):
        self.assertEqual(checks.normalize("الإبلاغ عن المخالفة"), checks.normalize("الابلاغ عن المخالفه"))

    def fiscal(self):
        from core.models import FiscalYear

        fiscal = FiscalYear.objects.filter(end_date__year=YEAR).first()
        if fiscal is None:
            fiscal = FiscalYear.objects.create(name=str(YEAR), start_date=datetime.date(YEAR, 1, 1),
                                               end_date=datetime.date(YEAR, 12, 31))
        return fiscal

    def test_year_end_closed(self):
        self.assertEqual(self.status("year_end_closed", year=1990), "not_met")
        fiscal = self.fiscal()
        fiscal.is_closed = False
        fiscal.save()
        self.assertEqual(self.status("year_end_closed"), "not_met")
        fiscal.is_closed = True
        fiscal.save()
        self.assertEqual(self.status("year_end_closed"), "met")

    def test_books_kept(self):
        from accounts.models import Account, GLEntry

        self.assertEqual(self.status("books_kept"), "not_met")
        GLEntry.objects.create(posting_date=TODAY, account=Account.objects.filter(is_group=False).first(), debit=1,
                               voucher_type="journal-entry", voucher_no="JV-1", voucher_id=1)
        self.assertEqual(self.status("books_kept"), "met")

    def test_budgets(self):
        from accounts.models import Budget
        from core.models import FiscalYear

        fiscal = self.fiscal()
        self.assertEqual(self.status("budget_exists"), "not_met")
        Budget.objects.create(name="موازنة العام", fiscal_year=fiscal)
        self.assertEqual(self.status("budget_exists"), "met")
        self.assertEqual(self.status("budget_next_year", year=YEAR - 1), "met")
        self.assertEqual(self.status("budget_next_year"), "not_met")
        next_year = FiscalYear.objects.filter(end_date__year=YEAR + 1).first() or FiscalYear.objects.create(
            name=f"ncnp-{YEAR + 1}", start_date=datetime.date(YEAR + 1, 1, 1), end_date=datetime.date(YEAR + 1, 12, 31))
        Budget.objects.create(name="موازنة العام القادم", fiscal_year=next_year, disabled=True)
        self.assertEqual(self.status("budget_next_year"), "not_met")

    def test_funds_no_deficit(self):
        from accounts.models import Fund

        self.assertEqual(self.status("funds_no_deficit"), "partial")
        Fund.objects.create(name="صندوق مقيد", fund_type="restricted")
        with mock.patch.object(Fund, "balance", new_callable=mock.PropertyMock, return_value=Decimal("-5")):
            suggestion = run_check("funds_no_deficit", YEAR)
        self.assertEqual(suggestion.status, "not_met")
        self.assertIn("صندوق مقيد", suggestion.text)


class AssessmentAndReportTests(TenantCase):
    def setUp(self):
        super().setUp()
        load_framework(self.company)

    def assessment(self, year=YEAR):
        assessment = GovernanceAssessment.objects.create(framework=FrameworkSource.NCNP, year=year)
        assessment.calculate_totals()
        assessment.save()
        return assessment

    def test_ncnp_assessment_suggests_but_does_not_score(self):
        assessment = self.assessment()
        self.assertEqual(assessment.scores.count(), TOTAL)
        row = assessment.scores.get(indicator__code="N2.01")
        self.assertEqual((row.status, row.suggested_status), ("", "not_met"))  # لا موقع
        self.assertIn("موقع", row.auto_result)
        self.assertEqual(assessment.score_percent, 0)
        self.assertEqual(assessment.draft_actions[0][0], "apply_suggestions")
        custom = GovernanceAssessment.objects.create(year=YEAR)
        custom.calculate_totals()
        self.assertEqual(custom.scores.count(), 6)  # الإطار الخاص لا يختلط بمعيار المركز
        count = assessment.apply_suggestions(self.user)
        self.assertGreater(count, 10)
        row.refresh_from_db()
        self.assertEqual(row.status, "not_met")

    def test_report_scoring_and_band(self):
        assessment = self.assessment()
        for row in assessment.scores.select_related("indicator__standard"):
            row.status = {"NCNP-1": "met", "NCNP-2": "not_met", "NCNP-3": "partial"}[row.indicator.standard.code]
            row.save()
        assessment.scores.filter(indicator__code="N1.39").update(status="na")
        assessment.calculate_totals()
        assessment.save()
        report = readiness(assessment)
        parts = {p["standard"].code: p for p in report["standards"]}
        self.assertEqual(parts["NCNP-1"]["percent"], Decimal("100.0"))
        self.assertEqual(parts["NCNP-2"]["percent"], Decimal("0.0"))
        self.assertEqual(parts["NCNP-3"]["percent"], Decimal("50.0"))
        self.assertEqual(report["overall"], Decimal("60.0"))  # (100×40 + 0×20 + 50×40) ÷ 100
        self.assertEqual(report["band"], "متوسط")
        self.assertEqual(assessment.score_percent, Decimal("60.0"))
        gap_codes = {g["indicator"].code for g in report["gaps"]}
        self.assertTrue(all(c.startswith(("N2", "N3")) for c in gap_codes))
        self.assertEqual(report["gaps"][0]["indicator"].code, "N3.02")  # الأكبر خسارة أولاً
        financials = next(g for g in report["gaps"] if g["indicator"].code == "N2.18")
        self.assertEqual(financials["fix_url"], "/app/published-document/")
        self.assertEqual(financials["lost"], 13)

    def test_rating_bands(self):
        for value, label in ((100, "ممتاز"), (90, "ممتاز"), (Decimal("89.9"), "جيد جداً"), (80, "جيد جداً"),
                             (70, "جيد"), (50, "متوسط"), (Decimal("49.9"), "ضعيف"), (0, "ضعيف")):
            self.assertEqual(rating_band(value), label, value)
        self.assertEqual(rating_band(None), "")

    def test_evidence_counted_and_fix_fallback(self):
        assessment = self.assessment()
        indicator = GovernanceIndicator.objects.get(code="N2.19")  # يدوي بلا مكان معالجة
        GovernanceEvidence.objects.create(indicator=indicator, title="نموذج الإفصاح", link="https://example.com",
                                          year=YEAR)
        report = readiness(assessment)
        self.assertEqual(report["evidence_total"], 1)
        gap = next(g for g in report["gaps"] if g["indicator"].code == "N2.19")
        self.assertEqual(gap["evidence_count"], 1)
        self.assertEqual(gap["fix_url"], indicator.get_absolute_url())
        self.assertEqual(resolve_fix("doc_list:no-such-doctype", indicator), indicator.get_absolute_url())
        self.assertEqual(resolve_fix("website_settings"), "/website/settings/")


class ViewsAndIsolationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية الجاهزية", "ready_gov")
        cls.other, cls.other_user = make_nonprofit("جمعية أخرى للجاهزية", "ready_other")

    def test_load_report_and_isolation(self):
        self.client.force_login(self.user)
        html = self.client.get("/governance/").content.decode()
        self.assertIn("تحميل معيار المركز الوطني", html)
        self.assertIn("/governance/readiness/", html)
        self.assertContains(self.client.get("/governance/readiness/"), "لا يوجد تقييم ذاتي")
        self.client.post("/governance/ncnp/load/")
        self.client.post("/governance/ncnp/load/")
        self.assertEqual(GovernanceIndicator.all_objects.filter(company=self.company, code__startswith="N").count(), TOTAL)
        self.assertFalse(GovernanceIndicator.all_objects.filter(company=self.other, code__startswith="N").exists())
        self.assertNotContains(self.client.get("/governance/"), "تحميل معيار المركز الوطني")
        self.client.post("/app/governance-assessment/new/", {"year": YEAR, "framework": "ncnp",
                                                             "posting_date": TODAY.isoformat(),
                                                             "scores-TOTAL_FORMS": 0, "scores-INITIAL_FORMS": 0})
        assessment = GovernanceAssessment.all_objects.get(company=self.company)
        self.assertEqual(assessment.framework, "ncnp")
        detail = self.client.get(f"/app/governance-assessment/{assessment.pk}/")
        self.assertContains(detail, "اقتراحات التحقق من بيانات النظام")
        report = self.client.get(f"/governance/readiness/?assessment={assessment.pk}")
        self.assertContains(report, "تقرير الجاهزية لتقييم الحوكمة")
        self.assertContains(report, "ضعيف")
        self.assertContains(report, "/app/published-document/")
        self.assertContains(report, "lh-name")  # ترويسة الطباعة
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(f"/governance/readiness/?assessment={assessment.pk}").status_code, 404)
        self.assertContains(self.client.get("/governance/readiness/"), "لا يوجد تقييم ذاتي")

    def test_demo_seed_is_idempotent(self):
        from governance.demo_ncnp import seed_demo

        first = seed_demo(self.company)
        second = seed_demo(self.company)
        self.assertEqual(first.pk, second.pk)
        with tenant_context(self.company):
            self.assertEqual(GovernanceAssessment.objects.filter(framework="ncnp").count(), 1)
            self.assertEqual(first.scores.exclude(status="").count(), 8)
            self.assertGreater(first.score_percent, 0)
        self.assertFalse(GovernanceAssessment.all_objects.filter(company=self.other).exists())

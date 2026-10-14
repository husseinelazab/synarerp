import datetime
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.models import Notification
from core.tenancy import tenant_context
from relations.models import (EventStatus, MediaCoverage, Partnership, PartnershipStatus, PRContact, PREvent,
                              QuestionType, Survey, SurveyResponse, Visit, VisitStatus)

TODAY = timezone.localdate()


class RelationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية تواصل", "tawasol")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.survey = Survey.objects.create(title="رضا المستفيدين عن السلال الغذائية")
        self.q_rate = self.survey.questions.create(text="ما تقييمك للخدمة؟", question_type=QuestionType.RATING)
        self.q_choice = self.survey.questions.create(text="كيف عرفت عنا؟", question_type=QuestionType.CHOICE,
                                                     choices="تويتر\nصديق\nالموقع")
        self.q_text = self.survey.questions.create(text="ملاحظاتك", question_type=QuestionType.TEXT, required=False)

    def answer(self, rating="5", choice="صديق", text="", **extra):
        return self.client.post(f"/s/{self.survey.public_token}/", {
            f"q_{self.q_rate.pk}": rating, f"q_{self.q_choice.pk}": choice, f"q_{self.q_text.pk}": text, **extra})

    def test_public_survey_submission(self):
        self.assertContains(self.client.get(f"/s/{self.survey.public_token}/"), "كيف عرفت عنا؟")
        self.assertContains(self.answer("4", text="خدمة ممتازة"), "تم استلام إجاباتك")
        response = SurveyResponse.objects.get()
        self.assertEqual(response.answers, {str(self.q_rate.pk): 4, str(self.q_choice.pk): "صديق",
                                            str(self.q_text.pk): "خدمة ممتازة"})
        self.assertContains(self.answer(rating=""), "هذا السؤال إجباري")
        self.assertContains(self.answer(rating="7"), "من 1 إلى 5")
        self.assertContains(self.answer(choice="تلفزيون"), "اختر أحد الخيارات")
        self.assertContains(self.answer(website="spam"), "تم استلام إجاباتك")  # مصيدة البوتات
        self.assertEqual(SurveyResponse.objects.count(), 1)
        self.survey.end_date = TODAY - datetime.timedelta(days=1)
        self.survey.save()
        self.assertContains(self.answer(), "مغلق")
        self.survey.end_date = None
        self.survey.is_open = False
        self.survey.save()
        self.assertContains(self.answer(), "مغلق")
        self.assertEqual(SurveyResponse.objects.count(), 1)
        self.assertEqual(self.client.get("/s/unknown/").status_code, 404)

    def test_results_aggregation(self):
        for rating, choice, text in (("5", "صديق", "رائع"), ("3", "صديق", ""), ("4", "تويتر", "")):
            self.answer(rating, choice, text)
        self.client.force_login(self.user)
        page = self.client.get(f"/relations/survey/{self.survey.pk}/results/")
        results = {r["question"].pk: r for r in page.context["results"]}
        self.assertEqual(page.context["count"], 3)
        self.assertEqual(results[self.q_rate.pk]["average"], 4)
        self.assertEqual([n for _, n, _ in results[self.q_rate.pk]["distribution"]], [0, 0, 1, 1, 1])
        self.assertEqual(results[self.q_choice.pk]["counts"], [("تويتر", 1, 33), ("صديق", 2, 67), ("الموقع", 0, 0)])
        self.assertEqual(results[self.q_text.pk]["texts"], ["رائع"])
        self.assertContains(page, "رائع")

    def test_partnership_expiry_and_alerts(self):
        partnership = Partnership.objects.create(partner="شركة الخير", start_date=TODAY - datetime.timedelta(days=300),
                                                 end_date=TODAY + datetime.timedelta(days=20))
        partnership.end_date = partnership.start_date - datetime.timedelta(days=1)
        with self.assertRaises(ValidationError):
            partnership.full_clean()
        partnership.end_date = TODAY + datetime.timedelta(days=20)
        self.assertEqual(partnership.days_to_expiry, 20)
        partnership.activate(self.user)
        Partnership.objects.create(partner="بعيدة", start_date=TODAY, end_date=TODAY + datetime.timedelta(days=90),
                                   status=PartnershipStatus.ACTIVE)
        Visit.objects.create(entity="أمانة المنطقة", visit_date=TODAY - datetime.timedelta(days=5), purpose="تعريف",
                             follow_up_date=TODAY, status=VisitStatus.DONE)
        Visit.objects.create(entity="لاحقة", visit_date=TODAY, purpose="تعريف",
                             follow_up_date=TODAY + datetime.timedelta(days=3))
        call_command("relations_alerts", stdout=StringIO())
        titles = list(Notification.objects.filter(user=self.user).values_list("title", flat=True))
        self.assertEqual(len(titles), 2)
        self.assertTrue(any("شركة الخير" in t for t in titles))
        self.assertTrue(any("أمانة المنطقة" in t for t in titles))
        call_command("relations_alerts", stdout=StringIO())  # لا تكرار في اليوم نفسه
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)
        partnership.terminate(self.user)
        self.assertEqual(partnership.status, PartnershipStatus.ENDED)

    def test_event_mark_done_requires_attendees(self):
        event = PREvent.objects.create(title="ملتقى الشركاء", start_date=TODAY, budget=5000)
        with self.assertRaisesMessage(ValidationError, "الحضور الفعلي"):
            event.mark_done(self.user)
        event.actual_attendees = 120
        event.actual_cost = 4200
        event.save()
        event.mark_done(self.user)
        self.assertEqual(event.status, EventStatus.DONE)
        self.client.force_login(self.user)
        report = self.client.get("/relations/report/", {"from_date": TODAY.replace(day=1).isoformat(),
                                                        "to_date": TODAY.isoformat()})
        self.assertEqual(report.context["event_attendees"], 120)
        self.assertContains(report, "ملتقى الشركاء")

    def test_pages(self):
        contact = PRContact.objects.create(name="أ. سعد", organization="وزارة الموارد البشرية")
        partnership = Partnership.objects.create(partner="بنك محلي", start_date=TODAY, contact=contact,
                                                 end_date=TODAY + datetime.timedelta(days=40),
                                                 status=PartnershipStatus.ACTIVE)
        event = PREvent.objects.create(title="حملة الشتاء", start_date=TODAY + datetime.timedelta(days=3),
                                       partner=partnership)
        visit = Visit.objects.create(entity="جمعية شقيقة", visit_date=TODAY + datetime.timedelta(days=2),
                                     purpose="تبادل خبرات", contact=contact)
        coverage = MediaCoverage.objects.create(title="خبر الحملة", published_on=TODAY, event=event, reach=1000)
        self.client.force_login(self.user)
        html = self.client.get("/relations/").content.decode()
        for text in ("حملة الشتاء", "جمعية شقيقة", "بنك محلي", self.survey.title):
            self.assertIn(text, html)
        self.assertEqual(self.client.get("/relations/report/").status_code, 200)
        self.assertEqual(self.client.get(f"/relations/survey/{self.survey.pk}/results/").status_code, 200)
        objects = {"pr-contact": contact, "partnership": partnership, "pr-event": event, "visit": visit,
                   "media-coverage": coverage, "survey": self.survey}
        for slug, obj in objects.items():
            for url in (f"/app/{slug}/", f"/app/{slug}/new/", f"/app/{slug}/{obj.pk}/"):
                self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(f"/app/survey/{self.survey.pk}/"), self.survey.public_token)
        self.client.post(f"/app/partnership/{partnership.pk}/terminate/")
        partnership.refresh_from_db()
        self.assertEqual(partnership.status, PartnershipStatus.ENDED)
        # المستخدم المسجل دخوله يرى الصفحة العامة أيضاً
        self.assertContains(self.client.get(f"/s/{self.survey.public_token}/"), "ما تقييمك للخدمة؟")

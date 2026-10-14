import datetime

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from beneficiaries.tests import make_nonprofit
from core.models import Notification
from core.tenancy import tenant_context
from office.models import Correspondence
from website.models import FAQ, HeroSlide, ImpactCounter, NewsletterSubscriber, Video, Website, youtube_id
from website.site import clear_cache

HOST = "albirr.org.sa"
MEMBER = {"full_name": "فهد عبدالله السالم", "national_id": "1012345678", "phone": "0551234567",
          "email": "fahad@example.com", "notes": "أرغب في دعم الجمعية", "website": ""}
FEEDBACK = {"kind": "complaint", "name": "منى", "phone": "0559876543", "email": "", "subject": "تأخر الرد",
            "message": "تأخر الرد على طلبي", "website": ""}


class YoutubeAndSlideTests(TestCase):
    def test_youtube_id_parsing(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtube.com/watch?feature=x&v=dQw4w9WgXcQ",
                    "https://youtu.be/dQw4w9WgXcQ?t=10", "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                    "https://www.youtube.com/embed/dQw4w9WgXcQ", "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"):
            self.assertEqual(youtube_id(url), "dQw4w9WgXcQ", url)
            self.assertEqual(Video(youtube_url=url).youtube_id, "dQw4w9WgXcQ")
        for url in ("https://vimeo.com/123", "https://www.youtube.com/", "https://evil.com/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube.com/watch?v=<script>", "", "not a url"):
            self.assertEqual(youtube_id(url), "", url)
        with self.assertRaises(ValidationError):
            Video(title="x", youtube_url="https://vimeo.com/123").clean()
        Video(title="x", youtube_url="https://youtu.be/dQw4w9WgXcQ").clean()
        self.assertEqual(Video(youtube_url="https://youtu.be/dQw4w9WgXcQ").embed_url,
                         "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")

    def test_slide_resolved_url(self):
        self.assertEqual(HeroSlide(button_url="donate/").resolved_url("/site/albirr/"), "/site/albirr/donate/")
        self.assertEqual(HeroSlide(button_url="donate/").resolved_url("/"), "/donate/")
        self.assertEqual(HeroSlide(button_url="programs/").resolved_url(""), "/programs/")
        self.assertEqual(HeroSlide(button_url="/about/").resolved_url("/site/albirr/"), "/about/")
        self.assertEqual(HeroSlide(button_url="https://x.org/a").resolved_url("/site/albirr/"), "https://x.org/a")
        self.assertEqual(HeroSlide(button_url="").resolved_url("/site/albirr/"), "")


class ExtraPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from governance.models import MembershipCategory

        cls.company, cls.user = make_nonprofit("جمعية البر الأهلية", "extra_owner")
        cls.other, _ = make_nonprofit("جمعية أخرى", "extra_other")
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="albirr", domain=HOST, is_published=True)
            cls.category, _ = MembershipCategory.objects.get_or_create(name="عضو عامل")
            FAQ.objects.create(question="كيف أتبرع؟", answer="من صفحة التبرع", category="التبرع")
            FAQ.objects.create(question="سؤال مخفي سري", answer="x", is_published=False)
            FAQ.objects.create(question="ما أوقات العمل؟", answer="الأحد إلى الخميس")
            Video.objects.create(title="فيديو الملتقى", youtube_url="https://youtu.be/dQw4w9WgXcQ")
            HeroSlide.objects.create(title="اكفل يتيماً", button_url="donate/")
            ImpactCounter.objects.create(label="مستفيد", value=1250, suffix="+")

    def setUp(self):
        clear_cache()

    def test_pages_render(self):
        pages = {
            "services/": ["services-grid", 'data-icon="aid"', 'data-icon="member"', "/site/albirr/programs/#volunteering",
                          "/site/albirr/donate/", "/site/albirr/verify/"],
            "faq/": ["كيف أتبرع؟", "التبرع", "<details", "أسئلة عامة", "ما أوقات العمل؟"],
            "videos/": ["فيديو الملتقى", "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", 'loading="lazy"'],
            "membership/": ["عضو عامل", 'name="website"', 'name="national_id"'],
            "feedback/": ["شكر وثناء", 'name="website"'],
            "verify/": ['name="code"'],
            "privacy/": ["جمعية البر الأهلية", "نظام حماية البيانات الشخصية"],
        }
        for path, texts in pages.items():
            page = self.client.get(f"/site/albirr/{path}")
            self.assertEqual(page.status_code, 200, path)
            for text in texts:
                self.assertContains(page, text, msg_prefix=path)
        self.assertNotContains(self.client.get("/site/albirr/faq/"), "سؤال مخفي سري")
        Website.all_objects.filter(pk=self.site.pk).update(privacy_policy="سياسة خاصة بالجمعية")
        self.assertContains(self.client.get("/site/albirr/privacy/"), "سياسة خاصة بالجمعية")

    @override_settings(ALLOWED_HOSTS=["testserver", HOST])
    def test_pages_on_domain_and_sitemap(self):
        self.assertEqual(self.client.get("/faq/", HTTP_HOST=HOST).status_code, 200)
        sitemap = self.client.get("/sitemap.xml", HTTP_HOST=HOST)
        for path in ("services/", "faq/", "videos/", "membership/", "feedback/", "verify/", "privacy/"):
            self.assertContains(sitemap, f"<loc>http://{HOST}/{path}</loc>")

    def test_membership_disabled_404(self):
        Website.all_objects.filter(pk=self.site.pk).update(allow_membership_requests=False)
        self.assertEqual(self.client.get("/site/albirr/membership/").status_code, 404)
        self.assertNotContains(self.client.get("/site/albirr/services/"), 'data-icon="member"')

    def test_membership_creates_pending_member_and_rejects_duplicates(self):
        from governance.models import Member, MemberStatus

        response = self.client.post("/site/albirr/membership/", dict(MEMBER, category=self.category.pk))
        self.assertContains(response, "استلمنا طلب العضوية")
        member = Member.all_objects.get(company=self.company, national_id="1012345678")
        self.assertEqual(member.status, MemberStatus.PENDING)
        self.assertTrue(member.notes.startswith("طلب عضوية عبر الموقع الإلكتروني"))
        self.assertIn("أرغب في دعم الجمعية", member.notes)
        self.assertContains(response, member.member_number)
        self.assertFalse(member.can_vote(timezone.localdate().year))
        self.assertTrue(Notification.objects.filter(url=f"/app/member/{member.pk}/").exists())
        again = self.client.post("/site/albirr/membership/", dict(MEMBER, category=self.category.pk))
        self.assertContains(again, "قيد الدراسة")
        Member.all_objects.filter(pk=member.pk).update(status=MemberStatus.ACTIVE)
        active = self.client.post("/site/albirr/membership/", dict(MEMBER, category=self.category.pk))
        self.assertContains(active, "عضو في الجمعية العمومية")
        self.assertEqual(Member.all_objects.filter(national_id="1012345678").count(), 1)
        bad = self.client.post("/site/albirr/membership/", dict(MEMBER, national_id="3012345678",
                                                                category=self.category.pk))
        self.assertContains(bad, "رقم الهوية أو الإقامة")
        bot = self.client.post("/site/albirr/membership/", dict(MEMBER, national_id="2012345675",
                                                                category=self.category.pk, website="x"))
        self.assertEqual(bot.status_code, 200)
        self.assertEqual(Member.all_objects.filter(company=self.company).count(), 1)
        self.client.force_login(self.user)
        dashboard = self.client.get("/website/")
        self.assertEqual([v for _, v, _ in dashboard.context["inbound"]], [0, 0, 1, 0])

    def test_feedback_creates_correspondence(self):
        response = self.client.post("/site/albirr/feedback/", FEEDBACK)
        self.assertContains(response, "وصلتنا رسالتك")
        item = Correspondence.all_objects.get(company=self.company)
        self.assertEqual(item.direction, "incoming")
        self.assertTrue(item.subject.startswith("شكوى من الموقع: تأخر الرد"))
        self.assertEqual(item.external_reference, "الموقع الإلكتروني")
        self.assertContains(response, item.number)
        missing = self.client.post("/site/albirr/feedback/", dict(FEEDBACK, phone=""))
        self.assertContains(missing, "أدخل رقم الجوال أو البريد")
        praise = self.client.post("/site/albirr/feedback/", dict(FEEDBACK, kind="suggestion", phone="", name=""))
        self.assertContains(praise, "وصلتنا رسالتك")
        self.assertEqual(Correspondence.all_objects.filter(company=self.company).count(), 2)
        self.client.force_login(self.user)
        self.client.post("/site/albirr/contact/", {"name": "خالد", "phone": "0559876543", "subject": "سؤال",
                                                   "message": "x", "website": ""})
        dashboard = self.client.get("/website/")
        self.assertEqual([v for _, v, _ in dashboard.context["inbound"]], [0, 1, 0, 2])

    def test_verify_codes(self):
        from programs.models import ProgramEnrollment, TrainingProgram
        from store.models import DonationOrder
        from volunteering.models import Volunteer, VolunteerOpportunity, VolunteerParticipation

        today = timezone.localdate()
        with tenant_context(self.company):
            program = TrainingProgram.objects.create(title="دورة الإسعافات", start_date=today,
                                                     end_date=today + datetime.timedelta(days=2))
            enrollment = ProgramEnrollment.objects.create(program=program, full_name="سارة", national_id="1012345678",
                                                          phone="0551234567")
            ProgramEnrollment.all_objects.filter(pk=enrollment.pk).update(status="passed", certificate_code="TRN12345")
            opportunity = VolunteerOpportunity.objects.create(title="توزيع السلال", start_date=today,
                                                              end_date=today + datetime.timedelta(days=1))
            volunteer = Volunteer.objects.create(full_name="خالد", national_id="1012345678", phone="0551234567")
            part = VolunteerParticipation.objects.create(opportunity=opportunity, volunteer=volunteer)
            VolunteerParticipation.all_objects.filter(pk=part.pk).update(status="completed", certificate_code="VOL12345")
            order = DonationOrder.objects.create(donor_name="متبرع", donor_phone="0551234567", total=100)
            DonationOrder.all_objects.filter(pk=order.pk).update(status="paid")
            order.refresh_from_db()
        with tenant_context(self.other):
            other_program = TrainingProgram.objects.create(title="برنامج آخر", start_date=today,
                                                           end_date=today + datetime.timedelta(days=2))
            other = ProgramEnrollment.objects.create(program=other_program, full_name="س", national_id="1012345678",
                                                     phone="0551234567")
            ProgramEnrollment.all_objects.filter(pk=other.pk).update(status="passed", certificate_code="OTHER123")
        self.assertRedirects(self.client.get("/site/albirr/verify/?code=trn12345"),
                             "/site/albirr/t/verify/TRN12345/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/site/albirr/verify/?code= VOL12345 "),
                             "/site/albirr/v/verify/VOL12345/", fetch_redirect_response=False)
        donation = self.client.get(f"/site/albirr/verify/?code={order.verify_code.lower()}")
        self.assertContains(donation, "شهادة تبرع صحيحة")
        self.assertContains(donation, f"/site/albirr/donate/certificate/{order.number}/?c={order.verify_code}")
        for code in ("NOPE0000", "OTHER123"):  # رمز غير موجود أو لجمعية أخرى
            self.assertContains(self.client.get(f"/site/albirr/verify/?code={code}"), "لم يُعثر على شهادة بهذا الرمز")

    def test_newsletter_subscribe_and_safe_redirect(self):
        response = self.client.post("/site/albirr/newsletter/", {"email": "Reader@Example.com",
                                                                 "next": "/site/albirr/news/?page=2"})
        self.assertRedirects(response, "/site/albirr/news/?page=2&subscribed=1", fetch_redirect_response=False)
        sub = NewsletterSubscriber.all_objects.get(company=self.company)
        self.assertEqual(sub.email, "reader@example.com")
        NewsletterSubscriber.all_objects.filter(pk=sub.pk).update(is_active=False)
        for evil in ("https://evil.com", "//evil.com/x", "/\\evil.com", "javascript:alert(1)"):
            response = self.client.post("/site/albirr/newsletter/", {"email": "reader@example.com", "next": evil})
            self.assertRedirects(response, "/site/albirr/?subscribed=1", fetch_redirect_response=False)
        sub.refresh_from_db()
        self.assertTrue(sub.is_active)
        self.assertEqual(NewsletterSubscriber.all_objects.filter(company=self.company).count(), 1)
        bad = self.client.post("/site/albirr/newsletter/", {"email": "not-an-email", "next": "/site/albirr/faq/"})
        self.assertRedirects(bad, "/site/albirr/faq/?subscribed=0", fetch_redirect_response=False)
        bot = self.client.post("/site/albirr/newsletter/", {"email": "bot@example.com", "website": "x"})
        self.assertRedirects(bot, "/site/albirr/?subscribed=1", fetch_redirect_response=False)
        self.assertFalse(NewsletterSubscriber.all_objects.filter(email="bot@example.com").exists())
        self.assertEqual(self.client.get("/site/albirr/newsletter/").status_code, 405)

    def test_erp_doctypes_and_slide_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from website.test_content import PNG

        self.client.force_login(self.user)
        for slug in ("hero-slide", "impact-counter", "faq", "video", "newsletter-subscriber"):
            self.assertEqual(self.client.get(f"/app/{slug}/").status_code, 200, slug)
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200, slug)
        slide = HeroSlide.all_objects.get(company=self.company)
        self.assertContains(self.client.get(f"/app/hero-slide/{slide.pk}/"), "/website/upload/slide/")
        self.client.post(f"/website/upload/slide/{slide.pk}/", {"file": SimpleUploadedFile("s.png", PNG)})
        slide.refresh_from_db()
        self.assertTrue(slide.image.startswith("data:image/png;base64,"))
        bad = self.client.post("/app/video/new/", {"title": "x", "youtube_url": "https://vimeo.com/1",
                                                   "published_on": "2026-10-01", "is_published": "on"})
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(Video.all_objects.filter(title="x").exists())
        settings = self.client.post("/website/settings/", {
            "slug": "albirr", "domain": HOST, "is_published": "on", "primary_color": "#1e6e5a",
            "allow_membership_requests": "on", "tiktok_url": "https://tiktok.com/@albirr",
            "video_intro_url": "https://youtu.be/dQw4w9WgXcQ", "org_chart_file": SimpleUploadedFile("o.png", PNG)})
        self.assertEqual(settings.status_code, 302)
        site = Website.all_objects.get(pk=self.site.pk)
        self.assertTrue(site.org_chart.startswith("data:image/png;base64,"))
        self.assertEqual(site.tiktok_url, "https://tiktok.com/@albirr")
        self.assertContains(self.client.get("/site/albirr/videos/"), "youtube-nocookie")
        page = self.client.get("/website/")
        self.assertContains(page, "+ شريحة واجهة")

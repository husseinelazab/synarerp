import base64
import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from beneficiaries.models import Beneficiary, BeneficiaryCategory
from beneficiaries.tests import make_nonprofit
from core.models import Notification
from core.tenancy import tenant_context
from office.models import Correspondence
from website.models import Album, AlbumPhoto, NewsPost, Partner, PublishedDocument, Website
from website.site import clear_cache

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
HOST = "albirr.org.sa"
APPLY = {"full_name": "سارة محمد علي القحطاني", "national_id": "1012345678", "phone": "0551234567",
         "nationality": "سعودي", "gender": "female", "city": "الرياض", "family_size": "5",
         "needs": "مساعدة في سداد الإيجار", "consent": "on", "website": ""}
CONTACT = {"name": "خالد", "phone": "0559876543", "email": "", "subject": "استفسار عن التطوع",
           "message": "أرغب في التطوع لديكم", "website": ""}


class WebsiteContentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from excellence.models import StrategicGoal, StrategicPlan
        from governance.models import BoardSeat, BoardTerm, Member, MembershipCategory
        from programs.models import TrainingProgram
        from relations.models import Survey
        from volunteering.models import VolunteerOpportunity

        cls.company, cls.user = make_nonprofit("جمعية البر الأهلية", "content_owner")
        cls.other, cls.other_user = make_nonprofit("جمعية أخرى", "content_other")
        cls.company.license_number = "1234"
        cls.company.headquarters = "الرياض"
        cls.company.save()
        today = timezone.localdate()
        with tenant_context(cls.company):
            cls.site = Website.objects.create(slug="albirr", domain=HOST, about="جمعية خيرية تخدم الأسر",
                                              bank_accounts="مصرف الراجحي - SA0000000000000000000000",
                                              map_url="https://maps.example.com/x", working_hours="8 ص - 4 م")
            cls.news = NewsPost.objects.create(title="افتتاح مركز الجمعية", category="event", summary="ملخص الافتتاح",
                                               body="نص الخبر الكامل", cover=PNG_URI)
            cls.hidden_news = NewsPost.objects.create(title="خبر مخفي سري", is_published=False)
            cls.doc = PublishedDocument.objects.create(title="لائحة الصرف", category="policies", year=2026,
                                                       file_name="لائحة.pdf", content_type="application/pdf",
                                                       content=PDF, size=len(PDF))
            cls.hidden_doc = PublishedDocument.objects.create(title="مسودة داخلية", is_published=False,
                                                              file_name="x.pdf", content_type="application/pdf",
                                                              content=PDF, size=len(PDF))
            cls.album = Album.objects.create(title="حفل التكريم", event_date=today)
            AlbumPhoto.objects.create(album=cls.album, image=PNG_URI, caption="لقطة جماعية")
            Partner.objects.create(name="شركة الداعم الأول", logo=PNG_URI, url="https://partner.example.com")
            cls.program = TrainingProgram.objects.create(title="دورة الإسعافات الأولية", start_date=today,
                                                         end_date=today + datetime.timedelta(days=5))
            TrainingProgram.objects.filter(pk=cls.program.pk).update(status="open")
            cls.opportunity = VolunteerOpportunity.objects.create(title="توزيع السلال الغذائية", start_date=today,
                                                                  end_date=today + datetime.timedelta(days=3))
            cls.survey = Survey.objects.create(title="استبيان رضا المستفيدين")
            plan = StrategicPlan.objects.create(title="الخطة 2026–2030", start_year=2026, end_year=2030,
                                                vision="مجتمع متكافل", mission="نمكّن الأسر", values="الأمانة\nالإتقان")
            StrategicPlan.objects.filter(pk=plan.pk).update(status="approved")
            StrategicGoal.objects.create(plan=plan, code="ه1", title="تمكين الأسر اقتصادياً",
                                         vision2030_pillar="society")
            category = MembershipCategory.objects.create(name="عامل")
            chair = Member.objects.create(full_name="عبدالله الرئيس", category=category)
            term = BoardTerm.objects.create(name="الدورة الخامسة", start_date=today - datetime.timedelta(days=30),
                                            end_date=today + datetime.timedelta(days=900))
            BoardSeat.objects.create(parent=term, member=chair, position="chair")
        with tenant_context(cls.other):
            cls.foreign_doc = PublishedDocument.objects.create(title="وثيقة جمعية أخرى", file_name="o.pdf",
                                                               content_type="application/pdf", content=PDF,
                                                               size=len(PDF))
            from programs.models import TrainingProgram as TP

            cls.foreign_program = TP.objects.create(title="برنامج جمعية أخرى", start_date=today,
                                                    end_date=today + datetime.timedelta(days=5))
            TP.objects.filter(pk=cls.foreign_program.pk).update(status="open")

    def setUp(self):
        clear_cache()

    def publish(self):
        Website.all_objects.filter(pk=self.site.pk).update(is_published=True)
        clear_cache()

    # ------------------------------------------------------------ الصفحات العامة

    def test_home_sections(self):
        self.client.force_login(self.user)
        page = self.client.get("/site/albirr/")
        for text in ("افتتاح مركز الجمعية", "دورة الإسعافات الأولية", "توزيع السلال الغذائية",
                     "استبيان رضا المستفيدين", "شركاء النجاح", "شركة الداعم الأول"):
            self.assertContains(page, text)
        self.assertNotContains(page, "خبر مخفي سري")
        self.assertContains(page, f'href="/site/albirr/t/{self.program.public_token}/"')
        self.assertContains(page, f'href="/site/albirr/v/{self.opportunity.public_token}/"')
        self.assertContains(page, f'href="/site/albirr/s/{self.survey.public_token}/"')
        self.assertContains(page, f'href="/site/albirr/news/{self.news.pk}/"')
        self.assertNotContains(page, "برنامج جمعية أخرى")

    def test_home_without_content_hides_sections(self):
        NewsPost.all_objects.filter(company=self.company).delete()
        Partner.all_objects.filter(company=self.company).delete()
        self.client.force_login(self.user)
        page = self.client.get("/site/albirr/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "أحدث الأخبار")
        self.assertNotContains(page, "شركاء النجاح")

    def test_public_pages_render(self):
        self.client.force_login(self.user)
        pages = {
            "about/": ["جمعية خيرية تخدم الأسر", "مجتمع متكافل", "الأمانة", "تمكين الأسر اقتصادياً",
                       "مجتمع حيوي", "عبدالله الرئيس", "رئيس مجلس الإدارة", "1234", "الرياض"],
            "governance/": ["لائحة الصرف", "اللوائح والسياسات", f"/site/albirr/documents/{self.doc.pk}/",
                            "مسجلة لديه برقم", "عبدالله الرئيس"],
            "programs/": ["دورة الإسعافات الأولية", "توزيع السلال الغذائية"],
            "news/": ["افتتاح مركز الجمعية"],
            f"news/{self.news.pk}/": ["نص الخبر الكامل", 'property="og:title"', "og:description"],
            "gallery/": ["حفل التكريم"],
            f"gallery/{self.album.pk}/": ["لقطة جماعية"],
            "apply/": ["الاسم الرباعي", 'name="website"'],
            "contact/": ["مصرف الراجحي", "https://maps.example.com/x", "8 ص - 4 م", 'name="website"'],
        }
        for path, texts in pages.items():
            page = self.client.get(f"/site/albirr/{path}")
            self.assertEqual(page.status_code, 200, path)
            for text in texts:
                self.assertContains(page, text, msg_prefix=path)
        governance = self.client.get("/site/albirr/governance/")
        self.assertNotContains(governance, "مسودة داخلية")
        self.assertNotContains(self.client.get("/site/albirr/news/"), "خبر مخفي سري")

    def test_programs_lists_aid_programs_and_projects(self):
        from accounts.models import Account
        from beneficiaries.models import AidProgram
        from projects.models import Project

        with tenant_context(self.company):
            account = Account.objects.filter(is_group=False, root_type="expense").first()
            AidProgram.objects.create(name="برنامج السلة الغذائية", expense_account=account, description="شهري")
            Project.objects.create(name="مشروع سقيا الماء", description="حفر آبار")
            Project.objects.create(name="مشروع ملغى", status="cancelled")
        self.client.force_login(self.user)
        page = self.client.get("/site/albirr/programs/")
        self.assertContains(page, "برنامج السلة الغذائية")
        self.assertContains(page, "مشروع سقيا الماء")
        self.assertNotContains(page, "مشروع ملغى")

    def test_news_filter_pagination_and_hidden(self):
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get("/site/albirr/news/?category=announcement"), "افتتاح مركز الجمعية")
        self.assertContains(self.client.get("/site/albirr/news/?category=event"), "افتتاح مركز الجمعية")
        self.assertEqual(self.client.get("/site/albirr/news/?page=99").status_code, 200)
        self.assertEqual(self.client.get(f"/site/albirr/news/{self.hidden_news.pk}/").status_code, 404)

    def test_document_download_scoping(self):
        self.client.force_login(self.user)
        response = self.client.get(f"/site/albirr/documents/{self.doc.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(b"".join(response), PDF)
        self.assertIn("filename*=UTF-8''", response["Content-Disposition"])
        self.assertEqual(self.client.get(f"/site/albirr/documents/{self.hidden_doc.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/site/albirr/documents/{self.foreign_doc.pk}/").status_code, 404)

    # ------------------------------------------------------------ النماذج العامة

    @override_settings(ALLOWED_HOSTS=["testserver", HOST])
    def test_apply_creates_beneficiary_and_blocks_duplicates(self):
        self.publish()
        response = self.client.post("/apply/", APPLY, HTTP_HOST=HOST)
        self.assertContains(response, "استلمنا طلبك")
        b = Beneficiary.all_objects.get(company=self.company, national_id="1012345678")
        self.assertEqual(b.status, "new")
        self.assertContains(response, b.file_number)
        self.assertIn("عدد أفراد الأسرة: 5", b.notes)
        note = Notification.objects.filter(company=self.company, user=self.user).latest("id")
        self.assertEqual(note.url, f"/app/beneficiary/{b.pk}/")
        again = self.client.post("/apply/", dict(APPLY, full_name="اسم آخر"), HTTP_HOST=HOST)
        self.assertContains(again, "سبق تسجيل طلب بهذا الرقم")
        self.assertNotContains(again, "سارة")
        self.assertEqual(Beneficiary.all_objects.filter(national_id="1012345678").count(), 1)

    def test_apply_validation_and_honeypot(self):
        self.client.force_login(self.user)
        bad = self.client.post("/site/albirr/apply/", dict(APPLY, national_id="3012345678", phone="123"))
        self.assertContains(bad, "رقم الهوية أو الإقامة")
        self.assertContains(bad, "رقم جوال سعودي")
        bot = self.client.post("/site/albirr/apply/", dict(APPLY, website="http://spam"))
        self.assertContains(bot, "استلمنا طلبك")
        self.assertFalse(Beneficiary.all_objects.filter(national_id="1012345678").exists())
        with tenant_context(self.company):
            category = BeneficiaryCategory.objects.first()
        ok = self.client.post("/site/albirr/apply/", dict(APPLY, category=category.pk, housing_type="rented",
                                                          monthly_income="1500"))
        self.assertContains(ok, "استلمنا طلبك")
        b = Beneficiary.all_objects.get(national_id="1012345678")
        self.assertEqual((b.category_id, b.housing_type, int(b.monthly_income)), (category.pk, "rented", 1500))

    @override_settings(ALLOWED_HOSTS=["testserver", HOST])
    def test_contact_creates_incoming_correspondence(self):
        self.publish()
        response = self.client.post("/contact/", CONTACT, HTTP_HOST=HOST)
        self.assertContains(response, "وصلت رسالتك")
        item = Correspondence.all_objects.get(company=self.company)
        self.assertEqual(item.direction, "incoming")
        self.assertIn("خالد", item.party)
        self.assertIn("0559876543", item.party)
        self.assertIn("استفسار عن التطوع", item.subject)
        self.assertContains(response, item.number)
        self.assertTrue(Notification.objects.filter(url=f"/app/correspondence/{item.pk}/").exists())
        bot = self.client.post("/contact/", dict(CONTACT, website="x"), HTTP_HOST=HOST)
        self.assertContains(bot, "وصلت رسالتك")
        self.assertEqual(Correspondence.all_objects.filter(company=self.company).count(), 1)
        missing = self.client.post("/contact/", dict(CONTACT, phone=""), HTTP_HOST=HOST)
        self.assertContains(missing, "أدخل رقم الجوال أو البريد")

    # ------------------------------------------------------------ النطاق المستقل

    @override_settings(ALLOWED_HOSTS=["testserver", HOST])
    def test_public_links_and_token_pages_on_domain(self):
        self.publish()
        home = self.client.get("/", HTTP_HOST=HOST)
        self.assertContains(home, f'href="/t/{self.program.public_token}/"')
        self.assertContains(home, f'href="/v/{self.opportunity.public_token}/"')
        page = self.client.get(f"/t/{self.program.public_token}/", HTTP_HOST=HOST)
        self.assertContains(page, "دورة الإسعافات الأولية")
        self.assertEqual(self.client.get(f"/v/{self.opportunity.public_token}/", HTTP_HOST=HOST).status_code, 200)
        self.assertContains(self.client.get(f"/s/{self.survey.public_token}/", HTTP_HOST=HOST),
                            "استبيان رضا المستفيدين")
        # برنامج جمعية أخرى لا يُفتح عبر نطاق هذه الجمعية
        self.assertEqual(self.client.get(f"/t/{self.foreign_program.public_token}/", HTTP_HOST=HOST).status_code, 404)
        self.assertEqual(self.client.get(f"/documents/{self.doc.pk}/", HTTP_HOST=HOST).status_code, 200)
        self.assertEqual(self.client.get(f"/documents/{self.foreign_doc.pk}/", HTTP_HOST=HOST).status_code, 404)

    def test_token_pages_in_preview(self):
        self.client.force_login(self.user)
        page = self.client.get(f"/site/albirr/t/{self.program.public_token}/")
        self.assertContains(page, "دورة الإسعافات الأولية")
        self.assertEqual(self.client.get(f"/site/albirr/t/{self.foreign_program.public_token}/").status_code, 404)

    @override_settings(ALLOWED_HOSTS=["testserver", HOST])
    def test_robots_and_sitemap(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/site/albirr/robots.txt"), "Disallow: /")  # غير منشور
        self.publish()
        self.client.logout()
        robots = self.client.get("/robots.txt", HTTP_HOST=HOST)
        self.assertEqual(robots["Content-Type"], "text/plain; charset=utf-8")
        self.assertContains(robots, f"Sitemap: http://{HOST}/sitemap.xml")
        sitemap = self.client.get("/sitemap.xml", HTTP_HOST=HOST)
        self.assertContains(sitemap, f"<loc>http://{HOST}/about/</loc>")
        self.assertContains(sitemap, f"<loc>http://{HOST}/news/{self.news.pk}/</loc>")
        self.assertNotContains(sitemap, f"/news/{self.hidden_news.pk}/")

    def test_unpublished_site_hidden_from_anonymous(self):
        self.assertEqual(self.client.get("/site/albirr/news/").status_code, 404)
        self.assertEqual(self.client.get(f"/site/albirr/documents/{self.doc.pk}/").status_code, 404)

    # ------------------------------------------------------------ إدارة الموقع في النظام

    def test_erp_dashboard_and_doctypes(self):
        self.client.force_login(self.user)
        page = self.client.get("/website/")
        self.assertContains(page, "إدارة الموقع الإلكتروني")
        self.assertContains(page, "/site/albirr/")
        for slug, obj in (("news-post", self.news), ("album", self.album), ("published-document", self.doc),
                          ("partner", None)):
            self.assertEqual(self.client.get(f"/app/{slug}/").status_code, 200, slug)
            self.assertEqual(self.client.get(f"/app/{slug}/new/").status_code, 200, slug)
            if obj is not None:
                self.assertEqual(self.client.get(f"/app/{slug}/{obj.pk}/").status_code, 200, slug)
        response = self.client.post("/app/news-post/new/", {"title": "خبر من النظام", "category": "news",
                                                            "published_on": "2026-10-01", "is_published": "on"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(NewsPost.all_objects.filter(company=self.company, title="خبر من النظام").exists())

    def test_dashboard_counts_site_submissions(self):
        self.client.force_login(self.user)
        self.client.post("/site/albirr/apply/", APPLY)
        self.client.post("/site/albirr/contact/", CONTACT)
        page = self.client.get("/website/")
        self.assertEqual([v for _, v, _ in page.context["inbound"]], [1, 1])

    def test_album_photo_upload(self):
        self.client.force_login(self.user)
        url = f"/website/albums/{self.album.pk}/photos/"
        self.assertEqual(self.client.get(url).status_code, 200)
        files = [SimpleUploadedFile(f"p{i}.png", PNG, content_type="image/png") for i in range(3)]
        response = self.client.post(url, {"photos": files, "caption": "يوم التطوع"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.album.photos.count(), 4)
        self.assertEqual(self.album.photos.filter(caption="يوم التطوع").count(), 3)
        bad = self.client.post(url, {"photos": [SimpleUploadedFile("x.png", b"not an image")]})
        self.assertContains(bad, "ليس صورة")
        big = self.client.post(url, {"photos": [SimpleUploadedFile("big.png", PNG + b"0" * 900 * 1024)]})
        self.assertContains(big, "أكبر من 800")
        many = self.client.post(url, {"photos": [SimpleUploadedFile(f"{i}.png", PNG) for i in range(21)]})
        self.assertContains(many, "الحد الأقصى 20")
        self.assertEqual(self.album.photos.count(), 4)
        photo = self.album.photos.last()
        self.client.post(f"{url}{photo.pk}/delete/")
        self.assertEqual(self.album.photos.count(), 3)

    def test_album_of_other_company_not_accessible(self):
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(f"/website/albums/{self.album.pk}/photos/").status_code, 404)

    def test_cover_logo_and_document_uploads(self):
        self.client.force_login(self.user)
        with tenant_context(self.company):
            post = NewsPost.objects.create(title="بلا غلاف")
            partner = Partner.objects.create(name="شريك جديد")
            doc = PublishedDocument.objects.create(title="القوائم المالية 2025", category="financials", year=2025)
        self.client.post(f"/website/upload/news/{post.pk}/", {"file": SimpleUploadedFile("c.png", PNG)})
        post.refresh_from_db()
        self.assertTrue(post.cover.startswith("data:image/png;base64,"))
        self.client.post(f"/website/upload/news/{post.pk}/", {"remove": "on"})
        post.refresh_from_db()
        self.assertEqual(post.cover, "")
        self.client.post(f"/website/upload/partner/{partner.pk}/", {"file": SimpleUploadedFile("l.png", PNG)})
        partner.refresh_from_db()
        self.assertTrue(partner.logo)
        # بلا ملف لا تظهر الوثيقة في الموقع
        self.assertNotContains(self.client.get("/site/albirr/governance/"), "القوائم المالية 2025")
        self.client.post(f"/website/upload/document/{doc.pk}/", {"file": SimpleUploadedFile("fs.exe", b"MZ...")})
        doc.refresh_from_db()
        self.assertFalse(doc.has_file)
        self.client.post(f"/website/upload/document/{doc.pk}/", {"file": SimpleUploadedFile("قوائم.pdf", PDF)})
        doc.refresh_from_db()
        self.assertEqual((doc.content_type, doc.file_name, bytes(doc.content)), ("application/pdf", "قوائم.pdf", PDF))
        self.assertContains(self.client.get("/site/albirr/governance/"), "القوائم المالية 2025")
        self.assertContains(self.client.get(f"/app/published-document/{doc.pk}/"), "قوائم.pdf")
        self.assertEqual(self.client.post(f"/website/upload/bogus/{doc.pk}/").status_code, 404)

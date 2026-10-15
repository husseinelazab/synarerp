import datetime
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from beneficiaries.models import Beneficiary, BeneficiaryCategory, BeneficiaryStatus
from beneficiaries.tests import make_nonprofit
from campaigns import service
from campaigns.models import (Campaign, CampaignRecipient, CampaignSettings, CampaignStatus, Channel, OptOut,
                              ReminderLog, Segment, SegmentRule)
from core.tenancy import tenant_context
from funds.models import DonationReceipt, Donor, Sponsorship, SponsorshipDue, SponsorshipType

TODAY = timezone.localdate()


def receipt(donor, amount, days_ago, **extra):
    company = donor.company
    return DonationReceipt.all_objects.create(
        company=company, donor=donor, donor_name=donor.name, donor_phone=donor.phone, amount=amount,
        account=company.default_cash_account, posting_date=TODAY - datetime.timedelta(days=days_ago), docstatus=1,
        **extra)


@override_settings(SYNAR_MESSAGING_ASYNC=False, SYNAR_EMAIL_ASYNC=False)
class CampaignTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company, cls.user = make_nonprofit("جمعية التواصل", "camp_owner")
        cls.other, cls.other_user = make_nonprofit("جمعية أخرى", "camp_other")

    def setUp(self):
        self.ctx = tenant_context(self.company)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.ali = Donor.objects.create(name="علي", phone="0551000001", email="ali@example.com")
        self.sara = Donor.objects.create(name="سارة", phone="0551000002")
        self.mail_only = Donor.objects.create(name="خالد", email="Khalid@Example.com")
        Donor.objects.create(name="بلا تواصل")

    def segment(self, rule=SegmentRule.ALL_DONORS, **kwargs):
        return Segment.objects.create(name=kwargs.pop("name", f"شريحة {Segment.objects.count()}"), rule=rule,
                                      **kwargs)

    def sponsor(self, donor, national_id="1200000001"):
        orphan = Beneficiary.objects.create(full_name=f"يتيم {national_id}", national_id=national_id, phone="1",
                                            category=BeneficiaryCategory.objects.get(name="أيتام"),
                                            status=BeneficiaryStatus.APPROVED,
                                            next_review_date=TODAY + datetime.timedelta(days=300))
        s = Sponsorship(donor=donor, beneficiary=orphan, sponsorship_type=SponsorshipType.objects.get(name="كفالة يتيم"),
                        monthly_amount=300, start_date=TODAY.replace(day=1) - datetime.timedelta(days=90))
        s.full_clean()
        s.save()
        return s

    def mock_send(self):
        patcher = mock.patch("messaging.service.send", return_value=(None, ""))
        send = patcher.start()
        self.addCleanup(patcher.stop)
        sms = mock.patch("campaigns.service.sms_available", return_value=True)
        sms.start()
        self.addCleanup(sms.stop)
        return send


class SegmentTests(CampaignTestBase):
    def emails(self, seg):
        return sorted(r["email"] for r in seg.recipients() if r["email"])

    def names(self, seg):
        return sorted(r["name"] for r in seg.recipients())

    def test_all_donors_normalized(self):
        rows = self.segment().recipients()
        self.assertEqual(sorted(r["name"] for r in rows), ["خالد", "سارة", "علي"])
        ali = next(r for r in rows if r["name"] == "علي")
        self.assertEqual((ali["phone"], ali["email"], ali["source"]), ("966551000001", "ali@example.com", "الداعمون"))
        self.assertIn("khalid@example.com", self.emails(self.segment()))

    def test_lapsed(self):
        receipt(self.ali, 100, 300)  # تبرع قديم فقط ← منقطع
        receipt(self.sara, 100, 400)
        receipt(self.sara, 100, 10)  # تبرع حديث ← ليس منقطعاً
        self.assertEqual(self.names(self.segment(SegmentRule.LAPSED, months=6)), ["علي"])

    def test_major(self):
        receipt(self.ali, 3000, 30)
        receipt(self.ali, 2500, 100)
        receipt(self.sara, 4000, 30)
        receipt(self.sara, 9000, 500)  # أقدم من 12 شهراً
        self.assertEqual(self.names(self.segment(SegmentRule.MAJOR, min_amount=5000)), ["علي"])

    def test_sponsors_and_overdue(self):
        s = self.sponsor(self.ali)
        self.sponsor(self.sara, "1200000002")
        self.assertEqual(self.names(self.segment(SegmentRule.ACTIVE_SPONSORS)), ["سارة", "علي"])
        last_month = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        SponsorshipDue.objects.create(sponsorship=s, period=last_month, amount=300)
        self.assertEqual(self.names(self.segment(SegmentRule.OVERDUE_SPONSORS)), ["علي"])

    def test_store_recurring_newsletter_and_dedupe(self):
        from store.models import DonationOpportunity, DonationOrder, RecurringDonation
        from website.models import NewsletterSubscriber

        opp = DonationOpportunity.objects.create(title="سقيا", kind="general")
        for phone in ("0559000001", "+966559000001", "0559000002"):
            DonationOrder.objects.create(donor_name="متبرع", donor_phone=phone, status="paid", total=50)
        DonationOrder.objects.create(donor_name="معلّق", donor_phone="0559000003", status="pending", total=50)
        rows = self.segment(SegmentRule.STORE_DONORS).recipients()
        self.assertEqual(sorted(r["phone"] for r in rows), ["966559000001", "966559000002"])

        RecurringDonation.objects.create(donor_name="شهري", donor_phone="0559000005", opportunity=opp, amount=100,
                                         next_charge_on=TODAY)
        RecurringDonation.objects.create(donor_name="متوقف", donor_phone="0559000006", opportunity=opp, amount=100,
                                         next_charge_on=TODAY, active=False)
        self.assertEqual(self.names(self.segment(SegmentRule.RECURRING)), ["شهري"])

        NewsletterSubscriber.objects.create(email="n1@example.com")
        NewsletterSubscriber.objects.create(email="n2@example.com", is_active=False)
        rows = self.segment(SegmentRule.NEWSLETTER).recipients()
        self.assertEqual([(r["phone"], r["email"]) for r in rows], [("", "n1@example.com")])

    def test_fund_donors(self):
        from accounts.models import Fund

        fund = Fund.objects.get(name="صندوق الزكاة")
        receipt(self.sara, 100, 5, donation_type="zakat", fund=fund)
        receipt(self.ali, 100, 5)
        self.assertEqual(self.names(self.segment(SegmentRule.FUND_DONORS, fund=fund)), ["سارة"])
        with self.assertRaises(ValidationError):
            Segment(name="بلا صندوق", rule=SegmentRule.FUND_DONORS).full_clean()

    def test_dedupe_merges_phone_and_email(self):
        from campaigns.segments import Collector

        c = Collector(self.company)
        c.add("أحمد", "0551234567", "", "أ")
        c.add("", "+966 55 123 4567", "ahmad@example.com", "ب")
        c.add("آخر", "", "AHMAD@example.com", "ج")
        self.assertEqual(len(c.rows), 1)
        self.assertEqual((c.rows[0]["name"], c.rows[0]["phone"], c.rows[0]["email"]),
                         ("أحمد", "966551234567", "ahmad@example.com"))

    def test_cross_company_isolation(self):
        with tenant_context(self.other):
            Donor.objects.create(name="داعم الجمعية الأخرى", phone="0557777777")
            other_seg = Segment.objects.create(name="الكل", rule=SegmentRule.ALL_DONORS)
        self.assertNotIn("داعم الجمعية الأخرى", self.names(self.segment()))
        with tenant_context(self.other):
            self.assertEqual(self.names(other_seg), ["داعم الجمعية الأخرى"])
        self.assertFalse(Segment.objects.filter(pk=other_seg.pk).exists())


class CampaignSendTests(CampaignTestBase):
    def campaign(self, **kwargs):
        kwargs.setdefault("segment", self.segment())
        kwargs.setdefault("message", "{الاسم}، شكراً لدعمكم {الجمعية} {الرابط}")
        kwargs.setdefault("link", "https://example.org/d/1")
        return Campaign.objects.create(title=kwargs.pop("title", "حملة"), **kwargs)

    def test_render_placeholders_and_sms_counter(self):
        self.assertEqual(service.render("{الاسم}، شكراً {الجمعية} {الرابط}", "علي", self.company, "https://x.y"),
                         "علي، شكراً جمعية التواصل https://x.y")
        self.assertTrue(service.render("{الاسم}", "", self.company).startswith("داعمنا"))
        self.assertEqual(service.sms_segments("ا" * 70)["segments"], 1)
        self.assertEqual(service.sms_segments("ا" * 71)["segments"], 2)
        self.assertEqual(service.sms_segments("ا" * 135)["segments"], 3)
        self.assertEqual(service.sms_segments("a" * 160)["segments"], 1)
        self.assertEqual(service.sms_segments("a" * 161)["segments"], 2)
        self.assertEqual(service.sms_segments("")["segments"], 0)

    def test_link_required_for_placeholder(self):
        with self.assertRaises(ValidationError):
            Campaign(title="x", segment=self.segment(), message="{الرابط}").full_clean()

    def test_send_idempotent_with_optout_and_footer(self):
        send = self.mock_send()
        OptOut.objects.create(phone="0551000002")  # سارة
        campaign = self.campaign()
        campaign.send_now(self.user)
        self.assertEqual(send.call_count, 1)  # علي فقط (خالد بلا جوال، سارة موقوفة)
        args, kwargs = send.call_args
        self.assertEqual(args[1], "966551000001")
        self.assertIn("علي، شكراً لدعمكم جمعية التواصل https://example.org/d/1", args[2])
        self.assertIn(service.SMS_FOOTER, args[2])
        self.assertEqual(kwargs["mode"], "provider")
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, CampaignStatus.SENT)
        self.assertEqual(campaign.stats, {"total": 2, "queued": 0, "sent": 1, "failed": 0, "skipped": 1})
        self.assertEqual(CampaignRecipient.objects.get(address="966551000002").error, service.OPTED_OUT)

        # إعادة التشغيل لا تكرر الإرسال، حتى بعد إضافة داعم جديد لا يُرسل إلا للجديد
        service.send_campaign(campaign)
        self.assertEqual(send.call_count, 1)
        Donor.objects.create(name="جديد", phone="0551000009")
        service.send_campaign(campaign)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(CampaignRecipient.objects.filter(campaign=campaign, address="966551000001").count(), 1)

    def test_failure_without_provider(self):
        with mock.patch("messaging.service.send") as send:
            campaign = self.campaign()
            campaign.send_now(self.user)
        send.assert_not_called()
        stats = campaign.stats
        self.assertEqual((stats["sent"], stats["failed"]), (0, 2))
        self.assertEqual(set(campaign.recipients.values_list("error", flat=True)), {service.NO_SMS_PROVIDER})
        self.assertIn(("retry_failed", "إعادة محاولة الفاشلة"), campaign.available_actions)

    def test_real_messaging_without_provider_marks_failed(self):
        # مع افتراض توفر المزوّد بالخطأ: messaging يرفض بوضع provider ويُسجل السبب ولا يتعطل الإرسال
        with mock.patch("campaigns.service.sms_available", return_value=True):
            campaign = self.campaign()
            service.send_campaign(campaign)
        self.assertEqual(campaign.stats["failed"], 2)
        self.assertIn("غير مفعّل", campaign.recipients.first().error)

    def test_email_channel_with_unsubscribe_link(self):
        from django.core import mail

        campaign = self.campaign(channel=Channel.EMAIL)
        with mock.patch("campaigns.service.mail_available", return_value=True), \
                mock.patch("core.mail.send", return_value=True) as send_mail:
            campaign.send_now(self.user)
        self.assertEqual(send_mail.call_count, 2)  # علي وخالد
        body = send_mail.call_args_list[0][0][2]
        self.assertIn("/u/", body)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(campaign.stats["sent"], 2)

    def test_both_channels_and_actions(self):
        campaign = self.campaign(channel=Channel.BOTH, scheduled_at=timezone.now() + datetime.timedelta(hours=2))
        campaign.schedule(self.user)
        self.assertEqual(campaign.status, CampaignStatus.SCHEDULED)
        send = self.mock_send()
        self.assertEqual(service.run_scheduled_campaigns(self.company), 0)  # لم يحن الموعد
        self.assertEqual(service.run_scheduled_campaigns(self.company,
                                                         now=timezone.now() + datetime.timedelta(hours=3)), 1)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, CampaignStatus.SENT)
        self.assertEqual(send.call_count, 2)  # SMS لعلي وسارة
        self.assertEqual(campaign.recipients.filter(channel="email").count(), 2)  # فشلت لعدم تفعيل البريد
        draft = self.campaign(title="ملغاة")
        draft.cancel_campaign(self.user)
        self.assertEqual(draft.status, CampaignStatus.CANCELLED)
        with self.assertRaises(ValidationError):
            draft.send_now(self.user)

    def test_send_test(self):
        send = self.mock_send()
        campaign = self.campaign()
        ok, info = service.send_test(campaign, "0551112222")
        self.assertTrue(ok, info)
        self.assertEqual(send.call_args[0][1], "966551112222")
        self.assertFalse(campaign.recipients.exists())
        self.assertFalse(service.send_test(campaign, "abc")[0])

    def test_cross_company_send_isolation(self):
        send = self.mock_send()
        with tenant_context(self.other):
            Donor.objects.create(name="غريب", phone="0558888888")
        self.campaign().send_now(self.user)
        self.assertNotIn("966558888888", [c[0][1] for c in send.call_args_list])


class UnsubscribeTests(CampaignTestBase):
    def test_signed_link(self):
        token = service.unsubscribe_token(self.company, "Ali@Example.com")
        url = f"/u/{token}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(OptOut.all_objects.filter(company=self.company).exists())
        response = self.client.post(url)
        self.assertContains(response, "تم إيقاف")
        self.assertTrue(OptOut.all_objects.filter(company=self.company, email="ali@example.com").exists())
        self.client.post(url)
        self.assertEqual(OptOut.all_objects.filter(company=self.company).count(), 1)
        self.assertEqual(self.client.get(f"/u/{token[:-2]}xx/").status_code, 400)

        # البريد الموقوف لا يُرسل له
        campaign = Campaign.objects.create(title="بريد", channel=Channel.EMAIL, segment=self.segment(),
                                           message="مرحبا {الاسم}")
        with mock.patch("campaigns.service.mail_available", return_value=True), \
                mock.patch("core.mail.send", return_value=True) as send_mail:
            service.send_campaign(campaign)
        self.assertEqual([c[0][3] for c in send_mail.call_args_list], [["khalid@example.com"]])


class ReminderTests(CampaignTestBase):
    def test_due_reminders_once_per_period(self):
        send = self.mock_send()
        self.sponsor(self.ali)
        self.sponsor(self.ali, "1200000003")
        cs = CampaignSettings.for_company(self.company)
        cs.due_reminder_enabled, cs.due_days_before = True, 3
        cs.save()
        next_month = (TODAY.replace(day=28) + datetime.timedelta(days=5)).replace(day=1)
        early = next_month - datetime.timedelta(days=10)
        self.assertEqual(service.campaigns_due_reminders(self.company, today=early), 0)  # مبكر
        day = next_month - datetime.timedelta(days=2)
        self.assertEqual(service.campaigns_due_reminders(self.company, today=day), 1)
        self.assertEqual(service.campaigns_due_reminders(self.company, today=day + datetime.timedelta(days=1)), 0)
        self.assertEqual(send.call_count, 1)
        self.assertIn("600", send.call_args[0][2])  # مجموع الكفالتين
        self.assertEqual(ReminderLog.objects.count(), 1)

    def test_overdue_reminder_and_disabled(self):
        send = self.mock_send()
        s = self.sponsor(self.sara)
        last_month = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
        SponsorshipDue.objects.create(sponsorship=s, period=last_month, amount=Decimal("300"))
        self.assertEqual(service.campaigns_due_reminders(self.company), 0)  # غير مفعّل
        cs = CampaignSettings.for_company(self.company)
        cs.overdue_reminder_enabled = True
        cs.save()
        OptOut.objects.create(phone="0551000002")
        self.assertEqual(service.campaigns_due_reminders(self.company), 0)  # موقوفة
        OptOut.objects.all().delete()
        self.assertEqual(service.campaigns_due_reminders(self.company), 1)
        self.assertEqual(service.campaigns_due_reminders(self.company), 0)
        self.assertEqual(send.call_count, 1)

    def test_no_provider_no_reminders(self):
        cs = CampaignSettings.for_company(self.company)
        cs.due_reminder_enabled, cs.due_days_before = True, 40
        cs.save()
        self.sponsor(self.ali)
        with mock.patch("messaging.service.send") as send:
            self.assertEqual(service.campaigns_due_reminders(self.company), 0)
        send.assert_not_called()


class PageTests(CampaignTestBase):
    def test_pages_and_demo(self):
        from campaigns.demo import seed_demo

        seed_demo(self.company)
        seed_demo(self.company)
        self.assertEqual(Segment.objects.count(), 3)
        self.assertEqual(Campaign.objects.count(), 2)
        sent = Campaign.objects.get(status=CampaignStatus.SENT)
        self.assertTrue(sent.recipients.filter(status="sent").exists())

        self.client.force_login(self.user)
        self.assertContains(self.client.get("/campaigns/"), "لوحة حملات التواصل")
        self.assertContains(self.client.get(f"/app/campaign/{sent.pk}/"), "إرسال تجريبي")
        self.assertContains(self.client.get(f"/app/campaign-segment/{Segment.objects.first().pk}/"), "معاينة")
        self.assertContains(self.client.get("/app/campaign/new/"), "data-sms-counter")
        self.assertEqual(self.client.get("/campaigns/settings/").status_code, 200)
        response = self.client.post("/campaigns/settings/", {"sms_optout_footer": "on", "due_days_before": 5,
                                                              "due_text": "تذكير {الاسم}", "overdue_text": "م"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CampaignSettings.for_company(self.company).due_days_before, 5)
        response = self.client.post(f"/campaigns/{sent.pk}/test/", {"target": "0551112222"})
        self.assertEqual(response.status_code, 302)  # بلا مزوّد: رسالة خطأ واضحة

        draft = Campaign.objects.get(status=CampaignStatus.DRAFT)
        response = self.client.post(f"/app/campaign/{draft.pk}/cancel_campaign/")
        draft.refresh_from_db()
        self.assertEqual(draft.status, CampaignStatus.CANCELLED)

    def test_other_company_cannot_open(self):
        with tenant_context(self.other):
            seg = Segment.objects.create(name="أخرى")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(f"/app/campaign-segment/{seg.pk}/").status_code, 404)

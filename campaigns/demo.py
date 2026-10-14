"""بيانات تجريبية لحملات التواصل (لا ترسل أي رسالة فعلية)."""
from django.utils import timezone

from campaigns.models import Campaign, CampaignRecipient, CampaignStatus, Channel, Segment, SegmentRule
from core.tenancy import tenant_context


def seed_demo(company):
    """ينشئ 3 شرائح وحملة مرسلة (بمستلمين مسجلين مباشرة) وحملة مسودة. آمن للتكرار."""
    with tenant_context(company):
        lapsed, _ = Segment.all_objects.get_or_create(
            company=company, name="داعمون منقطعون منذ 6 أشهر", defaults={"rule": SegmentRule.LAPSED, "months": 6})
        major, _ = Segment.all_objects.get_or_create(
            company=company, name="كبار الداعمين (5,000 ريال فأكثر)",
            defaults={"rule": SegmentRule.MAJOR, "min_amount": 5000})
        sponsors, _ = Segment.all_objects.get_or_create(
            company=company, name="الكافلون النشطون", defaults={"rule": SegmentRule.ACTIVE_SPONSORS})

        sent = Campaign.all_objects.filter(company=company, title="شكر الكافلين — رمضان").first()
        if sent is None:
            now = timezone.now()
            sent = Campaign.all_objects.create(
                company=company, title="شكر الكافلين — رمضان", channel=Channel.SMS, segment=sponsors,
                message="{الاسم}، شكراً لكفالتكم المستمرة، أجركم عند الله عظيم. {الجمعية}",
                status=CampaignStatus.SENT, sent_at=now - timezone.timedelta(days=3))
            rows = _demo_rows(sponsors)
            CampaignRecipient.all_objects.bulk_create([
                CampaignRecipient(company=company, campaign=sent, channel="sms", address=phone, name=name,
                                  source="الكافلون", status=status, error=error,
                                  attempted_at=now - timezone.timedelta(days=3))
                for name, phone, status, error in rows], ignore_conflicts=True)

        Campaign.all_objects.get_or_create(
            company=company, title="دعوة المنقطعين للعودة", defaults={
                "channel": Channel.SMS, "segment": lapsed,
                "message": "{الاسم}، اشتقنا لعطائكم! ساهموا معنا من جديد في مشاريع {الجمعية}: {الرابط}",
                "link": "https://example.org/donate/"})
    return {"segments": 3, "campaigns": 2}


def _demo_rows(segment):
    rows = [(r["name"], r["phone"], "sent", "") for r in segment.recipients() if r["phone"]][:5]
    if not rows:
        rows = [("أبو عبدالله", "966550000101", "sent", ""), ("أم محمد", "966550000102", "sent", ""),
                ("فاعل خير", "966550000103", "sent", "")]
    rows.append(("داعم برقم خاطئ", "966500000000", "failed", "رقم غير صحيح لدى المزوّد"))
    return rows

"""إرسال التنبيهات داخل النظام، وبالبريد الإلكتروني إن فُعّل خادم البريد من الإعدادات."""
from core.models import Membership, Notification, Role


def managers(company):
    return [m.user for m in Membership.objects.filter(company=company, is_active=True,
                                                      role__in=[Role.OWNER, Role.ADMIN]).select_related("user")]


def notify(company, users, title, url="", body=""):
    from core.mail import send

    users = [u for u in users if u is not None]
    Notification.all_objects.bulk_create([Notification(company=company, user=u, title=title, url=url) for u in users])
    send(company, title, body or title, [u.email for u in users], link=url)


def notify_managers(company, title, url="", body=""):
    notify(company, managers(company), title, url, body)

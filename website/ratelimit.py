"""حد لعدد الطلبات من الجهاز نفسه على نماذج الموقع العامة (طلب مساعدة، تواصل، تبرع، رمز الدخول)."""
from django.core.cache import cache

from core.utils import client_ip

LIMIT_MESSAGE = "أرسلت طلبات كثيرة خلال وقت قصير. حاول مرة أخرى بعد قليل."


def too_many(request, scope, limit, seconds=3600):
    """يسجل محاولة ويعيد True إذا تجاوز الجهاز حد هذا النموذج خلال المدة (مخزّن في الذاكرة المؤقتة المشتركة)."""
    site = getattr(request, "site", None)
    key = f"rl:{scope}:{getattr(site, 'pk', 0)}:{client_ip(request) or 'unknown'}"
    if cache.add(key, 1, seconds):
        return False
    try:
        count = cache.incr(key)
    except ValueError:  # انتهت صلاحية المفتاح بين add و incr
        cache.add(key, 1, seconds)
        return False
    return count > limit

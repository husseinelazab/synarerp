from core.registry import DocType, register
from website.models import (FAQ, Album, HeroSlide, ImpactCounter, NewsletterSubscriber, NewsPost, Partner,
                            PublishedDocument, Video)

# الصور والملفات تُرفع من صفحة المستند (القالب الإضافي) لأن النموذج العام لا يستقبل الملفات
register(DocType(
    slug="news-post", model=NewsPost, module="website",
    form_fields=["title", "category", "published_on", "summary", "body", "is_published"],
    list_fields=["title", "category", "published_on", "is_published"],
    search_fields=["title", "summary", "body"], detail_template="website/_news_detail.html",
    detail_exclude=["cover"],
))
register(DocType(
    slug="album", model=Album, module="website",
    form_fields=["title", "event_date", "description", "is_published"],
    list_fields=["title", "event_date", "is_published"],
    search_fields=["title", "description"], detail_template="website/_album_detail.html",
))
register(DocType(
    slug="published-document", model=PublishedDocument, module="website",
    form_fields=["title", "category", "year", "description", "is_published"],
    list_fields=["title", "category", "year", "file_name", "is_published"],
    search_fields=["title", "description"], detail_template="website/_document_detail.html",
    detail_exclude=["content", "content_type", "size"],
))
register(DocType(
    slug="partner", model=Partner, module="website",
    form_fields=["name", "url", "sort", "is_published"],
    list_fields=["name", "url", "sort", "is_published"],
    search_fields=["name"], detail_template="website/_partner_detail.html",
    detail_exclude=["logo"],
))
register(DocType(
    slug="hero-slide", model=HeroSlide, module="website",
    form_fields=["title", "subtitle", "button_text", "button_url", "sort", "is_published"],
    list_fields=["title", "button_text", "sort", "is_published"],
    search_fields=["title", "subtitle"], detail_template="website/_slide_detail.html",
    detail_exclude=["image"],
))
register(DocType(
    slug="impact-counter", model=ImpactCounter, module="website",
    form_fields=["label", "value", "suffix", "icon", "sort", "is_published"],
    list_fields=["label", "value", "suffix", "icon", "sort", "is_published"],
    search_fields=["label"],
))
register(DocType(
    slug="faq", model=FAQ, module="website",
    form_fields=["question", "answer", "category", "sort", "is_published"],
    list_fields=["question", "category", "sort", "is_published"],
    search_fields=["question", "answer", "category"],
))
register(DocType(
    slug="video", model=Video, module="website",
    form_fields=["title", "youtube_url", "description", "published_on", "is_published"],
    list_fields=["title", "published_on", "is_published"],
    search_fields=["title", "description"],
))
register(DocType(
    slug="newsletter-subscriber", model=NewsletterSubscriber, module="website",
    form_fields=["email", "is_active"],
    list_fields=["email", "created_at", "is_active"],
    search_fields=["email"],
))

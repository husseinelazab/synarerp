"""مسارات موقع الجمعية العام: تُخدم على نطاقها المستقل مباشرة، وعلى المنصة تحت /site/<المعرّف>/ للمعاينة."""
from django.urls import include, path

from website import public_views as views

urlpatterns = [
    path("", views.home, name="site_home"),
    path("about/", views.about, name="site_about"),
    path("governance/", views.governance, name="site_governance"),
    path("programs/", views.programs, name="site_programs"),
    path("news/", views.news, name="site_news"),
    path("news/<int:pk>/", views.news_detail, name="site_news_detail"),
    path("gallery/", views.gallery, name="site_gallery"),
    path("apply/", views.apply, name="site_apply"),
    path("contact/", views.contact, name="site_contact"),
    path("donate/", include("store.site_urls")),
]

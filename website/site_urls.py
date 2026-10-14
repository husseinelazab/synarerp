"""مسارات موقع الجمعية العام: تُخدم على نطاقها المستقل مباشرة، وعلى المنصة تحت /site/<المعرّف>/ للمعاينة."""
from django.urls import include, path

from website import public_views as views

urlpatterns = [
    path("", views.home, name="site_home"),
    path("about/", views.about, name="site_about"),
    path("governance/", views.governance, name="site_governance"),
    path("documents/<int:pk>/", views.document, name="site_document"),
    path("programs/", views.programs, name="site_programs"),
    path("news/", views.news, name="site_news"),
    path("news/<int:pk>/", views.news_detail, name="site_news_detail"),
    path("gallery/", views.gallery, name="site_gallery"),
    path("gallery/<int:pk>/", views.album, name="site_album"),
    path("apply/", views.apply, name="site_apply"),
    path("contact/", views.contact, name="site_contact"),
    path("donate/", include("store.site_urls")),
    path("robots.txt", views.robots, name="site_robots"),
    path("sitemap.xml", views.sitemap, name="site_sitemap"),
    # روابط التسجيل العامة للبرامج والفرص التطوعية والاستبيانات تعمل على نطاق الجمعية أيضاً
    path("t/verify/<str:code>/", views.program_verify, name="program_verify"),
    path("t/<str:token>/", views.program_register, name="program_register"),
    path("v/verify/<str:code>/", views.volunteer_verify, name="volunteer_verify"),
    path("v/<str:token>/", views.volunteer_register, name="volunteer_register"),
    path("s/<str:token>/", views.survey_respond, name="survey_respond"),
]

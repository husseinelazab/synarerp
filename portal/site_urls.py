"""مسارات بوابة المستفيد على موقع الجمعية (تحت /portal/)."""
from django.urls import path

from portal import site_views as views, appointments

urlpatterns = [
    path("programs/", views.portal_programs, name="portal_programs"),
    path("appointments/", appointments.appointments, name="portal_appointments"),
    path("", views.portal_login, name="portal_login"),
    path("home/", views.portal_home, name="portal_home"),
    path("aid/", views.portal_aid, name="portal_aid"),
    path("requests/", views.portal_requests, name="portal_requests"),
    path("profile/", views.portal_profile, name="portal_profile"),
    path("documents/", views.portal_documents, name="portal_documents"),
    path("logout/", views.portal_logout, name="portal_logout"),
]

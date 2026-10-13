from django.urls import path

from volunteering import views

urlpatterns = [
    path("", views.volunteering_dashboard, name="volunteering_dashboard"),
    path("certificate/<int:pk>/", views.certificate, name="volunteer_certificate"),
]

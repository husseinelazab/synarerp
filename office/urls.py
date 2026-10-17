from django.urls import path

from office import views, organization

urlpatterns = [
    path("organization/", organization.chart, name="organization_chart"),
    path("", views.my_desk, name="my_desk"),
    path("circular/<int:pk>/ack/", views.acknowledge_circular, name="acknowledge_circular"),
    path("documents/", views.documents_dashboard, name="documents_dashboard"),
    path("documents/correspondence/<int:pk>/route/", views.route_correspondence, name="route_correspondence"),
    path("documents/correspondence/<int:pk>/letter/", views.letter, name="correspondence_letter"),
]

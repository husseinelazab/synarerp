from django.urls import path

from website import views

urlpatterns = [
    path("", views.website_dashboard, name="website_dashboard"),
    path("settings/", views.website_settings, name="website_settings"),
    path("albums/<int:pk>/photos/", views.album_photos, name="website_album_photos"),
    path("albums/<int:pk>/photos/<int:photo_pk>/delete/", views.album_photo_delete, name="website_album_photo_delete"),
    path("upload/<slug:kind>/<int:pk>/", views.upload, name="website_upload"),
]

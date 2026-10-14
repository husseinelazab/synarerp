from django.urls import path

from campaigns import views

urlpatterns = [
    path("<str:token>/", views.unsubscribe, name="campaigns_unsubscribe"),
]

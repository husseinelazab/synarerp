from django.urls import path

from relations import views

urlpatterns = [
    path("<str:token>/", views.survey_respond, name="survey_respond"),
]

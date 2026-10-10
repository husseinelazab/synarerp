from django.urls import path

from saas import views

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("switch-company/<int:pk>/", views.switch_company, name="switch_company"),
    path("billing/", views.billing, name="billing"),
    path("settings/company/", views.company_settings, name="company_settings"),
    path("settings/users/", views.users_list, name="users_list"),
    path("settings/users/new/", views.user_create, name="user_create"),
    path("settings/users/<int:pk>/", views.user_edit, name="user_edit"),
    path("platform/", views.platform_dashboard, name="platform_dashboard"),
    path("platform/companies/<int:pk>/", views.platform_company, name="platform_company"),
]

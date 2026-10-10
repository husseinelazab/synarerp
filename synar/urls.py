from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import password_reset

admin.site.site_header = "سِينار Synar ERP - لوحة الإدارة"
admin.site.site_title = "سِينار"
admin.site.index_title = "إدارة النظام"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password-reset/", password_reset.ResetView.as_view(), name="password_reset"),
    path("password-reset/sent/", password_reset.sent, name="password_reset_done"),
    path("password-reset/<uidb64>/<token>/", password_reset.confirm, name="password_reset_confirm"),
    path("password-reset/complete/", password_reset.complete, name="password_reset_complete"),
    path("account/password/", auth_views.PasswordChangeView.as_view(
        template_name="registration/password_change.html", success_url="/account/password/done/"),
        name="password_change"),
    path("account/password/done/", auth_views.PasswordChangeDoneView.as_view(
        template_name="registration/password_change_done.html"), name="password_change_done"),
    path("reports/accounts/", include("accounts.urls")),
    path("reports/stock/", include("stock.urls")),
    path("pos/", include("pos.urls")),
    path("einvoice/", include("einvoice.urls")),
    path("hr/", include("hr.urls")),
    path("me/", include("hr.portal_urls")),
    path("", include("saas.urls")),
    path("", include("core.urls")),
]

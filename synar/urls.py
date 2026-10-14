from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path, re_path

from core import password_reset, two_factor
from core import views as core_views
from hr import adms

admin.site.site_header = "سِينار Synar ERP - لوحة الإدارة"
admin.site.site_title = "سِينار"
admin.site.index_title = "إدارة النظام"

urlpatterns = [
    path("healthz/", core_views.healthz, name="healthz"),
    path("api/v1/", include("core.api_urls")),
    # أجهزة البصمة (ZKTeco ADMS) — عناوين ثابتة يفرضها برنامج الجهاز
    re_path(r"^iclock/cdata(?:\.aspx)?/?$", adms.cdata),
    re_path(r"^iclock/getrequest(?:\.aspx)?/?$", adms.getrequest),
    re_path(r"^iclock/devicecmd(?:\.aspx)?/?$", adms.devicecmd),
    path("admin/", admin.site.urls),
    path("login/", two_factor.LoginView.as_view(), name="login"),
    path("login/verify/", two_factor.verify_login, name="login_verify"),
    path("account/two-factor/", two_factor.two_factor_setup, name="two_factor_setup"),
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
    path("stock/", include("stock.tool_urls")),
    path("manufacturing/", include("manufacturing.urls")),
    path("", include("messaging.urls")),
    path("pos/", include("pos.urls")),
    path("einvoice/", include("einvoice.urls")),
    path("hr/", include("hr.urls")),
    path("selling/", include("selling.urls")),
    path("crm/", include("crm.urls")),
    path("projects/", include("projects.urls")),
    path("support/", include("support.urls")),
    path("assets/", include("assets.urls")),
    path("beneficiaries/", include("beneficiaries.urls")),
    path("funds/", include("funds.urls")),
    path("resources/", include("funds.resource_urls")),
    path("governance/", include("governance.urls")),
    path("awqaf/", include("awqaf.urls")),
    path("volunteering/", include("volunteering.urls")),
    path("v/", include("volunteering.public_urls")),
    path("programs/", include("programs.urls")),
    path("t/", include("programs.public_urls")),
    path("excellence/", include("excellence.urls")),
    path("relations/", include("relations.urls")),
    path("s/", include("relations.public_urls")),
    path("fleet/", include("fleet.urls")),
    path("office/", include("office.urls")),
    path("me/", include("hr.portal_urls")),
    path("", include("buying.urls")),
    path("", include("saas.urls")),
    path("", include("core.urls")),
]

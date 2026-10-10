from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = "سِينار Synar ERP - لوحة الإدارة"
admin.site.site_title = "سِينار"
admin.site.index_title = "إدارة النظام"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("reports/accounts/", include("accounts.urls")),
    path("reports/stock/", include("stock.urls")),
    path("pos/", include("pos.urls")),
    path("einvoice/", include("einvoice.urls")),
    path("", include("saas.urls")),
    path("", include("core.urls")),
]

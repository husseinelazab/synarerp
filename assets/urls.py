from django.urls import path

from assets import views

urlpatterns = [
    path("depreciation/", views.depreciation, name="asset_depreciation"),
    path("register/", views.asset_register, name="asset_register"),
    path("<int:pk>/dispose/", views.dispose_asset, name="asset_dispose"),
]

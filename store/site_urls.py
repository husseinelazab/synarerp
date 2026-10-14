from django.urls import path

from store import site_views as views

urlpatterns = [
    path("", views.store_home, name="store_home"),
    path("<int:pk>/", views.store_opportunity, name="store_opportunity"),
    path("cart/", views.store_cart, name="store_cart"),
    path("checkout/", views.store_checkout, name="store_checkout"),
    path("pay/<str:number>/", views.store_pay, name="store_pay"),
    path("callback/", views.store_callback, name="store_callback"),
    path("thanks/<str:number>/", views.store_thanks, name="store_thanks"),
    path("certificate/<str:number>/", views.store_certificate, name="store_certificate"),
    path("account/", views.store_account, name="store_account"),
]

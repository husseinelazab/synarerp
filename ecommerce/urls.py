from django.urls import path

from ecommerce import views

urlpatterns = [
    path("integrations/<str:platform>/<str:key>/", views.webhook, name="ecommerce_webhook"),
    path("ecommerce/orders/", views.orders, name="ecommerce_orders"),
    path("ecommerce/orders/<int:pk>/", views.order_detail, name="ecommerce_order"),
    path("ecommerce/stores/<int:pk>/test/", views.test_payload, name="ecommerce_test"),
]

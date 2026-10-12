from django.urls import path

from selling import commission

urlpatterns = [
    path("sales-by-person/", commission.sales_by_person, name="sales_by_person"),
]

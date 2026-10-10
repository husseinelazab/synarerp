from django.urls import path

from stock import tools

urlpatterns = [
    path("labels/", tools.labels, name="stock_labels"),
    path("labels/generate-barcodes/", tools.generate_barcodes, name="stock_generate_barcodes"),
    path("price-checker/", tools.price_checker, name="stock_price_checker"),
    path("count/<int:pk>/scan/", tools.count_scan, name="stock_count_scan"),
    path("count/<int:pk>/scan/api/", tools.count_scan_api, name="stock_count_scan_api"),
    path("count/<int:pk>/scan/apply/", tools.count_apply, name="stock_count_apply"),
    path("api/items/", tools.items_api, name="stock_items_api"),
]

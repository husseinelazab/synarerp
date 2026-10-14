from django.urls import path

from funds import package_views, reports

urlpatterns = [
    path("balances/", reports.fund_balances, name="fund_balances"),
    path("activities/", reports.statement_of_activities, name="statement_of_activities"),
    path("statements/", package_views.statements_package, name="statements_package"),
    path("statements/export/", package_views.statements_export, name="statements_export"),
]

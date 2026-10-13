from django.urls import path

from funds import reports

urlpatterns = [
    path("balances/", reports.fund_balances, name="fund_balances"),
    path("activities/", reports.statement_of_activities, name="statement_of_activities"),
]

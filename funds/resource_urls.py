from django.urls import path

from funds import resources

urlpatterns = [
    path("", resources.resources_dashboard, name="resources_dashboard"),
    path("generate-dues/", resources.generate_month_dues, name="generate_month_dues"),
    path("donor/<int:pk>/statement/", resources.donor_statement, name="donor_statement"),
]

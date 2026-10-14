from core.registry import DocType, register
from store.forms import MarketerForm, OpportunityForm
from store.models import DonationOpportunity, DonationOrder, Marketer, RecurringDonation

register(DocType(
    slug="donation-opportunity", model=DonationOpportunity, module="store", form_class=OpportunityForm,
    list_fields=["title", "kind", "fund", "target_amount", "fixed_amount", "is_published", "is_featured", "end_date"],
    search_fields=["title", "description"], detail_template="store/_opportunity_detail.html",
))
register(DocType(
    slug="donation-order", model=DonationOrder, module="store",
    list_fields=["number", "created_at", "donor_name", "donor_phone", "total", "status", "payment_method", "marketer"],
    form_fields=["donor_name", "donor_phone", "donor_email"],
    search_fields=["number", "donor_name", "donor_phone", "payment_id", "gift_to_name"],
    detail_template="store/_order_detail.html", detail_exclude=["gateway_response"],
))
register(DocType(
    slug="marketer", model=Marketer, module="store", form_class=MarketerForm,
    list_fields=["name", "code", "phone", "is_active"], search_fields=["name", "code", "phone"],
    detail_template="store/_marketer_detail.html",
))
register(DocType(
    slug="recurring-donation", model=RecurringDonation, module="store",
    list_fields=["donor_name", "donor_phone", "opportunity", "amount", "next_charge_on", "active", "failures"],
    form_fields=["donor_name", "donor_phone", "amount", "next_charge_on"],
    search_fields=["donor_name", "donor_phone", "opportunity__title"],
    detail_template="store/_recurring_detail.html",
))

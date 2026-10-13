from core.registry import DocType, register
from funds.models import DonationReceipt

register(DocType(
    slug="donation-receipt", model=DonationReceipt, module="funds",
    list_fields=["name", "posting_date", "donor_name", "donation_type", "fund", "amount", "payment_method"],
    form_fields=["posting_date", "donor_name", "donor_phone", "donation_type", "fund", "amount", "payment_method",
                 "account", "reference_no", "income_account", "remarks"],
    search_fields=["name", "donor_name", "donor_phone", "reference_no"],
))

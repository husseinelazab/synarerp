"""Regression coverage for cashier, counting, association and plan workflows."""
import datetime
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import GLEntry
from core.models import Company, Membership, PrintSettings, Role
from core.provisioning import provision_company
from core.tenancy import tenant_context
from office.models import Correspondence
from pos.models import POSProfile, POSShift
from pos.services import checkout, pos_return
from programs.models import NewMuslim, QuranCircle, QuranStudent, UmrahParticipant, UmrahTrip, HealthPatient
from saas.forms import SignupForm
from saas.models import Plan
from saas.services import ensure_default_plans
from selling.models import Customer, SalesInvoice
from stock.models import Item, ItemGroup, ItemUOM, StockCount, UOM, Warehouse


class WorkflowFixTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_default_plans()
        cls.owner = User.objects.create_user('workflow-owner', password='pass12345')
        cls.company = provision_company('شركة اختبارات المسارات', owner=cls.owner)
        cls.other = provision_company('جمعية الدعوة للاختبارات', organization_type='nonprofit')
        cls.other.association_specialty = 'dawah'
        cls.other.save(update_fields=['association_specialty'])
        Membership.objects.create(user=cls.owner, company=cls.other, role=Role.OWNER)
        with tenant_context(cls.company):
            cls.item = Item.objects.create(code='SERVICE', name='خدمة الاختبارات', item_group=ItemGroup.objects.first(),
                                           uom=UOM.objects.first(), selling_rate=115, is_stock_item=False)
            cls.customer = Customer.objects.create(name='عميل مسجل')
            cls.profile = POSProfile.objects.get()
        with tenant_context(cls.other):
            cls.foreign = Customer.objects.create(name='عميل جمعية أخرى')

    def setUp(self):
        self.context = tenant_context(self.company)
        self.context.__enter__()
        self.addCleanup(self.context.__exit__, None, None, None)
        self.client.force_login(self.owner)
        session = self.client.session
        session['company_id'] = self.company.pk
        session.save()

    def sale(self, **kwargs):
        shift = POSShift.current_for(self.owner) or POSShift.open(self.profile, self.owner, 0)
        return checkout(shift, self.owner, [{'item': self.item.pk, 'qty': 1}], **kwargs)

    def test_credit_posts_receivable_without_cash(self):
        invoice = self.sale(customer_id=self.customer.pk, credit_sale=True)
        self.assertEqual(invoice.outstanding_amount, Decimal('115'))
        self.assertEqual(invoice.cash_received, 0)
        entries = GLEntry.objects.filter(voucher_type='sales-invoice', voucher_id=invoice.pk)
        self.assertEqual(sum(e.debit - e.credit for e in entries), 0)
        self.assertEqual(sum(e.debit - e.credit for e in entries.filter(customer=self.customer)), Decimal('115'))
        self.assertEqual(invoice.pos_shift.summary()['expected_cash'], 0)
        self.assertContains(self.client.get(f'/pos/receipt/{invoice.pk}/'), 'آجل')

    def test_partial_credit_preserves_balance(self):
        invoice = self.sale(customer_id=self.customer.pk, credit_sale=True, paid_cash=40)
        self.assertEqual(invoice.outstanding_amount, Decimal('75'))
        self.assertEqual(invoice.pos_shift.summary()['expected_cash'], Decimal('40'))

    def test_default_customer_cannot_buy_on_credit(self):
        for customer_id in (None, self.profile.default_customer_id):
            with self.assertRaises(ValidationError):
                self.sale(customer_id=customer_id, credit_sale=True)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_credit_hold_and_limit_are_enforced(self):
        self.customer.credit_limit = 100
        self.customer.save()
        with self.assertRaises(ValidationError):
            self.sale(customer_id=self.customer.pk, credit_sale=True)
        self.customer.credit_hold = True
        self.customer.save()
        with self.assertRaises(ValidationError):
            self.sale(customer_id=self.customer.pk, credit_sale=True, paid_cash=100)
        self.assertFalse(SalesInvoice.objects.exists())

    def test_foreign_customer_rejected_even_without_tenant_manager(self):
        with tenant_context(None), self.assertRaises(Customer.DoesNotExist):
            self.sale(customer_id=self.foreign.pk, credit_sale=True)

    def test_credit_requires_online_validation(self):
        with self.assertRaises(ValidationError):
            self.sale(customer_id=self.customer.pk, credit_sale=True, offline_id='offline-credit')

    def test_credit_return_settles_debt_before_refunding_cash(self):
        invoice = self.sale(customer_id=self.customer.pk, credit_sale=True, paid_cash=40)
        ret = pos_return(invoice.pos_shift, self.owner, invoice.name, [{'item': self.item.pk, 'qty': 1}])
        invoice.refresh_from_db()
        self.assertEqual(ret.paid_cash, Decimal('-40'))
        self.assertEqual(ret.applied_to_original, Decimal('75'))
        self.assertEqual(ret.outstanding_amount, 0)
        self.assertEqual(invoice.outstanding_amount, 0)
        self.assertEqual(invoice.pos_shift.summary()['expected_cash'], 0)
        ret.cancel()
        invoice.refresh_from_db()
        self.assertEqual(invoice.outstanding_amount, Decimal('75'))

    def make_item(self, code, barcode='', **kwargs):
        return Item.objects.create(code=code, name=code, item_group=self.item.item_group, uom=self.item.uom,
                                   barcode=barcode, **kwargs)

    def test_duplicate_and_upc_equivalent_barcodes_rejected(self):
        self.make_item('A', '0123456789012')
        for barcode in ('0123456789012', '123456789012', ' 0123456789012 '):
            with self.assertRaises(ValidationError):
                self.make_item('B', barcode)
        self.make_item('BLANK1')
        self.make_item('BLANK2')

    def test_database_rejects_bulk_duplicate_barcodes(self):
        self.make_item('A', 'unique-barcode')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.bulk_create([Item(company=self.company, code='B', name='B', item_group=self.item.item_group,
                                           uom=self.item.uom, barcode='unique-barcode')])

    def test_unit_barcode_cannot_collide_with_item(self):
        self.make_item('A', '0123456789012')
        carton = UOM.objects.create(name='صندوق الاختبار')
        with self.assertRaises(ValidationError):
            ItemUOM.objects.create(item=self.item, uom=carton, factor=12, barcode='123456789012')

    def test_same_barcode_is_allowed_in_another_association(self):
        self.make_item('A', 'shared-retail-code')
        with tenant_context(self.other):
            item = Item.objects.create(code='A', name='A', barcode='shared-retail-code',
                                       item_group=ItemGroup.objects.first(), uom=UOM.objects.first())
        self.assertEqual(item.company_id, self.other.pk)

    def test_count_manual_set_uses_selected_id_not_barcode_lookup(self):
        stock = self.make_item('COUNT', 'count-code')
        # Another barcode equals this item's code: a manual edit must still use its ID.
        wrong = self.make_item('OTHER', 'COUNT')
        count = StockCount.objects.create(warehouse=Warehouse.objects.first())
        count.record_scan(stock, 2, self.owner)
        url = f'/stock/count/{count.pk}/scan/api/'
        response = self.client.post(url, json.dumps({'action': 'set', 'item': stock.pk, 'qty': 5}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(count.scanned_totals()[0]['qty'], Decimal('5'))
        self.assertFalse(count.scans.filter(item=wrong).exists())
        self.assertEqual(self.client.post(url, json.dumps({'action': 'scan', 'code': 'count-code', 'qty': -1}), content_type='application/json').status_code, 400)

    def test_retried_scanner_request_is_not_counted_twice(self):
        stock = self.make_item('SCAN', 'scan-idempotent')
        count = StockCount.objects.create(warehouse=Warehouse.objects.first())
        data = {'code': 'scan-idempotent', 'qty': 2, 'request_id': 'same-reading'}
        url = f'/stock/count/{count.pk}/scan/api/'
        for _ in range(2):
            response = self.client.post(url, json.dumps(data), content_type='application/json')
            self.assertEqual(response.status_code, 200)
        self.assertEqual(count.scans.count(), 1)
        self.assertEqual(count.scanned_totals()[0]['qty'], Decimal('2'))
        carton = UOM.objects.create(name='صندوق المسح')
        ItemUOM.objects.create(item=stock, uom=carton, factor=12, barcode='carton-scan')
        data = {'code': 'carton-scan', 'qty': 2, 'request_id': 'carton-reading'}
        for _ in range(2):
            response = self.client.post(url, json.dumps(data), content_type='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['qty'], 24)
        self.assertEqual(count.scanned_totals()[0]['qty'], Decimal('26'))

    def test_bulk_selection_scoped_and_atomic(self):
        a, b = self.make_item('A'), self.make_item('B')
        response = self.client.post('/app/item/bulk/', {'selected': [a.pk, b.pk], 'action': 'disable'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Item.objects.filter(disabled=True).count(), 2)
        response = self.client.post('/app/item/bulk/', {'selected': [a.pk, 999999], 'action': 'enable'})
        a.refresh_from_db()
        self.assertTrue(a.disabled)

    def test_read_only_member_cannot_bulk_modify(self):
        user = User.objects.create_user('workflow-reader')
        Membership.objects.create(user=user, company=self.company, role=Role.USER, read_only_doctypes=['item'])
        self.client.force_login(user)
        self.client.post('/app/item/bulk/', {'selected': [self.item.pk], 'action': 'disable'})
        self.item.refresh_from_db()
        self.assertFalse(self.item.disabled)

    def test_archived_plan_does_not_reappear_after_seeding(self):
        self.owner.is_superuser = True
        self.owner.save()
        plan = Plan.objects.get(code='basic')
        self.client.post(f'/platform/plans/{plan.pk}/archive/')
        ensure_default_plans()
        plan.refresh_from_db()
        self.assertFalse(plan.is_active)
        self.assertNotContains(self.client.get('/platform/plans/'), plan.name)
        self.client.logout()
        self.assertNotContains(self.client.get('/'), f'?plan={plan.code}"')

    def test_signup_and_upgraded_plan_categories(self):
        from django.apps import apps
        from django.db import connection
        from importlib import import_module
        Plan.objects.get(code='nonprofit-enterprise').delete()
        import_module('saas.migrations.0005_classify_existing_association_plans').classify(apps, connection.schema_editor())
        self.assertEqual(Plan.objects.get(code='nonprofit-enterprise').organization_type, 'nonprofit')
        form = SignupForm({'organization_type': 'nonprofit', 'plan': Plan.objects.get(code='pro').pk})
        self.assertFalse(form.is_valid())
        self.assertIn('plan', form.errors)

    def trip(self, capacity=2):
        return UmrahTrip.objects.create(title='عمرة المسلمين الجدد', departure_date=timezone.localdate(),
            return_date=timezone.localdate() + datetime.timedelta(days=2), supervisor='المشرف',
            departure_point='مقر الجمعية', capacity=capacity, frequency='monthly')

    def use_association(self):
        session = self.client.session
        session['company_id'] = self.other.pk
        session.save()

    def test_umrah_roster_capacity_uniqueness_and_printing(self):
        self.use_association()
        with tenant_context(self.other):
            trip = self.trip(capacity=1)
            person = NewMuslim.objects.create(full_name='المشارك الأول', language='الأردية')
            UmrahParticipant.objects.create(trip=trip, participant=person)
            second = NewMuslim.objects.create(full_name='المشارك الثاني', language='العربية')
            with self.assertRaises(ValidationError):
                UmrahParticipant.objects.create(trip=trip, participant=second)
            with self.assertRaises(ValidationError):
                UmrahParticipant.objects.create(trip=trip, participant=person)
            self.assertEqual(trip.seats_remaining, 0)
        self.assertContains(self.client.get(f'/programs/umrah/{trip.pk}/manifest/'), person.full_name)
        self.assertContains(self.client.get(f'/app/umrah-trip/{trip.pk}/'), 'طباعة كشف الرحلة')
        self.assertContains(self.client.get('/'), 'الدعوة والمسلمون الجدد والعمرة')

    def test_specialty_and_company_isolation(self):
        with self.assertRaises(ValidationError):
            self.trip()
        self.assertEqual(self.client.get('/app/umrah-trip/').status_code, 302)
        self.assertEqual(self.client.get('/module/dawah/').status_code, 302)
        self.use_association()
        self.assertEqual(self.client.get('/app/health-patient/').status_code, 302)
        self.assertEqual(self.client.get('/app/customer/').status_code, 302)
        with tenant_context(self.other):
            with self.assertRaises(ValidationError):
                HealthPatient.objects.create(file_number='X', full_name='مريض')

    def test_specialty_forms_create_and_show_validation(self):
        self.use_association()
        response = self.client.post('/app/new-muslim/new/', {'full_name': 'مشارك من النموذج', 'language': 'الإنجليزية'})
        self.assertEqual(response.status_code, 302)
        with tenant_context(self.other):
            self.assertTrue(NewMuslim.objects.filter(full_name='مشارك من النموذج').exists())
            self.other.association_specialty = 'quran'
            self.other.save()
            circle = QuranCircle.objects.create(name='حلقة الاختبار', teacher='المعلم', capacity=1)
            QuranStudent.objects.create(full_name='طالب أول', circle=circle)
            with self.assertRaises(ValidationError):
                QuranStudent.objects.create(full_name='طالب ثان', circle=circle)

    def test_official_letter_has_barcode_and_association_letterhead(self):
        self.use_association()
        with tenant_context(self.other):
            PrintSettings.objects.create(company=self.other, letterhead='data:image/png;base64,dGVzdA==')
            letter = Correspondence.objects.create(direction='outgoing', subject='خطاب رسمي', body='نص الخطاب')
        response = self.client.get(f'/office/documents/correspondence/{letter.pk}/letter/')
        self.assertContains(response, 'رقم الصادر')
        self.assertContains(response, '<svg')
        self.assertContains(response, 'كليشة')

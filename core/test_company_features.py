from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from beneficiaries.tests import make_nonprofit
from core.models import Membership, Role
from core.scoped_auth import create_company_user
from core.tenancy import tenant_context
from hr.models import AssociationRole, OrganizationUnit
from saas.tests import make_company


class CompanyLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a, cls.owner = make_company("جهة دخول أ", "code_owner")
        cls.b, cls.other = make_nonprofit("جهة دخول ب", "code_other")
        cls.ua = create_company_user(cls.a,"cashier","a@example.com","a-secret-123")
        cls.ub = create_company_user(cls.b,"cashier","b@example.com","b-secret-123")
        Membership.objects.create(company=cls.a,user=cls.ua,login_name="cashier",role=Role.USER)
        Membership.objects.create(company=cls.b,user=cls.ub,login_name="cashier",role=Role.USER)

    def test_same_name_requires_matching_company_and_password(self):
        self.assertNotEqual(self.ua.pk,self.ub.pk)
        for code, password, expected in [(self.a.login_code,"a-secret-123",self.a), (self.b.login_code.lower(),"b-secret-123",self.b)]:
            self.client.logout()
            response = self.client.post("/login/",{"company_code":code,"username":"CaShIeR","password":password})
            self.assertEqual(response.status_code,302)
            self.assertEqual(self.client.session["company_id"],expected.pk)
        self.client.logout()
        for code,password in [(self.a.login_code,"b-secret-123"),("invalid","a-secret-123"),("","a-secret-123")]:
            self.assertEqual(self.client.post("/login/",{"company_code":code,"username":"cashier","password":password}).status_code,200)
            self.assertNotIn("_auth_user_id",self.client.session)

    def test_duplicate_within_same_company_and_disabled_membership(self):
        with self.assertRaises(ValidationError):
            create_company_user(self.a,"CASHIER","","another-pass")
        Membership.objects.filter(company=self.a,user=self.ua).update(is_active=False)
        self.client.post("/login/",{"company_code":self.a.login_code,"username":"cashier","password":"a-secret-123"})
        self.assertNotIn("_auth_user_id",self.client.session)

    def test_company_selection_survives_account_switch_and_two_factor(self):
        from core import totp
        from core.models import UserTwoFactor
        Membership.objects.create(company=self.a,user=self.ub,login_name="shared_member",role=Role.USER)
        self.client.force_login(self.owner)
        self.client.post("/login/",{"company_code":self.b.login_code,"username":"cashier","password":"b-secret-123"})
        self.assertEqual(self.client.session["company_id"],self.b.pk)
        self.client.logout()
        secret=totp.generate_secret()
        UserTwoFactor.objects.create(user=self.ub,enabled=True,secret_encrypted=totp.encrypt_secret(secret))
        self.client.force_login(self.owner)
        response=self.client.post("/login/",{"company_code":self.b.login_code,"username":"cashier","password":"b-secret-123"})
        self.assertRedirects(response,reverse("login_verify"),fetch_redirect_response=False)
        self.client.post(reverse("login_verify"),{"code":totp.code_at(secret,totp.current_step())})
        self.assertEqual(self.client.session["company_id"],self.b.pk)
        self.assertEqual(int(self.client.session["_auth_user_id"]),self.ub.pk)

    def test_two_factor_lock_does_not_cross_company_aliases(self):
        from django.core.cache import cache
        from core.two_factor import _code_key, MAX_FAILURES
        key=_code_key(self.ua.username)
        cache.set(key,MAX_FAILURES*2,60)
        self.addCleanup(cache.delete,key)
        response=self.client.post("/login/",{"company_code":self.b.login_code,"username":"cashier","password":"b-secret-123"})
        self.assertEqual(response.status_code,302)
        self.client.logout()
        self.client.post("/login/",{"company_code":self.a.login_code,"username":"cashier","password":"a-secret-123"})
        self.assertNotIn("_auth_user_id",self.client.session)

    def test_legacy_owner_login_still_works(self):
        self.assertEqual(self.client.post("/login/",{"username":self.owner.username,"password":"pass12345"}).status_code,302)

    def test_member_create_accepts_name_used_by_other_company(self):
        self.client.force_login(self.owner)
        response = self.client.post("/settings/users/new/",{"full_name":"مستخدم جديد","username":"code_other","email":"", "password":"New-Strong-Pass-918", "role":"user"})
        self.assertEqual(response.status_code,302,response.content)
        self.assertTrue(Membership.objects.filter(company=self.a,login_key="code_other").exists())


class OrganizationAndLabelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.a, cls.owner = make_nonprofit("جمعية الهيكل أ", "structure_owner")
        cls.b, cls.other = make_nonprofit("جمعية الهيكل ب", "structure_other")

    def test_hierarchy_cycles_and_company_isolation(self):
        with tenant_context(self.a):
            board = OrganizationUnit.objects.create(name="المجلس",kind="board")
            unit = OrganizationUnit.objects.create(name="التنفيذية",kind="executive",parent=board)
            board.parent = unit
            with self.assertRaises(ValidationError):
                board.save()
        with tenant_context(self.b):
            with self.assertRaises((ValidationError,OrganizationUnit.DoesNotExist)):
                OrganizationUnit.objects.create(name="ربط خاطئ",kind="committee",parent=unit)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get("/office/organization/"),"التنفيذية")
        self.client.force_login(self.other)
        self.assertEqual(self.client.get("/office/organization/").context["count"],0)

    def test_group_permissions_restrict_member_and_cross_company_assignment(self):
        with tenant_context(self.a):
            policy = AssociationRole.objects.create(name="باحث",modules=["beneficiaries"],can_submit=False,can_delete=False,denied_doctypes=["aid-disbursement"])
            user = User.objects.create_user("policy_user",password="pass")
            member = Membership.objects.create(company=self.a,user=user,role=Role.USER,role_policy=policy)
            self.assertTrue(member.allows_module("beneficiaries"))
            self.assertFalse(member.allows_module("accounts"))
            self.assertFalse(member.may_submit)
            self.assertFalse(member.may("delete"))
            self.assertEqual(member.doctype_access("aid-disbursement"),"none")
            other_member = Membership.objects.get(company=self.b,user=self.other)
            other_member.role_policy=policy
            with self.assertRaises(ValidationError):
                other_member.save()
        self.client.force_login(user)
        self.assertEqual(self.client.post("/app/association-role/new/",{"name":"ترقية"}).status_code,302)

    def test_renames_do_not_affect_other_company_and_only_managers_can_write(self):
        self.client.force_login(self.owner)
        response = self.client.post("/settings/modules/",{"beneficiaries":"خدمات الأسر"})
        self.assertEqual(response.status_code,302)
        self.assertContains(self.client.get("/module/beneficiaries/"),"خدمات الأسر")
        self.b.refresh_from_db()
        self.assertEqual(self.b.module_labels,{})

class DependentCategoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from beneficiaries.models import Beneficiary, DependentCategory, FamilyMember
        cls.company, cls.owner = make_nonprofit("جمعية التصنيفات", "dependent_owner")
        cls.other, _ = make_nonprofit("جمعية أخرى للتصنيفات", "dependent_other")
        with tenant_context(cls.company):
            head = Beneficiary.objects.create(full_name="رب الأسرة",national_id="1234567890",phone="0500000000")
            cls.member = FamilyMember.objects.create(parent=head,full_name="تابع",relation="son",gender="male")
            cls.student = DependentCategory.objects.get(name="طالب")
            cls.orphan = DependentCategory.objects.get(name="يتيم")
        with tenant_context(cls.other):
            cls.foreign = DependentCategory.objects.first()

    def test_multiple_categories_and_cross_company_guard(self):
        from django.db import transaction
        with tenant_context(self.company):
            self.member.categories.add(self.student,self.orphan)
            self.assertEqual(self.member.categories.count(),2)
            with self.assertRaises(ValidationError), transaction.atomic():
                self.member.categories.add(self.foreign)
            with self.assertRaises(ValidationError), transaction.atomic():
                self.foreign.members.add(self.member)
            self.assertEqual(self.member.categories.count(),2)

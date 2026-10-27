"""Admin limits reject malformed data before any key or allowlist mutation."""
import unittest
from pydantic import ValidationError
from api.access import normalize_networks
from api.dashboard_validation import (
    KeyCreate, KeyUpdate, AllowlistUpdate, AdminLoginRequest,
    admin_validation_error,
)

class DashboardValidationTests(unittest.TestCase):
    def test_key_fields_require_bounded_clean_values(self):
        valid=KeyCreate(label=' Client ',allowed_ips=['203.0.113.1','2001:db8::1/128'],
                        expires_days=30,max_concurrent=2,requests_per_minute=60)
        self.assertEqual(valid.label,'Client')
        self.assertEqual(len(valid.allowed_ips),2)
        cases=[{'label':'   '},{'label':'x'*81},{'expires_days':0},
               {'expires_days':3651},{'max_concurrent':1.5},{'requests_per_minute':601},
               {'allowed_ips':['203.0.113.1']*101},{'allowed_ips':['bad']},
               {'allowed_ips':['1'*65]},{'unknown':'unexpected'},
               {'priority':0},{'priority':4},{'priority':1.5},{'priority':True},{'priority':'1'}]
        for change in cases:
            with self.subTest(change=change),self.assertRaises(ValidationError):
                KeyCreate.model_validate({'label':'Client',**change})
        with self.assertRaises(ValidationError):
            KeyUpdate.model_validate({'max_concurrent':1,'requests_per_minute':60})
        with self.assertRaises(ValidationError):
            AllowlistUpdate.model_validate({'allowed_ips':['bad']})
        with self.assertRaises(ValueError):
            normalize_networks(['127.0.0.1']*101)

    def test_priority_defaults_and_update_validation(self):
        self.assertEqual(KeyCreate(label='Client').priority, 2)
        fields = dict(allowed_ips=[], max_concurrent=2, requests_per_minute=60)
        self.assertIsNone(KeyUpdate(**fields).priority)
        for priority in (1, 2, 3):
            self.assertEqual(KeyUpdate(**fields, priority=priority).priority, priority)
        for priority in (0, 4, True, 'high'):
            with self.assertRaises(ValidationError):
                KeyUpdate(**fields, priority=priority)

    def test_login_rejects_non_ascii_without_echoing_password(self):
        with self.assertRaises(ValidationError):
            AdminLoginRequest(username='अकाउंट',password='valid-password')

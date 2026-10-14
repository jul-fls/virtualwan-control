import unittest
from unittest.mock import Mock
from bs4 import BeautifulSoup
from virtualwan.adapters.pfsense import PfSense,csrf_token,fields

TOKEN='sid:abcdef012345,1791980000;ip:123abc,1791980000'
PAGE='<html><head><script>var csrfMagicToken = "'+TOKEN+'";var csrfMagicName = "__csrf_magic";</script></head><body><a href="system_routes.php?act=del&id=0" usepost>Delete</a></body></html>'

class CsrfTests(unittest.TestCase):
    def parse(self,html):return csrf_token(BeautifulSoup(html,'html.parser'),'system_routes.php')
    def test_list_page_token_without_post_form(self):self.assertEqual(self.parse(PAGE),TOKEN)
    def test_hidden_field_still_supported(self):self.assertEqual(self.parse('<input name="__csrf_magic" value="'+TOKEN+'">'),TOKEN)
    def test_external_script_and_body_text_not_tokens(self):
        for html in ['<script src="csrf.js">var csrfMagicToken="'+TOKEN+'";</script>','<p>var csrfMagicToken="'+TOKEN+'";</p>']:
            with self.assertRaises(ValueError):self.parse(html)
    def test_javascript_expression_not_evaluated(self):
        with self.assertRaises(ValueError):self.parse('<script>var csrfMagicToken = makeToken();</script>')
    def test_login_page_does_not_allow_modification(self):
        html=PAGE+'<input name="usernamefld"><input name="passwordfld">'
        with self.assertRaisesRegex(ValueError,'Session pfSense'):self.parse(html)
    def test_native_delete_posts_current_token_and_exact_id(self):
        pf=PfSense.__new__(PfSense);pf.request=Mock(side_effect=[Mock(text=PAGE),Mock(text='<html>ok</html>')])
        pf.native_post('system_routes.php',{'act':'del','id':'2'})
        pf.request.assert_called_with('POST','system_routes.php',data={'act':'del','id':'2','__csrf_magic':TOKEN})
    def test_missing_token_blocks_post(self):
        pf=PfSense.__new__(PfSense);pf.request=Mock(return_value=Mock(text='<html>No token</html>'))
        with self.assertRaises(ValueError):pf.native_post('system_routes.php',{'act':'del','id':'2'})
        self.assertEqual(pf.request.call_count,1)
    def test_apply_uses_inline_token(self):
        pf=PfSense.__new__(PfSense);pf.request=Mock(side_effect=[Mock(text=PAGE),Mock(text='<html>ok</html>')])
        pf.apply('system_routes.php')
        pf.request.assert_called_with('POST','system_routes.php',data={'apply':'Apply Changes','__csrf_magic':TOKEN})
    def test_csrf_rejection_not_reported_as_success(self):
        pf=PfSense.__new__(PfSense);pf.request=Mock(side_effect=[Mock(text=PAGE),Mock(text='<h1>CSRF check failed</h1>')])
        with self.assertRaises(ValueError):pf.native_post('system_routes.php',{'act':'del','id':'2'})
    def test_form_fields_use_shared_token_parser(self):
        html=PAGE+'<form method="post"><input name="apply" value="Apply"></form>'
        self.assertEqual(fields(html,['apply'])['__csrf_magic'],TOKEN)

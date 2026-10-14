import unittest,json
from unittest.mock import Mock
from urllib.parse import quote
from virtualwan.catalogue import purchase_prices,enrich,is_ip_offer,describe,configure_draft,pricing_valid
from virtualwan.providers import OvhAPI
from test_control import Tests as Fixture

SETUP={'capacities':['installation'],'duration':'P0D','pricingMode':'default','price':{'currencyCode':'EUR','value':0}}
MONTH={'capacities':['renew'],'duration':'P1M','pricingMode':'default','price':{'currencyCode':'EUR','value':8.49}}
class PriceTests(unittest.TestCase):
    def test_monthly_cost_not_free_installation(self):
        self.assertEqual(purchase_prices({'prices':[SETUP,MONTH]}),[MONTH])
    def test_setup_not_selectable_as_zero_day_rental(self):
        with self.assertRaises(ValueError):pricing_valid({'planCode':'vps','prices':[SETUP,MONTH]},SETUP)
    def test_month_duration_valid_for_order(self):pricing_valid({'planCode':'vps','prices':[SETUP,MONTH]},MONTH)
    def test_paid_initial_and_renewal_keep_renewal_when_same_mode(self):
        self.assertEqual(purchase_prices({'prices':[{**SETUP,'duration':'P1M'},MONTH]}),[MONTH])
    def test_real_zero_monthly_price_is_preserved(self):
        free={**MONTH,'price':{'currencyCode':'EUR','value':0}};self.assertEqual(purchase_prices({'prices':[SETUP,free]}),[free])
    def test_legacy_demo_prices_remain_supported(self):
        p={**MONTH};p.pop('capacities');self.assertEqual(purchase_prices({'pricings':[p]}),[p])
    def test_enriched_ui_prices_are_separate_from_setup(self):
        o=enrich([{'planCode':'vps','prices':[SETUP,MONTH]}],{})[0]
        self.assertEqual(o['purchase_prices'],[MONTH]);self.assertEqual(o['setup_prices'],[SETUP])
    def test_single_ipv4_only(self):
        for code in ['ip-failover-ripe','ip-failover-arin']:self.assertTrue(is_ip_offer({'planCode':code}))
        for code in ['ip-v4-s24-ripe','ip-v4-s30-ripe','ip-v6-s56-ripe','option-additional-disk-50g']:self.assertFalse(is_ip_offer({'planCode':code}))

class CatalogueProviderTests(unittest.TestCase):
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    def api(self):
        api=OvhAPI.__new__(OvhAPI);api.store=self.store;api.public_catalogue=Mock(return_value={'plans':[{'planCode':'vps','invoiceName':'VPS'}]});return api
    def test_vps_main_selector_excludes_addons(self):
        api=self.api();api.call=Mock(side_effect=[{'cartId':'c'},[{'planCode':'disk','prices':[SETUP,MONTH]},{'planCode':'vps','prices':[SETUP,MONTH]}]])
        rows=api.catalogue(self.store.document('profiles',self.profile),'router');self.assertEqual([r['planCode'] for r in rows],['vps'])
        self.assertEqual(rows[0]['purchase_prices'][0]['price']['value'],8.49)
    def test_ip_catalogue_uses_dedicated_ip_endpoint(self):
        api=self.api();api.call=Mock(side_effect=[{'cartId':'c'},[{'planCode':'ip-failover-ripe','prices':[MONTH]},{'planCode':'ip-v4-s24-ripe','prices':[MONTH]}]])
        rows=api.catalogue(self.store.document('profiles',self.profile),'additional_ip')
        self.assertEqual([r['planCode'] for r in rows],['ip-failover-ripe'])
        self.assertEqual(api.call.call_args.args,('GET','/order/cart/c/ip'))
        api.public_catalogue.assert_not_called()
    def ip_draft(self):
        api=self.api();api.catalogue=Mock(return_value=[{'planCode':'ip-failover-ripe','prices':[MONTH]}])
        api.call=Mock(side_effect=[{'cartId':'c'},None,{'itemId':1},[{'label':'country','required':True,'allowedValues':['FR','DE'],'fields':None},{'label':'destination','required':False,'allowedValues':None,'fields':None}]])
        profile=self.store.document('profiles',self.profile);body={'kind':'additional_ip','planCode':'ip-failover-ripe','duration':'P1M','pricingMode':'default','quantity':1}
        result=describe(api,profile,body);body.update(draft_id=result['draft_id'],item_configurations={'1':{'country':'FR'}})
        return api,profile,body,result
    def test_country_values_and_locked_destination(self):
        api,p,b,r=self.ip_draft();self.assertEqual(r['fields'][0]['values'],['FR','DE'])
        self.assertTrue(r['fields'][1]['locked']);self.assertEqual(r['fields'][1]['value'],p['config']['ovh']['expected_service'])
        self.assertEqual(api.call.call_args_list[2].args[1],'/order/cart/c/ip')
    def test_destination_cannot_be_overridden_by_browser(self):
        api,p,b,r=self.ip_draft();b['item_configurations']['1']['destination']='other-vps'
        with self.assertRaises(ValueError):configure_draft(api,p,b)
    def test_destination_is_posted_automatically_without_checkout(self):
        api,p,b,r=self.ip_draft();api.call=Mock(side_effect=[{}, {}, {'prices':{'withTax':{'currencyCode':'EUR','value':2.39}}}])
        configure_draft(api,p,b)
        self.assertEqual(api.call.call_args_list[1].args[2],{'label':'destination','value':p['config']['ovh']['expected_service']})
        self.assertEqual(api.call.call_args_list[-1].args,('GET','/order/cart/c/checkout'))

del Fixture

const assert=require('node:assert/strict');
const {basePrice,hardware,selectOffers}=require('../virtualwan/static/catalogue-view.js');
function offer(id,ram,cpu,storage,value,duration='P1M'){return {planCode:id,technical:{memory:{size:ram},cpu:{cores:cpu},storage:{disks:[{capacity:storage,technology:'SSD'}]}},prices:[{duration:'P0D',pricingMode:'default',capacities:['installation'],price:{value:0,currencyCode:'EUR'}},{duration,pricingMode:'default',capacities:['renew'],price:{value,currencyCode:'EUR'}}]};}
const small=offer('small',2,1,20,3),medium=offer('medium',8,4,80,72,'P1Y'),large=offer('large',16,8,160,12),unknown={planCode:'unknown'};
assert.equal(basePrice(small).value,3);assert.equal(basePrice(medium).value,6);assert.equal(basePrice(medium).months,12);
assert.equal(basePrice(offer('free',1,1,20,0)).value,0);assert.equal(basePrice(unknown),null);
assert.equal(basePrice(offer('daily',1,1,20,1,'P1D')),null);
const discount=offer('discount',2,1,20,8);discount.prices.push({duration:'P1M',pricingMode:'commitment12',capacities:['renew'],price:{value:5,currencyCode:'EUR'}});
assert.equal(basePrice(discount).value,8); // base defaults are not silently replaced by a commitment discount
assert.deepEqual(selectOffers([large,unknown,medium,small]).map(o=>o.planCode),['small','medium','large','unknown']);
assert.deepEqual(selectOffers([small,unknown,large,medium],{sort:'price',order:'desc'}).map(o=>o.planCode),['large','medium','small','unknown']);
assert.deepEqual(selectOffers([small,large,medium],{sort:'ram',order:'desc'}).map(o=>o.planCode),['large','medium','small']);
assert.deepEqual(selectOffers([small,large,medium],{ram:8,cpu:4,storage:80}).map(o=>o.planCode),['medium']);
const raid=offer('raid',8,4,80,10);raid.technical.storage.disks[0].number=2;assert.equal(hardware(raid).storage,160);
assert.equal(hardware(unknown).storage,null);
console.log('Catalogue view: monthly prices, unknown prices, ordering and combined filters pass.');

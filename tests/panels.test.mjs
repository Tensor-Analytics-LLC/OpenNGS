import test from 'node:test';
import assert from 'node:assert/strict';
import {panelPreferences,panelLayout} from '../frontend/panels.mjs';

test('Untrusted saved panel state cannot create invalid or off-screen sizes',()=>{
 const p=panelPreferences({left:{size:Infinity,collapsed:'false'},right:{size:9000,collapsed:true},bottom:{size:-100}},1280);
 assert.deepEqual(p.left,{size:274,collapsed:false});
 assert.deepEqual(p.right,{size:500,collapsed:true});
 assert.deepEqual(p.bottom,{size:120,collapsed:false});
 assert.deepEqual(panelPreferences(null,390).right,{size:240,collapsed:false});
});

test('All eight independent minimisation combinations release the corresponding space',()=>{
 for(let mask=0;mask<8;mask++){
  const p=panelPreferences({left:{size:350,collapsed:!!(mask&1)},right:{size:450,collapsed:!!(mask&2)},bottom:{size:320,collapsed:!!(mask&4)}});
  const layout=panelLayout(p,1440,900);
  assert.equal(layout.left,mask&1?34:350);
  assert.equal(layout.right,mask&2?34:450);
  assert.equal(layout.bottom,mask&4?37:320);
  assert.equal(p.left.size,350);
  assert.equal(p.right.size,450);
  assert.equal(p.bottom.size,320);
 }
});

test('Temporary narrow and short windows preserve preferences for restoration',()=>{
 const p=panelPreferences({left:{size:440},right:{size:500},bottom:{size:450}});
 const before=structuredClone(p);
 const narrow=panelLayout(p,651,400);
 assert.ok(651-narrow.left-narrow.right>=180);
 assert.equal(narrow.bottom,131);
 assert.deepEqual(p,before);
 const wide=panelLayout(p,1600,1000);
 assert.equal(wide.left,440);
 assert.equal(wide.right,500);
 assert.equal(wide.bottom,450);
});

test('Mobile and short-window layouts keep an accessible execution header',()=>{
 const p=panelPreferences();
 assert.equal(panelLayout(p,650,600).mobile,true);
 assert.equal(panelLayout(p,651,600).mobile,false);
 assert.equal(panelLayout(p,390,250).bottom,37);
 p.bottom.collapsed=true;
 assert.equal(panelLayout(p,390,900).bottom,37);
});

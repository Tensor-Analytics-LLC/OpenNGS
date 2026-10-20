import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,randomBytes} from 'node:crypto';
import {SHA256} from '../frontend/sha256.mjs';
test('Streaming digest matches independent native SHA-256 across padding and chunk boundaries',()=>{for(const n of [0,1,55,56,63,64,65,127,128,129,1024*1024]){const bytes=randomBytes(n),hash=new SHA256();for(let i=0;i<n;i+=137)hash.update(bytes.subarray(i,i+137));assert.equal(hash.hex(),createHash('sha256').update(bytes).digest('hex'));}});

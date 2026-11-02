import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {createRequestId} from '../static/request-id.js';

test('LAN HTTP sends can create valid unique IDs without randomUUID', () => {
  const lanCrypto = {getRandomValues: array => webcrypto.getRandomValues(array)};
  const ids = new Set(Array.from({length:1000}, () => createRequestId(lanCrypto)));
  assert.equal(ids.size, 1000);
  for (const id of ids) assert.match(id, /^presence-[a-f0-9]{32}$/);
});

test('secure-context IDs remain supported; missing randomness is an explicit error', () => {
  assert.match(createRequestId(webcrypto), /^presence-[a-f0-9-]{36}$/);
  assert.throws(() => createRequestId({}), /请求编号/);
});

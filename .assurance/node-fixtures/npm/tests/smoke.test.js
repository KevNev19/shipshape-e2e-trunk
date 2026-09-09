const test = require('node:test');
const assert = require('node:assert/strict');
const { double } = require('local-library');
test('workspace dependency resolves and behaves correctly', () => {
  assert.equal(double(4), 8);
  assert.equal(double(-3), -6);
});

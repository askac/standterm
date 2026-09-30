'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
require('../static/js/standterm-font-recommendations.js');
const recommendations = globalThis.StandTermFontRecommendations;

test('capacity and aspect choose opposite sides of the observed tradeoff', () => {
    const results = [
        { index: 0, metrics: { capacity: 15176, aspect: 17 / 7 } },
        { index: 1, metrics: { capacity: 13983, aspect: 2 } },
        { index: 2, metrics: null }
    ];
    assert.deepEqual(recommendations.rank(results, 'capacity').map(item => item.index), [0, 1]);
    assert.deepEqual(recommendations.rank(results, 'aspect').map(item => item.index), [1, 0]);
    assert.deepEqual(results.map(item => item.index), [0, 1, 2]);
});

test('equal fallback metrics preserve the current font before other candidates', () => {
    const results = [1, 0, 2].map(index => ({ index, metrics: { capacity: 1000, aspect: 2 } }));
    for (const goal of ['capacity', 'aspect']) {
        assert.deepEqual(recommendations.rank(results, goal).map(item => item.index), [0, 1, 2]);
    }
    assert.equal(recommendations.candidates('"Custom", monospace', false)[0], '"Custom", monospace');
    const defaults = recommendations.candidates('Consolas, "Cascadia Mono", "Courier New", monospace', false);
    assert.equal(defaults.length, 3);
});

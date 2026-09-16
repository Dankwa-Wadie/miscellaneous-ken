const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Run the actual UI and its registered timer without a network or pipeline job.
let now = 1_800_000_000_000;
const elements = Object.fromEntries(
  ['next-run-countdown', 'next-run-time', 'automation-next-run'].map(id => [id, {textContent: ''}])
);
const timers = [];
const context = vm.createContext({
  Date: class extends Date { static now() { return now; } },
  document: {
    querySelector: () => ({addEventListener() {}}),
    getElementById: id => elements[id],
    addEventListener() {},
  },
  fetch: () => new Promise(() => {}),
  setInterval: (fn, delay) => timers.push({fn, delay}),
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../studio/app.js'), 'utf8'), context);
vm.runInContext(`state = {
  automation: {enabled: true, next_run: Date.now()/1000 + 3602},
  busy: true, online: true, free_disk_bytes: 1024*1024*1024
}`, context);
const tick = timers.find(t => t.delay === 1000).fn;
tick();
assert.equal(elements['next-run-countdown'].textContent, '1h 00m 02s');
now += 1000;
tick();
assert.equal(elements['next-run-countdown'].textContent, '1h 00m 01s');
assert.match(elements['automation-next-run'].textContent, /^1h 00m 01s/);
now += 3601000;
tick();
assert.equal(elements['next-run-countdown'].textContent, 'After current job');
vm.runInContext('state.busy = false; state.online = false', context);
tick();
assert.equal(elements['next-run-countdown'].textContent, 'Waiting for internet');
vm.runInContext('state.online = true; state.free_disk_bytes = 0', context);
tick();
assert.equal(elements['next-run-countdown'].textContent, 'Waiting for disk space');
vm.runInContext('state.free_disk_bytes = 1024*1024*1024', context);
tick();
assert.equal(elements['next-run-countdown'].textContent, 'Due now');
vm.runInContext('state.automation.enabled = false', context);
tick();
assert.equal(elements['next-run-countdown'].textContent, 'Paused');
console.log('Countdown timer and waiting states passed.');

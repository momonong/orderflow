import assert from "node:assert/strict";
import {createActivityLock} from "../web/integration-activity.mjs";

const attributes = new Map();
const workspace = {inert: false,
  setAttribute: (name, value) => attributes.set(name, value),
  removeAttribute: name => attributes.delete(name)};
const activity = createActivityLock(workspace);
let release;
const delayed = new Promise(resolve => {release = resolve;});
let requests = 0;
const first = activity.run(async () => {requests++; await delayed;});
assert.equal(activity.busy, true);
assert.equal(workspace.inert, true);
assert.equal(attributes.get("aria-busy"), "true");
assert.equal(await activity.run(async () => {requests++;}), false);
assert.equal(requests, 1);
release();
assert.equal(await first, true);
assert.equal(activity.busy, false);
assert.equal(workspace.inert, false);
assert.equal(attributes.has("aria-busy"), false);

await assert.rejects(activity.run(async () => {throw new Error("synthetic failure");}),
  /synthetic failure/);
assert.equal(activity.busy, false);
assert.equal(workspace.inert, false);
console.log("integration pending-operation lock and release: ok");

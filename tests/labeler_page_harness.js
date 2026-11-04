// Runs the labelling page's own script in Node against a minimal fake DOM, for tests/test_superagent_labeler.py.
//   node labeler_page_harness.js <page.html> '<json: {"storage": "memory"|"throw", "saved": <object|null>, "actions": [...]}>'
// actions: "key:<k>" presses a key, "click:<element id>" clicks an element, "button:<n>" clicks the n-th choice button.
// Prints {steps: [state after each action], downloads: [{name, text}], saved: <last value written to storage>}.
"use strict";
const fs = require("fs");

const html = fs.readFileSync(process.argv[2], "utf8");
const opts = JSON.parse(process.argv[3] || "{}");
const dataText = /<script id="data"[^>]*>([\s\S]*?)<\/script>/.exec(html)[1];
const code = /<script>([\s\S]*?)<\/script>\s*<\/body>/.exec(html)[1];

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.className = ""; this.hidden = false;
                     this.style = {}; this._text = ""; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  click() { if (this.listeners.click) { this.listeners.click({}); } }
  remove() {}
}

const els = {};
const document = {
  getElementById(id) {
    if (id === "data") { return { textContent: dataText }; }
    return els[id] || (els[id] = new El("div"));
  },
  createElement(tag) { return new El(tag); },
  body: new El("body"),
  _listeners: {},
  addEventListener(type, fn) { this._listeners[type] = fn; },
  querySelectorAll() { return []; },
};
let lastWritten = null;
let lastKey = null;
const store = { value: opts.saved === undefined ? null : JSON.stringify(opts.saved) };
const localStorage = {
  getItem(k) { lastKey = k; if (opts.storage === "throw") { throw new Error("no storage"); } return store.value; },
  setItem(k, v) { lastKey = k; if (opts.storage === "throw") { throw new Error("no storage"); } store.value = v; lastWritten = JSON.parse(v); },
};
const blobs = new Map();
const downloads = [];
const URLStub = {
  createObjectURL(b) { const u = "blob:" + blobs.size; blobs.set(u, b); return u; },
  revokeObjectURL() {},
};
const timers = [];
new Function("document", "localStorage", "URL", "Blob", "setTimeout", code)(
  document, localStorage, URLStub, Blob, (fn) => timers.push(fn));

function state() {
  const g = (id) => document.getElementById(id);
  const buttons = g("buttons").children.map((b) => ({ text: b.textContent, on: b.className === "on" }));
  return { meta: g("meta").textContent, text: g("text").textContent, cut: g("cut").textContent, cutHidden: g("cut").hidden,
           progress: g("progress").textContent, done: g("done").textContent, saved: g("saved").textContent, buttons };
}

(async () => {
  const steps = [state()];
  for (const action of opts.actions || []) {
    const [kind, arg] = [action.slice(0, action.indexOf(":")), action.slice(action.indexOf(":") + 1)];
    if (kind === "key") {
      document._listeners.keydown({ key: arg, ctrlKey: false, metaKey: false, altKey: false, preventDefault() {} });
    } else if (kind === "click") {
      document.getElementById(arg).click();
    } else if (kind === "button") {
      document.getElementById("buttons").children[Number(arg)].click();
    }
    steps.push(state());
  }
  // anchors are created on the fly: collect what download() wrote by replaying document.body.children
  for (const a of document.body.children) {
    if (a.download) { downloads.push({ name: a.download, blob: blobs.get(a.href) }); }
  }
  const out = [];
  for (const d of downloads) { out.push({ name: d.name, text: await d.blob.text() }); }
  process.stdout.write(JSON.stringify({ steps, downloads: out, saved: lastWritten, key: lastKey }));
})();

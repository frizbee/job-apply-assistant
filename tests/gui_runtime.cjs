// Run the actual embedded UI script against a small DOM/API fixture, no packages.
const fs = require("node:fs");
const vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const markup = input.page.split("<script>")[0];
const script = input.page.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map(), translated = [], requests = [], timers = new Map();

function element(attrs) {
  const get = name => attrs.match(new RegExp(`\\b${name}="([^"]*)"`))?.[1];
  const classes = new Set();
  return { id: get("id"), dataset: { l: get("data-l"), t: get("data-t") },
    value: "", textContent: "", innerHTML: "", checked: false, disabled: false, style: {},
    classList: { toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
      add(name) { classes.add(name); }, remove(name) { classes.delete(name); }, contains(name) { return classes.has(name); } },
    scrollIntoView() {}, focus() {}, click() { return this.onclick?.(); } };
}
for (const match of markup.matchAll(/<[a-z][\w-]*\b([^>]*)>/gi)) {
  const el = element(match[1]);
  if (el.id) elements.set(el.id, el);
  if (el.dataset.t) translated.push(el);
}
const buttons = [...markup.match(/<div class="lang">([\s\S]*?)<\/div>/)[1].matchAll(/<button\b([^>]*)>/g)]
  .map(match => { const el = element(match[1]); return elements.get(el.id) || el; });
const body = { innerHTML: "" };
const document = {
  body,
  getElementById(id) { return body.innerHTML ? null : elements.get(id); },
  querySelectorAll(selector) {
    if (selector === "[data-t]") return translated;
    if (selector === ".lang button") return buttons;
    if (selector === ".lang button[data-l]") return buttons.filter(b => b.dataset.l !== undefined);
    return [];
  },
};
let stored = input.saved ?? null, logResolve, logReject;
const localStorage = {
  getItem() { if (input.blockStorage) throw Error("Storage disabled"); return stored; },
  setItem(key, value) { if (input.blockStorage) throw Error("Storage disabled"); stored = value; },
};
const state = { settings: {}, profile: null, cv_name: "", has_key: false,
  default_locations: ["Worldwide"], running: input.running || false, profile_error: input.profileError || "" };
const context = vm.createContext({ document, localStorage, navigator: { language: input.navigator || "en-US" },
  async fetch(path, options) {
    requests.push({ path, ...options });
    if (path.startsWith("/api/log")) return { json: () => new Promise((resolve, reject) => { logResolve = resolve; logReject = reject; }) };
    return { json: async () => path === "/api/state" ? state : {} };
  },
  setInterval(callback) { const id = timers.size + 1; timers.set(id, callback); return id; },
  clearInterval(id) { timers.delete(id); },
});

async function main() {
  await vm.runInContext(script, context);
  const read = code => vm.runInContext(code, context);
  const en = buttons.find(b => b.dataset.l === "en"), tr = buttons.find(b => b.dataset.l === "tr");
  const quit = elements.get("quit");
  const before = read("lang");
  const switched = [];
  if (input.scenario === "switch" || input.scenario === "blocked-switch") {
    for (const button of [tr, en]) {
      button.click();
      switched.push({ lang: read("lang"), stored, quitText: quit.textContent, active: button.classList.contains("on"),
        english: elements.get("english").value });
    }
  }
  if (input.scenario === "invalid-button") {
    en.dataset.l = "constructor";
    en.click();
  }
  if (input.scenario?.startsWith("quit")) {
    let pending;
    if (input.scenario.startsWith("quit-polling")) {
      pending = [...timers.values()][0]();
      await Promise.resolve();
    }
    await quit.click();
    if (pending) {
      if (input.scenario.endsWith("-error")) logReject(Error("Server exited"));
      else logResolve({ lines: ["late log"], running: false });
      await pending;
    }
  }
  process.stdout.write(JSON.stringify({ before, lang: read("lang"), stored, requests, switched,
    quitText: quit.textContent, quitActive: quit.classList.contains("on"), body: body.innerHTML,
    timers: timers.size, english: elements.get("english").value, message: elements.get("m1").textContent }));
}
main().catch(error => { console.error(error); process.exitCode = 1; });

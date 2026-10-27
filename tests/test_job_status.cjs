// Run with: node tests/test_job_status.cjs
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const banners = Array.from({ length: 2 }, () => {
  const parts = Object.fromEntries([".job-title", ".job-count", ".job-detail"].map((key) => [key, { textContent: "" }]));
  parts.progress = { value: undefined, removeAttribute() { this.value = undefined; } };
  const classes = new Set();
  return { hidden: true, classes, classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); } }, querySelector: (key) => parts[key] };
});
const buttons = new Map();
let timer, toasts = [];
const context = vm.createContext({
  state: { library: { job: {} } }, jobConnectionError: "", jobTimer: undefined, jobDismissed: false,
  sessionStorage: { setItem() {} },
  document: { title: "", querySelectorAll: (selector) => selector === ".job-status" ? banners : [] },
  $: (selector) => { if (!buttons.has(selector)) buttons.set(selector, {}); return buttons.get(selector); },
  setTimeout: (callback) => { timer = callback; }, clearTimeout() {},
  toast: (message) => toasts.push(message), reload: async () => {},
});
vm.runInContext(source.slice(source.indexOf("function renderJob()"), source.indexOf("function renderReview()")), context);
function render(job) { context.state.library.job = { running: false, done: 0, total: 0, errors: [], message: "", ...job }; context.renderJob(); }

async function main() {
  render({}); assert(banners.every((banner) => banner.hidden));
  render({ running: true, message: "classify", done: 3, total: 10 });
  for (const banner of banners) {
    assert(!banner.hidden && banner.classes.has("running"));
    assert.equal(banner.querySelector("progress").value, 0.3);
    assert.match(banner.querySelector(".job-count").textContent, /3 \/ 10개 · 30%/);
  }
  assert.match(context.document.title, /자료 분류 진행 중/);
  assert(buttons.get("#restore-button").disabled);
  context.setJobDismissed(true);
  assert(banners.every((banner) => banner.hidden));
  render({ running: true, message: 'classify', done: 4, total: 10 });
  assert(banners.every((banner) => banner.hidden));
  assert(buttons.get('#restore-button').disabled);
  render({ message: 'classify', done: 10, total: 10 });
  assert(banners.every((banner) => banner.hidden));
  context.setJobDismissed(false);
  assert(banners.every((banner) => !banner.hidden));
  render({ running: true, message: "model", total: 1 });
  assert.equal(banners[0].querySelector("progress").value, undefined);
  assert(!banners[0].querySelector(".job-count").textContent.includes("%"));
  render({ message: "classify", done: 10, total: 10 });
  assert(!banners[0].hidden && !banners[0].classes.has("running"));
  assert.match(banners[0].querySelector(".job-title").textContent, /완료/);
  assert(!buttons.get("#restore-button").disabled);
  render({ message: "text", done: 1, total: 1, errors: [{ message: "OCR 실패" }] });
  assert(banners[0].classes.has("failed"));
  assert.match(banners[0].querySelector(".job-detail").textContent, /OCR 실패/);
  render({ message: "classify" });
  assert.match(banners[0].querySelector(".job-detail").textContent, /분석할 자료가 없습니다/);

  render({ running: true, message: "text", done: 1, total: 2 });
  context.api = async () => { throw new Error("연결 끊김"); };
  context.watchJob(); await timer();
  assert.match(banners[0].querySelector(".job-title").textContent, /확인 중단/);
  assert(!banners[0].classes.has("running"));
  assert.equal(banners[0].querySelector("progress").value, undefined);
  await timer(); assert.equal(toasts.length, 1);
  context.api = async () => ({ json: async () => ({ running: false, message: "text", done: 2, total: 2, errors: [] }) });
  await timer();
  assert.equal(context.jobConnectionError, "");
  assert.match(banners[0].querySelector(".job-title").textContent, /완료/);
  assert.equal(context.document.title, "RefArchive · 나의 자료함");
  context.modeSaveFailures = new Set();
  context.modeEditors = new Map();
  let finishSave;
  context.pendingModeSave = new Promise((resolve) => { finishSave = resolve; });
  let dispatched = false;
  context.post = async () => { dispatched = true; return {}; };
  buttons.set("#ocr-language", { value: "ko-KR" });
  const request = context.startAnalysis("classify", ["test"]);
  await Promise.resolve(); assert(!dispatched);
  finishSave(); await request; assert(dispatched);
  context.modeSaveFailures.add("숲"); dispatched = false;
  await assert.rejects(context.startAnalysis("classify", ["test"]), /분류 설정 저장에 실패/);
  assert(!dispatched); context.modeSaveFailures.clear();
  let flushed = false;
  context.state.library.tags = [{ name: "숲", mode: "suggest", threshold: .5 }];
  context.modeEditors.set("숲", { mode: { value: "auto", dataset: {} }, threshold: { value: "0.8", checkValidity: () => true }, save: async () => { flushed = true; context.state.library.tags[0] = { name: "숲", mode: "auto", threshold: .8 }; } });
  await context.waitForModeSettings(); assert(flushed);
  context.modeEditors.clear();
  buttons.set("#classify-scope", { value: "filtered" });
  buttons.set("#classify-hours", { value: "2.5", checkValidity: () => true });
  buttons.set("#classify-tag", { value: "숲" });
  buttons.set("#classify-category", { value: "참고" });
  buttons.set("#classify-type", { value: "document" });
  context.filteredItems = () => [{ id: "search-result" }];
  assert.deepEqual(JSON.parse(JSON.stringify(context.classificationScope())), { mode: "ids", ids: ["search-result"], tag: "숲", category: "참고", type: "document", hours: 2.5 });
  buttons.get("#classify-hours").checkValidity = () => false;
  assert.throws(() => context.classificationScope(), /최근 등록 시간/);
  console.log("분석 상태: 대기·진행·모델 준비·완료·실패·빈 작업·연결 끊김·복구 검증 통과");
  console.log("분류 실행: 설정 저장 대기·저장 실패 시 중단·검색 및 시간 조건 검증 통과");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });

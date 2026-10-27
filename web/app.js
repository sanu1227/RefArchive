"use strict";

const $ = (selector) => document.querySelector(selector);
const token = $('meta[name="refarchive-token"]').content;
const state = { library: null, view: "all", category: null, tags: new Set(), selected: new Set(), files: [], busy: false, collection: "", savedSearch: "" };
let toastTimer;
let jobTimer;
let jobConnectionError = "";
let jobDismissed = false;
try { jobDismissed = sessionStorage.getItem('refarchive-hide-analysis') === 'true'; } catch {}
let uploadPreviews = [];
let uploadBundleId = '';
let detailFileId = '';
let analysisItemId = "";
let pendingModeSave = Promise.resolve();
const modeSaveFailures = new Set();
const modeEditors = new Map();
$("#detail-form").append($("#analysis-template").content.cloneNode(true));
$("#settings-dialog").append($("#runtime-template").content.cloneNode(true));
$('#settings-dialog').append($('#maintenance-template').content.cloneNode(true));
$('#backup-button').textContent = '백업 ZIP 만들기';
const backupBlock = $('#backup-button').closest('.settings-block'), backupPanel = $('#backup-settings').closest('.settings-block');
$('#backup-settings').after($('#backup-button'), $('#metadata-button'));
backupBlock.replaceWith(backupPanel);
$('#settings-dialog').append($('#prepare-model').closest('.settings-block'));
const bundleEditor = node('div', 'bundle-editor'); bundleEditor.hidden = true; $('#bundle-files').after(bundleEditor);
const pdfOption = document.createElement("option"); pdfOption.value = "pdf"; pdfOption.textContent = "PDF"; $("#type-filter").append(pdfOption);
for (const id of ["#add-images", "#empty-upload"]) $(id).textContent = "+ 자료 추가";
$("#upload-dialog h2").textContent = "자료 추가";
$("#empty-state small").textContent = "이미지 · PDF · 문서 최대 50 MB / MP4 · WebM 최대 200 MB";
$(".workspace-footer span").textContent = "로컬 저장 · 선택형 자동 분석";
$("#settings-dialog .info-note").textContent = "자료와 분석 결과는 이 PC에 보관됩니다. 모델 준비와 링크 가져오기는 인터넷 연결이 필요합니다. 클라우드 동기화는 제공하지 않습니다.";
$("#link-dialog .dialog-description").textContent = "주소와 메모를 저장합니다. 공개 페이지의 이미지는 별도로 선택해 가져올 수 있습니다.";
const importButton = document.createElement("button"); importButton.type = "button"; importButton.id = "inspect-link"; importButton.className = "button secondary"; importButton.textContent = "링크 이미지 찾기"; $("#link-form .dialog-actions").prepend(importButton);
for (const [id, text] of [["auto-summaries", "새 자료 자동 요약"], ["auto-ocr", "새 이미지 · 스캔 PDF 자동 OCR"]]) {
  const label = node("label"), input = node("input"); input.type = "checkbox"; input.id = id; label.append(input, document.createTextNode(` ${text}`)); $("#download-enabled").closest("label").before(label);
}
const languageLabel = node("label", "", "자동 OCR 언어"), languageSelect = node("select"); languageSelect.id = "auto-ocr-language";
for (const [value, text] of [["ko-KR", "한국어"], ["en-US", "영어 · 언어팩 필요"]]) { const option = node("option", "", text); option.value = value; languageSelect.append(option); }
languageLabel.append(languageSelect); $("#download-enabled").closest("label").before(languageLabel);
$("#classify-all").before($("#classification-template").content.cloneNode(true));
$("#classify-all").textContent = "이 범위로 다시 분류";
const detailTrash = node("button", "button danger", "휴지통으로 이동"); detailTrash.type = "button"; detailTrash.id = "detail-trash";
$("#detail-form .close-button").before(detailTrash);
const trashNote = node("p", "dialog-description", "휴지통에 보관 중입니다. 아래 복원 버튼으로 자료함에 돌려놓을 수 있습니다."); trashNote.id = "detail-trash-note"; trashNote.hidden = true;
$("#detail-meta").after(trashNote);
for (const dialog of document.querySelectorAll("#detail-dialog, #review-dialog, #settings-dialog")) {
  const status = $("#job-status").cloneNode(true); status.removeAttribute("id");
  dialog.querySelector(".dialog-heading").after(status);
  const toggle = node('button', 'job-toggle', '분석 상태'); toggle.type = 'button'; toggle.hidden = true;
  dialog.querySelector('.dialog-heading .close-button').before(toggle);
}

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}

function toast(message, error = false) {
  clearTimeout(toastTimer);
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = false;
  const dialog = [...document.querySelectorAll("dialog[open]")].at(-1);
  if (dialog) {
    let feedback = dialog.querySelector(".dialog-feedback");
    if (!feedback) {
      feedback = node("p", "dialog-feedback");
      dialog.querySelector(".dialog-heading").after(feedback);
    }
    feedback.textContent = message;
    feedback.classList.toggle("error", error);
    feedback.setAttribute("role", error ? "alert" : "status");
  }
  toastTimer = setTimeout(() => { element.hidden = true; }, error ? 9000 : 5000);
}

function ask(message, initialValue = null) {
  const dialog = $("#choice-dialog"), input = $("#choice-input");
  const editing = initialValue !== null;
  $("#choice-title").textContent = editing ? "이름 입력" : "작업 확인";
  $("#choice-message").textContent = message;
  $("#choice-label").hidden = !editing;
  input.value = initialValue || ""; input.required = editing;
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.onclose = () => resolve(dialog.returnValue === "ok" ? editing ? input.value : true : null);
    $("#choice-form").onsubmit = (event) => { event.preventDefault(); dialog.close("ok"); };
    $("#choice-cancel").onclick = () => dialog.close("cancel");
    dialog.showModal();
    if (editing) input.focus();
  });
}

function on(element, event, callback) {
  element.addEventListener(event, async (...args) => {
    try { await callback(...args); }
    catch (error) { toast(error.message || "작업을 완료하지 못했습니다.", true); }
  });
}

async function api(path, body, raw = false) {
  let response;
  try {
    response = await fetch(path, body === undefined ? {} : {
      method: "POST",
      headers: { "X-RefArchive-Token": token, ...(body instanceof FormData ? {} : { "Content-Type": raw ? "application/octet-stream" : "application/json" }) },
      body: raw ? body : JSON.stringify(body),
    });
  } catch {
    throw new Error("프로그램 연결이 끊겼습니다. 실행 창을 확인한 뒤 새로고침하세요.");
  }
  if (!response.ok) {
    const result = await response.json().catch(() => ({}));
    throw new Error(result.error || "요청을 완료하지 못했습니다.");
  }
  return response;
}

async function post(path, body, raw = false) {
  return (await api(path, body, raw)).json();
}

const names = (text) => [...new Set(text.split(/[,，\n]/).map((value) => value.trim()).filter(Boolean))];
const normalized = (text) => text.normalize("NFKC").toLocaleLowerCase("ko");

function formValues(form) {
  const result = Object.fromEntries(new FormData(form));
  result.tags = names(result.tags || "");
  result.categories = names(result.categories || "");
  return result;
}

function libraryEntries(library) {
  const files = new Map(library.items.map((item) => [item.id, item]));
  const owners = new Map(), bundles = new Map(), emitted = new Set();
  for (const bundle of library.bundles || []) {
    const members = bundle.ids.map((id) => files.get(id)).filter(Boolean);
    if (!members.length) continue;
    const tags = [...new Set(members.flatMap((item) => item.tags))].sort();
    const cover = members.find((item) => item.id === bundle.cover) || members[0];
    bundles.set(bundle.id, { ...members[0], ...bundle, type: 'bundle', memberIds: members.map((item) => item.id),
      coverId: cover.id, coverType: cover.type, tags, material_date: bundle.material_date || '',
      categories: [...new Set(members.flatMap((item) => item.categories))].sort(),
      autoCategories: [...new Set(members.flatMap((item) => item.autoCategories || []))].filter((name) => members.filter((item) => item.categories.includes(name)).every((item) => (item.autoCategories || []).includes(name))),
      autoTags: tags.filter((tag) => members.filter((item) => item.tags.includes(tag)).every((item) => item.autoTags.includes(tag))),
      size: members.reduce((sum, item) => sum + item.size, 0), created: members.map((item) => item.created).sort().at(-1),
      searchText: members.map((item) => [item.title, item.filename, item.note, item.source, ...(item.analysis?.pages || []).map((page) => page.text)].join(' ')).join(' ') });
    for (const member of members) owners.set(member.id, bundle.id);
  }
  return library.items.flatMap((item) => {
    const owner = owners.get(item.id);
    if (!owner) return [{ ...item, memberIds: [item.id], coverId: item.id, coverType: item.type }];
    if (emitted.has(owner)) return [];
    emitted.add(owner); return [bundles.get(owner)];
  });
}

function entryFor(id) { return state.entries.find((entry) => entry.id === id || entry.memberIds.includes(id)); }
function selectedFile() { return state.library.items.find((item) => item.id === detailFileId); }
function selectedFileIds() { return state.entries.filter((entry) => state.selected.has(entry.id)).flatMap((entry) => entry.memberIds); }

async function reload() {
  state.library = await (await api("/api/state")).json();
  state.entries = libraryEntries(state.library);
  const ids = new Set(state.entries.map((item) => item.id));
  state.selected = new Set([...state.selected].filter((id) => ids.has(id)));
  const known = new Set(state.library.tags.map((tag) => tag.name));
  for (const name of modeSaveFailures) if (!known.has(name)) modeSaveFailures.delete(name);
  const saved = state.library.searches.find((entry) => entry.id === state.savedSearch);
  if (saved) { state.tags = new Set(saved.filters.tags); state.category = saved.filters.category || null; state.collection = saved.filters.collection || ""; }
  if (!state.savedSearch) state.tags = new Set([...state.tags].filter((name) => known.has(name)));
  if (!state.savedSearch && state.category && !state.library.categories.includes(state.category)) state.category = null;
  $("#connection-status").textContent = "이 PC에 연결됨";
  $("#storage-path").textContent = state.library.storage;
  renderSidebar();
  render();
  renderManage();
  renderSaved();
  renderReview();
  renderScopeFilters();
  renderJob();
  $("#download-enabled").checked = state.library.preferences.downloads;
  $("#auto-summaries").checked = state.library.preferences.summaries;
  $("#auto-ocr").checked = state.library.preferences.ocr;
  $("#auto-ocr-language").value = state.library.preferences.language;
  $("#model-status").textContent = state.library.modelReady ? "로컬 CLIP 모델 준비됨" : "모델 준비 필요 · 최초 약 88.6 MB 다운로드";
  if ($("#detail-dialog").open) { renderAnalysis(); renderDetailActions(); }
  for (const [id, list] of [["#known-tags", state.library.tags.map((tag) => tag.name)], ["#known-categories", state.library.categories]]) {
    $(id).replaceChildren(...list.map((name) => { const option = node("option"); option.value = name; return option; }));
  }
}

function renderSidebar() {
  const items = state.entries;
  const active = items.filter((item) => !item.trashed);
  $("#all-count").textContent = active.length;
  $("#unfiled-count").textContent = active.filter((item) => !item.tags.length).length;
  $('#uncategorized-count').textContent = active.filter((item) => !item.categories.length).length;
  $("#favorite-count").textContent = active.filter((item) => item.favorite).length;
  $("#trash-count").textContent = items.filter((item) => item.trashed).length;
  document.querySelectorAll("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view && !state.category && !state.collection));
  $("#collection-nav").replaceChildren(...state.library.collections.map((group) => {
    const button = node('button', state.collection === group.id ? 'active' : '');
    const ids = new Set(group.ids);
    button.append(node('span', '', group.name), node('small', '', String(active.filter((item) => item.memberIds.some((id) => ids.has(id))).length)));
    on(button, 'click', () => { resetFilters(); state.collection = group.id; renderSaved(); renderSidebar(); render(); });
    return button;
  }));
  if (!state.library.collections.length) $("#collection-nav").append(node('p', 'sidebar-hint', '자료를 모음으로 정리해 보세요.'));
  const categoryNav = $("#category-nav");
  categoryNav.replaceChildren();
  if (!state.library.categories.length) categoryNav.append(node("p", "dialog-description", "카테고리를 추가해 보세요."));
  for (const name of state.library.categories) {
    const button = node("button", state.category === name ? "active" : "");
    button.append(node("span", "", name), node("small", "", String(active.filter((item) => item.categories.includes(name)).length)));
    on(button, "click", () => { state.category = name; state.view = "all"; state.collection = ""; state.savedSearch = ""; state.selected.clear(); renderSaved(); renderSidebar(); render(); });
    categoryNav.append(button);
  }
}

function baseItems() {
  return state.entries.filter((item) => {
    if (state.collection && !item.memberIds.some((id) => state.library.collections.find((entry) => entry.id === state.collection)?.ids.includes(id))) return false;
    if (state.view === "trash") return Boolean(item.trashed);
    if (item.trashed) return false;
    if (state.view === "unfiled" && item.tags.length) return false;
    if (state.view === 'uncategorized' && item.categories.length) return false;
    if (state.view === "favorites" && !item.favorite) return false;
    return !state.category || item.categories.includes(state.category);
  });
}

function matchesMaterialDate(item, from, to) {
  if (from && to && from > to) return false;
  const value = item.material_date || '';
  return (!from && !to) || Boolean(value && (!from || value >= from) && (!to || value <= to));
}

function compareMaterialDate(a, b, descending = true) {
  const left = a.material_date || '', right = b.material_date || '';
  if (!left || !right) return left ? -1 : right ? 1 : 0;
  return (descending ? -1 : 1) * left.localeCompare(right);
}

function materialDateGroups(items, mode, ascending = false) {
  if (mode === 'none') return [['', items]];
  const groups = new Map();
  for (const item of items) {
    let value = item.material_date || '';
    if (mode.startsWith('registered-')) {
      const date = new Date(item.created);
      value = Number.isNaN(date.getTime()) ? '' : `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
    }
    const key = value.slice(0, mode.endsWith('year') ? 4 : mode.endsWith('day') ? 10 : 7);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  }
  return [...groups].sort(([a], [b]) => !a ? 1 : !b ? -1 : (ascending ? 1 : -1) * a.localeCompare(b));
}

function filteredItems(withTags = true, withType = true) {
  const query = normalized($("#search").value).split(/\s+/).filter(Boolean);
  const exclude = names($("#exclude-tags").value).map(normalized);
  let items = baseItems().filter((item) => {
    if (!matchesMaterialDate(item, $('#date-from').value, $('#date-to').value)) return false;
    if (withType && $("#type-filter").value !== "all" && item.type !== $("#type-filter").value && !item.memberIds.some((id) => state.library.items.find((file) => file.id === id)?.type === $("#type-filter").value)) return false;
    const text = normalized([item.title, item.filename, item.note, item.source, item.searchText || '', ...item.tags, ...item.categories, ...(item.analysis?.pages || []).map((page) => page.text)].join(" "));
    if (!query.every((term) => text.includes(term))) return false;
    if (exclude.some((name) => item.tags.some((tag) => normalized(tag) === name))) return false;
    if (withTags && state.tags.size) {
      const matches = [...state.tags].map((tag) => item.tags.includes(tag));
      if ($("#match-mode").value === "all" ? !matches.every(Boolean) : !matches.some(Boolean)) return false;
    }
    return true;
  });
  const sort = $("#sort").value;
  if (sort === "oldest") items.reverse();
  if (sort === 'date-desc' || sort === 'date-asc') items.sort((a, b) => compareMaterialDate(a, b, sort === 'date-desc'));
  if (sort === "title") items.sort((a, b) => a.title.localeCompare(b.title, "ko"));
  return items;
}

function render() {
  if (!state.library) return;
  const descriptions = {
    all: ["전체 자료", "모아 둔 자료에서 다음 아이디어를 찾아보세요."],
    unfiled: ["키워드 미지정", "키워드가 없는 자료입니다. 여러 개를 선택해 함께 정리하세요."],
    uncategorized: ['카테고리 미지정', '분류 근거가 부족하거나 후보가 비슷한 자료입니다. 분류 확인함에서 후보를 확인하세요.'],
    favorites: ["즐겨찾기", "자주 참고하는 자료를 가까이 두세요."],
    trash: ["휴지통", "자료와 키워드를 함께 보관하고 있습니다. 선택한 자료를 복원할 수 있습니다."],
  };
  $("#page-title").textContent = state.category || descriptions[state.view][0];
  const collection = state.library.collections.find((entry) => entry.id === state.collection);
  if (state.collection) $("#page-title").textContent = collection?.name || "삭제된 모음";
  $("#page-description").textContent = state.category ? "카테고리 안에서도 키워드와 메모로 검색할 수 있습니다." : descriptions[state.view][1];
  if (collection) $("#page-description").textContent = '폴더처럼 자료를 모아 보는 목록입니다. 하나의 카드로 보관하려면 자료를 선택해 하나로 묶으세요.';
  const items = filteredItems();
  const visibleIds = new Set(items.map((item) => item.id));
  state.selected = new Set([...state.selected].filter((id) => visibleIds.has(id)));
  $("#results-count").textContent = `${items.length.toLocaleString()}개의 자료${state.tags.size ? ` · ${state.tags.size}개 키워드 적용` : ""}`;
  const summaryItems = filteredItems(true, false);
  $("#library-summary").replaceChildren(...[['image', '이미지'], ['video', '동영상'], ['pdf', 'PDF'], ['document', '문서'], ['link', '링크'], ['bundle', '묶음']].map(([type, label]) => {
    const button = node('button', `type-chip${$('#type-filter').value === type ? ' active' : ''}`);
    button.setAttribute('aria-pressed', String($('#type-filter').value === type));
    button.append(node('span', '', label), node('b', '', String(summaryItems.filter((item) => item.type === type || item.memberIds.some((id) => state.library.items.find((file) => file.id === id)?.type === type)).length)));
    on(button, 'click', () => { $('#type-filter').value = $('#type-filter').value === type ? 'all' : type; state.savedSearch = ''; renderSaved(); render(); });
    return button;
  }));
  $('#advanced-label').textContent = `상세 검색${state.tags.size || $('#exclude-tags').value || state.savedSearch || state.collection || state.category || $('#date-from').value || $('#date-to').value ? ' · 조건 적용 중' : ''}`;
  $('#date-filter-error').hidden = !($('#date-from').value && $('#date-to').value && $('#date-from').value > $('#date-to').value);
  const group = $('#date-group').value;
  $('#grid').replaceChildren(...materialDateGroups(items, group, $('#sort').value === 'date-asc').flatMap(([key, entries]) => {
    const parts = key.split('-');
    const dateLabel = key ? parts.map((part, index) => `${part}${['년', '월', '일'][index]}`).join(' ') : '날짜 미지정';
    const heading = node('h2', 'date-group-heading', `${group.startsWith('registered-') ? '등록일' : '자료 날짜'} · ${dateLabel} · ${entries.length}개`);
    return group === 'none' ? entries.map(renderCard) : [heading, ...entries.map(renderCard)];
  }));
  const filterTags = new Set([...filteredItems(false).flatMap((item) => item.tags), ...state.tags]);
  const chips = [...filterTags].sort((a, b) => a.localeCompare(b, "ko")).map((name) => {
    const button = node("button", `tag-pill${state.tags.has(name) ? " active" : ""}`);
    button.append(document.createTextNode(name), node("small", "", String(items.filter((item) => item.tags.includes(name)).length)));
    button.setAttribute("aria-pressed", String(state.tags.has(name)));
    on(button, "click", () => { if (state.tags.has(name)) state.tags.delete(name); else state.tags.add(name); state.savedSearch = ""; $("#saved-search").value = ""; render(); });
    return button;
  });
  $("#tag-filters").replaceChildren(...chips);
  const pristine = state.library.items.filter((item) => !item.trashed).length === 0 && state.view !== "trash" && !state.category && !$("#search").value && !state.tags.size;
  $("#empty-state").hidden = items.length > 0;
  $("#empty-title").textContent = pristine ? "첫 번째 자료를 모아보세요" : state.view === "trash" ? "휴지통이 비어 있습니다" : "조건에 맞는 자료가 없습니다";
  $("#empty-description").textContent = pristine ? "이미지를 끌어 놓거나 링크를 저장하세요. 키워드는 지금 붙여도, 나중에 정리해도 괜찮습니다." : "검색 조건을 줄이거나 다른 자료함을 선택해 보세요.";
  $("#empty-upload").hidden = !pristine;
  $("#batch-bar").hidden = state.selected.size === 0;
  $("#selected-count").textContent = `${state.selected.size}개 선택`;
  $("#batch-trash").dataset.batch = state.view === "trash" ? "restore" : "trash";
  $("#batch-trash").textContent = state.view === "trash" ? "복원" : "휴지통";
  $("#collection-remove").hidden = !state.collection;
  $("#collection-add").hidden = false;
}

function renderCard(item) {
  const card = node("article", `asset-card${state.selected.has(item.id) ? " selected" : ""}`);
  const open = node("button", "card-open");
  open.setAttribute("aria-label", `${item.title} 상세 보기`);
  const art = node("div", item.coverType !== "link" ? "card-image" : "card-image link-art");
  if (item.coverType !== "link") {
    const image = node("img"); image.src = `/thumb/${item.coverId}`; image.alt = item.title; image.loading = "lazy";
    art.append(image);
    if (item.type === 'video') art.classList.add('video-art');
  } else {
    art.append(node("span", "", "↗"), node("span", "", item.type === 'bundle' ? '링크 묶음' : new URL(item.source).hostname));
  }
  art.append(node('span', 'media-badge', item.type === 'bundle' ? `묶음 · ${item.memberIds.length}개 파일` : { image: 'IMAGE', video: 'VIDEO', pdf: 'PDF', document: 'DOC', link: 'LINK' }[item.type]));
  const content = node("div", "card-content");
  const labels = node("div", "card-labels");
  labels.append(...(item.tags.length ? item.tags.slice(0, 3) : ["키워드 없음"]).map((tag) => node("span", "", `${tag}${item.autoTags.includes(tag) ? " · 자동" : ""}`)));
  for (const name of item.categories.slice(0, 2)) labels.prepend(node('span', 'category-badge', `${name}${(item.autoCategories || []).includes(name) ? ' · 자동' : ''}`));
  const meta = node("div", "card-meta");
  meta.append(node("span", "", item.type === 'bundle' ? `${item.memberIds.length}개 파일 · ${formatSize(item.size)}` : item.type === "image" ? `${item.width} × ${item.height}` : item.type === "pdf" ? "PDF 문서" : item.type === 'video' ? formatSize(item.size) : item.type === "document" ? (item.filename.split(".").at(-1) || "문서").toUpperCase() : "웹 링크"), node("span", "", item.material_date || "날짜 미지정"));
  content.append(node("div", "card-title", item.title), labels, meta);
  open.append(art, content);
  on(open, "click", () => openDetail(item.id));
  const checkbox = node("input", "select-check");
  checkbox.type = "checkbox"; checkbox.checked = state.selected.has(item.id);
  checkbox.setAttribute("aria-label", `${item.title} 선택`);
  on(checkbox, "change", () => { if (checkbox.checked) state.selected.add(item.id); else state.selected.delete(item.id); render(); });
  const favorite = node("button", `card-star${item.favorite ? " starred" : ""}`, item.favorite ? "★" : "☆");
  favorite.setAttribute("aria-label", `${item.title} 즐겨찾기 ${item.favorite ? "해제" : "추가"}`);
  on(favorite, "click", async () => { await post("/api/batch", { ids: [item.id], action: item.favorite ? "unfavorite" : "favorite" }); await reload(); });
  card.append(open, checkbox, favorite);
  return card;
}

function openDetail(id) {
  const item = entryFor(id);
  if (!item) return;
  const form = $("#detail-form");
  for (const name of ["id", "title", "source", "note", "material_date"]) form.elements[name].value = item[name] || "";
  form.elements.tags.value = item.tags.join(", ");
  form.elements.categories.value = item.categories.join(", ");
  $('#detail-form h2').textContent = item.type === 'bundle' ? '묶음 자료 상세' : '자료 상세';
  $('#split-bundle').hidden = item.type !== 'bundle';
  $('#bundle-files').hidden = item.type !== 'bundle';
  $('#bundle-files').replaceChildren(...item.memberIds.map((fileId, index) => {
    const file = state.library.items.find((file) => file.id === fileId);
    const button = node('button', 'bundle-file', `${index + 1}. ${file.filename || file.title}`); button.type = 'button';
    on(button, 'click', () => renderDetailFile(fileId)); button.dataset.file = fileId;
    return button;
  }));
  $('#detail-meta').textContent = item.type === 'bundle' ? `${item.memberIds.length}개 파일을 하나의 자료로 보관 · 공통 키워드·카테고리는 모든 파일에 적용됩니다.` : `${item.filename || '웹 링크'} · ${formatSize(item.size)}`;
  $('#detail-meta').append(document.createTextNode(` · 등록일 ${new Date(item.created).toLocaleString('ko')}`));
  $('#source-link').hidden = !item.source; $('#source-link').href = item.source || '#';
  $('#detail-favorite').textContent = item.favorite ? '★ 즐겨찾기 해제' : '☆ 즐겨찾기';
  renderDetailActions();
  if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  renderDetailFile(item.memberIds.includes(id) ? id : item.coverId);
}

function renderDetailFile(id) {
  const previous = $('#detail-preview video');
  if (previous) { previous.pause(); previous.removeAttribute('src'); previous.load(); }
  detailFileId = id;
  const item = selectedFile();
  for (const button of $('#bundle-files').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.file === id));
  const preview = $("#detail-preview"); preview.replaceChildren();
  preview.classList.toggle("pdf-preview", item.type === "pdf" || item.type === 'video');
  if (item.type === "image") {
    const image = node("img"); image.src = `/media/${item.id}`; image.alt = item.title; preview.append(image);
  } else if (item.type === 'video') {
    const video = node('video'); video.src = `/media/${item.id}`; video.controls = true; video.preload = 'metadata'; video.playsInline = true;
    video.setAttribute('aria-label', item.title);
    const hint = node('p', 'video-hint', '재생 버튼으로 확인하세요. 브라우저에서 지원하지 않는 코덱은 원본을 저장해 재생할 수 있습니다.');
    on(video, 'error', () => { hint.textContent = '이 브라우저에서 재생할 수 없습니다. 원본 저장 후 동영상 플레이어로 열어 주세요.'; });
    const link = node('a', 'button secondary', '동영상 원본 저장'); link.href = `/media/${item.id}`; link.download = item.filename;
    preview.append(video, link, hint);
  } else if (item.type === 'document') {
    preview.append(node('p', 'document-preview-note', '텍스트 미리보기 · 서식·그림·차트는 표시하지 않습니다. Excel은 저장된 계산값만 표시하며 수식을 실행하지 않습니다.'));
    const content = (item.analysis?.pages || []).map((page) => page.text).join('\n\n');
    preview.append(node('pre', 'document-text', content || '추출할 텍스트가 없습니다.'));
    if (item.analysis?.truncated) preview.append(node('p', 'document-preview-note', '미리보기·검색은 앞 50만 자까지 표시합니다. 원본은 전부 보관됩니다.'));
    if (item.analysis?.openReason) preview.append(node('p', 'document-preview-note', `연결 앱 열기 제한: ${item.analysis.openReason}`));
  } else if (item.type === "pdf") {
    const link = node("a", "", "PDF 원본 열기 ↗"); link.href = `/media/${item.id}`; link.target = "_blank"; link.rel = "noopener noreferrer";
    const image = node("img"); image.src = `/thumb/${item.id}`; image.alt = item.title; preview.append(link, image);
  } else {
    const link = node("a", "", `링크만 저장된 자료\n${item.source}`); link.href = item.source; link.target = "_blank"; link.rel = "noopener noreferrer"; preview.append(link);
  }
  if (item.type !== 'link' && item.type !== 'video') {
    const download = node('a', 'button secondary', '선택 파일 원본 저장'); download.href = `/media/${item.id}`; download.download = item.filename; preview.append(download);
  }
  if (item.type !== 'link') {
    const open = node('button', 'button secondary', '연결된 앱으로 복사본 열기'); open.type = 'button';
    open.disabled = Boolean(item.trashed || item.type === 'document' && item.analysis?.openReason);
    on(open, 'click', async () => { open.disabled = true; try { const result = await post('/api/open', {id: item.id}); toast(`복사본을 열었습니다. 수정은 자료함에 자동 반영되지 않습니다. ${result.copy}`); } finally { open.disabled = false; } });
    preview.append(open, node('p', 'document-preview-note', '보관 원본은 유지됩니다. 열린 복사본의 변경 내용은 파일 추가로 다시 가져오세요. 복사본은 데이터 폴더의 opened에 남으며 백업에는 포함되지 않습니다.'));
  }
  preview.append(node('p', 'detail-file-caption', `${item.filename || item.title} · ${formatSize(item.size)}`));
  const entry = entryFor($('#detail-form').elements.id.value);
  renderBundleEditor(entry, item);
  $('#file-original-note').hidden = entry.type !== 'bundle' || !item.note;
  $('#file-original-note').textContent = item.note ? `선택 파일의 기존 메모: ${item.note}` : '';
  $("#detail-similar").hidden = item.type !== "image";
  renderAnalysis(true);
}

function renderDetailActions() {
  const item = entryFor($("#detail-form").elements.id.value);
  if (!item) return;
  $("#detail-trash").textContent = item.trashed ? "자료함으로 복원" : "휴지통으로 이동";
  $("#detail-trash").classList.toggle("danger", !item.trashed);
  $("#detail-trash-note").hidden = !item.trashed;
}

function renderBundleEditor(entry, item) {
  bundleEditor.hidden = entry.type !== 'bundle';
  bundleEditor.replaceChildren();
  if (entry.type !== 'bundle') return;
  const index = entry.memberIds.indexOf(item.id);
  bundleEditor.append(node('p', 'dialog-description', `선택 ${index + 1} / ${entry.memberIds.length}${entry.coverId === item.id ? ' · 대표 파일' : ''}`));
  for (const [action, label, disabled] of [['cover', '대표로 지정', entry.coverId === item.id], ['previous', '앞으로', index === 0], ['next', '뒤로', index === entry.memberIds.length - 1], ['detach', '이 파일만 분리', false]]) {
    const button = node('button', 'button secondary', label); button.type = 'button'; button.disabled = disabled;
    on(button, 'click', async () => {
      const form = $('#detail-form'), draft = formValues(form);
      if (action === 'detach') {
        if (['title', 'source', 'note', 'material_date'].some((field) => draft[field] !== (entry[field] || '')) || ['tags', 'categories'].some((field) => JSON.stringify(draft[field]) !== JSON.stringify(entry[field]))) {
          toast('입력한 내용을 먼저 변경 저장한 뒤 분리하세요.', true); return;
        }
        if (!await ask('선택 파일만 별도 자료로 분리할까요? 원본·분류는 유지하고 묶음 제목·출처·메모를 개별 메모에 남깁니다.')) return;
        await post('/api/bundle', { action: 'detach', id: entry.id, file: item.id });
        await reload(); openDetail(item.id); toast('선택 파일을 별도 자료로 분리했습니다.'); return;
      }
      const ids = [...entry.memberIds];
      if (action !== 'cover') { const next = index + (action === 'previous' ? -1 : 1); [ids[index], ids[next]] = [ids[next], ids[index]]; }
      button.disabled = true;
      try {
        await post('/api/bundle', { action: 'edit', id: entry.id, ids, cover: action === 'cover' ? item.id : entry.coverId });
        await reload(); openDetail(item.id);
        for (const field of ['title', 'source', 'note', 'material_date', 'tags', 'categories']) form.elements[field].value = Array.isArray(draft[field]) ? draft[field].join(', ') : draft[field];
        toast(action === 'cover' ? '대표 파일을 변경했습니다.' : '파일 순서를 변경했습니다.');
      } finally { button.disabled = false; }
    });
    bundleEditor.append(button);
  }
}

function searchFilters() {
  return { view: state.view, category: state.category || "", collection: state.collection, query: $("#search").value,
    tags: [...state.tags], exclude: names($("#exclude-tags").value), type: $("#type-filter").value, match: $("#match-mode").value, sort: $("#sort").value, dateFrom: $("#date-from").value, dateTo: $("#date-to").value, group: $("#date-group").value };
}

function applySearch(entry) {
  const filters = entry.filters;
  state.view = filters.view; state.category = filters.category || null; state.collection = filters.collection || "";
  state.tags = new Set(filters.tags); state.savedSearch = entry.id; state.selected.clear();
  $("#search").value = filters.query; $("#exclude-tags").value = filters.exclude.join(", ");
  $("#type-filter").value = filters.type; $("#match-mode").value = filters.match; $("#sort").value = filters.sort;
  $("#date-from").value = filters.dateFrom || ""; $("#date-to").value = filters.dateTo || ""; $("#date-group").value = filters.group || "none";
  renderSaved(); renderSidebar(); render();
}

function renderSaved() {
  for (const [selector, entries, value, empty] of [["#saved-search", state.library.searches, state.savedSearch, "검색 조건 선택"], ["#collection-filter", state.library.collections, state.collection, "모든 자료"]]) {
    const select = $(selector), first = node("option", "", empty); first.value = "";
    select.replaceChildren(first, ...entries.map((entry) => { const option = node("option", "", entry.name); option.value = entry.id; return option; }));
    select.value = value;
  }
  for (const [kind, selector, entries] of [["search", "#saved-search-list", state.library.searches], ["collection", "#collection-list", state.library.collections]]) {
    const rows = entries.map((entry) => {
      const row = node("div", "manage-row"), label = node("div", "", entry.name);
      if (kind === "collection") label.append(node("small", "", `${state.entries.filter((item) => item.memberIds.some((id) => entry.ids.includes(id))).length}개 자료 · 휴지통 포함`));
      const open = node("button", "", "열기"), rename = node("button", "", "이름 변경"), remove = node("button", "", "삭제");
      on(open, "click", () => {
        if (kind === "search") applySearch(entry);
        else { resetFilters(); state.collection = entry.id; renderSaved(); renderSidebar(); render(); }
        $("#saved-dialog").close();
      });
      on(rename, "click", async () => { const name = (await ask("새 이름을 입력하세요.", entry.name))?.trim(); if (!name || name === entry.name) return; await post("/api/saved", { ...entry, kind, name }); await reload(); toast("이름을 변경했습니다."); });
      on(remove, "click", async () => { if (!await ask(`‘${entry.name}’을 삭제할까요? 원본 자료는 유지됩니다.`)) return; await post("/api/saved", { kind, id: entry.id, action: "delete" }); await reload(); toast("저장 항목만 삭제했습니다."); });
      row.append(label, open, rename, remove); return row;
    });
    $(selector).replaceChildren(...(rows.length ? rows : [node("p", "dialog-description", "저장 항목이 없습니다.")]));
  }
}

function resetFilters() {
  state.view = "all"; state.category = null; state.collection = ""; state.savedSearch = ""; state.tags.clear(); state.selected.clear();
  $("#search").value = ""; $("#exclude-tags").value = ""; $("#type-filter").value = "all"; $("#match-mode").value = "all"; $("#sort").value = "newest"; $("#date-from").value = ""; $("#date-to").value = ""; $("#date-group").value = "registered-month";
}

async function createCollection(ids) {
  const name = (await ask("새 모음 이름을 입력하세요.", ""))?.trim(); if (!name) return;
  const result = await post("/api/saved", { kind: "collection", name, ids });
  resetFilters(); state.collection = result.id; await reload(); toast("모음을 만들었습니다.");
}

function renderJob() {
  const job = state.library.job, labels = { model: "모델 준비", classify: "자료 분류", text: "내용 분석", import: "새 자료 분석" };
  const label = labels[job.message] || "분석", disconnected = !!jobConnectionError;
  const title = disconnected ? "분석 상태 확인 중단" : `${label} ${job.running ? "진행 중" : job.errors.length ? "완료 · 실패 항목 확인" : "완료"}`;
  const count = job.message === "model" ? job.running ? "모델 내려받기 · 준비 중" : "모델 준비 종료" : `처리 완료 ${job.done} / ${job.total}개${job.total ? ` · ${Math.floor(job.done / job.total * 100)}%` : ""}`;
  const detail = disconnected ? "서버 연결을 다시 확인하는 중입니다. 작업 완료 여부는 아직 확인할 수 없습니다." : job.errors.length ? `실패 ${job.errors.length}건: ${job.errors.slice(0, 3).map((error) => error.message).join(" / ")}${job.errors.length > 3 ? " · 나머지 실패 자료도 선택 후 다시 분석할 수 있습니다." : ""}` : job.running ? "이 창을 닫고 다른 자료를 정리해도 분석은 계속됩니다." : job.total ? "작업이 끝났습니다. 분석 결과를 확인하세요." : "분석할 자료가 없습니다.";
  for (const status of document.querySelectorAll(".job-status")) {
    status.hidden = !job.message || jobDismissed;
    status.classList.toggle("running", job.running && !disconnected);
    status.classList.toggle("failed", disconnected || !!job.errors.length);
    for (const [selector, text] of [[".job-title", title], [".job-count", count], [".job-detail", detail]]) {
      if (status.querySelector(selector).textContent !== text) status.querySelector(selector).textContent = text;
    }
    const progress = status.querySelector("progress");
    if (disconnected || job.running && (job.message === "model" || !job.total)) progress.removeAttribute("value");
    else progress.value = job.total ? job.done / job.total : 1;
  }
  for (const toggle of document.querySelectorAll('.job-toggle')) {
    toggle.hidden = !job.message;
    toggle.textContent = disconnected ? '연결 확인 중' : job.running ? `${label} · ${job.message === 'model' || !job.total ? '진행 중' : `${Math.floor(job.done / job.total * 100)}%`}` : job.errors.length ? '분석 실패 확인' : '분석 완료';
    toggle.classList.toggle('running', job.running);
    toggle.classList.toggle('failed', disconnected || !!job.errors.length);
    toggle.setAttribute('aria-expanded', String(!jobDismissed));
    toggle.setAttribute('aria-label', `${toggle.textContent} · 분석 표시 ${jobDismissed ? '열기' : '닫기'}`);
  }
  document.title = job.running ? `${disconnected ? "연결 확인 중" : `${label} 진행 중`} · RefArchive` : "RefArchive · 나의 자료함";
  for (const selector of ["#prepare-model", "#analyze-text", "#analyze-ocr", "#classify-all", "#classify-selected", "#shutdown-button", "#restore-button", "#classify-preview"]) $(selector).disabled = job.running;
  for (const control of document.querySelectorAll(".classification-mode, .classification-threshold, .classification-save, .text-rule-button, #text-rule-save, .category-rule-button, #category-rule-save")) control.disabled = job.running || control.dataset.saving === "true";
}

function setJobDismissed(value) {
  jobDismissed = value;
  try { sessionStorage.setItem('refarchive-hide-analysis', String(value)); } catch {}
  renderJob();
}

function watchJob() {
  clearTimeout(jobTimer);
  jobTimer = setTimeout(async () => {
    try {
      const job = await (await api("/api/job")).json(); jobConnectionError = ""; state.library.job = job; renderJob();
      if (job.running) watchJob();
      else { await reload(); toast(job.errors.length ? `${job.errors.length}개 분석 실패: ${job.errors[0].message}` : "분석 작업을 완료했습니다.", !!job.errors.length); }
    } catch (error) {
      if (!jobConnectionError) toast(error.message, true);
      jobConnectionError = error.message; renderJob(); watchJob();
    }
  }, 1200);
}

async function startAnalysis(operation, ids, ocr = false) {
  await waitForModeSettings();
  await post("/api/job", { operation, ...(ids ? { ids } : {}), ocr, language: $("#ocr-language").value });
  await reload(); watchJob(); toast("분석을 시작했습니다. 다른 자료를 계속 정리할 수 있습니다.");
}

async function waitForModeSettings() {
  for (const [name, editor] of modeEditors) {
    if (editor.mode.dataset.saving === "true") continue;
    const saved = state.library.tags.find((tag) => tag.name === name);
    if (!saved) continue;
    if (!editor.threshold.checkValidity() || editor.mode.value !== saved.mode || Number(editor.threshold.value) !== saved.threshold) await editor.save();
    else modeSaveFailures.delete(name);
  }
  await pendingModeSave.catch(() => {});
  if (modeSaveFailures.size) throw new Error(`분류 설정 저장에 실패했습니다: ${[...modeSaveFailures].join(", ")}. 설정을 다시 저장하세요.`);
}

function renderScopeFilters() {
  for (const [selector, values, label] of [["#classify-tag", state.library.tags.map((tag) => tag.name), "모든 키워드"], ["#classify-category", state.library.categories, "모든 카테고리"]]) {
    const select = $(selector), previous = select.value, blank = node("option", "", label); blank.value = "";
    select.replaceChildren(blank, ...values.map((name) => { const option = node("option", "", name); option.value = name; return option; }));
    select.value = values.includes(previous) ? previous : "";
  }
  $("#classify-count").textContent = "조건을 선택한 뒤 대상을 확인하세요.";
}

function classificationScope() {
  const mode = $("#classify-scope").value;
  if (!$("#classify-hours").checkValidity()) throw new Error("최근 등록 시간을 0 초과 8760 이하로 입력하세요.");
  return { mode: ["filtered", "selected"].includes(mode) ? "ids" : mode,
    ...(mode === "filtered" ? { ids: filteredItems().map((item) => item.id) } : mode === "selected" ? { ids: [...state.selected] } : {}),
    tag: $("#classify-tag").value, category: $("#classify-category").value, type: $("#classify-type").value,
    ...($("#classify-hours").value ? { hours: Number($("#classify-hours").value) } : {}) };
}

async function previewClassification(scope = classificationScope()) {
  const result = await post("/api/classification/scope", scope);
  $("#classify-count").textContent = `대상 파일 ${result.count}개${result.count > result.limit ? ` · 한 번에 ${result.limit}개까지 가능합니다. 범위를 좁혀 주세요.` : result.count ? " · 원본과 수동 분류는 보존됩니다." : " · 다른 범위나 조건을 선택하세요."}`;
  return result;
}

async function runClassification(scope) {
  await waitForModeSettings();
  const result = await previewClassification(scope);
  if (!result.count) { toast("분류할 자료가 없습니다. 범위를 변경하세요."); return; }
  if (result.count > result.limit) throw new Error(`한 번에 ${result.limit}개까지 분류할 수 있습니다. 범위를 좁혀 주세요.`);
  if (await ask(`확인한 파일 ${result.count}개를 현재 설정으로 다시 분류할까요? 확인 전 자동 결과는 갱신하며 직접 지정한 분류·승인·거절 기록과 원본은 유지합니다.`)) await startAnalysis("classify", result.ids);
}

function renderReview() {
  const history = $("#review-filter").value === "history";
  const items = new Map(state.library.items.map((item) => [item.id, item]));
  const predictions = [...(state.library.categoryPredictions || []).map((entry) => ({ ...entry, source: 'category', tag: entry.name })), ...state.library.predictions];
  const entries = predictions.filter((entry) => items.has(entry.item) && !items.get(entry.item).trashed && (history ? ["accepted", "rejected"].includes(entry.status) : ["pending", "applied"].includes(entry.status)));
  const status = { pending: "추천", applied: "자동 적용 · 확인 전", accepted: "승인", rejected: "거절" };
  $("#review-button").textContent = `분류 확인함 · ${predictions.filter((entry) => ["pending", "applied"].includes(entry.status) && items.has(entry.item) && !items.get(entry.item).trashed).length}`;
  const rows = entries.map((entry) => {
    const row = node("section", "review-row"), target = node("div", "review-target"), item = items.get(entry.item);
    const image = item.type === 'link' ? node('span', 'file-type', 'LINK') : node("img");
    if (item.type !== 'link') { image.src = `/thumb/${item.id}`; image.alt = item.title; }
    const title = node("button", "", item.title); on(title, "click", () => openDetail(item.id));
    const category = entry.source === 'category';
    const basis = category ? `카테고리 · ${entry.reason}` : entry.source === 'text' ? '텍스트 규칙 일치' : `이미지 유사도 ${entry.score.toFixed(3)}`;
    target.append(image, title); row.append(target, node("p", "dialog-description", `${entry.tag} · ${basis} · ${status[entry.status]}`));
    const evidence = node("div", "evidence");
    for (const id of entry.evidence) { const reference = items.get(id); if (!reference) continue; const button = node("button"), thumb = node("img"); thumb.src = `/thumb/${id}`; thumb.alt = reference.title; button.append(thumb, document.createTextNode(reference.title)); on(button, "click", () => openDetail(id)); evidence.append(button); }
    if (entry.source === 'text') {
      const fields = { title: '제목', filename: '파일명', note: '메모', content: '본문' };
      for (const match of entry.matches) evidence.append(node('p', 'rule-evidence', `${fields[match.field]}${match.page ? ` ${match.page}페이지` : ''}: “${match.term}” 포함`));
    }
    if (category) {
      const fields = { title: '제목', filename: '파일명', note: '메모', content: '본문', description: '설명 비교', example: '확정 사례 비교', manual: '사용자 지정' };
      for (const match of entry.reasons) evidence.append(node('p', 'rule-evidence', `${fields[match.field]}${match.page ? ` ${match.page}페이지` : ''}: ${match.term}`));
    }
    row.append(node("p", "dialog-description", category || entry.source === 'text' ? '분류 당시 근거 · 정확도 확률이 아닙니다' : "유사한 확정 사례"), evidence);
    if (!history) for (const [action, text] of [["accept", "승인"], ["reject", "거절"]]) { const button = node("button", "button secondary", text); on(button, "click", async () => { await post(category ? '/api/category-review' : '/api/review', { id: entry.item, tag: entry.tag, name: entry.name, action }); await reload(); toast(action === "accept" ? "분류를 확정했습니다." : "제안을 거절했습니다."); }); row.append(button); }
    return row;
  });
  $("#review-list").replaceChildren(...(rows.length ? rows : [node("p", "dialog-description", "표시할 분류 결과가 없습니다.")]));
}

function renderAnalysisPage() {
  const item = selectedFile();
  const number = Number($("#analysis-page").value), page = item?.analysis?.pages.find((page) => page.number === number);
  $("#analysis-text").value = page?.text || "";
  $("#analysis-page-link").hidden = item?.type !== "pdf";
  $("#analysis-page-link").href = item?.type === "pdf" ? `/media/${item.id}#page=${number}` : "#";
}

function renderAnalysis(resetPage = false) {
  const item = selectedFile();
  if (!item) return;
  const analysis = item.analysis;
  const previousPage = !resetPage && analysisItemId === item.id ? Number($("#analysis-page").value) : null;
  analysisItemId = item.id;
  $("#analyze-ocr").hidden = !['image', 'pdf'].includes(item.type);
  $('#ocr-language').closest('label').hidden = !['image', 'pdf'].includes(item.type);
  $('#analyze-text').textContent = item.type === 'video' ? '저장한 메모 요약' : '텍스트 추출 · 요약';
  $("#analysis-method").textContent = analysis ? `${analysis.method} · ${analysis.summarized === false ? "텍스트 추출 · 요약 꺼짐" : "원문 문장 추출 요약"}${analysis.pages.every((page) => !page.text.trim()) ? " · 추출 가능한 텍스트 없음" : analysis.summary.length || analysis.summarized === false ? "" : " · 요약할 긴 문장 없음"}` : "분석 결과가 없습니다. 메모를 저장하거나 PDF·OCR 텍스트를 추출하세요.";
  $("#analysis-pages").hidden = !analysis;
  $("#apply-analysis-tags").hidden = !analysis?.keywords.length;
  $("#analysis-summary").replaceChildren(...(analysis?.summary || []).map((sentence) => {
    const row = node("p", "summary-row"), button = node("button", "", `${sentence.page}페이지 근거 보기`); button.type = "button";
    on(button, "click", () => { $("#analysis-page").value = String(sentence.page); renderAnalysisPage(); $("#analysis-text").focus(); });
    row.append(document.createTextNode(sentence.text + " "), button); return row;
  }));
  $("#analysis-keywords").replaceChildren(...(analysis?.keywords || []).map((keyword) => { const label = node("label"), input = node("input"); input.type = "checkbox"; input.value = keyword; label.append(input, document.createTextNode(keyword)); return label; }));
  $("#analysis-page").replaceChildren(...(analysis?.pages || []).map((page) => { const option = node("option", "", `${page.number}페이지 · ${page.method}`); option.value = page.number; return option; }));
  if (analysis?.pages.length) {
    let selected = analysis.pages.find((page) => page.number === previousPage);
    if (!selected) {
      const terms = normalized($("#search").value).split(/\s+/).filter(Boolean);
      selected = analysis.pages.map((page) => ({ page, matches: terms.filter((term) => normalized(page.text).includes(term)).length })).sort((a, b) => b.matches - a.matches)[0].page;
    }
    $("#analysis-page").value = String(selected.number);
  }
  renderAnalysisPage();
}

function renderManage() {
  if (!state.library) return;
  modeEditors.clear();
  for (const [kind, selector, list] of [["tags", "#manage-tags", state.library.tags], ["categories", "#manage-categories", state.library.categories.map((name) => ({ name }))]]) {
    const rows = list.map((entry) => {
      const row = node("div", "manage-row");
      const label = node("div", "", entry.name);
      if (kind === "tags") label.append(node("small", "", `${entry.count}개 자료 · 확정 이미지 ${entry.examples}/10개${entry.eligible ? " · 조건 충족" : ""}${!entry.eligible && entry.mode !== "off" ? " · 새 분류 일시 중단" : ""}`));
      row.append(label);
      if (kind === 'categories') {
        const rule = (state.library.categoryRules || []).find((value) => value.name === entry.name) || { mode: 'auto', description: '', include: [], exclude: [], examples: 0 };
        label.append(node('small', '', `${{ off: '끄기', suggest: '추천만', auto: '자동 배정' }[rule.mode]} · 확정 자료 ${rule.examples}개`));
        const button = node('button', 'button secondary category-rule-button', '분류 기준'); button.type = 'button'; button.disabled = state.library.job.running;
        button.setAttribute('aria-label', `${entry.name} 분류 기준`);
        on(button, 'click', () => {
          const form = $('#category-rule-form'); form.reset(); form.dataset.name = entry.name;
          $('#category-rule-title').textContent = `${entry.name} · 카테고리 분류 기준`;
          form.elements.mode.value = rule.mode; form.elements.description.value = rule.description;
          form.elements.include.value = rule.include.join('\n'); form.elements.exclude.value = rule.exclude.join('\n');
          $('#category-rule-dialog').showModal();
        });
        row.append(button);
      }
      if (kind === "tags") {
        const mode = node("select", "classification-mode"); mode.setAttribute("aria-label", `${entry.name} 이미지 분류 모드`);
        for (const [value, text] of [["off", "이미지 · 끄기"], ["suggest", "이미지 · 추천만"], ["auto", "이미지 · 자동 적용"]]) { const option = node("option", "", text); option.value = value; option.disabled = value !== "off" && (!entry.eligible || !state.library.modelReady); mode.append(option); }
        mode.value = entry.mode;
        const threshold = node("input", "classification-threshold"); threshold.type = "number"; threshold.required = true; threshold.min = "0.5"; threshold.max = "1"; threshold.step = "0.01"; threshold.value = entry.threshold; threshold.setAttribute("aria-label", `${entry.name} 유사도 기준`);
        const saved = node("small", "mode-save-status", "변경 시 자동 저장"); saved.setAttribute("role", "status");
        const save = async () => {
          if (!threshold.checkValidity()) { modeSaveFailures.add(entry.name); saved.textContent = "유사도 기준 확인 필요"; throw new Error("유사도 기준을 0.5~1 사이로 입력하세요."); }
          const settings = { name: entry.name, mode: mode.value, threshold: Number(threshold.value) };
          for (const control of [mode, threshold, saveButton]) { control.disabled = true; control.dataset.saving = "true"; }
          saved.textContent = "저장 중…";
          pendingModeSave = pendingModeSave.catch(() => {}).then(() => post("/api/mode", settings));
          try {
            await pendingModeSave; modeSaveFailures.delete(entry.name);
            const current = state.library.tags.find((tag) => tag.name === entry.name); if (current) Object.assign(current, settings);
            saved.textContent = "저장됨"; toast("분류 설정을 저장했습니다. 기존 이미지는 범위를 정해 다시 분류하세요.");
          } catch (error) { modeSaveFailures.add(entry.name); saved.textContent = "저장 실패 · 다시 변경해 저장하세요"; throw error; }
          finally { for (const control of [mode, threshold, saveButton]) { control.dataset.saving = "false"; control.disabled = state.library.job.running; } }
        };
        const saveButton = node("button", "button secondary classification-save", "저장"); saveButton.type = "button";
        on(saveButton, "click", save);
        modeEditors.set(entry.name, { mode, threshold, save });
        on(mode, "change", save); on(threshold, "change", save); row.append(mode, threshold, saveButton, saved);
        const textRule = node('button', 'button secondary text-rule-button', `텍스트 규칙 · ${{ off: '끄기', suggest: '추천만', auto: '자동 적용' }[entry.textRule?.mode || 'off']}`);
        textRule.type = 'button'; textRule.disabled = state.library.job.running;
        on(textRule, 'click', async () => {
          await waitForModeSettings();
          const rule = entry.textRule || { mode: 'off', fields: ['title', 'content'], match: 'any', terms: [] };
          const form = $('#text-rule-form'); form.reset(); form.dataset.tag = entry.name;
          $('#text-rule-title').textContent = `${entry.name} · 텍스트 분류 규칙`;
          form.elements.mode.value = rule.mode; form.elements.match.value = rule.match;
          form.elements.terms.value = rule.terms.join('\n');
          for (const input of form.querySelectorAll('[name="fields"]')) input.checked = rule.fields.includes(input.value);
          $('#text-rule-dialog').showModal();
        });
        row.append(textRule);
      }
      const rename = node("button", "", "변경·병합");
      on(rename, "click", async () => {
        const target = (await ask("새 이름을 입력하세요. 기존 이름을 입력하면 분류를 병합합니다.", entry.name))?.trim();
        if (!target || target === entry.name) return;
        const exists = list.some((value) => value.name === target);
        if (exists && !await ask(`‘${entry.name}’을 ‘${target}’에 병합할까요? 원본 자료는 유지됩니다.`)) return;
        await post("/api/taxonomy", { kind, name: entry.name, action: "rename", target }); await reload(); toast("분류를 변경했습니다.");
      });
      const remove = node("button", "", "삭제");
      on(remove, "click", async () => {
        if (!await ask(`‘${entry.name}’ 분류와 자료의 연결을 제거할까요? 원본 자료는 삭제하지 않습니다.`)) return;
        await post("/api/taxonomy", { kind, name: entry.name, action: "delete" }); await reload(); toast("분류만 제거했습니다. 원본은 유지됩니다.");
      });
      row.append(rename, remove);
      return row;
    });
    $(selector).replaceChildren(...rows);
  }
}

function prepareUpload(files) {
  if (state.busy) { toast("현재 저장 작업이 끝난 뒤 추가하세요."); return; }
  if (document.querySelector("dialog[open]")) { toast("현재 편집을 마친 뒤 자료를 추가하세요."); return; }
  state.files = [...files];
  if (!state.files.length) return;
  $("#upload-form").reset();
  uploadBundleId = '';
  renderUploadModes();
  renderUploadGroups(state.collection);
  if (state.category) $("#upload-form").elements.categories.value = state.category;
  $("#upload-files").textContent = `${state.files.length}개 파일 · ${state.files.slice(0, 3).map((file) => file.name).join(", ")}${state.files.length > 3 ? " 외" : ""}`;
  $("#upload-progress").textContent = "";
  $('#upload-meter').hidden = true;
  renderUploadFiles();
  $("#upload-dialog").showModal();
}

function formatSize(size) { return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${(size / 1024).toFixed(1)} KB`; }

function renderUploadModes(selected = 'separate') {
  const options = [['separate', '파일마다 각각 저장'], ['bundle', '여러 파일을 하나의 묶음으로 저장'],
    ...state.entries.filter((entry) => entry.type === 'bundle' && !entry.trashed).map((entry) => [entry.id, `기존 묶음에 추가: ${entry.title}`])];
  $('#upload-mode').replaceChildren(...options.map(([value, label]) => { const option = node('option', '', label); option.value = value; return option; }));
  $('#upload-mode').value = selected;
  $('#bundle-title-label').hidden = selected !== 'bundle';
  $('#bundle-title').required = selected === 'bundle';
}

function renderUploadGroups(selected = '') {
  const select = $('#upload-group');
  select.replaceChildren(...[['', '모음 없이 저장'], ['new', '+ 새 모음 만들기'], ...state.library.collections.map((group) => [group.id, group.name])].map(([id, name]) => {
    const option = node('option', '', name); option.value = id; return option;
  }));
  select.value = selected;
  $('#upload-group-name-label').hidden = select.value !== 'new';
  $('#upload-group-name').required = select.value === 'new';
}

function renderUploadFiles() {
  uploadPreviews.forEach((url) => URL.revokeObjectURL(url)); uploadPreviews = [];
  $('#upload-files').textContent = `${state.files.length}개 파일 · ${formatSize(state.files.reduce((sum, file) => sum + file.size, 0))}`;
  $('#upload-file-list').replaceChildren(...state.files.map((file, index) => {
    const row = node('div', 'upload-file'), art = node('span', 'upload-file-art', /\.(mp4|webm)$/i.test(file.name) ? '▶' : 'FILE');
    if (/\.(jpe?g|png|webp)$/i.test(file.name)) {
      const img = node('img'); img.src = URL.createObjectURL(file); img.alt = ''; uploadPreviews.push(img.src); art.replaceChildren(img);
    }
    const label = node('div', 'upload-file-label'); label.append(node('strong', '', file.name), node('small', '', formatSize(file.size)));
    const remove = node('button', 'upload-remove', '×'); remove.type = 'button'; remove.setAttribute('aria-label', `${file.name} 업로드 제외`);
    on(remove, 'click', () => { state.files.splice(index, 1); renderUploadFiles(); });
    row.append(art, label, remove); return row;
  }));
  $('#upload-form [type="submit"]').disabled = !state.files.length;
}

async function saveForm(form, path, dialog) {
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try { await post(path, formValues(form)); await reload(); dialog.close(); toast("저장했습니다."); }
  finally { button.disabled = false; }
}

function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = node("a"); link.href = url; link.download = filename; document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

on($("#upload-form"), "submit", async (event) => {
  event.preventDefault();
  if (state.busy || !state.files.length) return;
  state.busy = true;
  const controls = [...event.target.querySelectorAll("button,input,textarea,select")];
  const metadata = formValues(event.target);
  const uploadMode = $('#upload-mode').value;
  if (uploadMode !== 'separate') {
    if (uploadMode === 'bundle') uploadBundleId ||= crypto.randomUUID().replaceAll('-', '');
    metadata.uploadBundle = uploadMode === 'bundle' ? uploadBundleId : uploadMode;
  }
  if (metadata.uploadGroup === 'new') metadata.uploadGroup = crypto.randomUUID().replaceAll('-', '');
  else delete metadata.uploadGroupName;
  controls.forEach((control) => { control.disabled = true; });
  const failures = []; let added = 0; let duplicates = 0;
  try {
    $('#upload-meter').hidden = false; $('#upload-meter').max = state.files.length; $('#upload-meter').value = 0;
    for (let index = 0; index < state.files.length; index++) {
      const file = state.files[index];
      $("#upload-progress").textContent = `${index + 1}/${state.files.length} 저장 중: ${file.name}`;
      try {
        const limit = /\.(mp4|webm)$/i.test(file.name) ? 200 : 50;
        if (file.size > limit * 1024 * 1024) throw new Error(`파일당 최대 ${limit} MB입니다.`);
        const upload = new FormData();
        upload.append("metadata", JSON.stringify({ ...metadata, filename: file.name }));
        upload.append("file", file, file.name);
        const result = await post("/api/files", upload, true);
        if (result.duplicate) duplicates++; else added++;
      } catch (error) { failures.push({ file, message: `${file.name}: ${error.message}` }); }
      $('#upload-meter').value = index + 1;
    }
    await reload();
    if (metadata.uploadGroup && added + duplicates) renderUploadGroups(metadata.uploadGroup);
    if (metadata.uploadBundle && added + duplicates) renderUploadModes(metadata.uploadBundle);
    if (state.library.job.running) watchJob();
    if (failures.length) {
      state.files = failures.map((failure) => failure.file);
      renderUploadFiles();
      $("#upload-files").textContent = `실패한 ${failures.length}개 파일만 다시 시도합니다.`;
      $("#upload-progress").textContent = `${metadata.uploadBundle ? '묶음에 ' : ''}새 파일 ${added}개 · 중복 ${duplicates}개 저장\n${failures.map((failure) => failure.message).join("\n")}`;
      toast(`${failures.length}개 파일을 저장하지 못했습니다. 실패 목록을 확인하세요.`, true);
    } else {
      $("#upload-dialog").close(); state.files = [];
      if (metadata.uploadGroup) { resetFilters(); state.collection = metadata.uploadGroup; renderSaved(); renderSidebar(); render(); }
      if (metadata.uploadBundle) { resetFilters(); renderSidebar(); render(); openDetail(metadata.uploadBundle); }
      toast(metadata.uploadBundle ? `묶음 자료 1개에 파일을 저장했습니다.` : `새 자료 ${added}개 저장${duplicates ? ` · 중복 ${duplicates}개는 기존 자료에 분류만 추가` : ""}`);
    }
  } finally { state.busy = false; controls.forEach((control) => { control.disabled = false; }); }
});

on($('#upload-mode'), 'change', () => {
  uploadBundleId = '';
  $('#bundle-title-label').hidden = $('#upload-mode').value !== 'bundle';
  $('#bundle-title').required = $('#upload-mode').value === 'bundle';
  if ($('#bundle-title').required) $('#bundle-title').focus();
});
on($('#bundle-selected'), 'click', async () => {
  if (state.selected.size < 2) throw new Error('묶을 자료를 2개 이상 선택하세요.');
  if ([...state.selected].some((id) => entryFor(id).type === 'bundle')) throw new Error('이미 묶인 자료는 먼저 묶음을 풀어 주세요.');
  const title = (await ask('선택한 자료를 카드 하나로 보관합니다. 묶음 제목을 입력하세요.', ''))?.trim();
  if (!title) return;
  const result = await post('/api/bundle', {ids: [...state.selected], title});
  state.selected.clear(); await reload(); openDetail(result.id); toast('하나의 묶음 자료로 저장했습니다.');
});
on($('#split-bundle'), 'click', async () => {
  const id = $('#detail-form').elements.id.value;
  if (!await ask('묶음을 풀고 파일마다 별도 카드로 표시할까요? 공통 메모와 제목도 각 파일의 메모에 보관합니다. 편집 중인 내용은 먼저 저장하세요.')) return;
  await post('/api/bundle', {id, action: 'split'}); $('#detail-dialog').close(); await reload(); toast('묶음을 풀었습니다. 원본 파일은 유지됩니다.');
});

on($('#upload-group'), 'change', () => {
  const isNew = $('#upload-group').value === 'new';
  $('#upload-group-name-label').hidden = !isNew; $('#upload-group-name').required = isNew;
  if (isNew) $('#upload-group-name').focus();
});
on($('#sidebar-new-group'), 'click', () => createCollection([]));
for (const button of document.querySelectorAll('.job-dismiss')) on(button, 'click', () => setJobDismissed(true));
for (const button of document.querySelectorAll('.job-toggle')) on(button, 'click', () => setJobDismissed(!jobDismissed));
on($('#upload-dialog'), 'close', () => { uploadPreviews.forEach((url) => URL.revokeObjectURL(url)); uploadPreviews = []; });
on($('#detail-dialog'), 'close', () => { const video = $('#detail-preview video'); if (video) { video.pause(); video.removeAttribute('src'); video.load(); } });

on($("#link-form"), "submit", async (event) => { event.preventDefault(); await saveForm(event.target, "/api/links", $("#link-dialog")); });
on($("#detail-form"), "submit", async (event) => { event.preventDefault(); await saveForm(event.target, "/api/update", $("#detail-dialog")); });
on($("#detail-favorite"), "click", async () => {
  const id = $("#detail-form").elements.id.value;
  const item = entryFor(id);
  await post("/api/batch", { ids: [id], action: item.favorite ? "unfavorite" : "favorite" }); await reload();
  $("#detail-favorite").textContent = item.favorite ? "☆ 즐겨찾기" : "★ 즐겨찾기 해제";
});
on($("#detail-trash"), "click", async () => {
  const id = $("#detail-form").elements.id.value, item = entryFor(id);
  const action = item.trashed ? "restore" : "trash";
  if (action === "trash" && !await ask(`‘${item.title}’ 자료를 휴지통으로 이동할까요? 원본은 보관하며 이 화면에서 다시 복원할 수 있습니다.`)) return;
  const button = $("#detail-trash"); button.disabled = true;
  try { await post("/api/batch", { ids: [id], action }); await reload(); toast(action === "trash" ? "휴지통으로 이동했습니다. 원본은 보관됩니다." : "자료함으로 복원했습니다."); }
  finally { button.disabled = false; }
});

for (const button of document.querySelectorAll("[data-close]")) on(button, "click", async () => {
  if (state.busy) return;
  const dialog = button.closest("dialog");
  if (dialog.id === "manage-dialog") await waitForModeSettings();
  dialog.close();
});
for (const dialog of document.querySelectorAll("dialog")) {
  on(dialog, "cancel", async (event) => {
    if (state.busy || dialog.id === "manage-dialog") event.preventDefault();
    if (!state.busy && dialog.id === "manage-dialog") { await waitForModeSettings(); dialog.close(); }
  });
  on(dialog, "close", () => dialog.querySelector(".dialog-feedback")?.remove());
}
for (const button of document.querySelectorAll("[data-view]")) on(button, "click", () => { state.view = button.dataset.view; state.category = null; state.collection = ""; state.savedSearch = ""; state.selected.clear(); renderSaved(); renderSidebar(); render(); });
for (const selector of ["#search", "#exclude-tags", "#type-filter", "#match-mode", "#sort", "#date-from", "#date-to", "#date-group"]) on($(selector), "input", () => { state.savedSearch = ""; $("#saved-search").value = ""; render(); });
on($("#clear-filters"), "click", () => { resetFilters(); renderSaved(); renderSidebar(); render(); });
on($("#select-all"), "click", () => { state.selected = new Set(filteredItems().map((item) => item.id)); render(); });
on($("#clear-selection"), "click", () => { state.selected.clear(); render(); });
for (const button of document.querySelectorAll("[data-batch]")) on(button, "click", async () => {
  if (!state.selected.size) return;
  const action = button.dataset.batch;
  const tags = names($("#batch-tags").value), categories = names($("#batch-categories").value);
  if (["add", "remove"].includes(action) && !tags.length && !categories.length) { toast("키워드 또는 카테고리를 입력하세요."); return; }
  if (action === "trash" && !await ask(`선택한 ${state.selected.size}개 자료를 휴지통으로 이동할까요? 나중에 복원할 수 있습니다.`)) return;
  const result = await post("/api/batch", { ids: [...state.selected], action, tags, categories });
  await reload(); toast(`${result.changed}개 자료를 변경했습니다.`);
});
for (const selector of ["#add-images", "#empty-upload"]) on($(selector), "click", () => $("#file-input").click());
on($("#file-input"), "change", (event) => { prepareUpload(event.target.files); event.target.value = ""; });
on($("#add-link"), "click", () => { $("#link-form").reset(); if (state.category) $("#link-form").elements.categories.value = state.category; $("#link-dialog").showModal(); });
on($("#manage-button"), "click", () => $("#manage-dialog").showModal());
on($("#settings-button"), "click", async () => { $('#settings-dialog').showModal(); await refreshMaintenance(true); });
on($("#new-category"), "click", async () => {
  const name = (await ask("새 카테고리 이름을 입력하세요.", ""))?.trim(); if (!name) return;
  await post("/api/taxonomy", { kind: "categories", name }); await reload();
});
for (const [selector, kind] of [["#new-tag-form", "tags"], ["#new-category-form", "categories"]]) on($(selector), "submit", async (event) => {
  event.preventDefault(); await post("/api/taxonomy", { kind, name: event.target.elements.name.value }); event.target.reset(); await reload();
});
on($("#backup-button"), "click", async (event) => {
  event.target.disabled = true;
  try { await post('/api/maintenance', { operation: 'backup' }); await refreshMaintenance(); toast('지정한 폴더에 백업을 만들고 있습니다. 완료 후 ZIP 다운로드도 가능합니다.'); }
  catch (error) { event.target.disabled = false; throw error; }
});
on($("#metadata-button"), "click", () => {
  download(new Blob([JSON.stringify(state.library, null, 2)], { type: "application/json" }), "RefArchive-metadata.json");
  toast("메타데이터 다운로드를 요청했습니다. 원본 포함 백업은 ZIP을 사용하세요.");
});
on($("#restore-button"), "click", () => $("#restore-input").click());
on($("#restore-input"), "change", async (event) => {
  const file = event.target.files[0]; event.target.value = "";
  if (!file) return;
  if (!await ask(`‘${file.name}’의 내용으로 현재 자료함을 교체할까요? 현재 자료함은 교체 전에 자동 백업합니다.`)) return;
  const button = $("#restore-button"); button.disabled = true; state.busy = true;
  button.textContent = '복원 중… 창을 닫지 마세요';
  try { const result = await post("/api/restore", file, true); state.selected.clear(); await reload(); toast(`${result.items}개 자료 복원 완료. 이전 자료함의 안전 백업을 보관했습니다.`); }
  finally { button.disabled = false; button.textContent = '백업 ZIP 선택'; state.busy = false; }
});

let dragDepth = 0;
on(document, "dragenter", (event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); dragDepth++; $("#drop-overlay").hidden = false; } });
on(document, "dragover", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
on(document, "dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#drop-overlay").hidden = true; });
on(document, "drop", (event) => { event.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true; prepareUpload(event.dataTransfer.files); });
on(document, "paste", (event) => {
  if (document.querySelector("dialog[open]") || ["INPUT", "TEXTAREA"].includes(event.target.tagName)) return;
  const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
  if (files.length) { event.preventDefault(); prepareUpload(files); }
});
on($("#save-search"), "click", async () => {
  const current = state.library.searches.find((entry) => entry.id === state.savedSearch);
  const name = (await ask("검색 조건 이름을 입력하세요.", current?.name || ""))?.trim(); if (!name) return;
  const existing = state.library.searches.find((entry) => entry.name === name);
  if (existing && !await ask(`‘${name}’의 기존 검색 조건을 현재 조건으로 바꿀까요?`)) return;
  const result = await post("/api/saved", { kind: "search", name, filters: searchFilters(), ...(existing ? { id: existing.id } : {}) });
  state.savedSearch = result.id; await reload(); toast("검색 조건을 저장했습니다.");
});
on($("#saved-search"), "change", (event) => { const entry = state.library.searches.find((entry) => entry.id === event.target.value); if (entry) applySearch(entry); else state.savedSearch = ""; });
on($("#collection-filter"), "change", (event) => { state.collection = event.target.value; state.savedSearch = ""; state.selected.clear(); renderSaved(); renderSidebar(); render(); });
on($("#saved-manager-button"), "click", () => $("#saved-dialog").showModal());
on($("#new-collection"), "click", () => createCollection([...state.selected]));
on($("#empty-collection"), "click", () => createCollection([]));
for (const [selector, add] of [["#collection-add", true], ["#collection-remove", false]]) on($(selector), "click", async () => {
  let collection = state.library.collections.find((entry) => entry.id === state.collection);
  if (add) {
    const name = (await ask("자료를 추가할 기존 모음 이름을 입력하세요.", collection?.name || ""))?.trim();
    if (!name) return;
    collection = state.library.collections.find((entry) => entry.name === name);
  }
  if (!collection) throw new Error("모음을 먼저 선택하세요.");
  const selected = new Set(selectedFileIds());
  const ids = add ? [...new Set([...collection.ids, ...selected])] : collection.ids.filter((id) => !selected.has(id));
  await post("/api/saved", { ...collection, kind: "collection", ids }); await reload(); toast(add ? "모음에 자료를 추가했습니다." : "모음에서 자료를 제외했습니다. 원본은 유지됩니다.");
});
on($("#compare-button"), "click", () => {
  const selected = new Set(selectedFileIds());
  const items = state.library.items.filter((item) => selected.has(item.id));
  if (items.length < 2 || items.length > 6 || items.some((item) => item.type !== "image")) throw new Error("비교할 이미지를 2~6개만 선택하세요.");
  $("#compare-grid").replaceChildren(...items.map((item) => { const figure = node("figure"), image = node("img"); image.src = `/media/${item.id}`; image.alt = item.title; figure.append(image, node("figcaption", "", item.title)); return figure; }));
  $("#compare-dialog").showModal();
});
on($("#review-button"), "click", () => { renderReview(); renderScopeFilters(); $("#review-dialog").showModal(); });
on($("#review-filter"), "change", renderReview);
on($('#category-rule-form'), 'submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget, button = $('#category-rule-save');
  const terms = (field) => form.elements[field].value.split(/\r?\n/).map((term) => term.trim()).filter(Boolean);
  const rule = { mode: form.elements.mode.value, description: form.elements.description.value, include: terms('include'), exclude: terms('exclude') };
  button.disabled = true; button.dataset.saving = 'true';
  try {
    await post('/api/category-rule', { name: form.dataset.name, rule });
    $('#category-rule-dialog').close(); await reload();
    toast('카테고리 기준을 저장했습니다. 기존 자료는 분류 확인함에서 범위를 정해 다시 분류하세요.');
  } finally { button.dataset.saving = 'false'; button.disabled = state.library.job.running; }
});
on($('#text-rule-form'), 'submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget, button = $('#text-rule-save');
  const rule = { mode: form.elements.mode.value, match: form.elements.match.value,
    fields: [...form.querySelectorAll('[name="fields"]:checked')].map((input) => input.value),
    terms: form.elements.terms.value.split(/\r?\n/).map((term) => term.trim()).filter(Boolean) };
  button.disabled = true; button.dataset.saving = 'true';
  try {
    await post('/api/text-rule', { name: form.dataset.tag, rule });
    $('#text-rule-dialog').close(); await reload();
    toast('텍스트 규칙을 저장했습니다. 기존 자료는 분류 확인함에서 다시 분류하세요.');
  } finally { button.dataset.saving = 'false'; button.disabled = state.library.job.running; }
});
on($("#classify-preview"), "click", () => previewClassification());
for (const selector of ["#classify-scope", "#classify-tag", "#classify-category", "#classify-hours", "#classify-type"]) on($(selector), "input", () => { $("#classify-count").textContent = "조건이 변경되었습니다. 대상을 다시 확인하세요."; });
on($("#classify-all"), "click", () => runClassification(classificationScope()));
on($("#classify-selected"), "click", () => runClassification({ mode: "ids", ids: [...state.selected] }));
on($("#prepare-model"), "click", async () => { if (await ask("Hugging Face에서 CLIP 모델 약 88.6 MB를 내려받을까요? 준비 후 이미지 분석은 PC에서 수행합니다.")) await startAnalysis("model"); });
for (const [selector, ocr] of [["#analyze-text", false], ["#analyze-ocr", true]]) on($(selector), "click", async () => {
  const id = detailFileId, item = selectedFile();
  if (!ocr && item.type !== "pdf" && $("#detail-form").elements.note.value !== entryFor(id).note) throw new Error("메모 변경 내용을 먼저 저장한 뒤 분석하세요.");
  await startAnalysis("text", [id], ocr);
});
on($("#analysis-page"), "change", renderAnalysisPage);
on($("#save-analysis-text"), "click", async () => { await post("/api/analysis/edit", { id: detailFileId, page: Number($("#analysis-page").value), text: $("#analysis-text").value }); await reload(); toast("추출 텍스트와 요약을 갱신했습니다."); });
on($("#apply-analysis-tags"), "click", async () => {
  const tags = [...document.querySelectorAll("#analysis-keywords input:checked")].map((input) => input.value);
  if (!tags.length) throw new Error("추가할 키워드 후보를 선택하세요.");
  const id = $("#detail-form").elements.id.value;
  await post("/api/batch", { ids: [id], action: "add", tags }); await reload();
  $("#detail-form").elements.tags.value = entryFor(id).tags.join(", "); toast("선택한 키워드를 추가했습니다.");
});
on($("#detail-similar"), "click", async (event) => {
  const button = event.target; button.disabled = true;
  try {
    const result = await post("/api/similar", { id: detailFileId });
    const items = new Map(state.library.items.map((item) => [item.id, item]));
    $("#similar-list").replaceChildren(...(result.items.length ? result.items.map((match) => { const item = items.get(match.id), card = node("button"), image = node("img"); image.src = `/thumb/${item.id}`; image.alt = item.title; card.append(image, node("p", "", item.title), node("p", "", `유사도 ${match.score.toFixed(3)}`)); on(card, "click", () => { $("#similar-dialog").close(); $("#detail-dialog").close(); openDetail(item.id); }); return card; }) : [node("p", "dialog-description", "비교할 다른 이미지가 없습니다.")]));
    $("#similar-dialog").showModal();
  } finally { button.disabled = false; }
});
on($("#inspect-link"), "click", async (event) => {
  event.target.disabled = true;
  event.target.textContent = "이미지 찾는 중…";
  try {
    const metadata = formValues($("#link-form")), result = await post("/api/link/inspect", { source: metadata.source });
    $("#link-image-list").replaceChildren(...(result.images.length ? result.images.map((address) => {
      const row = node("div", "link-candidate"), save = node("button", "button secondary", "이미지 저장");
      on(save, "click", async () => { save.disabled = true; save.textContent = "저장 중…"; try { await post("/api/link/import", { ...metadata, image: address, source: result.source }); await reload(); if (state.library.job.running) watchJob(); save.textContent = "저장됨"; toast("이미지 원본을 저장했습니다."); } catch (error) { save.disabled = false; save.textContent = "이미지 저장"; throw error; } });
      row.append(node("span", "", address), save); return row;
    }) : [node("p", "dialog-description", "가져올 이미지 주소를 찾지 못했습니다. 로그인 또는 스크립트로 생성되는 페이지는 지원하지 않습니다.")]));
    $("#link-images-dialog").showModal();
  } finally { event.target.disabled = false; event.target.textContent = "링크 이미지 찾기"; }
});
for (const selector of ["#download-enabled", "#auto-summaries", "#auto-ocr", "#auto-ocr-language"]) on($(selector), "change", async () => {
  await post("/api/preferences", { downloads: $("#download-enabled").checked, summaries: $("#auto-summaries").checked, ocr: $("#auto-ocr").checked, language: $("#auto-ocr-language").value }); await reload();
});
on($("#shortcut-button"), "click", async () => { await post("/api/shortcut", {}); toast("바탕화면에 RefArchive 바로가기를 만들었습니다."); });
on($("#shutdown-button"), "click", async () => {
  if (!await ask("RefArchive 서버를 종료할까요? 저장한 자료는 유지됩니다. 다시 열려면 start.cmd 또는 바로가기를 실행하세요.")) return;
  await post("/api/shutdown", {}); clearTimeout(jobTimer); clearTimeout(maintenanceTimer); $("#connection-status").textContent = "프로그램 종료됨";
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  document.querySelectorAll("button,input,select,textarea").forEach((element) => { element.disabled = true; });
  toast("프로그램을 종료했습니다. 다시 실행하려면 start.cmd 또는 바탕화면 바로가기를 사용하세요.");
});
let maintenanceTimer, maintenanceState;
async function refreshMaintenance(fill = false) {
  clearTimeout(maintenanceTimer);
  try {
    const value = await (await api('/api/maintenance')).json(), previous = maintenanceState;
    maintenanceState = value;
    const label = value.operation === 'duplicates' ? '유사 중복 검사' : '백업';
    $('#maintenance-status').textContent = value.error || (value.running ? `${label} 진행 중 · ${value.done}/${value.total || '준비 중'}` : value.operation ? `${label} 완료` : '진행 중인 보관 작업이 없습니다.');
    $('#maintenance-status').classList.toggle('error', !!value.error);
    $('#maintenance-progress').hidden = !value.running;
    if (value.total) $('#maintenance-progress').value = value.done / value.total * 100;
    else $('#maintenance-progress').removeAttribute('value');
    $('#settings-button').textContent = value.running ? `보관 · ${label} 중` : '보관 · 백업';
    for (const id of ['#backup-button', '#backup-settings', '#duplicates-start', '#restore-button']) $(id).disabled = value.running || state.busy;
    $('#backup-download').hidden = !value.downloadReady;
    $('#last-backup').textContent = value.settings.lastBackup ? `마지막 성공: ${new Date(value.settings.lastBackup).toLocaleString('ko-KR')}\n${value.settings.lastFile}` : '완료한 백업이 없습니다.';
    $('#runtime-status').textContent = `RefArchive ${value.version} 실행 중 · 시작 ${new Date(value.started).toLocaleString('ko-KR')}`;
    if (fill) {
      $('#backup-folder').value = value.settings.folder; $('#backup-automatic').checked = value.settings.automatic;
      $('#pilot-checks').replaceChildren(...Object.entries(value.pilotChecks).map(([key, title]) => {
        const label = node('label', '', title), select = node('select'); select.dataset.check = key; select.setAttribute('aria-label', title);
        for (const [value, text] of [['pending', '미확인'], ['pass', '통과'], ['fail', '문제 있음']]) { const option = node('option', '', text); option.value = value; select.append(option); }
        select.value = value.settings.pilot?.checks?.[key] || 'pending'; label.append(select); return label;
      }));
      $('#pilot-status').textContent = value.settings.pilot?.saved ? `버전 ${value.settings.pilot.version} · ${new Date(value.settings.pilot.saved).toLocaleString('ko-KR')} 저장` : '아직 점검 결과를 저장하지 않았습니다.';
    }
    if (previous?.running && !value.running) toast(value.error || `${label} 완료${value.operation === 'duplicates' ? ` · 후보 ${value.pairs.length}쌍` : ' · ZIP을 지정 폴더에 저장했습니다.'}`, !!value.error);
    if ($('#duplicates-dialog').open && (JSON.stringify(previous?.pairs) !== JSON.stringify(value.pairs) || previous?.running !== value.running || previous?.done !== value.done)) renderDuplicates();
  } catch (error) {
    $('#maintenance-status').textContent = `상태 확인 실패: ${error.message}`;
    $('#runtime-status').textContent = '연결 상태를 확인하지 못했습니다. EXE 실행 여부를 확인하세요.';
  }
  maintenanceTimer = setTimeout(() => refreshMaintenance(), maintenanceState?.running ? 1000 : 15000);
}

function renderDuplicates() {
  const value = maintenanceState;
  if (!value || !state.library) return;
  $('#duplicates-summary').textContent = value.running && value.operation === 'duplicates' ? `검사 중 · ${value.done}/${value.total}` : `${value.pairs.length}쌍의 후보 · 정보가 적거나 읽지 못한 이미지 ${value.skipped}개 제외${value.limited ? ' · 200쌍 한도에 도달했습니다. 정리 후 다시 검사하세요.' : ''}`;
  $('#duplicates-list').replaceChildren(...value.pairs.map((pair) => {
    const row = node('article', 'duplicate-pair');
    for (const id of pair.ids) {
      const item = state.library.items.find((item) => item.id === id);
      if (!item) continue;
      const file = node('div', 'duplicate-file'), link = node('a'); link.href = `/media/${id}`; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.title = '원본을 새 탭에서 비교';
      const image = node('img'); image.src = `/thumb/${id}`; image.alt = item.title; image.loading = 'lazy'; link.append(image);
      const button = node('button', 'button danger', '이 파일만 휴지통으로'); button.disabled = value.running;
      const owner = entryFor(id);
      on(button, 'click', async () => {
        if (!await ask(`‘${item.title}’만 휴지통으로 옮길까요?${owner?.type === 'bundle' ? ' 묶음에서 이 파일만 분리하고 공통 메모를 보존합니다.' : ''} 다른 파일은 유지합니다.`)) return;
        button.disabled = true;
        try { await post('/api/duplicates/resolve', { keep: pair.ids.find((other) => other !== id), remove: id }); await reload(); await refreshMaintenance(); toast('선택 파일을 휴지통으로 옮겼습니다.'); }
        finally { button.disabled = false; }
      });
      file.append(link, node('h3', '', item.title), node('p', '', `${item.width} × ${item.height} · ${formatSize(item.size)}${owner?.type === 'bundle' ? ` · 묶음: ${owner.title}` : ''}`), button); row.append(file);
    }
    return row;
  }));
  if (!value.pairs.length) $('#duplicates-list').append(node('p', 'dialog-description', '표시할 후보가 없습니다. 검사 전이거나 조건에 맞는 이미지가 없는 상태입니다.'));
}

on($('#backup-settings'), 'click', async () => {
  await post('/api/backup/settings', { automatic: $('#backup-automatic').checked, folder: $('#backup-folder').value });
  await refreshMaintenance(true); toast('백업 설정을 저장했습니다.');
});
on($('#backup-download'), 'click', async () => {
  const result = await post('/api/backup/download', {}), link = node('a'); link.href = result.url; link.download = ''; document.body.append(link); link.click(); link.remove();
  toast('브라우저 다운로드 목록을 확인하세요. 백업 폴더에도 ZIP이 보관되어 있습니다.');
});
on($('#duplicates-start'), 'click', async () => {
  await post('/api/maintenance', { operation: 'duplicates' }); await refreshMaintenance();
  $('#duplicates-dialog').showModal(); renderDuplicates();
});
on($('#duplicates-open'), 'click', async () => { await reload(); await refreshMaintenance(); $('#duplicates-dialog').showModal(); renderDuplicates(); });
on($('#release-check'), 'click', () => $('#release-input').click());
on($('#release-input'), 'change', async (event) => {
  const file = event.target.files[0]; event.target.value = ''; if (!file) return;
  if (file.size > 65536) throw new Error('버전 정보 파일이 너무 큽니다. 업데이트 정보.json을 선택하세요.');
  let metadata;
  try { metadata = JSON.parse((await file.text()).replace(/^\uFEFF/, '')); } catch { throw new Error('JSON 버전 정보를 읽지 못했습니다.'); }
  const result = await post('/api/release/check', metadata);
  $('#release-result').textContent = `현재 ${result.current} · 받은 버전 ${result.incoming} — ${{ newer: '새 버전입니다. 백업 후 종료하고 새 EXE를 실행하세요.', same: '현재와 같은 버전입니다.', older: '이전 버전입니다. 자료 호환성을 위해 현재 버전을 유지하세요.' }[result.status]}`;
});
on($('#diagnostics-save'), 'click', async () => {
  const report = await post('/api/diagnostics', {});
  download(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }), `RefArchive-진단-${report.version}.json`);
  toast('진단 보고서 다운로드를 요청했습니다. 외부 전송은 하지 않습니다.');
});
on($('#pilot-save'), 'click', async () => {
  const checks = Object.fromEntries([...$('#pilot-checks').querySelectorAll('select')].map((select) => [select.dataset.check, select.value]));
  await post('/api/pilot', { checks }); await refreshMaintenance(true); toast('점검 결과를 저장했습니다. 진단 보고서에 함께 포함됩니다.');
});
reload().then(() => { if (state.library.job.running) watchJob(); refreshMaintenance(); }).catch((error) => { $("#connection-status").textContent = "연결 실패"; toast(error.message, true); });

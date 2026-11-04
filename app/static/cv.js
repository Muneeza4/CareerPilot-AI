const $ = (id) => document.getElementById(id);

const TABS = [
  ["all", "All"],
  ["contact", "Contact"],
  ["summary", "Summary"],
  ["education", "Education"],
  ["skill", "Skills"],
  ["experience", "Experience"],
  ["project", "Projects"],
  ["achievement", "Details"],
  ["certification", "Certifications"],
];

const state = { profile: null, tab: "all", editing: null };

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : "Something went wrong. Please try again.");
  }
  return data;
}

function sendJson(path, method, body) {
  return api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function showNotice(message, isError = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", isError);
  $("notice").hidden = !message;
}

async function run(action) {
  try {
    setProfile(await action());
  } catch (error) {
    showNotice(error.message, true);
  }
}

function setProfile(profile) {
  state.profile = profile;
  render();
}

function visibleFacts() {
  const facts = state.profile.facts;
  return state.tab === "all" ? facts : facts.filter((fact) => fact.type === state.tab);
}

function renderTabs() {
  const present = new Set(state.profile.facts.map((fact) => fact.type));
  const tabs = TABS.filter(([type]) => type === "all" || present.has(type)).map(([type, label]) => {
    const tab = element("button", "fact-tab", label);
    tab.type = "button";
    tab.setAttribute("aria-pressed", String(type === state.tab));
    tab.addEventListener("click", () => {
      state.tab = type;
      state.editing = null;
      render();
    });
    return tab;
  });
  $("fact-tabs").replaceChildren(...tabs);
}

function miniButton(label, onClick) {
  const button = element("button", "mini", label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function factDetail(fact) {
  const type = fact.label || TABS.find(([key]) => key === fact.type)[1];
  const origin = fact.source === "user_stated" ? "Added by you" : `CV page ${fact.page}`;
  const status = { extracted: "Needs review", confirmed: "Confirmed", rejected: "Rejected" }[fact.status];
  return `${type} · ${origin} · ${status}`;
}

function updateFact(fact, body) {
  run(() => sendJson(`/api/profile/facts/${fact.id}`, "PATCH", body));
}

function editRow(fact) {
  const row = element("li", "fact-row");
  const form = document.createElement("form");
  form.className = "fact-text";
  const input = element("input", "fact-edit");
  input.value = fact.value;
  input.maxLength = 1000;
  input.setAttribute("aria-label", "Edit fact");
  form.append(input);
  const save = () => {
    const value = input.value.trim();
    if (!value) return;
    state.editing = null;
    updateFact(fact, { value, status: "confirmed" });
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    save();
  });
  const actions = element("div", "fact-actions");
  actions.append(
    miniButton("Save", save),
    miniButton("Cancel", () => {
      state.editing = null;
      render();
    }),
  );
  row.append(form, actions);
  return row;
}

function factRow(fact) {
  if (state.editing === fact.id) return editRow(fact);

  const row = element("li", `fact-row${fact.status === "rejected" ? " rejected-fact" : ""}`);
  const text = element("div", "fact-text");
  text.append(element("span", "fact-value", fact.value), element("small", "", factDetail(fact)));

  const actions = element("div", "fact-actions");
  if (fact.status !== "confirmed") actions.append(miniButton("Confirm", () => updateFact(fact, { status: "confirmed" })));
  if (fact.status === "rejected") {
    actions.replaceChildren(miniButton("Restore", () => updateFact(fact, { status: "extracted" })));
  } else {
    actions.append(
      miniButton("Edit", () => {
        state.editing = fact.id;
        render();
      }),
    );
    if (fact.page) actions.append(miniButton("Source", () => openSource(fact)));
    actions.append(miniButton("Reject", () => updateFact(fact, { status: "rejected" })));
  }
  row.append(text, actions);
  return row;
}

function render() {
  const { facts, gaps, has_cv: hasCv } = state.profile;
  $("empty-state").hidden = hasCv;
  $("profile-view").hidden = !hasCv;
  if (!hasCv) return;

  const active = facts.filter((fact) => fact.status !== "rejected");
  const confirmed = active.filter((fact) => fact.status === "confirmed").length;
  $("progress-text").textContent = `${confirmed} of ${active.length} facts confirmed`;
  $("progress-bar").style.width = `${active.length ? (confirmed / active.length) * 100 : 0}%`;

  const items = gaps.length ? gaps.map((gap) => element("li", "", gap.message)) : [element("li", "", "Nothing needs attention.")];
  $("gap-list").replaceChildren(...items);

  if (state.tab !== "all" && !facts.some((fact) => fact.type === state.tab)) state.tab = "all";
  renderTabs();
  $("fact-list").replaceChildren(...visibleFacts().map(factRow));
}

function openSource(fact) {
  $("source-meta").textContent = `Page ${fact.page}: ${fact.value}`;
  $("source-image").src = `/api/profile/facts/${fact.id}/source`;
  $("source-dialog").showModal();
}

async function uploadCv(file) {
  if (!file) return;
  showNotice("");
  $("upload-label").classList.add("busy");
  $("upload-text").textContent = "Reading CV...";
  const form = new FormData();
  form.append("file", file);
  try {
    state.tab = "all";
    setProfile(await api("/api/profile/cv", { method: "POST", body: form }));
    showNotice("CV read successfully. Please review each fact.");
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    $("upload-label").classList.remove("busy");
    $("upload-text").textContent = "Upload CV";
    $("cv-file").value = "";
  }
}

function splitList(value) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function loadPreferences() {
  const preferences = await api("/api/preferences");
  $("pref-roles").value = preferences.target_roles.join(", ");
  $("pref-locations").value = preferences.locations.join(", ");
  $("pref-remote").value = preferences.remote_preference;
  $("pref-salary").value = preferences.salary_min ?? "";
  $("pref-currency").value = preferences.salary_currency ?? "";
}

async function savePreferences(event) {
  event.preventDefault();
  const salary = $("pref-salary").value;
  try {
    await sendJson("/api/preferences", "PUT", {
      target_roles: splitList($("pref-roles").value),
      locations: splitList($("pref-locations").value),
      remote_preference: $("pref-remote").value,
      salary_min: salary === "" ? null : Number(salary),
      salary_currency: $("pref-currency").value.trim() || null,
    });
    showNotice("Preferences saved.");
  } catch (error) {
    showNotice(error.message, true);
  }
}

function bindEvents() {
  $("cv-file").addEventListener("change", (event) => uploadCv(event.target.files[0]));
  $("preferences-form").addEventListener("submit", savePreferences);
  $("close-source").addEventListener("click", () => $("source-dialog").close());
  $("confirm-all").addEventListener("click", () => {
    const ids = visibleFacts().filter((fact) => fact.status === "extracted").map((fact) => fact.id);
    if (ids.length) run(() => sendJson("/api/profile/facts/status", "PUT", { ids, status: "confirmed" }));
  });
  $("add-fact-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const value = $("add-fact-value").value.trim();
    if (!value) return;
    const type = state.tab === "all" ? "skill" : state.tab;
    $("add-fact-value").value = "";
    run(() => sendJson("/api/profile/facts", "POST", { type, value }));
  });
}

async function init() {
  bindEvents();
  try {
    await loadPreferences();
    setProfile(await api("/api/profile"));
  } catch (error) {
    showNotice("Your profile could not be loaded. Check that the server is running, then reload.", true);
  }
}

init();

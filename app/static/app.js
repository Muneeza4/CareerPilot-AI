const $ = (id) => document.getElementById(id);

const filterInputs = {
  query: $("query"),
  company: $("company"),
  location: $("location"),
  days: $("days"),
  source: $("source"),
  remote_only: $("remote-only"),
  hide_senior: $("hide-senior"),
  search_description: $("search-description"),
};

const LOGO_COLORS = ["#2563eb", "#0d9488", "#c2410c", "#7c3aed", "#be185d", "#4d7c0f"];

const state = { page: 1, pages: 1, keywords: [], requestId: 0, jobs: new Map() };

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const error = new Error(`Request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function showNotice(message, isError = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", isError);
  $("notice").hidden = !message;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function formatPosted(value) {
  if (!value) return "";
  const days = Math.floor((Date.now() - new Date(`${value}T00:00:00Z`)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 30) return `${days} days ago`;
  return new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function sourceName(value) {
  const name = value.split(":")[0];
  return name === "careers" ? "Career page" : name[0].toUpperCase() + name.slice(1);
}

function logoColor(company) {
  let hash = 0;
  for (const character of company) hash = (hash * 31 + character.charCodeAt(0)) % 997;
  return LOGO_COLORS[hash % LOGO_COLORS.length];
}

function isWebUrl(value) {
  return /^https?:\/\//i.test(value);
}

function queryString() {
  const params = new URLSearchParams({ page: state.page });
  for (const [name, input] of Object.entries(filterInputs)) {
    const value = input.type === "checkbox" ? input.checked : input.value.trim();
    if (value) params.set(name, value);
  }
  return params;
}

function jobCard(job) {
  const card = element("article", "job-card");
  const logo = element("div", "logo", job.company.charAt(0).toUpperCase());
  logo.style.setProperty("--logo", logoColor(job.company));
  logo.setAttribute("aria-hidden", "true");

  const main = element("div", "job-main");
  const title = element("button", "job-title", job.title);
  title.type = "button";
  title.addEventListener("click", () => openDetail(job.id));
  main.append(
    title,
    element("div", "job-meta", [job.company, job.location].filter(Boolean).join(" · ")),
  );
  if (job.snippet) main.append(element("div", "job-snippet", job.snippet));

  const tags = element("div", "tags");
  if (job.is_remote) tags.append(element("span", "tag", "Remote"));
  const details = [formatPosted(job.posted_at), sourceName(job.source)].filter(Boolean).join(" · ");
  tags.append(element("span", "", details));
  if (isWebUrl(job.apply_url)) {
    const link = element("a", "job-link", "View job ↗");
    link.href = job.apply_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    tags.append(link);
  }
  main.append(tags);
  card.append(logo, main);
  return card;
}

function showSkeleton() {
  const list = $("job-list");
  if (list.children.length) return;
  for (let i = 0; i < 6; i += 1) list.append(element("div", "skeleton"));
}

function emptyState() {
  const empty = element("div", "empty");
  empty.append(
    element("strong", "", "No jobs match these filters"),
    "Try another keyword, clear a filter, or fetch new jobs.",
  );
  return empty;
}

async function loadJobs() {
  const requestId = ++state.requestId;
  showSkeleton();
  try {
    const data = await api(`/api/jobs?${queryString()}`);
    if (requestId !== state.requestId) return;

    state.pages = data.pages;
    $("job-list").replaceChildren(...(data.jobs.length ? data.jobs.map(jobCard) : [emptyState()]));
    $("count").textContent = `${data.total.toLocaleString()} ${data.total === 1 ? "job" : "jobs"}`;
    $("pagination").hidden = data.pages <= 1;
    $("page-label").textContent = `Page ${data.page} of ${data.pages}`;
    $("prev-page").disabled = data.page <= 1;
    $("next-page").disabled = data.page >= data.pages;
  } catch {
    if (requestId !== state.requestId) return;
    $("job-list").replaceChildren();
    $("count").textContent = "";
    showNotice("Jobs could not be loaded. Check that the server is running, then reload.", true);
  }
}

function applyFilters() {
  state.page = 1;
  loadJobs();
}

function renderKeywords() {
  const items = state.keywords.map((keyword) => {
    const item = element("li", "chip", keyword);
    const remove = element("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${keyword}`);
    remove.addEventListener("click", () => saveKeywords(state.keywords.filter((k) => k !== keyword)));
    item.append(remove);
    return item;
  });
  $("keyword-list").replaceChildren(...items);
}

async function saveKeywords(keywords) {
  try {
    const data = await api("/api/keywords", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keywords }),
    });
    state.keywords = data.keywords;
    renderKeywords();
    applyFilters();
  } catch {
    showNotice("Keywords could not be saved. Please try again.", true);
  }
}

async function loadFilterOptions() {
  const data = await api("/api/filters");
  $("company-options").replaceChildren(
    ...data.companies.map((company) => {
      const option = document.createElement("option");
      option.value = company;
      return option;
    }),
  );

  const selected = $("source").value;
  $("source").replaceChildren(new Option("All sources", ""), ...data.sources.map((name) => new Option(name.replace(":", ": "), name)));
  $("source").value = selected;

  if (data.last_refresh) {
    const when = new Date(`${data.last_refresh.replace(" ", "T")}Z`);
    $("last-refresh").textContent = `Updated ${when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}`;
  }
}

async function openDetail(id) {
  try {
    const job = await api(`/api/jobs/${id}`);
    $("detail-title").textContent = job.title;
    $("detail-meta").textContent = [job.company, job.location, formatPosted(job.posted_at)].filter(Boolean).join(" · ");
    $("detail-body").textContent = job.description || "No description available.";
    $("detail-body").scrollTop = 0;

    const safeUrl = isWebUrl(job.apply_url);
    $("detail-apply").hidden = !safeUrl;
    if (safeUrl) $("detail-apply").href = job.apply_url;
    $("detail").showModal();
  } catch {
    showNotice("This job could not be opened.", true);
  }
}

function setFetching(status) {
  const running = Boolean(status && status.running);
  $("fetch-button").disabled = running;
  $("progress").hidden = !running;
  if (running) {
    $("fetch-button").textContent = status.total ? `Fetching ${status.done} of ${status.total}` : "Fetching...";
    $("progress-bar").style.width = `${status.total ? (status.done / status.total) * 100 : 5}%`;
  } else {
    $("fetch-button").textContent = "Fetch new jobs";
  }
}

function finishedMessage(status) {
  if (status.error) return "Fetching failed. Please try again.";
  const count = status.new_jobs.toLocaleString();
  const message = `${count} new ${status.new_jobs === 1 ? "job" : "jobs"} saved.`;
  return status.failed.length ? `${message} These sources failed: ${status.failed.join(", ")}.` : message;
}

async function watchRefresh(announce) {
  let status = await api("/api/refresh");
  while (status.running) {
    setFetching(status);
    await new Promise((resolve) => setTimeout(resolve, 1000));
    status = await api("/api/refresh");
  }
  setFetching(status);
  await loadFilterOptions();
  await loadJobs();
  if (announce) showNotice(finishedMessage(status), Boolean(status.error || status.failed.length));
}

async function startRefresh() {
  showNotice("");
  try {
    await api("/api/refresh", { method: "POST" });
  } catch (error) {
    if (error.status !== 409) {
      showNotice("Fetching could not start. Please try again.", true);
      return;
    }
  }
  try {
    await watchRefresh(true);
  } catch {
    setFetching(null);
    showNotice("Lost contact with the server while fetching.", true);
  }
}

function toggleFilters(open) {
  $("sidebar").classList.toggle("open", open);
  $("scrim").hidden = !open;
}

function debounce(callback, delay = 300) {
  let timer;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(callback, delay);
  };
}

function bindEvents() {
  const debounced = debounce(applyFilters);
  for (const input of Object.values(filterInputs)) {
    input.addEventListener(input.matches("select, [type=checkbox]") ? "change" : "input", input.matches("select, [type=checkbox]") ? applyFilters : debounced);
  }

  $("keyword-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const keyword = $("keyword-input").value.trim();
    if (!keyword) return;
    $("keyword-input").value = "";
    saveKeywords([...state.keywords, keyword]);
  });

  $("clear-filters").addEventListener("click", () => {
    for (const input of Object.values(filterInputs)) {
      if (input.type === "checkbox") input.checked = false;
      else input.value = "";
    }
    applyFilters();
  });

  $("fetch-button").addEventListener("click", startRefresh);
  $("open-filters").addEventListener("click", () => toggleFilters(true));
  $("close-filters").addEventListener("click", () => toggleFilters(false));
  $("scrim").addEventListener("click", () => toggleFilters(false));
  $("close-detail").addEventListener("click", () => $("detail").close());
  $("detail").addEventListener("click", (event) => {
    if (event.target === $("detail")) $("detail").close();
  });

  const changePage = (step) => {
    state.page += step;
    loadJobs();
    window.scrollTo({ top: 0 });
  };
  $("prev-page").addEventListener("click", () => changePage(-1));
  $("next-page").addEventListener("click", () => changePage(1));
}

async function init() {
  bindEvents();
  try {
    state.keywords = (await api("/api/keywords")).keywords;
    renderKeywords();
    await Promise.all([loadJobs(), loadFilterOptions()]);
    // A scheduled refresh may already be running when the page opens.
    await watchRefresh(false);
  } catch {
    showNotice("The app could not load. Check that the server is running, then reload.", true);
  }
}

init();

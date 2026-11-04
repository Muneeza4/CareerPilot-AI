const $ = (id) => document.getElementById(id);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function entrySection(title, entries) {
  if (!entries.length) return [];
  const nodes = [element("h2", "", title)];
  for (const entry of entries) {
    const block = element("div", "paper-entry");
    const heading = element("h3", "", entry.title);
    if (entry.date) heading.append(element("span", "", ` (${entry.date})`));
    block.append(heading, ...entry.details.map((detail) => element("p", "", detail)));
    nodes.push(block);
  }
  return nodes;
}

function renderCv(cv) {
  const nodes = [
    element("h1", "paper-name", cv.name || ""),
    element("p", "paper-contact", cv.contact.join(" | ")),
  ];
  if (cv.summary) nodes.push(element("h2", "", "Summary"), element("p", "", cv.summary));
  if (cv.skills.length) {
    nodes.push(element("h2", "", "Skills"));
    for (const group of cv.skills) {
      const line = element("p");
      line.append(element("strong", "", `${group.name}: `), group.skills.join(", "));
      nodes.push(line);
    }
  }
  nodes.push(...entrySection("Experience", cv.experience));
  nodes.push(...entrySection("Projects", cv.projects));
  nodes.push(...entrySection("Education", cv.education));
  if (cv.certifications.length) {
    nodes.push(element("h2", "", "Certifications"), ...cv.certifications.map((item) => element("p", "", item)));
  }
  $("paper").replaceChildren(...nodes);
}

function showError(message) {
  $("job-name").textContent = "Your CV could not be prepared.";
  $("notice").textContent = message;
  $("notice").hidden = false;
}

function renderChecks(checks) {
  $("checks").replaceChildren(
    ...checks.map((check) => element("li", check.blocks ? "block" : check.ok ? "" : "warn", check.detail)),
  );
}

async function loadPackage(jobId) {
  const response = await fetch(`/api/package/${jobId}`);
  if (!response.ok) throw new Error();
  const draft = await response.json();
  $("letter").value = draft.letter;
  renderChecks(draft.checks);
  $("save-package").addEventListener("click", () => savePackage(jobId));
  $("package").hidden = false;
}

async function savePackage(jobId) {
  $("save-package").disabled = true;
  $("saved").textContent = "Saving...";
  try {
    const response = await fetch(`/api/package/${jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ letter: $("letter").value }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      $("saved").textContent = typeof body.detail === "string" ? body.detail : "The package could not be saved.";
      return;
    }
    renderChecks(body.checks);
    const link = element("a", "job-link", "Review it in Applications");
    link.href = "/applications";
    $("saved").replaceChildren(`Saved to ${body.folder}. `, link);
  } catch {
    $("saved").textContent = "The package could not be saved. Check that the server is running.";
  } finally {
    $("save-package").disabled = false;
  }
}

async function loadCv() {
  const jobId = new URLSearchParams(location.search).get("job");
  if (!/^\d+$/.test(jobId || "")) {
    showError("Choose a job from the Matches page first.");
    return;
  }
  try {
    const response = await fetch(`/api/tailor/${jobId}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      showError(body.detail || "Your CV could not be prepared. Reload to try again.");
      return;
    }
    const cv = await response.json();
    $("job-name").textContent = `For ${cv.job_title} at ${cv.company}`;
    renderCv(cv);
    $("changes").replaceChildren(...cv.changes.map((change) => element("li", "", change)));
    $("missing-part").hidden = !cv.missing_required.length;
    $("missing").replaceChildren(...cv.missing_required.map((skill) => element("span", "skill gap", skill)));
    if (/^https?:\/\//i.test(cv.apply_url)) {
      $("job-link").href = cv.apply_url;
      $("job-link").hidden = false;
    }
    $("download").href = `/api/tailor/${jobId}/cv.pdf`;
    $("download").hidden = false;
    $("tailor").hidden = false;
    await loadPackage(jobId);
  } catch {
    showError("Your CV could not be loaded. Check that the server is running, then reload.");
  }
}

loadCv();

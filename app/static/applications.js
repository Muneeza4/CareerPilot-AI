const $ = (id) => document.getElementById(id);

const COLUMNS = [
  ["Ready for review", ["ready"]],
  ["Approved", ["approved"]],
  ["Applied", ["applied"]],
  ["Interview", ["interview"]],
  ["Closed", ["rejected", "offer", "withdrawn"]],
];
const LABELS = {
  ready: "Ready for review",
  approved: "Approved",
  applied: "Applied",
  interview: "Interview",
  rejected: "Rejected",
  offer: "Offer",
  withdrawn: "Withdrawn",
};
const ACTIONS = {
  ready: [["Approve", "approved", true]],
  approved: [["I have applied", "applied", true], ["Back to review", "ready"]],
  applied: [["Interview", "interview", true], ["Rejected", "rejected"]],
  interview: [["Offer", "offer", true], ["Rejected", "rejected"]],
};
const CAN_WITHDRAW = ["ready", "approved", "applied", "interview"];

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showError(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

async function update(id, body) {
  try {
    const response = await fetch(`/api/applications/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      showError(typeof error.detail === "string" ? error.detail : "This change could not be saved.");
      return;
    }
    showError("");
    loadApplications();
  } catch {
    showError("This change could not be saved. Check that the server is running.");
  }
}

function fileLink(application, name, label) {
  const link = element("a", "job-link", label);
  link.href = `/api/applications/${application.id}/files/${name}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function actionButton(application, [label, status, primary]) {
  const button = element("button", `button${primary ? " primary" : ""}`, label);
  button.type = "button";
  button.addEventListener("click", () => update(application.id, { status }));
  return button;
}

function officialLink(application) {
  if (!/^https?:\/\//i.test(application.apply_url)) return null;
  const link = element("a", "button primary", "Open official page ↗");
  link.href = application.apply_url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function followUpField(application) {
  const label = element("label", "follow-up", "Follow up on");
  const input = document.createElement("input");
  input.type = "date";
  input.value = application.follow_up_on || "";
  input.addEventListener("change", () => update(application.id, { follow_up_on: input.value || null }));
  label.append(input);
  return label;
}

function card(application) {
  const node = element("article", "board-card");
  node.id = `application-${application.id}`;
  node.append(
    element("h3", "", application.title),
    element("p", "", application.company),
    element("small", "", LABELS[application.status]),
  );

  const files = element("div", "card-files");
  files.append(fileLink(application, "cv.pdf", "CV"), fileLink(application, "cover-letter.pdf", "Letter"));
  if (application.status === "ready" || application.status === "approved") {
    const edit = element("a", "job-link", "Edit");
    edit.href = `/tailor?job=${application.job_id}`;
    files.append(edit);
  }
  node.append(files);

  const actions = element("div", "card-actions");
  if (application.status === "approved") {
    const official = officialLink(application);
    if (official) actions.append(official);
  }
  actions.append(...(ACTIONS[application.status] || []).map((action) => actionButton(application, action)));
  if (actions.children.length) node.append(actions);
  if (application.status === "applied" || application.status === "interview") node.append(followUpField(application));

  const details = document.createElement("details");
  details.append(element("summary", "", "History"));
  const history = element("ul", "card-history");
  history.append(
    ...application.history.map((event) =>
      element("li", "", `${LABELS[event.status]} · ${event.happened_at.replace("T", " ").slice(0, 16)}`),
    ),
  );
  details.append(history);
  if (CAN_WITHDRAW.includes(application.status)) {
    details.append(actionButton(application, ["Withdraw", "withdrawn"]));
  }
  node.append(details);
  return node;
}

function attentionCard(item) {
  const node = element("div", "panel attention-item");
  const text = element("div");
  text.append(element("strong", "", item.title), element("p", "", item.detail));
  const open = element("a", "button", "Open");
  open.href = `#application-${item.application_id}`;
  node.append(element("span", "attention-dot"), text, open);
  return node;
}

function renderBoard(applications) {
  $("board").replaceChildren(
    ...COLUMNS.map(([title, statuses]) => {
      const items = applications.filter((application) => statuses.includes(application.status));
      const column = element("section", "column");
      const heading = element("h2", "", title);
      heading.append(element("span", "", items.length));
      column.append(heading, ...items.map(card));
      return column;
    }),
  );
}

async function loadApplications() {
  try {
    const response = await fetch("/api/applications");
    if (!response.ok) throw new Error();
    const data = await response.json();
    $("empty-board").hidden = data.applications.length > 0;
    $("board").hidden = data.applications.length === 0;
    $("attention").replaceChildren(...data.attention.map(attentionCard));
    $("attention").hidden = data.attention.length === 0;
    renderBoard(data.applications);
  } catch {
    showError("Applications could not be loaded. Check that the server is running, then reload.");
  }
}

loadApplications();

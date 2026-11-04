const $ = (id) => document.getElementById(id);

const COMPONENTS = [
  ["required", "Required skills"],
  ["preferred", "Preferred skills"],
  ["experience", "Experience"],
  ["constraints", "Role fit"],
];
const STRONG_SCORE = 60;

const state = { page: 1, pages: 1 };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function skillGroup(title, skills, className) {
  if (!skills.length) return null;
  const group = element("div", "skill-group");
  const row = element("div", "skill-row");
  row.append(...skills.map((skill) => element("span", `skill ${className}`, skill)));
  group.append(element("h3", "", title), row);
  return group;
}

function evidenceDetails(match) {
  const details = document.createElement("details");
  details.append(element("summary", "", "Why this score?"));

  const breakdown = element("div", "breakdown");
  for (const [key, label] of COMPONENTS) {
    const item = element("div");
    item.append(element("strong", "", `${match.components[key]}%`), label);
    breakdown.append(item);
  }
  details.append(breakdown);

  if (match.matched.length) {
    const list = element("ul", "evidence-list");
    for (const skill of match.matched) {
      const item = element("li");
      item.append(element("strong", "", skill.skill), ` · ${skill.strength}: ${skill.evidence}`);
      list.append(item);
    }
    details.append(list);
  }
  return details;
}

function matchCard(match) {
  const card = element("article", "match-card");

  const top = element("div", "match-top");
  const heading = element("div", "match-heading");
  heading.append(
    element("h2", "", match.title),
    element("p", "", [match.company, match.location, match.is_remote ? "Remote" : null].filter(Boolean).join(" · ")),
  );
  const ring = element("div", `score-ring${match.score < STRONG_SCORE ? " weak" : ""}`, match.score);
  ring.setAttribute("aria-label", `Match score ${match.score}`);
  top.append(heading, ring);

  const reasons = element("ul", "match-reasons");
  reasons.append(...match.reasons.map((reason) => element("li", "", reason)));

  card.append(top, reasons);
  const matched = skillGroup("You have", match.matched.map((skill) => skill.skill), "good");
  const missing = skillGroup("Missing required", match.missing_required, "gap");
  if (matched) card.append(matched);
  if (missing) card.append(missing);
  card.append(evidenceDetails(match));

  const foot = element("div", "match-foot");
  const tailor = element("a", "job-link", "Tailor CV");
  tailor.href = `/tailor?job=${match.job_id}`;
  foot.append(tailor);
  if (/^https?:\/\//i.test(match.apply_url)) {
    const link = element("a", "job-link", "View job ↗");
    link.href = match.apply_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    foot.append(link);
  }
  card.append(foot);
  return card;
}

async function loadMatches() {
  try {
    const response = await fetch(`/api/matches?page=${state.page}`);
    if (!response.ok) throw new Error();
    const data = await response.json();

    $("not-ready").hidden = data.ready;
    state.pages = data.pages;
    $("match-list").replaceChildren(...data.matches.map(matchCard));
    $("match-count").textContent = data.ready
      ? `${data.total.toLocaleString()} jobs ranked by fit with your confirmed facts`
      : "Your best jobs, with the reasons behind each score.";
    $("pagination").hidden = data.pages <= 1;
    $("page-label").textContent = `Page ${data.page} of ${data.pages}`;
    $("prev-page").disabled = data.page <= 1;
    $("next-page").disabled = data.page >= data.pages;
  } catch {
    $("notice").textContent = "Matches could not be loaded. Check that the server is running, then reload.";
    $("notice").hidden = false;
  }
}

function changePage(step) {
  state.page += step;
  loadMatches();
  window.scrollTo({ top: 0 });
}

$("prev-page").addEventListener("click", () => changePage(-1));
$("next-page").addEventListener("click", () => changePage(1));
loadMatches();

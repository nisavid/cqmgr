const variants = {
  A: { name: "A · Workload-first constraint explorer", render: renderWorkloadFirst },
  B: { name: "B · Evidence-first quota ledger", render: renderEvidenceFirst },
  C: { name: "C · Operation-first lifecycle workbench", render: renderOperationFirst },
};

const requestedVariant = new URLSearchParams(window.location.search).get("variant")?.toUpperCase();
const variant = variants[requestedVariant] ? requestedVariant : "A";
document.querySelector("#variant-name").textContent = variants[variant].name;
document.querySelector(`[data-variant="${variant}"]`).classList.add("active");
document.querySelector(`[data-variant="${variant}"]`).setAttribute("aria-current", "page");
document.querySelector("#app").innerHTML = variants[variant].render();

const planButton = document.querySelector("[data-action='preview']");
const planDrawerElement = document.querySelector("#plan-drawer");
if (planButton && planDrawerElement) {
  planButton.addEventListener("click", () => {
    planDrawerElement.hidden = false;
    planDrawerElement.scrollIntoView({ behavior: "smooth", block: "nearest" });
    planButton.textContent = "Plan preview refreshed";
  });
}

document.querySelectorAll("[data-select-row]").forEach((row) => {
  row.addEventListener("click", () => {
    document.querySelectorAll("[data-select-row]").forEach((candidate) => candidate.classList.remove("selected"));
    row.classList.add("selected");
  });
});

document.querySelectorAll("[data-operation]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-operation]").forEach((candidate) => candidate.classList.remove("active"));
    button.classList.add("active");
    const operation = button.dataset.operation;
    document.querySelector("#operation-title").textContent = button.textContent.trim();
    document.querySelector("#operation-copy").textContent = operation === "watch"
      ? "Follow preference reconciliation and effective-quota observations without collapsing them into one success state."
      : "Carry the selected exact slice and its evidence through this operation without changing target identity.";
  });
});

function heading(eyebrow, title, copy) {
  return `<header class="screen-heading"><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${copy}</p></header>`;
}

function planDrawer() {
  return `<section class="plan-drawer" id="plan-drawer" hidden>
    <div class="panel-title"><h3><span class="signal signal-warn">◆</span> Exact-slice plan · review required</h3><span>expires in 14:37 · single use</span></div>
    <div class="plan-grid">
      <div><span>Target slice</span><b>compute / GPUS-PER-GPU-FAMILY<br>region=us-central1 · H100</b></div>
      <div><span>Effective → desired</span><b>8 → 16 GPUs</b></div>
      <div><span>Preference</span><b>amend · etag 7cf…91a<br>existing desired 12</b></div>
      <div><span>Principal</span><b>ivan@example.com<br>no impersonation</b></div>
    </div>
    <div class="panel-body">
      <div class="notice"><strong>Companion warning.</strong> The all-regions H100 limit remains 12. This plan changes only the selected regional slice; no companion quota is changed implicitly.</div>
      <div class="actions"><button class="button primary" disabled>Apply unavailable in prototype</button><button class="button quiet">Export reviewable plan</button></div>
    </div>
  </section>`;
}

function constraintRows() {
  return `<div class="constraint-set" role="table" aria-label="Constraint set">
    <div class="constraint-row header" role="row"><span></span><span>Exact quota slice</span><span>Effective</span><span>Usage</span><span>Desired</span><span>State</span></div>
    <div class="constraint-row selected" role="row"><span class="signal signal-good">●</span><span class="identity">GPUS-PER-GPU-FAMILY<br>region=us-central1 · gpu_family=H100<span class="subtle">Compute Engine · regional · GPU</span></span><span>8</span><span>6</span><span>12</span><span class="state mutable">MUTABLE</span></div>
    <div class="constraint-row" role="row"><span class="signal signal-warn">◆</span><span class="identity">GPUS-ALL-REGIONS<br>gpu_family=H100<span class="subtle">Compute Engine · global · GPU</span></span><span>12</span><span>6</span><span>—</span><span class="state warning">BOTTLENECK</span></div>
    <div class="constraint-row" role="row"><span>○</span><span class="identity">PREEMPTIBLE-GPUS-PER-GPU-FAMILY<br>region=us-central1 · gpu_family=H100<span class="subtle">Separate quota pool</span></span><span>4</span><span>0</span><span>—</span><span class="state readonly">NOT USED</span></div>
  </div>`;
}

function renderWorkloadFirst() {
  return `${heading("Variant A · guided resolution", "Start from the workload", "Resolve a workload to its authoritative constraint set, then act on one exact slice without losing the whole picture.")}
  <div class="variant-a-layout">
    <nav class="panel" aria-label="Guided workflow"><div class="panel-title"><h2>Resolve workload</h2><span>3 / 5</span></div><ol class="steps">
      <li class="done"><span class="step-num">✓</span><span>Target and identity</span></li>
      <li class="done"><span class="step-num">✓</span><span>Management plane</span></li>
      <li class="active"><span class="step-num">03</span><span>Shape and mode</span></li>
      <li><span class="step-num">04</span><span>Constraint set</span></li>
      <li><span class="step-num">05</span><span>Plan and watch</span></li>
    </ol></nav>
    <section class="work-area">
      <div class="panel"><div class="panel-title"><h2>Workload requirement</h2><span>catalog snapshot · 2026-07-20</span></div><div class="panel-body">
        <div class="field-grid">
          <label>Management plane<select><option>Compute Engine</option><option>GKE</option><option>Cloud TPU API · legacy</option></select></label>
          <label>Accelerator<select><option>NVIDIA H100 80GB</option><option>NVIDIA A100 80GB</option><option>TPU v6e</option></select></label>
          <label>Shape and quantity<select><option>a3-highgpu-8g · 8 GPUs</option><option>a3-megagpu-8g · 8 GPUs</option></select></label>
          <label>Location<select><option>us-central1-a → region us-central1</option><option>us-east5-a → region us-east5</option></select></label>
        </div>
        ${constraintRows()}
        <div class="actions"><button class="button primary" data-action="preview">Preview exact-slice plan</button><button class="button">Copy equivalent CLI</button></div>
        <div class="command"><span class="prompt">$</span> cloud-quotas plan <span class="flag">--target-project</span> atlas-ml-dev <span class="flag">--slice</span> 'compute.googleapis.com/GPUS-PER-GPU-FAMILY-per-project-region?region=us-central1&amp;gpu_family=H100' <span class="flag">--desired</span> 16</div>
        ${planDrawer()}
      </div>
    </section>
    <aside class="evidence-rail"><div class="eyebrow">Pinned safety facts</div><ul class="fact-list">
      <li>Target project<b>atlas-ml-dev · #104281…</b><span>Explicit; not inferred from gcloud</span></li>
      <li>Acting principal<b>ivan@example.com</b><span>Direct user ADC · no impersonation</span></li>
      <li>Quota contact<b>operation input · verified human</b><span>Value excluded from plan and audit</span></li>
      <li>Provider evidence<b>QuotaInfo · 42 seconds old</b><span>Usage source · 61 seconds old</span></li>
      <li>Capacity caveat<b>Quota permits; capacity unknown</b></li>
    </ul></aside>
  </div>`;
}

function renderEvidenceFirst() {
  return `${heading("Variant B · provider ledger", "Inspect authoritative slices", "Browse every discovered slice first; guided meaning and mutation actions attach to provider evidence rather than replacing it.")}
  <div class="ledger-layout">
    <nav class="scope-tree" aria-label="Quota scopes"><div class="panel-title"><h2>Scope and service</h2><span>128 slices</span></div>
      <button class="active">▾ atlas-ml-dev</button><button>&nbsp;&nbsp;▾ Compute Engine <span class="subtle">84 slices</span></button><button>&nbsp;&nbsp;&nbsp;&nbsp;NVIDIA GPU <span class="subtle">guided · 18</span></button><button>&nbsp;&nbsp;&nbsp;&nbsp;TPU <span class="subtle">guided · 14</span></button><button>&nbsp;&nbsp;&nbsp;&nbsp;Other quota <span class="subtle">generic · 52</span></button><button>&nbsp;&nbsp;Cloud TPU API <span class="subtle">legacy · 11</span></button><button>&nbsp;&nbsp;Unknown services <span class="subtle">discovered · 33</span></button>
    </nav>
    <section class="ledger-workspace">
      <div class="ledger-toolbar"><button class="button">Filter: H100</button><button class="button">Region: all</button><button class="button">State: all</button><span class="grow"></span><span class="state mutable"><span class="signal signal-good">●</span>live provider evidence</span></div>
      <div><table class="ledger-table"><thead><tr><th>Quota ID and dimensions</th><th>Scope</th><th>Effective</th><th>Usage</th><th>Desired / granted</th><th>Catalog state</th><th>Lifecycle</th></tr></thead><tbody>
        <tr class="selected" data-select-row><td class="identity">GPUS-PER-GPU-FAMILY<br><span class="subtle">region=us-central1 · gpu_family=H100</span></td><td>regional</td><td>8 GPU</td><td>6 GPU</td><td>12 / 12</td><td class="state mutable">GUIDED · MUTABLE</td><td>settled</td></tr>
        <tr data-select-row><td class="identity">GPUS-ALL-REGIONS<br><span class="subtle">gpu_family=H100</span></td><td>global</td><td>12 GPU</td><td>6 GPU</td><td>—</td><td class="state mutable">GUIDED · MUTABLE</td><td>—</td></tr>
        <tr data-select-row><td class="identity">PREEMPTIBLE-GPUS-PER-GPU-FAMILY<br><span class="subtle">region=us-central1 · gpu_family=H100</span></td><td>regional</td><td>4 GPU</td><td>0 GPU</td><td>—</td><td class="state mutable">GUIDED · MUTABLE</td><td>—</td></tr>
        <tr data-select-row><td class="identity">custom-cloud-quotas-id<br><span class="subtle">region=us-central1 · provider_dimension=X9</span></td><td>regional</td><td>20 units</td><td>—</td><td>—</td><td class="state readonly">DISCOVERED</td><td>unknown</td></tr>
      </tbody></table></div>
      <div class="inspector"><section><div class="eyebrow">Selected exact slice</div><h2>H100 family · us-central1</h2><dl class="kv-grid"><dt>Canonical identity</dt><dd>atlas-ml-dev / compute.googleapis.com / GPUS-PER-GPU-FAMILY / region=us-central1,gpu_family=H100</dd><dt>Applicable locations</dt><dd>us-central1</dd><dt>Constraint set</dt><dd>regional 8 · all-regions 12 ◆ bottleneck at workload 16</dd><dt>Evidence</dt><dd>QuotaInfo 42s · usage metric 61s · complete</dd></dl></section><section><div class="eyebrow">Valid next operations</div><div class="actions"><button class="button">Diagnose workload</button><button class="button primary" data-action="preview">Preview amendment</button><button class="button">Watch settled preference</button></div><p class="subtle">Mutation acts on this one selected slice. Companion constraints remain independent.</p></section></div>
      ${planDrawer()}
    </section>
  </div>`;
}

function renderOperationFirst() {
  return `${heading("Variant C · lifecycle workbench", "Choose the operator action", "Use one operation vocabulary across TUI, CLI, and automation; each action carries an exact target and evidence envelope.")}
  <div class="operations">
    <nav class="operation-launcher" aria-label="Domain operations"><div class="eyebrow">Domain operations</div><h2>What do you need to do?</h2>
      <button class="button active" data-operation="diagnose">01 · Diagnose a workload</button><button class="button" data-operation="inspect">02 · Inspect an exact slice</button><button class="button" data-operation="plan">03 · Plan a preference</button><button class="button" data-operation="apply">04 · Apply a reviewed plan</button><button class="button" data-operation="watch">05 · Watch reconciliation</button><button class="button" data-operation="audit">06 · Verify audit continuity</button>
      <div class="command"><span class="prompt">$</span> cloud-quotas &lt;operation&gt;<br><span class="flag">--format</span> tui | text | json</div>
    </nav>
    <section class="operation-detail"><div class="eyebrow">Active operation</div><h2 id="operation-title">01 · Diagnose a workload</h2><p id="operation-copy">Carry the selected exact slice and its evidence through this operation without changing target identity.</p>
      <div class="phase-strip"><div class="phase done">✓ Select</div><div class="phase done">✓ Resolve</div><div class="phase active">◆ Review</div><div class="phase">○ Next action</div></div>
      <div class="panel"><div class="panel-title"><h3>Operation input</h3><span>same schema · TUI / CLI / JSON</span></div><div class="panel-body"><div class="field-grid"><label>Target<select><option>project · atlas-ml-dev</option></select></label><label>Workload kind<select><option>Compute Engine · NVIDIA GPU</option><option>GKE · TPU</option></select></label><label>Requirement<select><option>H100 80GB · 8 GPUs</option></select></label><label>Location<select><option>us-central1-a</option></select></label></div></div></div>
      <div class="artifact"><div class="eyebrow">Operation result · quota requirement</div><dl class="kv-grid"><dt>Selected slice</dt><dd>compute / GPUS-PER-GPU-FAMILY / us-central1 / H100</dd><dt>Required</dt><dd>8 GPUs · native unit preserved</dd><dt>Effective</dt><dd>8 GPUs · quota permits request</dd><dt>Companion</dt><dd>all-regions H100 · effective 12 · also permits</dd><dt>Capacity</dt><dd>unknown · quota is not physical availability</dd></dl></div>
      <div class="actions"><button class="button">Copy CLI operation</button><button class="button primary" data-action="preview">Continue to plan</button></div>
      ${planDrawer()}
    </section>
    <aside class="lifecycle-rail"><div class="eyebrow">Recent lifecycle</div><h2>Preference h100-uscentral1</h2><ol class="timeline"><li class="done">Previewed<small>plan 97f2…8ab · evidence fresh</small></li><li class="done">Submitted<small>provider preference accepted</small></li><li class="done">Reconciling<small>desired 12 · granted pending</small></li><li class="done">Preference settled<small>granted 12</small></li><li class="current">Effective-confirmed<small>QuotaInfo observed 12 · 2m ago</small></li></ol><div class="notice"><strong>Distinct systems.</strong> This timeline does not claim VM, queued-resource, or physical capacity state.</div></aside>
  </div>`;
}

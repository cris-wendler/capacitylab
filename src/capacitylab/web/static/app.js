// Crosshair + tooltip for server-rendered utilization panels. Called again after live updates replace the charts.
function initViz(root) {
  root.querySelectorAll("svg.viz").forEach((svg) => {
    if (svg.dataset.ready) return;
    svg.dataset.ready = "1";
    const values = JSON.parse(svg.dataset.values);
    const labels = JSON.parse(svg.dataset.labels);
    const left = +svg.dataset.left, right = +svg.dataset.right;
    const width = svg.viewBox.baseVal.width;
    const cross = svg.querySelector(".viz-crosshair");
    const panel = svg.closest(".panel");
    const tip = document.createElement("div");
    tip.className = "viz-tip";
    tip.hidden = true;
    panel.appendChild(tip);

    svg.addEventListener("mousemove", (event) => {
      const box = svg.getBoundingClientRect();
      const scale = width / box.width;
      const px = (event.clientX - box.left) * scale;
      const plot = width - left - right;
      const i = Math.max(0, Math.min(values.length - 1, Math.round(((px - left) / plot) * (values.length - 1))));
      const x = left + (plot * i) / Math.max(1, values.length - 1);
      cross.setAttribute("x1", x);
      cross.setAttribute("x2", x);
      cross.setAttribute("visibility", "visible");
      tip.textContent = `${labels[i]} · ${values[i]}% CPU`;
      tip.hidden = false;
      const panelBox = panel.getBoundingClientRect();
      tip.style.left = `${Math.min(panelBox.width - 110, Math.max(4, x / scale + (box.left - panelBox.left) + 8))}px`;
      tip.style.top = `${box.top - panelBox.top + 4}px`;
    });
    svg.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      tip.hidden = true;
    });
  });
}
initViz(document);

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// What-if sliders on the scenario page: recompute the options with the capacity model on the server.
(() => {
  const live = document.getElementById("options-live");
  const sliders = [...document.querySelectorAll("input[data-whatif]")];
  if (!live || !sliders.length) return;
  let timer = null;
  let request = 0;

  const refresh = () => {
    const params = new URLSearchParams();
    sliders.forEach((s) => { if (+s.value !== +s.dataset.default) params.set(s.name, s.value); });
    const id = ++request;
    live.classList.add("updating");
    fetch(`${live.dataset.src}?${params}`)
      .then((r) => (r.ok ? r.text() : Promise.reject(r.status)))
      .then((html) => {
        if (id !== request) return;
        live.innerHTML = html;
        initViz(live);
      })
      .catch(() => {})
      .finally(() => { if (id === request) live.classList.remove("updating"); });
  };
  const show = (slider) => {
    const row = slider.closest(".whatif-row");
    row.querySelector("output").textContent = `${(+slider.value).toFixed(1)}×`;
    const pct = ((slider.value - slider.min) / (slider.max - slider.min)) * 100;
    slider.style.setProperty("--fill", `${pct}%`);
    row.classList.toggle("changed", +slider.value !== +slider.dataset.default);
  };
  sliders.forEach((s) => {
    show(s);
    s.addEventListener("input", () => { show(s); clearTimeout(timer); timer = setTimeout(refresh, 140); });
  });
  document.querySelectorAll("[data-set]").forEach((b) => b.addEventListener("click", () => {
    const s = document.getElementById(`wi-${b.dataset.set}`);
    s.value = b.dataset.value;
    show(s);
    refresh();
  }));
  document.querySelector("[data-whatif-reset]")?.addEventListener("click", () => {
    sliders.forEach((s) => { s.value = s.dataset.default; show(s); });
    refresh();
  });
})();

// Round-by-round player on the run page.
(() => {
  const root = document.getElementById("player");
  const data = document.getElementById("player-data");
  if (!root || !data) return;
  const { run_id: runId, rounds } = JSON.parse(data.textContent);
  const tabs = [...root.querySelectorAll(".round-tab")];
  const bar = root.querySelector(".player-progress span");
  const playButton = root.querySelector('[data-player="play"]');
  let current = rounds.length - 1;
  let timer = null;

  const render = (index, animate) => {
    current = index;
    const step = rounds[index];
    tabs.forEach((t, i) => {
      t.classList.toggle("active", i === index);
      t.setAttribute("aria-selected", i === index ? "true" : "false");
    });
    bar.style.width = `${((index + 1) / rounds.length) * 100}%`;
    root.querySelectorAll(".agent-tile").forEach((tile) => {
      const turn = step.turns[tile.dataset.role];
      const field = (name) => tile.querySelector(`[data-field="${name}"]`);
      if (!turn) {
        field("label").textContent = "no turn this round";
        field("rationale").textContent = "";
        field("acts").innerHTML = "";
        field("changed").hidden = true;
        field("flags").hidden = true;
        return;
      }
      field("label").textContent = turn.label;
      const confidence = field("confidence");
      confidence.className = `conf conf-${turn.confidence}`;
      confidence.textContent = `${turn.confidence} confidence`;
      field("changed").hidden = !turn.shift;
      field("changed").textContent = turn.shift === "decided" ? "decided" : "changed position";
      field("flags").hidden = !turn.flags;
      field("flags").textContent = `${turn.flags} flagged`;
      field("rationale").textContent = turn.rationale;
      field("acts").innerHTML = [
        ...turn.challenges.map((c) => `<div class="act act-challenge">challenges the ${escapeHtml(c.target.toLowerCase())}</div>`),
        ...turn.requests.map((q) => `<div class="act act-request">asks for <span class="mono">${escapeHtml(q)}</span></div>`),
      ].join("");
      tile.classList.toggle("is-changed", turn.changed);
      if (animate && !reduceMotion) {
        tile.classList.remove("pop");
        void tile.offsetWidth;  // restart the animation
        tile.classList.add("pop");
      }
    });
    const feed = root.querySelector('[data-field="checks"]');
    feed.innerHTML = step.checks.length
      ? step.checks.map((c) => `<li><span class="pill ${c.status === "ok" ? "pill-ok" : "pill-warn"}">${escapeHtml(c.status.replace(/_/g, " "))}</span> `
          + `<span class="mono">${escapeHtml(c.tool)}</span> <span class="muted small">asked by ${escapeHtml(c.by.join(", "))}</span>`
          + (c.evidence_id ? ` → <a class="ev" href="/runs/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(c.evidence_id)}">${escapeHtml(c.evidence_id)}</a>` : "")
          + "</li>").join("")
      : '<li class="muted small">No checks ran after this round.</li>';
  };
  const stop = () => {
    clearInterval(timer);
    timer = null;
    playButton.innerHTML = "&#9654; Play";
  };
  const play = () => {
    if (timer) return stop();
    render(0, true);
    playButton.innerHTML = "&#10074;&#10074; Pause";
    timer = setInterval(() => {
      if (current >= rounds.length - 1) return stop();
      render(current + 1, true);
    }, 2600);
  };
  tabs.forEach((t, i) => t.addEventListener("click", () => { stop(); render(i, true); }));
  root.querySelector('[data-player="prev"]').addEventListener("click", () => { stop(); render(Math.max(0, current - 1), true); });
  root.querySelector('[data-player="next"]').addEventListener("click", () => { stop(); render(Math.min(rounds.length - 1, current + 1), true); });
  playButton.addEventListener("click", play);
})();

// Show one agent's turns in the discussion.
(() => {
  const chips = [...document.querySelectorAll(".filter-chip")];
  if (!chips.length) return;
  chips.forEach((chip) => chip.addEventListener("click", () => {
    chips.forEach((c) => c.classList.toggle("active", c === chip));
    const role = chip.dataset.filter;
    document.querySelectorAll(".turn[data-role]").forEach((t) => { t.hidden = role !== "all" && t.dataset.role !== role; });
    if (role !== "all") document.querySelectorAll("details.round").forEach((d) => { d.open = true; });
  }));
})();

// Live job page: follow progress without reloading, then open the result.
(() => {
  const job = document.getElementById("job-live");
  if (!job) return;
  const log = job.querySelector(".job-steps");
  const poll = () => fetch(job.dataset.src)
    .then((r) => r.json())
    .then((state) => {
      log.innerHTML = state.messages.map((m, i) => `<li class="${i === state.messages.length - 1 && !state.done ? "current" : "done"}">${escapeHtml(m)}</li>`).join("");
      if (state.error) {
        job.classList.add("failed");
        job.querySelector(".job-title").textContent = "This job failed";
        const error = job.querySelector(".job-error");
        error.textContent = state.error;
        error.hidden = false;
        return;
      }
      if (state.done && state.target) {
        window.location.assign(state.target);
        return;
      }
      setTimeout(poll, 800);
    })
    .catch(() => setTimeout(poll, 2000));
  poll();
})();

// Gentle entrance for cards once they scroll into view.
(() => {
  if (reduceMotion || !("IntersectionObserver" in window)) return;
  const items = document.querySelectorAll(".card, .finding-card, .role-card, .panel, .agent-tile, .step");
  const observer = new IntersectionObserver((entries) => entries.forEach((e) => {
    if (e.isIntersecting) {
      e.target.classList.add("in");
      observer.unobserve(e.target);
    }
  }), { threshold: 0.08 });
  items.forEach((el, i) => {
    el.classList.add("reveal");
    el.style.setProperty("--delay", `${Math.min(i % 6, 5) * 45}ms`);
    observer.observe(el);
  });
})();

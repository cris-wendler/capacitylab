// Crosshair + tooltip for server-rendered utilization panels.
document.querySelectorAll("svg.viz").forEach((svg) => {
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

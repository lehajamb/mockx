/* ==========================================================================
   MockX Documentation — Shared JavaScript Utilities
   ========================================================================== */
(function (global) {
  "use strict";

  const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /** Return a promise that resolves after `ms` milliseconds (capped when reduced motion). */
  function wait(ms) {
    return new Promise((r) => setTimeout(r, REDUCE ? Math.min(ms, 40) : ms));
  }

  /** Append a styled log line to a container. */
  function logLine(container, tagText, tagClass, text) {
    const empty = container.querySelector(".log-feed-empty, .pq-log-empty");
    if (empty) empty.remove();
    const row = document.createElement("div");
    row.className = "log-line";
    const tag = document.createElement("span");
    tag.className = "log-tag" + (tagClass ? " " + tagClass : "");
    tag.textContent = tagText;
    const body = document.createElement("span");
    body.className = "log-text";
    body.innerHTML = text;
    row.appendChild(tag);
    row.appendChild(body);
    container.appendChild(row);
    container.scrollTop = container.scrollHeight;
  }

  /** Pulse an element's class briefly. */
  function pulse(el, cls) {
    const c = cls || "pulse";
    el.classList.remove(c);
    void el.offsetWidth;
    el.classList.add(c);
  }

  /** Create a state machine UI with clickable states. */
  function installStateMachine(containerId, states, onChange) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const list = container.querySelector(".state-list");
    const detail = container.querySelector(".state-detail");
    if (!list || !detail) return;

    const nodes = {};
    states.forEach((s) => {
      const btn = document.createElement("button");
      btn.className = "state-node" + (s.terminal ? " terminal" : "");
      btn.textContent = s.name;
      btn.addEventListener("click", () => {
        Object.values(nodes).forEach((n) => n.classList.remove("selected"));
        btn.classList.add("selected");
        if (onChange) onChange(s, detail);
      });
      list.appendChild(btn);
      nodes[s.key] = btn;
    });

    // Select first state by default
    const firstBtn = list.querySelector(".state-node");
    if (firstBtn) firstBtn.click();

    return { nodes, detail };
  }

  /** Build a log-feed container if it doesn't exist. */
  function ensureLogFeed(parentSelector) {
    const parent = document.querySelector(parentSelector);
    if (!parent) return null;
    let feed = parent.querySelector(".log-feed");
    if (!feed) {
      feed = document.createElement("div");
      feed.className = "log-feed";
      const empty = document.createElement("p");
      empty.className = "log-feed-empty";
      empty.textContent = "Press run to begin.";
      feed.appendChild(empty);
      parent.appendChild(feed);
    }
    return feed;
  }

  /** Reset a log feed to its initial empty state. */
  function resetLogFeed(feed, emptyText) {
    feed.innerHTML =
      '<p class="log-feed-empty">' + (emptyText || "Press run to begin.") + "</p>";
  }

  /** Animate a playhead position. */
  function movePlayhead(playheadEl, pct) {
    playheadEl.style.left = pct + "%";
    return wait(650);
  }

  /** Pop a pin (marker on a timeline). */
  function popPin(pinEl) {
    pinEl.classList.add("popped");
  }

  /** Reset all pins. */
  function resetPins(container) {
    container.querySelectorAll(".pq-pin").forEach((p) => p.classList.remove("popped"));
  }

  // Export
  global.MockX = global.MockX || {};
  Object.assign(global.MockX, {
    REDUCE,
    wait,
    logLine,
    pulse,
    installStateMachine,
    ensureLogFeed,
    resetLogFeed,
    movePlayhead,
    popPin,
    resetPins,
  });
})(window);
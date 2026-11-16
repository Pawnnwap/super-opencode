"""Global keyboard shortcuts for the console.

A single-operator tool driven from the keyboard: switching pages used to mean
a mouse trip to the rail, and every search box had to be clicked before it
would accept a character. The bindings mirror what a terminal user already
knows (``g`` + page letter), so they cost nothing to learn.

Implemented as a plain DOM listener rather than ``ui.keyboard``: the NiceGUI
event bridge did not fire reliably for keys typed while nothing was focused
(the common case on a page you just opened), and a shortcut that works only
sometimes is worse than none.

Navigation uses ``location.href``, matching the rail's own ``ui.link`` — same
full reload, so the two cannot disagree about what a page switch does.
"""

from __future__ import annotations

from nicegui import ui

_SHORTCUT_JS = """
<script>
(() => {
  if (window.__sopShortcuts) { return; }
  window.__sopShortcuts = true;
  const PAGES = { w: "/", r: "/run", h: "/harvest" };
  const LEADER_MS = 1200;
  let leaderAt = 0;

  document.addEventListener("keydown", (event) => {
    const target = event.target || {};
    const tag = (target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" ||
        target.isContentEditable) {
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) { return; }

    const key = (event.key || "").toLowerCase();
    const now = Date.now();

    if (leaderAt && now - leaderAt < LEADER_MS) {
      leaderAt = 0;
      const href = PAGES[key];
      if (href) { window.location.href = href; }
      return;
    }
    if (key === "g") { leaderAt = now; return; }
    leaderAt = 0;

    if (key === "/") {
      event.preventDefault();
      // "Focus the thing I would type into" — the first *visible* field on the
      // page. Targeting the board's search box specifically does not work:
      // it lives inside a collapsed expansion and Quasar does not render that
      // content until it is opened, so there is nothing to focus.
      const fields = [
        ...document.querySelectorAll(
          "input:not([type=hidden]):not([disabled]), textarea:not([disabled])"
        ),
      ];
      const field = fields.find((el) => el.offsetParent !== null);
      if (field) { field.focus(); field.select && field.select(); }
    } else if (key === "escape") {
      const el = document.activeElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
        el.value = "";
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }
  });
})();
</script>
"""


def install() -> None:
    """Attach the shortcut listener to the current page (idempotent in JS)."""
    ui.add_body_html(_SHORTCUT_JS)


__all__ = ["install"]

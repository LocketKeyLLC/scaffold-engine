import { el, mount } from "../util.js";

// Phase-1 placeholder factory. Each real view replaces this in its own phase.
export function placeholder(title, note) {
  return function render(container) {
    mount(
      container,
      el("div", { class: "view-header" }, el("h1", { text: title })),
      el(
        "div",
        { class: "card empty-state" },
        el("div", { class: "empty-icon", text: "🚧" }),
        el("p", { text: note || "Coming online in a later build phase." })
      )
    );
    return () => {};
  };
}

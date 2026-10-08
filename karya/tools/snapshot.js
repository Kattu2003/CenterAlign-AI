// Ref-labelled text snapshot of the visible page (plan §11.6, DECISIONS.md D2).
// Run via page.evaluate(SCRIPT, maxRows). Interactive elements get a fresh
// data-kref="eN" attribute; the model acts by ref ("click e12"), never by pixels.
// Returns {url, title, text, count}.
(maxRows) => {
  const out = [];
  let counter = 0;
  let line = "";
  let indent = "";
  document.querySelectorAll("[data-kref]").forEach((el) => el.removeAttribute("data-kref"));

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const quote = (s) => '"' + clean(s).replace(/"/g, "'").slice(0, 160) + '"';
  const flush = () => {
    const text = clean(line);
    if (text) out.push(indent + text);
    line = "";
  };
  const isVisible = (el) => {
    if (!(el instanceof Element) || el.hidden || el.getAttribute("aria-hidden") === "true") return false;
    if (typeof el.checkVisibility === "function" && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  };
  const INTERACTIVE = "a[href], button, input, select, textarea, [role=button], [role=link]";
  const isInteractive = (el) => el.matches(INTERACTIVE) && !(el.tagName === "INPUT" && el.type === "hidden");
  const assign = (el) => {
    counter += 1;
    const ref = "e" + counter;
    el.setAttribute("data-kref", ref);
    return ref;
  };
  const labelFor = (el) => {
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label && clean(label.innerText)) return clean(label.innerText);
    }
    const wrapping = el.closest("label");
    if (wrapping && clean(wrapping.innerText)) return clean(wrapping.innerText);
    for (const attr of ["aria-label", "placeholder", "title"]) {
      if (el.getAttribute(attr)) return clean(el.getAttribute(attr));
    }
    const prev = el.previousElementSibling;
    if (prev && clean(prev.innerText)) return clean(prev.innerText).slice(0, 60);
    return el.getAttribute("name") || "";
  };
  const describe = (el) => {
    const ref = assign(el);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    if (tag === "a" || role === "link") {
      const href = el.getAttribute("href") || "";
      return "[" + ref + "] link " + quote(el.innerText || el.getAttribute("aria-label") || href) + (href ? " -> " + href : "");
    }
    if (tag === "button" || role === "button" || (tag === "input" && ["submit", "button", "reset"].includes(el.type))) {
      return "[" + ref + "] button " + quote(el.innerText || el.value || el.getAttribute("aria-label") || "");
    }
    if (tag === "select") {
      const options = Array.from(el.options).map((o) => clean(o.text));
      const shown = options.slice(0, 30).map(quote).join(", ") + (options.length > 30 ? ", … " + (options.length - 30) + " more" : "");
      const selected = el.selectedIndex >= 0 ? clean(el.options[el.selectedIndex].text) : "";
      return "[" + ref + "] select " + quote(labelFor(el)) + " name=" + el.name + " value=" + quote(el.value) + " selected=" + quote(selected) + " options=[" + shown + "]";
    }
    if (tag === "textarea") {
      return "[" + ref + "] textarea " + quote(labelFor(el)) + " name=" + el.name + " value=" + quote(el.value);
    }
    const type = (el.type || "text").toLowerCase();
    if (type === "password") return "[" + ref + "] password " + quote(labelFor(el)) + " (use browser_login)";
    if (type === "checkbox" || type === "radio") {
      return "[" + ref + "] " + type + " " + quote(labelFor(el)) + " name=" + el.name + " checked=" + el.checked;
    }
    const kind = { date: "date", number: "number", email: "email", search: "searchbox", file: "file" }[type] || "textbox";
    return "[" + ref + "] " + kind + " " + quote(labelFor(el)) + " name=" + el.name + " value=" + quote(el.value);
  };
  const cellText = (cell) => {
    const parts = [];
    const visit = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          const text = clean(child.textContent);
          if (text) parts.push(text);
        } else if (child.nodeType === Node.ELEMENT_NODE && isVisible(child)) {
          if (isInteractive(child)) {
            parts.push("[" + assign(child) + "] " + clean(child.innerText || child.value || child.getAttribute("aria-label") || ""));
          } else {
            visit(child);
          }
        }
      }
    };
    visit(cell);
    return parts.join(" ").replace(/\|/g, "/");
  };
  const renderTable = (table) => {
    const rows = Array.from(table.rows).filter(isVisible);
    let header = null;
    let body = rows;
    if (rows.length && (rows[0].parentElement.tagName === "THEAD" || Array.from(rows[0].cells).every((c) => c.tagName === "TH"))) {
      header = rows[0];
      body = rows.slice(1);
    }
    const caption = table.caption ? clean(table.caption.innerText) : "";
    out.push(indent + "[table] " + (caption ? caption + " " : "") + "(" + body.length + " rows)");
    if (header) out.push(indent + "  | " + Array.from(header.cells).map((c) => clean(c.innerText)).join(" | ") + " |");
    body.slice(0, maxRows).forEach((row) => out.push(indent + "  | " + Array.from(row.cells).map(cellText).join(" | ") + " |"));
    if (body.length > maxRows) out.push(indent + "  … " + (body.length - maxRows) + " more rows (use filters or browser_find)");
  };
  const BLOCK = new Set(["P", "DIV", "SECTION", "ARTICLE", "MAIN", "HEADER", "FOOTER", "NAV", "UL", "OL", "LI", "DL", "PRE", "BLOCKQUOTE", "FIELDSET", "LEGEND", "BR", "HR", "ASIDE", "FIGURE", "FIGCAPTION", "SPAN_BLOCK"]);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "HEAD", "CAPTION"]);

  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        line += " " + child.textContent;
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const el = child;
      const tag = el.tagName;
      if (SKIP.has(tag) || !isVisible(el)) continue;
      if (tag === "INPUT" && el.type === "hidden") continue;
      if (tag === "LABEL") {
        const target = el.htmlFor ? document.getElementById(el.htmlFor) : null;
        const inner = el.querySelectorAll("input, select, textarea");
        if (target || inner.length) {
          // The label's text names its input; describe the wrapped inputs instead of repeating it.
          inner.forEach((input) => {
            if (isVisible(input) && isInteractive(input)) {
              flush();
              out.push(indent + describe(input));
            }
          });
          continue;
        }
      }
      if (/^H[1-6]$/.test(tag)) {
        flush();
        out.push(indent + "[heading] " + clean(el.innerText));
        continue;
      }
      if (el.getAttribute("role") === "alert" || el.classList.contains("alert") || el.classList.contains("banner") || el.classList.contains("errors")) {
        flush();
        const items = el.querySelectorAll("li");
        if (items.length) items.forEach((li) => out.push(indent + "[alert] " + clean(li.innerText)));
        else out.push(indent + "[alert] " + clean(el.innerText));
        continue;
      }
      if (tag === "TABLE") {
        flush();
        renderTable(el);
        continue;
      }
      if (tag === "FORM") {
        flush();
        const method = (el.getAttribute("method") || "GET").toUpperCase();
        out.push(indent + "[form] " + method + " " + (el.getAttribute("action") || location.pathname));
        const saved = indent;
        indent += "  ";
        walk(el);
        flush();
        indent = saved;
        continue;
      }
      if (isInteractive(el)) {
        flush();
        out.push(indent + describe(el));
        continue;
      }
      if (tag === "DT") {
        flush();
        line = clean(el.innerText) + ":";
        continue;
      }
      if (tag === "DD") {
        walk(el);
        flush();
        continue;
      }
      if ((tag === "DIV" || tag === "PRE") && getComputedStyle(el).whiteSpace.startsWith("pre")) {
        flush();
        el.innerText.split("\n").forEach((text) => out.push(indent + text.trimEnd()));
        continue;
      }
      if (BLOCK.has(tag) || tag === "TR" || tag === "TD" || tag === "TH") {
        flush();
        walk(el);
        flush();
        continue;
      }
      walk(el);
    }
  };

  walk(document.body);
  flush();
  // Collapse runs of blank lines (from pre-formatted text).
  const lines = out.filter((text, i) => text.trim() !== "" || (i > 0 && out[i - 1].trim() !== ""));
  return { url: location.href, title: document.title, text: lines.join("\n"), count: counter };
}

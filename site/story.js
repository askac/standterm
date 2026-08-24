(() => {
  "use strict";

  const progress = document.querySelector(".progress");
  const article = document.querySelector("article.story");
  const readingTime = document.querySelector("[data-reading-time]");
  const wordCount = document.querySelector("[data-word-count]");

  if (article) {
    const words = (article.textContent || "")
      .trim()
      .split(/\s+/u)
      .filter(Boolean).length;
    if (wordCount) wordCount.textContent = words.toLocaleString("en-US");
    if (readingTime) readingTime.textContent = `${Math.max(1, Math.ceil(words / 220))} min`;
  }

  const updateProgress = () => {
    if (!progress) return;
    const root = document.documentElement;
    const distance = root.scrollHeight - root.clientHeight;
    const percent = distance > 0 ? (root.scrollTop / distance) * 100 : 0;
    progress.style.width = `${Math.min(100, Math.max(0, percent))}%`;
  };

  updateProgress();
  document.addEventListener("scroll", updateProgress, { passive: true });
  window.addEventListener("resize", updateProgress);

  const links = new Map(
    [...document.querySelectorAll(".toc a[href^='#']")].map((link) => [
      link.getAttribute("href").slice(1),
      link,
    ]),
  );

  if ("IntersectionObserver" in window && links.size) {
    const visible = new Map();
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) visible.set(entry.target.id, entry.boundingClientRect.top);
          else visible.delete(entry.target.id);
        });
        const current = [...visible.entries()].sort((a, b) => a[1] - b[1])[0]?.[0];
        if (!current) return;
        links.forEach((link, id) => {
          link.classList.toggle("active", id === current);
          if (id === current) link.setAttribute("aria-current", "location");
          else link.removeAttribute("aria-current");
        });
      },
      { rootMargin: "-15% 0px -70% 0px", threshold: [0, 0.2] },
    );
    links.forEach((_link, id) => {
      const section = document.getElementById(id);
      if (section) observer.observe(section);
    });
  }

  document.querySelectorAll(".terminal").forEach((terminal) => {
    const code = terminal.querySelector("code");
    if (!code || !navigator.clipboard) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-code";
    button.textContent = "copy";
    button.setAttribute("aria-label", "Copy terminal example");
    const status = document.createElement("span");
    status.className = "sr-only";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent || "");
        button.textContent = "copied";
        status.textContent = "Terminal example copied to the clipboard.";
      } catch {
        button.textContent = "select text";
        status.textContent = "Clipboard copy failed. Select the terminal text manually.";
      }
      window.setTimeout(() => {
        button.textContent = "copy";
        status.textContent = "";
      }, 1400);
    });
    terminal.appendChild(button);
    terminal.appendChild(status);
  });
})();

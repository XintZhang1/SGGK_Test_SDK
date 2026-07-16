"use strict";

(function installMarkdownPreview(root) {
  const HEADING_TAGS = Object.freeze(["h1", "h2", "h3", "h4", "h5", "h6"]);
  const FENCE = /^\s*```(?:\s*([A-Za-z0-9_+.-]+))?\s*$/;
  const FENCE_END = /^\s*```\s*$/;
  const HEADING = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
  const BULLET = /^\s*[-*+]\s+(.+)$/;
  const ORDERED = /^\s*\d+[.)]\s+(.+)$/;

  function textToken(value) {
    return { type: "text", value };
  }

  function parseInline(value) {
    const source = String(value || "");
    const tokens = [];
    let plain = "";
    let index = 0;

    function flushPlain() {
      if (!plain) return;
      tokens.push(textToken(plain));
      plain = "";
    }

    while (index < source.length) {
      if (source[index] === "`") {
        const closing = source.indexOf("`", index + 1);
        if (closing > index + 1) {
          flushPlain();
          tokens.push({ type: "code", value: source.slice(index + 1, closing) });
          index = closing + 1;
          continue;
        }
      }

      const marker = source.startsWith("**", index)
        ? "**"
        : (source.startsWith("__", index) ? "__" : "");
      if (marker) {
        const closing = source.indexOf(marker, index + marker.length);
        if (closing > index + marker.length) {
          flushPlain();
          tokens.push({
            type: "strong",
            value: source.slice(index + marker.length, closing),
          });
          index = closing + marker.length;
          continue;
        }
      }

      plain += source[index];
      index += 1;
    }

    flushPlain();
    return tokens;
  }

  function blockStart(line) {
    return !line.trim()
      || FENCE.test(line)
      || HEADING.test(line)
      || BULLET.test(line)
      || ORDERED.test(line);
  }

  function parse(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }

      const fence = line.match(FENCE);
      if (fence) {
        const body = [];
        index += 1;
        while (index < lines.length && !FENCE_END.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        blocks.push({
          type: "code_block",
          language: fence[1] || "",
          value: body.join("\n"),
        });
        continue;
      }

      const heading = line.match(HEADING);
      if (heading) {
        blocks.push({
          type: "heading",
          level: heading[1].length,
          inline: parseInline(heading[2]),
        });
        index += 1;
        continue;
      }

      const bullet = line.match(BULLET);
      const ordered = line.match(ORDERED);
      if (bullet || ordered) {
        const orderedList = Boolean(ordered);
        const matcher = orderedList ? ORDERED : BULLET;
        const items = [];
        while (index < lines.length) {
          const match = lines[index].match(matcher);
          if (!match) break;
          items.push(parseInline(match[1]));
          index += 1;
        }
        blocks.push({ type: "list", ordered: orderedList, items });
        continue;
      }

      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && !blockStart(lines[index])) {
        paragraph.push(lines[index].trim());
        index += 1;
      }
      blocks.push({
        type: "paragraph",
        inline: parseInline(paragraph.join(" ")),
      });
    }

    return blocks;
  }

  function appendInline(documentValue, parent, tokens) {
    tokens.forEach((token) => {
      if (token.type === "code") {
        const code = documentValue.createElement("code");
        code.textContent = token.value;
        parent.append(code);
        return;
      }
      if (token.type === "strong") {
        const strong = documentValue.createElement("strong");
        strong.textContent = token.value;
        parent.append(strong);
        return;
      }
      parent.append(documentValue.createTextNode(token.value));
    });
  }

  function renderInto(container, source) {
    if (!container || typeof container.replaceChildren !== "function") {
      throw new TypeError("Markdown preview container is invalid");
    }
    const documentValue = container.ownerDocument || root.document;
    if (!documentValue || typeof documentValue.createElement !== "function") {
      throw new TypeError("Markdown preview document is unavailable");
    }

    const article = documentValue.createElement("article");
    article.className = "markdown-preview";
    parse(source).forEach((block) => {
      if (block.type === "heading") {
        const heading = documentValue.createElement(HEADING_TAGS[block.level - 1]);
        appendInline(documentValue, heading, block.inline);
        article.append(heading);
        return;
      }
      if (block.type === "paragraph") {
        const paragraph = documentValue.createElement("p");
        appendInline(documentValue, paragraph, block.inline);
        article.append(paragraph);
        return;
      }
      if (block.type === "list") {
        const list = documentValue.createElement(block.ordered ? "ol" : "ul");
        block.items.forEach((item) => {
          const entry = documentValue.createElement("li");
          appendInline(documentValue, entry, item);
          list.append(entry);
        });
        article.append(list);
        return;
      }
      if (block.type === "code_block") {
        const pre = documentValue.createElement("pre");
        pre.className = "markdown-code-block";
        const code = documentValue.createElement("code");
        code.textContent = block.value;
        if (block.language) code.dataset.language = block.language;
        pre.append(code);
        article.append(pre);
      }
    });

    if (!article.childNodes.length) {
      const empty = documentValue.createElement("p");
      empty.className = "markdown-preview-empty";
      empty.textContent = "该 Markdown 报告为空。";
      article.append(empty);
    }
    container.replaceChildren(article);
    return article;
  }

  root.HarnessMarkdownPreview = Object.freeze({ parse, parseInline, renderInto });
})(typeof window === "undefined" ? globalThis : window);

import assert from "node:assert/strict";

await import("../ui/static/markdown-preview.js");

const preview = globalThis.HarnessMarkdownPreview;
assert.ok(preview, "renderer must install a public API");

const blocks = preview.parse([
  "# 结论",
  "",
  "这是 **通过** 的 `api_boolean` 测试。",
  "",
  "- 第一项",
  "- 第二项",
  "",
  "1. 先审查",
  "2. 再执行",
  "",
  "```cpp",
  "if (value < 0) return;",
  "```",
].join("\n"));

assert.deepEqual(blocks.map((block) => block.type), [
  "heading",
  "paragraph",
  "list",
  "list",
  "code_block",
]);
assert.equal(blocks[1].inline[1].type, "strong");
assert.equal(blocks[1].inline[3].type, "code");
assert.equal(blocks[2].ordered, false);
assert.equal(blocks[3].ordered, true);
assert.equal(blocks[4].language, "cpp");

class FakeNode {
  constructor(ownerDocument, tagName = "", text = "") {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName;
    this.nodeText = text;
    this.childNodes = [];
    this.className = "";
    this.dataset = {};
  }

  append(...children) {
    this.childNodes.push(...children);
  }

  replaceChildren(...children) {
    this.childNodes = [...children];
  }

  set textContent(value) {
    this.nodeText = String(value);
    this.childNodes = [];
  }

  get textContent() {
    return this.nodeText + this.childNodes.map((child) => child.textContent).join("");
  }
}

class FakeDocument {
  createElement(tagName) {
    return new FakeNode(this, String(tagName).toLowerCase());
  }

  createTextNode(value) {
    return new FakeNode(this, "#text", String(value));
  }
}

function tags(node) {
  return [node.tagName, ...node.childNodes.flatMap(tags)].filter((tag) => tag !== "#text");
}

const documentValue = new FakeDocument();
const container = new FakeNode(documentValue, "div");
const hostile = [
  "# <img src=x onerror=alert(1)>",
  "",
  "<script>alert('unsafe')</script> **safe** `code`",
  "",
  "```html",
  "<iframe src=javascript:alert(1)></iframe>",
  "```",
].join("\n");
const article = preview.renderInto(container, hostile);

assert.deepEqual(tags(article), ["article", "h1", "p", "strong", "code", "pre", "code"]);
assert.match(article.textContent, /<img src=x onerror=alert\(1\)>/);
assert.match(article.textContent, /<script>alert\('unsafe'\)<\/script>/);
assert.match(article.textContent, /<iframe src=javascript:alert\(1\)><\/iframe>/);
assert.ok(!tags(article).includes("script"));
assert.ok(!tags(article).includes("img"));
assert.ok(!tags(article).includes("iframe"));

console.log("markdown-preview tests passed");

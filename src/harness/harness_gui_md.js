// Markdown for the GUI, as a plain tree. No HTML is ever produced here: the page turns the tree
// into DOM nodes with textContent only, so whatever a model writes is characters, never markup.
//
// parseMarkdown(text) -> blocks:
//   {type: "code", text}
//   {type: "heading", level, inline}
//   {type: "list", ordered, items: [inline, ...]}
//   {type: "para", inline}
// inline = [{t: "text" | "strong" | "code", v}, ...]
//
// Supported on purpose: ``` fences (an unfinished fence is already code, so streaming looks right),
// # headings, - / * / 1. lists, **bold**, `code`. Not supported: links, images, tables, HTML.
(function (root) {
  "use strict";
  const LOOKAHEAD = 400;                       // a marker that does not close within this is plain text

  function inline(text) {
    const out = [];
    let buf = "", i = 0;
    const flush = () => { if (buf) { out.push({t: "text", v: buf}); buf = ""; } };
    while (i < text.length) {
      const c = text[i];
      if (c === "*" && text[i + 1] === "*") {
        const seg = text.slice(i + 2, i + 2 + LOOKAHEAD), j = seg.indexOf("**");
        if (j > 0 && seg.slice(0, j).indexOf("\n") < 0) { flush(); out.push({t: "strong", v: seg.slice(0, j)}); i += j + 4; continue; }
      } else if (c === "`") {
        const seg = text.slice(i + 1, i + 1 + LOOKAHEAD), j = seg.indexOf("`");
        if (j > 0 && seg.slice(0, j).indexOf("\n") < 0) { flush(); out.push({t: "code", v: seg.slice(0, j)}); i += j + 2; continue; }
      }
      buf += c; i++;
    }
    flush();
    return out;
  }

  const HEADING = /^(#{1,4})[ \t]+(.*)$/;
  const ITEM = /^[ \t]*([-*•]|[0-9]{1,3}[.)])[ \t]+(.*)$/;

  function textBlocks(text, blocks) {
    let para = [], list = null;
    const endPara = () => { if (para.length) { blocks.push({type: "para", inline: inline(para.join("\n"))}); para = []; } };
    const endList = () => { if (list) { blocks.push({type: "list", ordered: list.ordered, items: list.items.map(inline)}); list = null; } };
    for (let line of text.split("\n")) {
      line = line.replace(/\r$/, "");          // text that came through a Windows file or command
      if (line.trim() === "") { endPara(); endList(); continue; }
      let m = HEADING.exec(line);
      if (m) { endPara(); endList(); blocks.push({type: "heading", level: m[1].length, inline: inline(m[2])}); continue; }
      m = ITEM.exec(line);
      if (m) {
        endPara();
        const ordered = /^[0-9]/.test(m[1]);
        if (list && list.ordered !== ordered) endList();
        if (!list) list = {ordered, items: []};
        list.items.push(m[2]);
        continue;
      }
      if (list && /^[ \t]/.test(line)) { list.items[list.items.length - 1] += "\n" + line.trim(); continue; }   // a wrapped item
      endList();
      para.push(line);
    }
    endPara(); endList();
  }

  function parseMarkdown(text) {
    const blocks = [];
    String(text).split("```").forEach((part, k) => {
      if (k % 2 === 1) {                       // inside a fence: the first line is the language
        const nl = part.indexOf("\n");
        blocks.push({type: "code", text: nl >= 0 ? part.slice(nl + 1) : part});
      } else {
        textBlocks(part, blocks);
      }
    });
    return blocks;
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {parseMarkdown};
  else root.parseMarkdown = parseMarkdown;
})(typeof window !== "undefined" ? window : this);

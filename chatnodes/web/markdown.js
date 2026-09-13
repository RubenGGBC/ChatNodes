// Renderizador de Markdown minimo y sin dependencias.
// Cubre el subconjunto que usa Claude al explicar: titulos, listas, tablas,
// codigo, citas y enfasis. Determinista: el texto plano resultante es estable,
// que es lo que necesitan los anclajes de los nodos.

(function () {
  // Centinela imposible en texto normal: aparca el codigo inline mientras se
  // procesan negritas y cursivas, para que no se toquen entre si.
  const CODE_MARK = "\u0000";

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Segundo centinela, para las formulas: se aparcan igual que el codigo para
  // que el Markdown no les toque los guiones bajos ni los asteriscos.
  const MATH_MARK = String.fromCharCode(1);

  function inline(text) {
    const codes = [];
    let out = String(text).replace(/`([^`]+)`/g, (_m, code) => {
      codes.push(code);
      return CODE_MARK + (codes.length - 1) + CODE_MARK;
    });

    const maths = [];
    if (window.renderMath) {
      out = out.replace(/\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g, (whole, block, span) => {
        const body = block != null ? block : span;
        if (!window.looksLikeMath(body)) return whole;
        maths.push({ body: body, display: block != null });
        return MATH_MARK + (maths.length - 1) + MATH_MARK;
      });
    }

    out = escapeHtml(out);
    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img alt="$1" src="$2">');
    out = out.replace(
      /\[([^\]]+)\]\(([^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>'
    );
    out = out.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^\w*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^\w_])__([^_]+)__/g, "$1<strong>$2</strong>");
    out = out.replace(/(^|[^\w_])_([^_\n]+)_/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    out = out.replace(
      new RegExp(MATH_MARK + "(\\d+)" + MATH_MARK, "g"),
      (_m, index) => window.renderMath(maths[Number(index)].body, maths[Number(index)].display)
    );
    out = out.replace(
      new RegExp(CODE_MARK + "(\\d+)" + CODE_MARK, "g"),
      (_m, index) => "<code>" + escapeHtml(codes[Number(index)]) + "</code>"
    );
    return out;
  }

  const RE_FENCE = /^\s*(?:```|~~~)([\w+-]*)\s*$/;
  const RE_FENCE_END = /^\s*(?:```|~~~)\s*$/;
  const RE_HEADING = /^(#{1,6})\s+(.*)$/;
  const RE_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
  const RE_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const RE_QUOTE = /^\s*>\s?(.*)$/;
  const RE_TABLE_SEP = /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/;

  function cells(line) {
    let raw = line.trim();
    if (raw.startsWith("|")) raw = raw.slice(1);
    if (raw.endsWith("|")) raw = raw.slice(0, -1);
    return raw.split("|").map((cell) => cell.trim());
  }

  function render(source) {
    const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    const stack = [];
    let index = 0;

    function closeLists(depth) {
      while (stack.length > depth) out.push("</" + stack.pop() + ">");
    }

    while (index < lines.length) {
      const line = lines[index];

      const fence = line.match(RE_FENCE);
      if (fence) {
        closeLists(0);
        index += 1;
        const body = [];
        while (index < lines.length && !RE_FENCE_END.test(lines[index])) {
          body.push(lines[index]);
          index += 1;
        }
        index += 1;
        const cls = fence[1] ? ' class="lang-' + fence[1] + '"' : "";
        out.push("<pre><code" + cls + ">" + escapeHtml(body.join("\n")) + "</code></pre>");
        continue;
      }

      if (!line.trim()) {
        closeLists(0);
        index += 1;
        continue;
      }

      if (RE_HR.test(line) && !RE_ITEM.test(line)) {
        closeLists(0);
        out.push("<hr>");
        index += 1;
        continue;
      }

      const heading = line.match(RE_HEADING);
      if (heading) {
        closeLists(0);
        const level = heading[1].length;
        out.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        index += 1;
        continue;
      }

      if (RE_QUOTE.test(line)) {
        closeLists(0);
        const body = [];
        while (index < lines.length && RE_QUOTE.test(lines[index])) {
          body.push(lines[index].match(RE_QUOTE)[1]);
          index += 1;
        }
        out.push("<blockquote>" + render(body.join("\n")) + "</blockquote>");
        continue;
      }

      // Tabla: cabecera + separador de guiones.
      if (
        line.includes("|") &&
        index + 1 < lines.length &&
        RE_TABLE_SEP.test(lines[index + 1]) &&
        lines[index + 1].includes("-")
      ) {
        closeLists(0);
        const head = cells(line);
        index += 2;
        const body = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          body.push(cells(lines[index]));
          index += 1;
        }
        let table = "<div class='table-wrap'><table><thead><tr>";
        head.forEach((cell) => {
          table += "<th>" + inline(cell) + "</th>";
        });
        table += "</tr></thead><tbody>";
        body.forEach((row) => {
          table += "<tr>";
          for (let col = 0; col < head.length; col += 1) {
            table += "<td>" + inline(row[col] || "") + "</td>";
          }
          table += "</tr>";
        });
        out.push(table + "</tbody></table></div>");
        continue;
      }

      const item = line.match(RE_ITEM);
      if (item) {
        const depth = Math.min(Math.floor(item[1].replace(/\t/g, "  ").length / 2), 5);
        const tag = /^\d/.test(item[2]) ? "ol" : "ul";
        closeLists(depth + 1);
        while (stack.length < depth + 1) {
          stack.push(tag);
          out.push("<" + tag + ">");
        }
        if (stack[stack.length - 1] !== tag) {
          out.push("</" + stack.pop() + ">");
          stack.push(tag);
          out.push("<" + tag + ">");
        }
        const parts = [item[3]];
        index += 1;
        // Continuacion perezosa: lineas indentadas que no abren otro item.
        while (index < lines.length) {
          const next = lines[index];
          if (!next.trim() || RE_ITEM.test(next) || RE_FENCE.test(next) || RE_HEADING.test(next)) break;
          if (!/^\s{2,}/.test(next)) break;
          parts.push(next.trim());
          index += 1;
        }
        out.push("<li>" + inline(parts.join(" ")) + "</li>");
        continue;
      }

      closeLists(0);
      const paragraph = [];
      while (index < lines.length) {
        const next = lines[index];
        if (
          !next.trim() ||
          RE_FENCE.test(next) ||
          RE_HEADING.test(next) ||
          RE_ITEM.test(next) ||
          RE_QUOTE.test(next) ||
          RE_HR.test(next)
        ) {
          break;
        }
        paragraph.push(next.trim());
        index += 1;
      }
      out.push("<p>" + inline(paragraph.join(" ")) + "</p>");
    }

    closeLists(0);
    return out.join("");
  }

  window.renderMarkdown = render;
})();

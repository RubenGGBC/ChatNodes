// Renderizador de un subconjunto de LaTeX, suficiente para las formulas que
// aparecen en apuntes de carrera. Sin dependencias: funciona sin conexion.
// Traduce a HTML con CSS; lo que no reconoce lo deja legible en lugar de romper.

(function () {
  const GREEK = {
    alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
    zeta: "ζ", eta: "η", theta: "θ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ",
    nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", upsilon: "υ",
    phi: "φ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
    Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
    Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  };

  const SYMBOLS = {
    times: "×", cdot: "·", div: "÷", pm: "±", mp: "∓", ast: "∗",
    le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠",
    approx: "≈", equiv: "≡", sim: "∼", propto: "∝", infty: "∞",
    to: "→", rightarrow: "→", Rightarrow: "⇒", leftarrow: "←", Leftarrow: "⇐",
    leftrightarrow: "↔", mapsto: "↦",
    ldots: "…", cdots: "⋯", vdots: "⋮", dots: "…",
    sum: "∑", prod: "∏", int: "∫", oint: "∮", partial: "∂", nabla: "∇",
    in: "∈", notin: "∉", subset: "⊂", subseteq: "⊆", supset: "⊃",
    cup: "∪", cap: "∩", emptyset: "∅", forall: "∀", exists: "∃", neg: "¬",
    land: "∧", lor: "∨", oplus: "⊕", otimes: "⊗",
    lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉", langle: "⟨", rangle: "⟩",
    degree: "°", percent: "%", prime: "′",
  };

  // Operadores que se escriben rectos, no en cursiva.
  const WORDS = [
    "log", "ln", "lg", "exp", "sin", "cos", "tan", "max", "min", "lim", "sup",
    "inf", "det", "dim", "mod", "bmod", "gcd", "arg",
  ];

  const SPACES = {
    ",": " ", ";": " ", ":": " ", "!": "", " ": " ",
    quad: " ", qquad: "  ",
  };

  function esc(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function parse(src) {
    let i = 0;

    function group() {
      if (src[i] === "{") {
        i += 1;
        const inner = seq("}");
        if (src[i] === "}") i += 1;
        return inner;
      }
      return atom();
    }

    function command() {
      i += 1; // la barra
      const rest = src.slice(i);
      const match = /^[a-zA-Z]+/.exec(rest);
      if (!match) {
        const ch = src[i] || "";
        i += 1;
        if (ch === "\\") return "<br>";
        if (SPACES[ch] !== undefined) return SPACES[ch];
        return esc(ch);
      }
      const name = match[0];
      i += name.length;

      if (name === "frac" || name === "dfrac" || name === "tfrac") {
        const num = group();
        const den = group();
        return (
          '<span class="frac"><span class="fnum">' + num +
          '</span><span class="fden">' + den + "</span></span>"
        );
      }
      if (name === "sqrt") {
        return '<span class="sqrt">√<span class="rad">' + group() + "</span></span>";
      }
      if (name === "text" || name === "mathrm" || name === "operatorname" || name === "mathbf") {
        const cls = name === "mathbf" ? "upright bold" : "upright";
        return '<span class="' + cls + '">' + group() + "</span>";
      }
      if (name === "left" || name === "right") {
        const delim = src[i] || "";
        i += 1;
        if (delim === ".") return "";
        return '<span class="op">' + esc(delim) + "</span>";
      }
      if (SPACES[name] !== undefined) return SPACES[name];
      if (WORDS.includes(name)) return '<span class="upright">' + name + "</span>";
      if (GREEK[name]) return GREEK[name];
      if (SYMBOLS[name]) return '<span class="op">' + SYMBOLS[name] + "</span>";
      // Comando desconocido: mejor mostrar su nombre que perder la formula.
      return '<span class="upright">' + esc(name) + "</span>";
    }

    function atom() {
      const ch = src[i];
      if (ch === "\\") return command();
      if (ch === "{") {
        i += 1;
        const inner = seq("}");
        if (src[i] === "}") i += 1;
        return inner;
      }
      i += 1;
      return esc(ch);
    }

    function seq(stop) {
      let out = "";
      while (i < src.length && src[i] !== stop) {
        const ch = src[i];
        if (ch === "^" || ch === "_") {
          i += 1;
          const body = group();
          out += ch === "^" ? "<sup>" + body + "</sup>" : "<sub>" + body + "</sub>";
          continue;
        }
        if (ch === "&") {
          i += 1;
          out += " ";
          continue;
        }
        if (/[0-9]/.test(ch)) {
          const digits = /^[0-9]+(?:[.,][0-9]+)*/.exec(src.slice(i))[0];
          i += digits.length;
          out += '<span class="lit">' + digits + "</span>";
          continue;
        }
        if (/[+\-=<>/()[\],;:|]/.test(ch)) {
          i += 1;
          out += '<span class="op">' + esc(ch) + "</span>";
          continue;
        }
        out += atom();
      }
      return out;
    }

    return seq(undefined);
  }

  // Distingue una formula de un precio. La pista mas fiable es el espacio:
  // "cuesta $5 y luego $7" deja un hueco pegado al delimitador, y una formula
  // nunca lo hace. Despues, o trae sintaxis de LaTeX, o es un unico termino.
  function looksLikeMath(body) {
    if (!body || /^\s|\s$/.test(body)) return false;
    if (/[\\^_{}=]/.test(body)) return true;
    return body.length <= 24 && /^[0-9A-Za-z.,()+\-*/|<>]+$/.test(body);
  }

  window.renderMath = function (body, display) {
    const cls = display ? "math math-display" : "math";
    return '<span class="' + cls + '">' + parse(String(body).trim()) + "</span>";
  };
  window.looksLikeMath = looksLikeMath;
})();

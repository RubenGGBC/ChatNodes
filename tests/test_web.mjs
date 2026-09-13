// Pruebas del renderizador de Markdown y de formulas, sin navegador.
//   node tests/test_web.mjs
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";

const web = join(dirname(fileURLToPath(import.meta.url)), "..", "chatnodes", "web");

// Los ficheros son scripts de navegador: se evalúan con un window de mentira.
const window = {};
globalThis.window = window;
for (const file of ["math.js", "markdown.js"]) {
  new Function(readFileSync(join(web, file), "utf8"))();
}
const md = window.renderMarkdown;
const math = window.renderMath;

let pasados = 0;
function test(nombre, fn) {
  try {
    fn();
    pasados += 1;
  } catch (err) {
    console.error(`FALLO: ${nombre}\n  ${err.message}`);
    process.exitCode = 1;
  }
}

// Texto plano tal y como lo ve el DOM, que es lo que usan los anclajes.
const plano = (html) =>
  html
    .replace(/<[^>]+>/g, "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">");

/* ---------- Markdown ---------- */

test("parrafos y enfasis", () => {
  const html = md("Hola **mundo** y *algo* mas.");
  assert.equal(html, "<p>Hola <strong>mundo</strong> y <em>algo</em> mas.</p>");
});

test("titulos", () => {
  assert.equal(md("## La frecuencia"), "<h2>La frecuencia</h2>");
});

test("listas anidadas", () => {
  const html = md("- uno\n- dos\n  - dos.uno");
  assert.match(html, /<ul><li>uno<\/li><li>dos<\/li><ul><li>dos\.uno<\/li><\/ul><\/ul>/);
});

test("lista numerada", () => {
  assert.match(md("1. primero\n2. segundo"), /^<ol><li>primero<\/li><li>segundo<\/li><\/ol>$/);
});

test("bloque de codigo conserva el contenido", () => {
  const html = md("```python\nx = 1 < 2\n```");
  assert.match(html, /<pre><code class="lang-python">x = 1 &lt; 2<\/code><\/pre>/);
});

test("codigo en linea no se procesa como markdown", () => {
  const html = md("usa `a_b_c` y `**x**`");
  assert.match(html, /<code>a_b_c<\/code>/);
  assert.match(html, /<code>\*\*x\*\*<\/code>/);
  assert.doesNotMatch(html, /<em>/);
});

test("el html del texto se escapa", () => {
  assert.match(md("<script>alert(1)</script>"), /&lt;script&gt;/);
  assert.doesNotMatch(md("<script>alert(1)</script>"), /<script>/);
});

test("tablas", () => {
  const html = md("| a | b |\n|---|---|\n| 1 | 2 |");
  assert.match(html, /<table><thead><tr><th>a<\/th><th>b<\/th><\/tr>/);
  assert.match(html, /<td>1<\/td><td>2<\/td>/);
});

test("citas", () => {
  assert.match(md("> ojo con esto"), /<blockquote><p>ojo con esto<\/p><\/blockquote>/);
});

test("sin centinelas sueltos en la salida", () => {
  const html = md("texto con `codigo` y $x^2$ mezclados");
  assert.doesNotMatch(html, /[\u0000\u0001]/);
});

/* ---------- Formulas ---------- */

test("formula en linea", () => {
  const html = md("El tiempo es $T = N \\times CPI$ en segundos.");
  assert.match(html, /class="math"/);
  assert.match(plano(html), /T = N × CPI/);
});

test("exponentes y subindices", () => {
  const html = math("3 \\times 10^9", false);
  assert.match(html, /<sup>/);
  assert.match(plano(html), /3 × 109/); // el 9 va en <sup>
  assert.match(math("x_{i}", false), /<sub>/);
});

test("fracciones", () => {
  const html = math("\\frac{1}{f}", false);
  assert.match(html, /class="frac"/);
  assert.match(html, /class="fnum"/);
  assert.match(html, /class="fden"/);
});

test("raiz y letras griegas", () => {
  assert.match(plano(math("\\sqrt{2}", false)), /√2/);
  assert.match(plano(math("\\alpha + \\Omega", false)), /α \+ Ω/);
});

test("formula en bloque", () => {
  const html = md("$$T = \\frac{1}{f}$$");
  assert.match(html, /math-display/);
});

test("el dinero no se toma por formula", () => {
  const html = md("cuesta $5 y luego $7 mas");
  assert.doesNotMatch(html, /class="math"/);
  assert.match(plano(html), /cuesta \$5 y luego \$7 mas/);
});

test("un comando desconocido no rompe la formula", () => {
  const html = math("\\wibble{x} + 1", false);
  assert.match(plano(html), /wibble/);
  assert.match(plano(html), /x \+ 1/);
});

test("la formula no se come el guion bajo del markdown", () => {
  const html = md("la variable $v_0$ es inicial");
  assert.doesNotMatch(html, /<em>/);
  assert.match(html, /<sub>/);
});

test("el texto plano de una formula es estable", () => {
  // Los anclajes guardan offsets sobre este texto: debe ser determinista.
  const uno = plano(md("vale $a^2 + b^2$ aqui"));
  const dos = plano(md("vale $a^2 + b^2$ aqui"));
  assert.equal(uno, dos);
  assert.match(uno, /vale a2 \+ b2 aqui/);
});


/* ---------- heuristica de dolares ---------- */

test("un numero suelto entre dolares si es formula", () => {
  assert.match(md("ciclos de $0,33$ ns"), /class="math"/);
});

test("precios con texto entre medias no son formula", () => {
  for (const texto of ["cuesta $5 y luego $7 mas", "entre $10 y $20", "de $5 al mes y $7 al año"]) {
    assert.doesNotMatch(md(texto), /class="math"/, texto);
  }
});

test("el espacio pegado al dolar descarta la formula", () => {
  assert.doesNotMatch(md("paga $5 o $9"), /class="math"/);
  assert.match(md("vale $x=1$ aqui"), /class="math"/);
});

test("un texto largo entre dolares no se toma por formula", () => {
  assert.doesNotMatch(md("el $precio.final.sin.iva.ni.nada.de.nada$ sube"), /class="math"/);
});

console.log(`${pasados} pruebas de web pasadas`);

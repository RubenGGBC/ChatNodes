// ChatNodes — interfaz: hilo principal a la izquierda, nodos de aclaracion a la
// derecha, unidos por una linea al fragmento anclado.

const state = {
  projects: [],
  projectId: null,
  files: [],
  selectedFiles: null, // null = todo el material del proyecto
  chats: [],
  chatId: null,
  messages: [],
  nodes: [],
  busy: new Set(),     // claves de hilo con respuesta en curso
  sel: null,           // seleccion pendiente de convertirse en nodo
  resizing: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function toast(message, ms = 4200) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, ms);
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.body && !(init.body instanceof FormData)) {
    init.headers = { "Content-Type": "application/json", ...(init.headers || {}) };
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* sin cuerpo */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------- streaming SSE ---------- */

async function streamRequest(path, init, onEvent) {
  const res = await fetch(path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* sin cuerpo */ }
    throw new Error(detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      if (frame.startsWith("data: ")) {
        try { onEvent(JSON.parse(frame.slice(6))); } catch (_) { /* trama parcial */ }
      }
    }
  }
}

const streamPost = (path, body, onEvent) =>
  streamRequest(
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
    onEvent
  );

const streamGet = (path, onEvent) => streamRequest(path, { method: "GET" }, onEvent);

/* ---------- helpers de modelo ---------- */

const nodeById = (id) => state.nodes.find((n) => n.id === id) || null;
const messageById = (id) => {
  const found = state.messages.find((m) => m.id === id);
  if (found) return found;
  for (const node of state.nodes) {
    const hit = (node.messages || []).find((m) => m.id === id);
    if (hit) return hit;
  }
  return null;
};

function depthOf(node) {
  let depth = 0;
  let parent = node.parent_node_id;
  const seen = new Set([node.id]);
  while (parent && !seen.has(parent)) {
    seen.add(parent);
    const found = nodeById(parent);
    if (!found) break;
    depth += 1;
    parent = found.parent_node_id;
  }
  return depth;
}

function childrenOf(id) {
  return state.nodes.filter((n) => n.parent_node_id === id);
}

function subtreeSize(id) {
  return 1 + childrenOf(id).reduce((acc, child) => acc + subtreeSize(child.id), 0);
}

function ancestorCollapsed(node) {
  let parent = node.parent_node_id;
  const seen = new Set([node.id]);
  while (parent && !seen.has(parent)) {
    seen.add(parent);
    const found = nodeById(parent);
    if (!found) return true;
    if (found.collapsed) return true;
    parent = found.parent_node_id;
  }
  return false;
}

const isVisible = (node) => !node.collapsed && !ancestorCollapsed(node);

const anchorKey = (node) =>
  `${node.anchor_message_id}:${node.anchor_start}:${node.anchor_end}`;

const anchorEl = (node) =>
  document.querySelector(`mark.anchor[data-key="${CSS.escape(anchorKey(node))}"]`);

const cardOf = (id) => document.querySelector(`.card[data-id="${CSS.escape(id)}"]`);

const mdOf = (messageId) =>
  document.querySelector(`.md[data-mid="${CSS.escape(messageId)}"]`);

/* Orden de colocacion: profundidad primero, cada nivel ordenado por la posicion
   vertical real de su ancla en pantalla. */
function visibleOrder() {
  const topOf = (node) => {
    const anchor = anchorEl(node);
    return anchor ? anchor.getBoundingClientRect().top : node.seq * 1e4;
  };
  const walk = (parentId) =>
    childrenOf(parentId)
      .filter(isVisible)
      .sort((a, b) => topOf(a) - topOf(b))
      .flatMap((node) => [node, ...walk(node.id)]);
  return state.nodes
    .filter((n) => !n.parent_node_id && isVisible(n))
    .sort((a, b) => topOf(a) - topOf(b))
    .flatMap((node) => [node, ...walk(node.id)]);
}

/* ---------- anclajes en el DOM ---------- */

function resolveOffsets(root, group) {
  const full = root.textContent;
  if (full.slice(group.start, group.end) === group.text) {
    return { start: group.start, end: group.end };
  }
  const idx = full.indexOf(group.text);
  if (idx >= 0) return { start: idx, end: idx + group.text.length };
  return null;
}

function wrapOffsets(root, start, end, makeMark) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const pieces = [];
  let pos = 0;
  let node;
  while ((node = walker.nextNode())) {
    const len = node.nodeValue.length;
    const from = Math.max(start, pos);
    const to = Math.min(end, pos + len);
    if (from < to) pieces.push({ node, from: from - pos, to: to - pos });
    pos += len;
    if (pos >= end) break;
  }
  const marks = [];
  for (const piece of pieces) {
    const range = document.createRange();
    range.setStart(piece.node, piece.from);
    range.setEnd(piece.node, piece.to);
    const mark = makeMark();
    try {
      range.surroundContents(mark);
      marks.push(mark);
    } catch (_) { /* rango no envolvible: se omite ese trozo */ }
  }
  return marks;
}

function groupsFor(messageId) {
  const groups = new Map();
  for (const node of state.nodes) {
    if (node.anchor_message_id !== messageId) continue;
    const key = anchorKey(node);
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        start: node.anchor_start,
        end: node.anchor_end,
        text: node.anchor_text,
        nodes: [],
      });
    }
    groups.get(key).nodes.push(node);
  }
  return Array.from(groups.values());
}

function applyHighlights(root) {
  const messageId = root.dataset.mid;
  if (!messageId) return;
  const groups = groupsFor(messageId);
  if (!groups.length) return;
  groups.sort((a, b) => b.start - a.start);
  for (const group of groups) {
    const offsets = resolveOffsets(root, group);
    if (!offsets) continue;
    const marks = wrapOffsets(root, offsets.start, offsets.end, () => {
      const mark = el("mark", "anchor");
      mark.dataset.key = group.key;
      return mark;
    });
    if (!marks.length) continue;
    // El contador va en un ::after, asi que no altera el texto plano ni, por
    // tanto, los offsets de otros anclajes.
    const pin = el("button", "pin");
    const open = group.nodes.some((n) => !n.collapsed);
    if (open) pin.classList.add("open");
    pin.dataset.count = String(
      group.nodes.reduce((acc, n) => acc + subtreeSize(n.id), 0)
    );
    pin.dataset.group = group.nodes.map((n) => n.id).join(",");
    pin.title = open ? "Colapsar las aclaraciones" : "Abrir las aclaraciones";
    marks[marks.length - 1].after(pin);
  }
}

/* ---------- render ---------- */

function renderSidebar() {
  const projects = $("#projects");
  projects.innerHTML = "";
  for (const project of state.projects) {
    const li = el("li", project.id === state.projectId ? "active" : "");
    li.appendChild(el("span", "label", project.name));
    li.onclick = () => selectProject(project.id);
    projects.appendChild(li);
  }

  const project = state.projects.find((p) => p.id === state.projectId);
  const chat = state.chats.find((c) => c.id === state.chatId);
  const workspaceProject = $("#workspace-project");
  const workspaceChat = $("#workspace-chat");
  if (workspaceProject) workspaceProject.textContent = project ? project.name : "Espacio de estudio";
  if (workspaceChat) workspaceChat.textContent = chat ? chat.title : "Selecciona un chat";

  const detail = $("#project-detail");
  detail.hidden = !state.projectId;
  if (!state.projectId) return;

  const files = $("#files");
  files.innerHTML = "";
  if (!state.files.length) {
    files.appendChild(el("li", "none", "Sin archivos todavía"));
  }
  for (const file of state.files) {
    const li = el("li", "file-item");
    li.title = "Previsualizar archivo";
    li.appendChild(el("span", "label", file.name));
    li.appendChild(el("span", "meta", `${Math.max(1, Math.round(file.size / 1024))} KB`));
    const enabled = state.selectedFiles === null || state.selectedFiles.includes(file.name);
    const use = el("button", `file-use${enabled ? " enabled" : ""}`);
    use.type = "button";
    use.disabled = !state.chatId;
    use.title = enabled ? "Excluir de este chat" : "Incluir en este chat";
    use.setAttribute("aria-label", use.title);
    use.setAttribute("aria-pressed", String(enabled));
    use.onclick = (ev) => {
      ev.stopPropagation();
      setChatFileEnabled(file.name, !enabled);
    };
    li.appendChild(use);
    const x = el("button", "x", "×");
    x.title = "Quitar del proyecto";
    x.onclick = async (ev) => {
      ev.stopPropagation();
      await api(`/api/projects/${state.projectId}/files/${encodeURIComponent(file.name)}`, {
        method: "DELETE",
      });
      await loadFiles();
    };
    li.appendChild(x);
    li.onclick = () => openFilePreview(file);
    files.appendChild(li);
  }
  $("#use-all-files").hidden = state.selectedFiles === null || !state.chatId;

  const chats = $("#chats");
  chats.innerHTML = "";
  if (!state.chats.length) chats.appendChild(el("li", "none", "Sin chats"));
  for (const chat of state.chats) {
    const li = el("li", chat.id === state.chatId ? "active" : "");
    li.appendChild(el("span", "label", chat.title));
    const x = el("button", "x", "×");
    x.title = "Borrar chat";
    x.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm(`¿Borrar "${chat.title}"?`)) return;
      await api(`/api/chats/${chat.id}`, { method: "DELETE" });
      if (state.chatId === chat.id) state.chatId = null;
      await loadChats();
    };
    li.appendChild(x);
    li.onclick = () => selectChat(chat.id);
    chats.appendChild(li);
  }

  if (project) {
    $("#model").value = project.model || "sonnet";
    if ($("#instructions") !== document.activeElement) {
      $("#instructions").value = project.instructions || "";
    }
  }
}

function turnsOf(node) {
  const turns = [];
  for (const message of node.messages || []) {
    if (message.role === "user") turns.push({ question: message, answer: null });
    else if (turns.length) turns[turns.length - 1].answer = message;
    else turns.push({ question: null, answer: message });
  }
  return turns;
}

function answerBlock(message, busyKey) {
  const frag = document.createDocumentFragment();
  const md = el("div", "md selectable");
  md.dataset.mid = message.id;
  md.innerHTML = window.renderMarkdown(message.content || "");
  frag.appendChild(md);
  if (state.busy.has(busyKey)) {
    const status = el("div", "status");
    status.dataset.status = message.id;
    status.appendChild(el("span", "pulse"));
    status.appendChild(el("span", "label", message.content ? "escribiendo…" : "pensando…"));
    const stop = el("button", "stop", "parar");
    stop.title = "Cortar esta respuesta";
    stop.onclick = (ev) => {
      ev.preventDefault();
      stopTurn(busyKey);
    };
    status.appendChild(stop);
    frag.appendChild(status);
  }
  if (message.error) frag.appendChild(el("div", "err", message.error));
  if (message.cost_usd) {
    frag.appendChild(el("div", "cost", `$${Number(message.cost_usd).toFixed(4)}`));
  }
  return frag;
}

function renderThread() {
  const thread = $("#thread");
  thread.innerHTML = "";
  for (const message of state.messages) {
    const wrap = el("div", `msg ${message.role}`);
    wrap.dataset.id = message.id;
    wrap.appendChild(el("div", "who", message.role === "user" ? "Tú" : "Claude"));
    if (message.role === "user") {
      wrap.appendChild(el("div", "bubble", message.content));
    } else {
      wrap.appendChild(answerBlock(message, state.chatId));
    }
    thread.appendChild(wrap);
  }
  $$(".md", thread).forEach(applyHighlights);
}

function renderNodes() {
  const gutter = $("#gutter");
  gutter.innerHTML = "";
  const gutterWidth = gutter.clientWidth - 32;
  for (const node of state.nodes) {
    if (!isVisible(node)) continue;
    const depth = depthOf(node);
    const card = el("article", "card");
    card.dataset.id = node.id;
    card.dataset.depth = String(depth);
    card.style.left = `${depth * 22}px`;
    card.style.width = `${node.width || Math.max(250, gutterWidth - depth * 22)}px`;
    if (node.height) card.style.height = `${node.height}px`;

    const header = el("header");
    header.appendChild(el("button", "chev", "▾"));
    header.appendChild(el("span", "title", node.title || "Aclaración"));
    if (depth > 0) header.appendChild(el("span", "depth", `n${depth + 1}`));
    const huerfano = el("span", "orphan-note", "sin ancla");
    huerfano.title = "El texto al que estaba anclada esta aclaración ya no está";
    header.appendChild(huerfano);
    const del = el("button", "del", "×");
    del.title = "Borrar este nodo y los suyos";
    del.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm("¿Borrar esta aclaración y las que cuelgan de ella?")) return;
      await api(`/api/nodes/${node.id}`, { method: "DELETE" });
      await loadChat(state.chatId);
    };
    header.appendChild(del);
    header.onclick = () => toggleCollapse([node.id], true);
    card.appendChild(header);

    const body = el("div", "card-body");
    for (const turn of turnsOf(node)) {
      const block = el("div", "turn");
      if (turn.question) block.appendChild(el("div", "q", turn.question.content));
      if (turn.answer) block.appendChild(answerBlock(turn.answer, node.id));
      body.appendChild(block);
    }
    card.appendChild(body);

    const form = el("form", "card-composer");
    const input = el("textarea");
    input.rows = 1;
    input.placeholder = "Otra duda sobre esto…";
    form.appendChild(input);
    const send = el("button", "", "→");
    send.type = "submit";
    form.appendChild(send);
    form.onsubmit = (ev) => {
      ev.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      sendNodeTurn(node.id, text);
    };
    input.onkeydown = (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        form.requestSubmit();
      }
    };
    card.appendChild(form);

    card.addEventListener("pointerdown", (ev) => {
      // El tirador nativo de resize pertenece a la propia tarjeta; si el evento
      // viene de un hijo (el boton de enviar vive en esa esquina) no es resize.
      if (ev.target !== card) return;
      const rect = card.getBoundingClientRect();
      if (ev.clientX > rect.right - 20 && ev.clientY > rect.bottom - 20) {
        state.resizing = node.id;
      }
    });
    resizeWatcher.observe(card);
    $$(".md", card).forEach(applyHighlights);
    gutter.appendChild(card);
  }
}

const resizeWatcher = new ResizeObserver(() => scheduleLayout());

/* ---------- colocacion y cables ---------- */

let layoutQueued = false;
function scheduleLayout() {
  if (layoutQueued) return;
  layoutQueued = true;
  requestAnimationFrame(() => {
    layoutQueued = false;
    layout();
  });
}

function layout() {
  const inner = $("#board-inner");
  const gutter = $("#gutter");
  if (!inner || !gutter) return;
  const innerRect = inner.getBoundingClientRect();
  const gutterTop = gutter.getBoundingClientRect().top - innerRect.top;
  let cursor = 34;
  for (const node of visibleOrder()) {
    const card = cardOf(node.id);
    if (!card) continue;
    const anchor = anchorEl(node);
    card.classList.toggle("orphan", !anchor);
    let desired = cursor;
    if (anchor) {
      desired = anchor.getBoundingClientRect().top - innerRect.top - gutterTop - 8;
    }
    const top = Math.max(desired, cursor);
    card.style.top = `${top}px`;
    cursor = top + card.offsetHeight + 14;
  }
  gutter.style.minHeight = `${cursor + 60}px`;
  drawWires(innerRect);
}

/* Una tarjeta nueva puede nacer fuera de la vista, debajo de su padre: se trae
   a pantalla despues de que layout() le haya fijado la posicion. */
function revealNode(nodeId) {
  requestAnimationFrame(() => {
    const card = cardOf(nodeId);
    if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

function drawWires(innerRect) {
  const svg = $("#wires");
  const inner = $("#board-inner");
  if (!svg || !inner) return;
  const rect = innerRect || inner.getBoundingClientRect();
  svg.setAttribute("width", String(inner.clientWidth));
  svg.setAttribute("height", String(inner.scrollHeight));
  const parts = [
    `<defs><linearGradient id="wire-gradient" x1="0" y1="0" x2="1" y2="0">` +
      `<stop offset="0" stop-color="#58a6ff"/>` +
      `<stop offset="0.5" stop-color="#c45cff"/>` +
      `<stop offset="1" stop-color="#ff8a45"/>` +
    `</linearGradient></defs>`,
  ];
  for (const node of visibleOrder()) {
    const card = cardOf(node.id);
    const anchor = anchorEl(node);
    if (!card || !anchor) continue;
    const a = anchor.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    const x1 = a.right - rect.left + 3;
    const y1 = a.top - rect.top + a.height / 2;
    const x2 = c.left - rect.left;
    const y2 = c.top - rect.top + 17;
    const dx = Math.max(26, (x2 - x1) * 0.45);
    parts.push(
      `<path class="wire" d="M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}"/>`
    );
    parts.push(`<circle class="wire-dot" cx="${x2}" cy="${y2}" r="2.6"/>`);
  }
  svg.innerHTML = parts.join("");
}

/* ---------- carga de datos ---------- */

async function loadProjects() {
  state.projects = (await api("/api/projects")).projects;
  $("#empty").hidden = state.projects.length > 0;
  renderSidebar();
}

async function loadFiles() {
  state.files = (await api(`/api/projects/${state.projectId}/files`)).files;
  renderSidebar();
}

async function loadChats() {
  state.chats = (await api(`/api/projects/${state.projectId}/chats`)).chats;
  renderSidebar();
  if (!state.chats.length) {
    await newChat();
    return;
  }
  const wanted = state.chats.some((c) => c.id === state.chatId)
    ? state.chatId
    : state.chats[0].id;
  await selectChat(wanted);
}

async function loadChat(chatId) {
  const data = await api(`/api/chats/${chatId}`);
  state.chatId = chatId;
  state.messages = data.messages;
  state.nodes = data.nodes;
  state.selectedFiles = data.chat.selected_files;
  $("#composer").hidden = false;
  renderSidebar();
  renderThread();
  renderNodes();
  scheduleLayout();
  attachLiveTurns(chatId);
}

async function selectProject(projectId) {
  state.projectId = projectId;
  state.chatId = localStorage.getItem(`chatnodes.chat.${projectId}`);
  state.selectedFiles = null;
  localStorage.setItem("chatnodes.project", projectId);
  await loadFiles();
  await loadChats();
}

async function selectChat(chatId) {
  localStorage.setItem(`chatnodes.chat.${state.projectId}`, chatId);
  await loadChat(chatId);
}

async function newProject() {
  const name = prompt("Nombre del proyecto (p. ej. Sistemas Operativos)");
  if (name === null) return;
  const { project } = await api("/api/projects", {
    method: "POST",
    body: { name: name.trim() || "Nuevo proyecto" },
  });
  await loadProjects();
  await selectProject(project.id);
}

async function newChat() {
  const { chat } = await api(`/api/projects/${state.projectId}/chats`, {
    method: "POST",
    body: { title: "Nuevo chat" },
  });
  state.chats = (await api(`/api/projects/${state.projectId}/chats`)).chats;
  await selectChat(chat.id);
}

/* ---------- material del chat y previsualizacion ---------- */

async function setChatFileEnabled(name, enabled) {
  if (!state.chatId) return;
  const current = state.selectedFiles === null
    ? state.files.map((file) => file.name)
    : [...state.selectedFiles];
  const names = new Set(current);
  if (enabled) names.add(name);
  else names.delete(name);
  const selected = Array.from(names);
  const payload = selected.length === state.files.length ? null : selected;
  try {
    const result = await api(`/api/chats/${state.chatId}/files`, {
      method: "PUT",
      body: { names: payload },
    });
    state.selectedFiles = result.selected_files;
    renderSidebar();
    toast(enabled ? `${name} incluido en el chat` : `${name} excluido del chat`);
  } catch (err) {
    toast(`No se pudo cambiar el material: ${err.message}`);
  }
}

async function useAllFiles() {
  if (!state.chatId) return;
  try {
    await api(`/api/chats/${state.chatId}/files`, { method: "PUT", body: { names: null } });
    state.selectedFiles = null;
    renderSidebar();
    toast("Este chat vuelve a usar todo el material");
  } catch (err) {
    toast(`No se pudo cambiar el material: ${err.message}`);
  }
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function openFilePreview(file) {
  if (!state.projectId) return;
  const modal = $("#file-preview");
  const body = $("#preview-body");
  const url = `/api/projects/${state.projectId}/files/${encodeURIComponent(file.name)}/content`;
  $("#preview-title").textContent = file.name;
  $("#preview-meta").textContent = `${formatBytes(file.size)} · ${(file.suffix || "archivo").replace(".", "").toUpperCase()}`;
  $("#preview-open").href = url;
  body.innerHTML = "";
  modal.hidden = false;

  if (file.preview === "pdf") {
    const frame = el("iframe");
    frame.src = `${url}#view=FitH`;
    frame.title = `Vista previa de ${file.name}`;
    body.appendChild(frame);
  } else if (file.preview === "image") {
    const image = el("img");
    image.src = url;
    image.alt = file.name;
    body.appendChild(image);
  } else if (file.preview === "text") {
    body.appendChild(el("span", "preview-loading", "Cargando contenido…"));
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(response.statusText);
      const text = await response.text();
      body.innerHTML = "";
      body.appendChild(el("pre", "", text));
    } catch (err) {
      body.innerHTML = "";
      body.appendChild(el("div", "err", `No se pudo abrir: ${err.message}`));
    }
  } else {
    const fallback = el("div", "preview-fallback");
    fallback.innerHTML = `<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M14 5h14l8 8v30H14a4 4 0 0 1-4-4V9a4 4 0 0 1 4-4Z"/><path d="M28 5v9h8M17 29h14M17 35h10"/></svg>`;
    fallback.appendChild(el("h3", "", "Vista previa externa"));
    fallback.appendChild(el("p", "", "Este formato se abrirá con la aplicación instalada en tu equipo. Claude puede utilizarlo como material del proyecto."));
    const link = el("a", "primary", "Abrir documento");
    link.href = url;
    link.target = "_blank";
    fallback.appendChild(link);
    body.appendChild(fallback);
  }
}

function closeFilePreview() {
  $("#file-preview").hidden = true;
  // Quitar el iframe detiene el visor de PDF y libera el documento.
  $("#preview-body").innerHTML = "";
}

/* ---------- modos de estudio ---------- */

let studyMode = "flashcards";

function setStudyMode(mode) {
  studyMode = mode;
  const copy = {
    flashcards: ["Crear flashcards", "Tarjetas breves para memorizar los conceptos importantes y tus dudas."],
    quiz: ["Preparar un quiz", "Preguntas de opción múltiple, una cada vez, con marcador y explicación."],
    exam: ["Preparar un examen", "Responde todo antes de ver la corrección, la nota y los temas que debes repasar."],
  };
  $("#study-title").textContent = copy[mode][0];
  $("#study-description").textContent = copy[mode][1];
  $$(".study-mode-picker button").forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-checked", String(active));
  });
}

function openStudy(mode = "flashcards") {
  if (!state.chatId) {
    toast("Abre un chat antes de iniciar una sesion de estudio");
    return;
  }
  setStudyMode(mode);
  $("#study-modal").hidden = false;
}

function closeStudy() {
  $("#study-modal").hidden = true;
}

async function startStudy() {
  if (!state.chatId) return;
  const button = $("#study-start");
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Preparando…";
  try {
    const result = await api(`/api/chats/${state.chatId}/study`, {
      method: "POST",
      body: { mode: studyMode, amount: Number($("#study-amount").value) },
    });
    closeStudy();
    state.chats = (await api(`/api/projects/${state.projectId}/chats`)).chats;
    await selectChat(result.chat.id);
    await sendMain(result.prompt);
  } catch (err) {
    toast(`No se pudo iniciar el modo de estudio: ${err.message}`, 8000);
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ---------- laboratorio de codigo ---------- */

const labExamples = {
  python: `# Lee dos numeros desde la entrada estandar y muestra su suma\na = int(input())\nb = int(input())\nprint(f"La suma es: {a + b}")\n`,
  javascript: `// Lee dos numeros desde la entrada estandar y muestra su suma\nconst fs = require("fs");\nconst [a, b] = fs.readFileSync(0, "utf8").trim().split(/\\s+/).map(Number);\nconsole.log(\`La suma es: \${a + b}\`);\n`,
  c: `#include <stdio.h>\n\nint main(void) {\n    int a, b;\n    if (scanf("%d %d", &a, &b) != 2) return 1;\n    printf("La suma es: %d\\n", a + b);\n    return 0;\n}\n`,
  cpp: `#include <iostream>\nusing namespace std;\n\nint main() {\n    int a, b;\n    if (!(cin >> a >> b)) return 1;\n    cout << "La suma es: " << a + b << "\\n";\n    return 0;\n}\n`,
  java: `import java.util.Scanner;\n\npublic class Main {\n    public static void main(String[] args) {\n        Scanner input = new Scanner(System.in);\n        int a = input.nextInt();\n        int b = input.nextInt();\n        System.out.println("La suma es: " + (a + b));\n    }\n}\n`,
  prolog: `% Hechos y reglas: ejecuta el objetivo main/0\npadre(ana, luis).\npadre(luis, marta).\n\nabuelo(X, Z) :- padre(X, Y), padre(Y, Z).\n\nmain :-\n    abuelo(ana, Nieto),\n    format('Nieto de Ana: ~w~n', [Nieto]).\n`,
  clips: `; Sistema experto sencillo\n(deftemplate persona\n   (slot nombre)\n   (slot edad (type INTEGER)))\n\n(deffacts datos-iniciales\n   (persona (nombre Ana) (edad 20))\n   (persona (nombre Luis) (edad 17)))\n\n(defrule detectar-mayor-de-edad\n   (persona (nombre ?nombre) (edad ?edad&:(>= ?edad 18)))\n   =>\n   (printout t ?nombre " es mayor de edad" crlf))\n`,
};

let labLoaded = false;
let labLanguage = "python";

function labStorageKey(language) {
  return `chatnodes.lab.code.${language}`;
}

function updateLabEditor() {
  const code = $("#lab-code");
  const count = Math.max(1, code.value.split("\n").length);
  $("#lab-lines").textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
  $("#lab-code-size").textContent = `${code.value.length} caracteres`;
  const selected = $("#lab-language").selectedOptions[0];
  $("#lab-extension").textContent = selected ? selected.dataset.extension : "";
  localStorage.setItem(labStorageKey(labLanguage), code.value);
}

function loadLabCode(language, forceExample = false) {
  labLanguage = language;
  const stored = localStorage.getItem(labStorageKey(language));
  $("#lab-code").value = !forceExample && stored !== null
    ? stored
    : (labExamples[language] || "");
  if (forceExample || stored === null) {
    localStorage.setItem(labStorageKey(language), $("#lab-code").value);
  }
  updateLabEditor();
  $("#lab-output").innerHTML = '<span class="terminal-hint">La salida del programa aparecerá aquí.</span>';
  $("#lab-result-meta").textContent = "Lista para ejecutar";
  $(".terminal-panel").classList.remove("success", "failure");
}

async function loadLabLanguages() {
  const select = $("#lab-language");
  const status = $("#lab-runtime-status");
  try {
    const result = await api("/api/runner/languages");
    select.innerHTML = "";
    for (const language of result.languages) {
      const option = el("option", "", language.name);
      option.value = language.id;
      option.dataset.extension = language.extension;
      select.appendChild(option);
    }
    const remembered = localStorage.getItem("chatnodes.lab.language");
    select.value = result.languages.some((item) => item.id === remembered)
      ? remembered
      : (result.languages[0] && result.languages[0].id);
    labLanguage = select.value || "python";
    status.className = "ready";
    status.innerHTML = `<i></i> ${result.languages.length} runtime${result.languages.length === 1 ? "" : "s"} disponible${result.languages.length === 1 ? "" : "s"}`;
    $("#lab-run").disabled = !result.languages.length;
    loadLabCode(labLanguage);
    labLoaded = true;
  } catch (err) {
    status.className = "error";
    status.innerHTML = `<i></i> No disponible`;
    $("#lab-run").disabled = true;
    $("#lab-output").textContent = `No se pudo iniciar el laboratorio: ${err.message}`;
  }
}

function openCodeLab() {
  $("#code-lab").hidden = false;
  if (!labLoaded) loadLabLanguages();
  else setTimeout(() => $("#lab-code").focus(), 0);
}

function closeCodeLab() {
  $("#code-lab").hidden = true;
}

async function runLabCode() {
  const code = $("#lab-code").value;
  const output = $("#lab-output");
  const terminal = $(".terminal-panel");
  const button = $("#lab-run");
  if (!code.trim()) {
    output.textContent = "Escribe algo de código antes de ejecutar.";
    output.className = "output-error";
    return;
  }

  button.disabled = true;
  button.classList.add("running");
  $("span", button).textContent = "Ejecutando…";
  terminal.classList.remove("success", "failure");
  output.className = "";
  output.textContent = "Compilando y ejecutando…";
  $("#lab-result-meta").textContent = "En proceso";

  try {
    const result = await api("/api/runner/run", {
      method: "POST",
      body: {
        language: labLanguage,
        code,
        stdin: $("#lab-stdin").value,
        timeout: 6,
      },
    });
    const sections = [];
    if (result.compile_output && result.phase !== "compile") {
      sections.push(`[compilador]\n${result.compile_output.trim()}`);
    }
    if (result.phase !== "compile" && result.stdout) sections.push(result.stdout.replace(/\s+$/, ""));
    if (result.phase !== "compile" && result.stderr) sections.push(`[stderr]\n${result.stderr.replace(/\s+$/, "")}`);
    if (result.phase === "compile" && result.compile_output) {
      sections.push(`[error de compilación]\n${result.compile_output.trim()}`);
    }
    if (result.timed_out) sections.push("[ejecución detenida: se superó el límite de tiempo]");
    if (result.truncated) sections.push("[salida truncada al alcanzar 64 KB]");
    output.textContent = sections.filter(Boolean).join("\n\n") || "Programa finalizado sin salida.";

    const success = result.exit_code === 0 && !result.timed_out;
    terminal.classList.add(success ? "success" : "failure");
    output.classList.toggle("output-error", !success);
    const phase = result.phase === "compile" ? "Error de compilación" : `Código ${result.exit_code}`;
    $("#lab-result-meta").textContent = `${phase} · ${result.duration_ms} ms`;
  } catch (err) {
    terminal.classList.add("failure");
    output.className = "output-error";
    output.textContent = `No se pudo ejecutar:\n${err.message}`;
    $("#lab-result-meta").textContent = "Error del laboratorio";
  } finally {
    button.disabled = false;
    button.classList.remove("running");
    $("span", button).textContent = "Ejecutar";
  }
}

/* ---------- envio de turnos ---------- */

let renderQueued = false;
function scheduleStreamPaint(messageId) {
  pendingPaints.add(messageId);
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    for (const id of pendingPaints) {
      const md = mdOf(id);
      const message = messageById(id);
      if (md && message) md.innerHTML = window.renderMarkdown(message.content || "");
    }
    pendingPaints.clear();
    layout();
  });
}
const pendingPaints = new Set();

function setStatus(messageId, text) {
  const status = document.querySelector(`.status[data-status="${CSS.escape(messageId)}"]`);
  const label = status && status.querySelector(".label");
  if (label && label.textContent !== text) label.textContent = text;
}

async function stopTurn(key) {
  try {
    await api(`/api/turns/${key}/stop`, { method: "POST" });
  } catch (err) {
    toast(`No se pudo parar: ${err.message}`);
  }
}

function updateUsage(usage) {
  if (!usage) return;
  const box = $("#usage");
  const parts = [];
  if (usage.five_hour != null) parts.push(`5 h ${Math.round(usage.five_hour * 100)}%`);
  if (usage.seven_day != null) parts.push(`7 d ${Math.round(usage.seven_day * 100)}%`);
  box.textContent = parts.join(" · ");
  box.title = "Consumo de tus ventanas de uso de Claude Code";
  box.hidden = !parts.length;
  const alto = Math.max(usage.five_hour || 0, usage.seven_day || 0) > 0.85;
  box.classList.toggle("warn", alto);
}

/* Reengancharse a un turno que siguió escribiendo sin el navegador delante.
   El historial del turno se reproduce completo, así que el mensaje arranca
   vacío para no duplicar lo que ya había guardado. */
async function followTurn(key, messageId) {
  const message = messageById(messageId);
  if (!message) return;
  const finish = () => { renderThread(); renderNodes(); scheduleLayout(); };
  try {
    await streamGet(`/api/turns/${key}/stream`, (event) => {
      if (["accepted", "node_created", "started"].includes(event.t)) return;
      handleCommon(event, message, key, finish);
    });
  } catch (err) {
    state.busy.delete(key);
    finish();
  }
}

async function attachLiveTurns(chatId) {
  let info;
  try {
    info = await api(`/api/chats/${chatId}/live`);
  } catch (_) {
    return;
  }
  if (!info.live.length || chatId !== state.chatId) return;
  const pendientes = [];
  for (const entry of info.live) {
    if (state.busy.has(entry.key)) continue;
    const message = messageById(entry.message_id);
    if (!message) continue;
    message.content = "";
    state.busy.add(entry.key);
    pendientes.push(entry);
  }
  if (!pendientes.length) return;
  renderThread();
  renderNodes();
  scheduleLayout();
  pendientes.forEach((entry) => followTurn(entry.key, entry.message_id));
}

function handleCommon(event, message, busyKey, onFinish) {
  if (event.t === "delta") {
    message.content = (message.content || "") + event.text;
    setStatus(message.id, "escribiendo…");
    scheduleStreamPaint(message.id);
  } else if (event.t === "tool") {
    setStatus(message.id, `leyendo el material (${event.name})…`);
  } else if (event.t === "limit") {
    updateUsage(event.usage);
  } else if (event.t === "stopped") {
    state.busy.delete(busyKey);
    onFinish();
    toast("Respuesta cortada. Se ha guardado lo que llevaba escrito.");
  } else if (event.t === "error") {
    state.busy.delete(busyKey);
    message.error = event.message;
    onFinish();
    toast(`Error de Claude Code: ${event.message}`, 9000);
  } else if (event.t === "done") {
    state.busy.delete(busyKey);
    message.cost_usd = event.cost_usd;
    onFinish();
  }
}

async function sendMain(text) {
  if (state.busy.has(state.chatId)) {
    toast("Espera a que termine la respuesta en curso.");
    return;
  }
  let assistant = null;
  const finish = () => { renderThread(); renderNodes(); scheduleLayout(); };
  try {
    await streamPost(`/api/chats/${state.chatId}/turn`, { text }, (event) => {
      if (event.t === "accepted") {
        state.busy.add(state.chatId);
        state.messages.push(event.user_message, event.assistant_message);
        assistant = event.assistant_message;
        const chat = state.chats.find((c) => c.id === state.chatId);
        if (chat && event.chat_title) chat.title = event.chat_title;
        renderThread();
        renderSidebar();
        scheduleLayout();
        $("#board").scrollTop = $("#board").scrollHeight;
        return;
      }
      if (!assistant) return;
      handleCommon(event, assistant, state.chatId, finish);
    });
  } catch (err) {
    state.busy.delete(state.chatId);
    toast(`No se pudo enviar: ${err.message}`, 9000);
    finish();
  }
}

async function sendNodeTurn(nodeId, text) {
  if (state.busy.has(nodeId)) {
    toast("Ese nodo ya está respondiendo.");
    return;
  }
  const node = nodeById(nodeId);
  if (!node) return;
  let assistant = null;
  const finish = () => { renderNodes(); scheduleLayout(); };
  try {
    await streamPost(`/api/nodes/${nodeId}/turn`, { text }, (event) => {
      if (event.t === "accepted") {
        state.busy.add(nodeId);
        node.messages.push(event.user_message, event.assistant_message);
        assistant = event.assistant_message;
        renderNodes();
        scheduleLayout();
        return;
      }
      if (!assistant) return;
      handleCommon(event, assistant, nodeId, finish);
    });
  } catch (err) {
    state.busy.delete(nodeId);
    toast(`No se pudo preguntar: ${err.message}`, 9000);
    finish();
  }
}

async function createNode(question) {
  const sel = state.sel;
  if (!sel) return;
  hideAsk();
  let assistant = null;
  let created = null;
  const finish = () => { renderThread(); renderNodes(); scheduleLayout(); };
  try {
    await streamPost(
      `/api/chats/${state.chatId}/nodes`,
      {
        anchor_message_id: sel.messageId,
        anchor_start: sel.start,
        anchor_end: sel.end,
        anchor_text: sel.text,
        question,
        parent_node_id: sel.nodeId,
      },
      (event) => {
        if (event.t === "node_created") {
          created = event.node;
          state.busy.add(created.id);
          state.nodes.push(created);
          // El mensaje viene duplicado en la trama (dentro del nodo y aparte),
          // y el repintado lee el del nodo: hay que acumular en ESE objeto.
          assistant = created.messages[created.messages.length - 1];
          renderThread();
          renderNodes();
          scheduleLayout();
          revealNode(created.id);
          return;
        }
        if (!assistant || !created) return;
        handleCommon(event, assistant, created.id, finish);
      }
    );
  } catch (err) {
    if (created) state.busy.delete(created.id);
    toast(`No se pudo abrir el nodo: ${err.message}`, 9000);
    finish();
  }
}

async function toggleCollapse(ids, collapse) {
  const targets = ids.map(nodeById).filter(Boolean);
  if (!targets.length) return;
  const value = collapse === undefined ? !targets[0].collapsed : collapse;
  for (const node of targets) {
    node.collapsed = value;
    api(`/api/nodes/${node.id}`, { method: "PATCH", body: { collapsed: value } }).catch(
      (err) => toast(`No se pudo guardar el estado: ${err.message}`)
    );
  }
  renderThread();
  renderNodes();
  scheduleLayout();
}

/* ---------- seleccion de texto ---------- */

function hideAsk() {
  $("#ask-button").hidden = true;
  $("#ask-popover").hidden = true;
  state.sel = null;
}

function showAskButton(rect) {
  const button = $("#ask-button");
  button.hidden = false;
  const top = Math.max(8, rect.top - 34);
  button.style.top = `${top}px`;
  button.style.left = `${Math.min(window.innerWidth - 110, Math.max(8, rect.left))}px`;
}

function openAskPopover() {
  const button = $("#ask-button");
  const rect = button.getBoundingClientRect();
  button.hidden = true;
  const pop = $("#ask-popover");
  $("#ask-quote").textContent = state.sel.text;
  $("#ask-input").value = "";
  pop.hidden = false;
  const width = 340;
  pop.style.left = `${Math.min(window.innerWidth - width - 12, Math.max(12, rect.left))}px`;
  pop.style.top = `${Math.min(window.innerHeight - 190, rect.top)}px`;
  $("#ask-input").focus();
}

function captureSelection() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return null;
  const raw = selection.toString();
  const text = raw.trim();
  if (text.length < 2) return null;
  const range = selection.getRangeAt(0);
  const start = range.startContainer;
  const host = (start.nodeType === 1 ? start : start.parentElement)?.closest(".md.selectable");
  if (!host || !host.dataset.mid) return null;
  if (!host.contains(range.endContainer)) return null;
  const pre = document.createRange();
  pre.selectNodeContents(host);
  pre.setEnd(range.startContainer, range.startOffset);
  const lead = raw.length - raw.trimStart().length;
  const offset = pre.toString().length + lead;
  const card = host.closest(".card");
  return {
    messageId: host.dataset.mid,
    nodeId: card ? card.dataset.id : null,
    start: offset,
    end: offset + text.length,
    text,
    rect: range.getBoundingClientRect(),
  };
}

/* ---------- eventos ---------- */

function wire() {
  const closeSidebar = () => document.body.classList.remove("sidebar-open");
  $("#sidebar-toggle").onclick = () => document.body.classList.add("sidebar-open");
  $("#sidebar-close").onclick = closeSidebar;
  $("#sidebar-scrim").onclick = closeSidebar;

  $("#new-project").onclick = newProject;
  $("#empty-new").onclick = newProject;
  $("#new-chat").onclick = () => newChat();
  $("#use-all-files").onclick = useAllFiles;

  $$(".study-shortcut").forEach((button) => {
    button.onclick = () => openStudy(button.dataset.study);
  });
  $$(".study-mode-picker button").forEach((button) => {
    button.onclick = () => setStudyMode(button.dataset.mode);
  });
  $("#study-close").onclick = closeStudy;
  $("#study-cancel").onclick = closeStudy;
  $("#study-start").onclick = startStudy;
  $("#study-modal").onclick = (ev) => {
    if (ev.target === ev.currentTarget) closeStudy();
  };

  $("#open-code-lab").onclick = openCodeLab;
  $("#lab-close").onclick = closeCodeLab;
  $("#code-lab").onclick = (ev) => {
    if (ev.target === ev.currentTarget) closeCodeLab();
  };
  $("#lab-language").onchange = (ev) => {
    labLanguage = ev.target.value;
    localStorage.setItem("chatnodes.lab.language", labLanguage);
    loadLabCode(labLanguage);
  };
  $("#lab-example").onclick = () => loadLabCode(labLanguage, true);
  $("#lab-clear").onclick = () => {
    $("#lab-code").value = "";
    updateLabEditor();
    $("#lab-code").focus();
  };
  $("#lab-run").onclick = runLabCode;
  $("#lab-code").oninput = updateLabEditor;
  $("#lab-code").onscroll = (ev) => {
    $("#lab-lines").scrollTop = ev.target.scrollTop;
  };
  $("#lab-code").onkeydown = (ev) => {
    if (ev.key === "Tab") {
      ev.preventDefault();
      const input = ev.target;
      const start = input.selectionStart;
      input.value = input.value.slice(0, start) + "    " + input.value.slice(input.selectionEnd);
      input.selectionStart = input.selectionEnd = start + 4;
      updateLabEditor();
    } else if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      runLabCode();
    }
  };

  $("#preview-close").onclick = closeFilePreview;
  $("#file-preview").onclick = (ev) => {
    if (ev.target === ev.currentTarget) closeFilePreview();
  };
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!$("#code-lab").hidden) closeCodeLab();
    else if (!$("#file-preview").hidden) closeFilePreview();
    else if (!$("#study-modal").hidden) closeStudy();
  });

  $("#add-files").onclick = () => $("#file-input").click();
  $("#file-input").onchange = async (ev) => {
    await uploadFiles(Array.from(ev.target.files || []));
    ev.target.value = "";
  };

  $("#export-chat").onclick = () => {
    if (!state.chatId) return;
    window.open(`/api/chats/${state.chatId}/export`, "_blank");
  };

  $("#review-chat").onclick = async () => {
    if (!state.chatId) return;
    try {
      const res = await api(`/api/chats/${state.chatId}/review`, { method: "POST" });
      state.chats = (await api(`/api/projects/${state.projectId}/chats`)).chats;
      await selectChat(res.chat.id);
      await sendMain(res.prompt);
    } catch (err) {
      toast(err.message, 7000);
    }
  };

  $("#reveal").onclick = () =>
    api(`/api/projects/${state.projectId}/reveal`, { method: "POST" }).catch((err) =>
      toast(err.message)
    );

  $("#delete-project").onclick = async () => {
    const project = state.projects.find((p) => p.id === state.projectId);
    if (!project) return;
    if (!confirm(`¿Eliminar "${project.name}"? Los archivos de la carpeta se conservan.`)) return;
    const res = await api(`/api/projects/${state.projectId}`, { method: "DELETE" });
    toast(`Proyecto eliminado. Los archivos siguen en ${res.folder}`, 8000);
    state.projectId = null;
    state.chatId = null;
    state.messages = [];
    state.nodes = [];
    $("#composer").hidden = true;
    $("#thread").innerHTML = "";
    $("#gutter").innerHTML = "";
    await loadProjects();
    if (state.projects.length) await selectProject(state.projects[0].id);
  };

  // Sin esto, pasar la rueda del raton por encima cambia el modelo sin querer
  // mientras se recorre el lateral.
  $("#model").addEventListener("wheel", (ev) => ev.preventDefault(), { passive: false });

  $("#model").onchange = (ev) => {
    const project = state.projects.find((p) => p.id === state.projectId);
    if (project) project.model = ev.target.value;
    api(`/api/projects/${state.projectId}`, {
      method: "PATCH",
      body: { model: ev.target.value },
    }).catch((err) => toast(err.message));
  };

  let instructionsTimer;
  $("#instructions").oninput = (ev) => {
    const project = state.projects.find((p) => p.id === state.projectId);
    if (project) project.instructions = ev.target.value;
    clearTimeout(instructionsTimer);
    instructionsTimer = setTimeout(() => {
      api(`/api/projects/${state.projectId}`, {
        method: "PATCH",
        body: { instructions: ev.target.value },
      }).catch((err) => toast(err.message));
    }, 600);
  };

  const composer = $("#composer");
  const input = $("#composer-input");
  const autogrow = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(190, input.scrollHeight)}px`;
  };
  input.oninput = autogrow;
  input.onkeydown = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      composer.requestSubmit();
    }
  };
  composer.onsubmit = (ev) => {
    ev.preventDefault();
    const text = input.value.trim();
    if (!text || !state.chatId) return;
    input.value = "";
    autogrow();
    sendMain(text);
  };

  // Seleccion de texto -> boton -> popover de pregunta.
  document.addEventListener("mouseup", (ev) => {
    if (ev.target.closest("#ask-popover") || ev.target.closest("#ask-button")) return;
    setTimeout(() => {
      const captured = captureSelection();
      if (!captured) {
        if (!$("#ask-popover").hidden) return;
        hideAsk();
        return;
      }
      state.sel = captured;
      showAskButton(captured.rect);
    }, 0);
  });

  $("#ask-button").onclick = openAskPopover;
  $("#ask-cancel").onclick = hideAsk;
  $("#ask-send").onclick = () => createNode($("#ask-input").value.trim());
  $("#ask-input").onkeydown = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      createNode($("#ask-input").value.trim());
    }
    if (ev.key === "Escape") hideAsk();
  };

  // Contadores en el margen del texto.
  document.addEventListener("click", (ev) => {
    const pin = ev.target.closest(".pin");
    if (!pin) return;
    ev.preventDefault();
    ev.stopPropagation();
    const ids = pin.dataset.group.split(",");
    const anyOpen = ids.map(nodeById).filter(Boolean).some((n) => !n.collapsed);
    toggleCollapse(ids, anyOpen);
  });

  // Resaltar el par ancla/tarjeta al pasar el raton.
  document.addEventListener("mouseover", (ev) => {
    const card = ev.target.closest(".card");
    const mark = ev.target.closest("mark.anchor");
    $$(".card.focus").forEach((c) => c.classList.remove("focus"));
    $$("mark.anchor.focus").forEach((m) => m.classList.remove("focus"));
    if (card) {
      card.classList.add("focus");
      const node = nodeById(card.dataset.id);
      const anchor = node && anchorEl(node);
      if (anchor) anchor.classList.add("focus");
    } else if (mark) {
      mark.classList.add("focus");
      for (const node of state.nodes) {
        if (anchorKey(node) === mark.dataset.key) {
          const target = cardOf(node.id);
          if (target) target.classList.add("focus");
        }
      }
    }
  });

  // Persistir el tamano solo cuando lo cambia el usuario con la esquina.
  document.addEventListener("pointerup", () => {
    const id = state.resizing;
    state.resizing = null;
    if (!id) return;
    const card = cardOf(id);
    const node = nodeById(id);
    if (!card || !node) return;
    node.width = Math.round(card.offsetWidth);
    node.height = Math.round(card.offsetHeight);
    api(`/api/nodes/${id}`, {
      method: "PATCH",
      body: { width: node.width, height: node.height },
    }).catch((err) => toast(err.message));
    scheduleLayout();
  });

  window.addEventListener("resize", scheduleLayout);

  // Arrastrar archivos sobre la ventana.
  let dragDepth = 0;
  window.addEventListener("dragenter", (ev) => {
    if (!state.projectId) return;
    ev.preventDefault();
    dragDepth += 1;
    $("#drop-hint").hidden = false;
  });
  window.addEventListener("dragover", (ev) => ev.preventDefault());
  window.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) $("#drop-hint").hidden = true;
  });
  window.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    dragDepth = 0;
    $("#drop-hint").hidden = true;
    if (!state.projectId) return;
    await uploadFiles(Array.from(ev.dataTransfer.files || []));
  });
}

async function uploadFiles(files) {
  if (!files.length || !state.projectId) return;
  const form = new FormData();
  files.forEach((file) => form.append("files", file));
  const progress = $("#upload-progress");
  const bar = $("i span", progress);
  const label = $("b", progress);
  progress.hidden = false;
  bar.style.width = "0%";
  label.textContent = "0%";
  try {
    const res = await uploadWithProgress(`/api/projects/${state.projectId}/files`, form, (value) => {
      const percent = `${Math.round(value * 100)}%`;
      bar.style.width = percent;
      label.textContent = percent;
    });
    state.files = res.files;
    renderSidebar();
    if (res.rejected && res.rejected.length) {
      toast(`Formato no admitido: ${res.rejected.join(", ")}`, 7000);
    } else {
      toast(`${files.length} archivo(s) añadidos al proyecto`);
    }
  } catch (err) {
    toast(`No se pudieron subir: ${err.message}`, 8000);
  } finally {
    setTimeout(() => { progress.hidden = true; }, 500);
  }
}

function uploadWithProgress(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onerror = () => reject(new Error("Error de red durante la subida"));
    xhr.onload = () => {
      let payload = null;
      try { payload = JSON.parse(xhr.responseText); } catch (_) { /* respuesta no JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve(payload || {});
      } else {
        reject(new Error((payload && payload.detail) || xhr.statusText || "Error al subir"));
      }
    };
    xhr.send(form);
  });
}

async function start() {
  wire();
  await loadProjects();
  if (!state.projects.length) return;
  const remembered = localStorage.getItem("chatnodes.project");
  const target = state.projects.some((p) => p.id === remembered)
    ? remembered
    : state.projects[0].id;
  await selectProject(target);
}

start().catch((err) => toast(`Fallo al arrancar: ${err.message}`, 12000));

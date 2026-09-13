# ChatNodes

Aplicación local para estudiar con Claude Code: el hilo principal te explica el material y
las dudas puntuales se abren como **nodos de aclaración** anclados al fragmento que no
entiendes, en un margen a la derecha. Así el hilo principal nunca se desvía.

El motor es tu propio CLI de Claude Code instalado en el ordenador, con tu cuenta. No hay
claves de API ni servicios externos.

## Arrancar

```
pip install -r requirements.txt
python -m chatnodes
```

Se abre en el navegador (`http://127.0.0.1:8765`). En Windows también sirve doble clic en
`iniciar.bat`.

Opciones: `--port N`, `--host`, `--no-browser`.

## Cómo se usa

1. **Crea un proyecto** (una asignatura). Es una carpeta en disco.
2. **Arrastra tu material** a la ventana. Admite PDF, texto, Markdown, CSV, JSON, LaTeX,
   imágenes, documentos Office y notebooks. La subida muestra su progreso y los formatos
   compatibles se pueden previsualizar sin salir de ChatNodes.
3. **Pide que te explique** el material en el hilo principal.
4. **Selecciona con el ratón** el trozo que no has entendido. Aparece un botón *Preguntar*:
   escribe tu duda y nace una tarjeta a la derecha, unida por una línea al fragmento, que
   queda resaltado.
5. Dentro de una tarjeta puedes **volver a seleccionar** y abrir otra aclaración: se anidan
   sin límite de profundidad.
6. Cuando lo hayas entendido, **colapsa** la tarjeta (la cabecera o el chevrón). Desaparece y
   en el párrafo se queda un contador con cuántas aclaraciones cuelgan de ahí. Un clic en ese
   número las vuelve a abrir.
7. Las tarjetas se **redimensionan** arrastrando la esquina inferior derecha; el tamaño se
   guarda.
8. Cuando termines, **Exportar apuntes (.md)** te baja el hilo completo con cada aclaración
   en su sitio, y **Generar repaso de mis dudas** abre un chat nuevo que te pregunta
   exactamente por los puntos que necesitaste aclarar.
9. En **Modo estudio** puedes convertir cualquier conversación y sus dudas en flashcards, un
   quiz interactivo o un examen que se corrige al final. Cada actividad abre un chat separado.

Cada chat puede usar todo el material del proyecto o solo los archivos que actives con el
interruptor situado junto a su nombre. La selección también se hereda al crear una actividad
de estudio.

### Laboratorio de código

El panel **Programación → Laboratorio de código** abre un campo de pruebas con editor, entrada
estándar y consola. Detecta automáticamente los runtimes instalados; actualmente puede usar
Python, JavaScript, C, C++, Java, Prolog (SWI) y CLIPS. `Ctrl+Enter` ejecuta el ejercicio y cada borrador se guarda
localmente por lenguaje.

Cada ejecución usa una carpeta temporal, no invoca un shell y limita el código a 100 KB, la
entrada a 20 KB, la salida a 64 KB y el tiempo a 10 segundos. Es una protección contra errores
accidentales, no una frontera de seguridad como Docker: ejecuta únicamente código de confianza.

Mientras escribe puedes **parar** la respuesta (botón junto al indicador de «escribiendo…»):
se guarda lo que llevara escrito y no se gasta el resto.

## Qué contexto ve cada hilo

Cada hilo es una **sesión independiente** de Claude Code:

- **Hilo principal**: sesión propia (`--session-id`, luego `--resume`). Las preguntas de los
  nodos no entran aquí nunca.
- **Nodo nuevo**: sesión nueva cuyo primer prompt lleva el hilo principal *hasta el mensaje
  anclado* (ni una línea más), el fragmento seleccionado marcado como la duda, y tu pregunta.
  Si el nodo está anidado, se añade la cadena de nodos padre.
- **Seguir preguntando dentro de un nodo**: se reanuda la sesión de ese nodo, así que el
  contexto se acumula solo en esa rama.

Cuando el fragmento anclado es la **última** respuesta del hilo del que nace —el caso
habitual— la sesión se hereda con `--fork-session` en lugar de redactar el hilo: el contexto
ya está dentro y se reaprovecha su caché, lo que abarata mucho abrir nodos. Si anclas un
párrafo antiguo se vuelve al prompt redactado, porque forkear traería contexto de más.

Los turnos **no dependen del navegador**: corren en el servidor escribiendo en la base de
datos. Si cierras la pestaña a media respuesta, al volver te reengancha y recuperas el texto
completo.

## Configuración

En el lateral, por proyecto: **modelo** (Sonnet / Opus / Haiku) e **instrucciones** propias
(por ejemplo, tu curso o cómo prefieres los ejemplos).

Claude Code se invoca en modo solo lectura (`--restricted`, únicamente `Read`, `Glob` y
`Grep`, sin diálogos de permisos), así que no puede modificar nada de tu equipo.

## Dónde se guardan los datos

`~/ChatNodes/`:

- `chatnodes.db` — proyectos, chats, mensajes y nodos (SQLite).
- `projects/<nombre>-<id>/` — el material que subes.

Al eliminar un proyecto desde la interfaz se borran sus chats, pero **los archivos se
conservan** en su carpeta.

## Desarrollo

```
python -m pytest tests/ -q     # backend
node tests/test_web.mjs        # Markdown y fórmulas
```

Las pruebas de `tests/test_turns.py` levantan un uvicorn real en un hilo: ni TestClient ni el
transporte ASGI de httpx entregan la respuesta por partes, así que con ellos no se puede
probar una petición mientras otra sigue abierta.

Piezas:

| Fichero | Responsabilidad |
|---|---|
| `chatnodes/engine.py` | Lanza `claude -p` y traduce su `stream-json` a fragmentos |
| `chatnodes/context.py` | Construye los prompts: qué contexto ve cada hilo |
| `chatnodes/store.py` | Persistencia SQLite |
| `chatnodes/server.py` | API HTTP y streaming de turnos por SSE |
| `chatnodes/web/app.js` | Anclajes en el DOM, tarjetas, líneas conectoras |
| `chatnodes/web/markdown.js` | Renderizador de Markdown sin dependencias |
| `chatnodes/web/math.js` | Fórmulas LaTeX (subconjunto), también sin dependencias |
| `chatnodes/export.py` | Apuntes en Markdown y prompt de repaso |

Variables de entorno útiles: `CHATNODES_HOME` (carpeta de datos) y `CHATNODES_CLAUDE_BIN`
(ruta al ejecutable de `claude` si no está en el PATH).

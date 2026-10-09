#!/usr/bin/python3
"""Aplicación de escritorio minimalista para gestionar notas en un fichero TXT.

Uso:
    notas.py [mis_notas.txt]

Sin argumento, pregunta qué fichero abrir. Todos los cambios se guardan
directamente en el TXT (y al arrancar se deja una copia en <fichero>.bak).

Requiere los paquetes de Ubuntu python3-gi y gir1.2-webkit2-4.0
(vienen instalados por defecto en Ubuntu de escritorio).
"""
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import gi
except ImportError:
    # GTK solo está en el Python del sistema (no en conda/venv): nos relanzamos con él.
    if sys.executable != "/usr/bin/python3" and os.path.exists("/usr/bin/python3"):
        os.execv("/usr/bin/python3", ["/usr/bin/python3", os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("Falta GTK para Python. Instálalo con: sudo apt install python3-gi gir1.2-webkit2-4.1")

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
DESKTOP_FILE = DATA_HOME / "applications/notas.desktop"
ICON_DIR = DATA_HOME / "notas"


def install_desktop_file():
    """Crea (o corrige, si el repo se ha movido) el lanzador notas.desktop.

    En Wayland el dock ignora el icono que pone la ventana y usa el del .desktop
    cuyo nombre coincide con el de la aplicación, así que sin él sale el icono genérico.
    GNOME Shell además cachea la imagen por ruta hasta cerrar sesión, así que el .desktop
    apunta a una copia del logo con su hash en el nombre: si el logo cambia, cambia la ruta.
    """
    try:
        data = (HERE / "logo.svg").read_bytes()
        icon = ICON_DIR / f"logo-{hashlib.sha1(data).hexdigest()[:10]}.svg"
        if not icon.exists():
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            for old in ICON_DIR.glob("logo-*.svg"):
                old.unlink()
            icon.write_bytes(data)
    except OSError:
        icon = HERE / "logo.svg"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Notas\n"
        f'Exec="{HERE / "notas.py"}" %f\n'
        f"Icon={icon}\n"
        "Categories=Utility;TextEditor;\n"
        "MimeType=text/plain;\n"
        "StartupWMClass=notas\n"
    )
    try:
        if DESKTOP_FILE.exists() and DESKTOP_FILE.read_text(encoding="utf-8") == content:
            return
        DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_FILE.write_text(content, encoding="utf-8")
    except OSError:
        pass  # sin lanzador la aplicación funciona igual, solo cambia el icono del dock


class NotesFile:
    """Lee y escribe el TXT, conservando el tipo de salto de línea original."""

    def __init__(self, path):
        self.path = path
        self.crlf = False

    def mtime(self):
        return str(self.path.stat().st_mtime_ns) if self.path.exists() else "0"

    def read(self):
        raw = self.path.read_bytes() if self.path.exists() else b""
        self.crlf = b"\r\n" in raw
        return {"text": raw.decode("utf-8").replace("\r\n", "\n"), "mtime": self.mtime()}

    def write(self, text):
        newline = "\r\n" if self.crlf else "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        if self.path.exists():
            shutil.copymode(self.path, tmp)
        os.replace(tmp, self.path)
        return self.mtime()


def open_external(target):
    """Abre un link o un documento con la aplicación por defecto del sistema."""
    subprocess.Popen(["xdg-open", os.path.expanduser(target)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


class NotesWindow(Gtk.Window):
    def __init__(self, path):
        super().__init__(title=f"{path.name} — Notas")
        self.notes = NotesFile(path)
        self.set_default_size(920, 820)
        self.set_icon_from_file(str(HERE / "logo.svg"))

        manager = WebKit2.UserContentManager()
        manager.register_script_message_handler("notas")
        manager.connect("script-message-received::notas", self.on_message)
        self.webview = WebKit2.WebView.new_with_user_content_manager(manager)
        self.webview.connect("decide-policy", self.on_decide_policy)
        self.webview.connect("context-menu", lambda *a: True)  # sin menú "Recargar/Inspeccionar"
        self.add(self.webview)

        page = (HERE / "ui.html").read_text(encoding="utf-8").replace("__TITLE__", html.escape(path.name))
        self.webview.load_html(page, "notas://app/")

        self.connect("key-press-event", self.on_key)
        self.connect("destroy", Gtk.main_quit)

    # --- Peticiones desde la interfaz (JS) ---
    def on_message(self, _manager, js_result):
        msg = json.loads(js_result.get_js_value().to_string())
        try:
            status, data = self.handle(msg["path"], msg.get("body"))
        except Exception as e:  # noqa: BLE001 - se informa a la interfaz
            status, data = 500, {"error": str(e)}
        script = f"__reply({int(msg['id'])}, {status}, {json.dumps(data)});"
        if hasattr(self.webview, "evaluate_javascript"):
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        else:
            self.webview.run_javascript(script, None, None, None)

    def handle(self, path, body):
        if path == "/api/file" and body is None:
            return 200, self.notes.read()
        if path == "/api/file":
            # Si el TXT se modificó por fuera desde la última lectura, no lo pisamos.
            if body.get("mtime") != self.notes.mtime():
                return 409, self.notes.read()
            return 200, {"mtime": self.notes.write(body["text"])}
        if path == "/api/mtime":
            return 200, {"mtime": self.notes.mtime()}
        if path == "/api/open":
            open_external(str(body.get("target", "")))
            return 200, {"ok": True}
        return 404, {"error": "not found"}

    # --- Ningún link navega dentro de la ventana: se abren fuera ---
    def on_decide_policy(self, _webview, decision, decision_type):
        if decision_type in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                             WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith("notas://app/"):
                decision.ignore()
                if uri.startswith(("http://", "https://", "file://", "mailto:")):
                    open_external(uri)
                return True
        return False

    def on_key(self, _widget, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and event.keyval in (Gdk.KEY_q, Gdk.KEY_w):
            self.destroy()
            return True
        if ctrl and event.keyval in (Gdk.KEY_plus, Gdk.KEY_equal):
            self.webview.set_zoom_level(self.webview.get_zoom_level() + 0.1)
            return True
        if ctrl and event.keyval == Gdk.KEY_minus:
            self.webview.set_zoom_level(max(0.5, self.webview.get_zoom_level() - 0.1))
            return True
        return False


def choose_file():
    dialog = Gtk.FileChooserDialog(title="Abrir notas", action=Gtk.FileChooserAction.OPEN)
    dialog.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Abrir", Gtk.ResponseType.OK)
    text_filter = Gtk.FileFilter()
    text_filter.set_name("Ficheros de texto")
    text_filter.add_mime_type("text/plain")
    dialog.add_filter(text_filter)
    path = Path(dialog.get_filename()) if dialog.run() == Gtk.ResponseType.OK else None
    dialog.destroy()
    return path


def main():
    # Nombre de la ventana para el sistema: así el dock la asocia con notas.desktop.
    GLib.set_prgname("notas")
    Gdk.set_program_class("notas")
    install_desktop_file()
    path = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else choose_file()
    if path is None:
        return
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    else:
        path.touch()
    NotesWindow(path).show_all()
    Gtk.main()


if __name__ == "__main__":
    main()

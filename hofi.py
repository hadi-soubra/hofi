#!/usr/bin/env python3
"""hofi — game launcher for Steam, Heroic/Epic, PCSX2, and custom games."""

import io
import os
import re
import json
import shlex
import subprocess
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

import fcntl
import signal
import sys
import threading

_show_pending = threading.Event()

# Must register before GTK imports (which are slow) so SIGUSR1
# doesn't kill the process while libraries are loading
if "--daemon" in sys.argv:
    signal.signal(signal.SIGUSR1, lambda *_: _show_pending.set())

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GtkLayerShell

_LOCK_FILE    = Path("/tmp/hofi.lock")
_VISIBLE_FILE = Path("/tmp/hofi.visible")
_lock_fh      = None  # held for the lifetime of the process

def _acquire_lock() -> bool:
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

load_dotenv(Path(__file__).parent / ".env")

COVER_CACHE     = Path.home() / ".cache" / "hofi"
GAME_CACHE_PATH = COVER_CACHE / "games.json"
STEAM_CDN       = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"

CONFIG_PATH = Path(__file__).parent / "config.toml"

DEFAULT_CONFIG = """\
[display]
columns      = 3
visible_rows = 3
icon_size    = 200
spacing      = 12

[steam]
enabled   = true
steamapps = "~/.steam/steam/steamapps"
# SteamGridDB key → set env var SGDB_KEY (do not put the key here)

[heroic]
enabled       = true
legendary_dir = "~/.config/heroic/legendaryConfig/legendary"

[pcsx2]
enabled    = true
emulator   = "pcsx2-qt"
game_dirs  = ["~/Games", "~/PS2games"]
slow_boot  = false
extensions = ["iso", "bin", "img", "mdf", "nrg"]

[custom]
enabled = true

[[custom.sources]]
name       = "Pirated"
tag        = "WIN"
dirs       = ["~/pgames"]
extensions = ["exe", "sh"]
launch     = "wine {path}"
"""

FALLBACK_ICONS = {
    "STEAM": "steam",
    "EPIC":  "heroic",
    "PS2":   "pcsx2",
    "WIN":   "wine",
    "MC":    "tlauncher",
}


@dataclass
class Game:
    title:      str
    tag:        str
    launch_cmd: list
    cover:      str = ""


# ---------------------------------------------------------------------------
# Game list cache
# ---------------------------------------------------------------------------

def load_game_cache() -> list["Game"]:
    try:
        data = json.loads(GAME_CACHE_PATH.read_text())
        return [Game(**d) for d in data]
    except Exception:
        return []

def save_game_cache(games: list["Game"]) -> None:
    COVER_CACHE.mkdir(parents=True, exist_ok=True)
    GAME_CACHE_PATH.write_text(json.dumps(
        [{"title": g.title, "tag": g.tag, "launch_cmd": g.launch_cmd, "cover": g.cover}
         for g in games],
        indent=2,
    ))

def _game_keys(games: list["Game"]) -> set[str]:
    return {f"{g.tag}:{g.title}" for g in games}


# ---------------------------------------------------------------------------
# Valve KeyValue parser (.acf files)
# ---------------------------------------------------------------------------

def _parse_acf(path: Path) -> dict:
    result: dict = {}
    stack = [result]
    pending_key = None
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line == "{":
            if pending_key is not None:
                child: dict = {}
                stack[-1][pending_key] = child
                stack.append(child)
                pending_key = None
        elif line == "}":
            if len(stack) > 1:
                stack.pop()
        else:
            m = re.match(r'"([^"]+)"\s+"([^"]*)"', line)
            if m:
                stack[-1][m.group(1)] = m.group(2)
            else:
                m2 = re.match(r'"([^"]+)"', line)
                if m2:
                    pending_key = m2.group(1)
    return result


# ---------------------------------------------------------------------------
# Cover art helpers
# ---------------------------------------------------------------------------

def _steam_cover(appid: str) -> str:
    cached = COVER_CACHE / f"steam_{appid}.jpg"
    if cached.exists():
        return str(cached)
    # 2. Steam's local librarycache (named files only — skip tiny hashed stubs)
    local = Path.home() / ".local/share/Steam/appcache/librarycache" / appid
    for name in ("library_600x900.jpg", "header.jpg"):
        p = local / name
        if p.exists() and p.stat().st_size > 10_000:
            return str(p)
    return ""


def _make_portrait(data: bytes) -> bytes:
    """Pad a landscape image into 2:3 portrait using a blurred background."""
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return data
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    if h >= w:               # already portrait or square — no change needed
        return data
    target_w, target_h = w, int(w * 1.5)   # keep width, expand height to 2:3
    # Blurred background: scale original to fill canvas, then blur
    scale = max(target_w / w, target_h / h)
    bg = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    bg = bg.crop((
        (bg.width  - target_w) // 2,
        (bg.height - target_h) // 2,
        (bg.width  - target_w) // 2 + target_w,
        (bg.height - target_h) // 2 + target_h,
    ))
    bg = bg.filter(ImageFilter.GaussianBlur(radius=20))
    # Darken the background so the cover pops
    bg = bg.point(lambda p: int(p * 0.45))
    # Paste original centered
    canvas = bg
    x = (target_w - w) // 2
    y = (target_h - h) // 2
    canvas.paste(img, (x, y))
    out = io.BytesIO()
    canvas.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _fetch_sgdb_cover(appid: str, api_key: str) -> bytes | None:
    """Fetch best 600x900 portrait cover from SteamGridDB; return bytes or None."""
    if not api_key:
        return None
    _headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "Mozilla/5.0"}
    url = f"https://www.steamgriddb.com/api/v2/grids/steam/{appid}?dimensions=600x900"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=_headers), timeout=8
        ) as resp:
            payload = json.loads(resp.read())
        covers = payload.get("data") or []
        if not covers:
            return None
        img_url = covers[0]["url"]
        with urllib.request.urlopen(
            urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=8,
        ) as resp:
            data = resp.read()
        return data if len(data) > 10_000 else None
    except Exception:
        return None


def _fetch_steam_cover(appid: str, sgdb_key: str = "") -> tuple[str, str]:
    """Download best available cover; return (appid, local_path|'')."""
    dest = COVER_CACHE / f"steam_{appid}.jpg"
    if dest.exists():
        return appid, str(dest)

    # 1 + 2. Steam CDN portrait candidates
    portrait_urls: list[str] = [STEAM_CDN.format(appid=appid)]
    landscape_urls: list[str] = []
    local = Path.home() / ".local/share/Steam/appcache/librarycache" / appid
    if local.exists():
        hashes = [f.name for f in local.iterdir() if "." not in f.name]
        new_base = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"
        for h in hashes:
            portrait_urls.append(f"{new_base}/{appid}/{h}/library_600x900.jpg")
            landscape_urls.append(f"{new_base}/{appid}/{h}/header.jpg")

    for url in portrait_urls:
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = resp.read()
            if len(data) > 10_000:
                dest.write_bytes(data)
                return appid, str(dest)
        except Exception:
            continue

    # 3. SteamGridDB — community portrait covers
    sgdb_data = _fetch_sgdb_cover(appid, sgdb_key)
    if sgdb_data:
        dest.write_bytes(sgdb_data)
        return appid, str(dest)

    # 4. Landscape CDN fallback + blurred padding
    for url in landscape_urls:
        try:
            with urllib.request.urlopen(url, timeout=6) as resp:
                data = resp.read()
            if len(data) > 10_000:
                data = _make_portrait(data)
                dest.write_bytes(data)
                return appid, str(dest)
        except Exception:
            continue
    return appid, ""


def prefetch_steam_covers(appids: list[str], sgdb_key: str = "") -> dict[str, str]:
    """Download any missing Steam covers in parallel; return appid→path map."""
    missing = [a for a in appids if not (COVER_CACHE / f"steam_{a}.jpg").exists()]
    results: dict[str, str] = {}
    if not missing:
        return results
    COVER_CACHE.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_steam_cover, a, sgdb_key): a for a in missing}
        for fut in as_completed(futures):
            appid, path = fut.result()
            if path:
                results[appid] = path
    return results


def _heroic_cover(app_name: str) -> str:
    for base in (
        Path.home() / ".config/heroic/icons",
        Path.home() / ".config/heroic/images",
        Path.home() / ".config/heroic/store_cache/images",
    ):
        for ext in ("jpg", "jpeg", "png"):
            p = base / f"{app_name}.{ext}"
            if p.exists():
                return str(p)
    return ""


def _pcsx2_cover(iso_path: Path) -> str:
    stem = iso_path.stem
    # 1. Same folder as the ISO
    for ext in ("png", "jpg", "jpeg"):
        p = iso_path.parent / f"{stem}.{ext}"
        if p.exists():
            return str(p)
    # 2. PCSX2 covers directory
    covers = Path.home() / ".config/PCSX2/covers"
    for ext in ("png", "jpg", "jpeg"):
        p = covers / f"{stem}.{ext}"
        if p.exists():
            return str(p)
    return ""


# ---------------------------------------------------------------------------
# Source: Steam
# ---------------------------------------------------------------------------

_STEAM_TOOL_PATTERNS = re.compile(
    r"^(Proton|Steam Linux Runtime|Steamworks|Steam VR|SteamVR)",
    re.IGNORECASE,
)

def get_steam_games(cfg: dict) -> list[Game]:
    if not cfg.get("enabled", True):
        return []
    steamapps = Path(cfg.get("steamapps", "~/.steam/steam/steamapps")).expanduser()
    games: list[Game] = []
    for acf in steamapps.glob("appmanifest_*.acf"):
        try:
            data = _parse_acf(acf).get("AppState", {})
            name = data.get("name", "").strip()
            appid = data.get("appid", "").strip()
            flags = int(data.get("StateFlags", 0))
            if name and appid and (flags & 4) and not _STEAM_TOOL_PATTERNS.match(name):
                games.append(Game(
                    title=name,
                    tag="STEAM",
                    launch_cmd=["steam", f"steam://rungameid/{appid}"],
                    cover=_steam_cover(appid),
                ))
        except Exception:
            continue
    return sorted(games, key=lambda g: g.title.casefold())


# ---------------------------------------------------------------------------
# Source: Heroic / Epic (legendary)
# ---------------------------------------------------------------------------

def get_heroic_games(cfg: dict) -> list[Game]:
    if not cfg.get("enabled", True):
        return []
    leg_dir = Path(cfg.get("legendary_dir",
                            "~/.config/heroic/legendaryConfig/legendary")).expanduser()
    installed_json = leg_dir / "installed.json"
    metadata_dir   = leg_dir / "metadata"

    # Build app_name → title map from metadata
    name_map: dict[str, str] = {}
    if metadata_dir.exists():
        for mf in metadata_dir.glob("*.json"):
            try:
                meta = json.loads(mf.read_text())
                app_name = meta.get("app_name") or ""
                title    = meta.get("app_title") or ""
                if app_name and title:
                    name_map[app_name] = title
            except Exception:
                continue

    # installed.json is the authoritative source of what's actually installed
    installed: dict = {}
    if installed_json.exists():
        try:
            installed = json.loads(installed_json.read_text())
        except Exception:
            pass

    # Fall back to scanning .manifest files if installed.json is missing/empty
    if not installed:
        manifests_dir = leg_dir / "manifests"
        if manifests_dir.exists():
            for mf in manifests_dir.glob("*.manifest"):
                try:
                    manifest = json.loads(mf.read_text())
                    app_name = (manifest.get("AppNameString") or manifest.get("app_name", "")).strip()
                    if app_name:
                        installed[app_name] = manifest
                except Exception:
                    continue

    games: list[Game] = []
    for app_name, info in installed.items():
        title = (name_map.get(app_name)
                 or info.get("title")
                 or info.get("app_title")
                 or app_name)
        games.append(Game(
            title=title,
            tag="EPIC",
            launch_cmd=["xdg-open", f"heroic://launch/legendary/{app_name}"],
            cover=_heroic_cover(app_name),
        ))
    return sorted(games, key=lambda g: g.title.casefold())


# ---------------------------------------------------------------------------
# Source: PCSX2
# ---------------------------------------------------------------------------

def get_pcsx2_games(cfg: dict) -> list[Game]:
    if not cfg.get("enabled", True):
        return []
    emulator  = cfg.get("emulator", "pcsx2-qt")
    game_dirs = [Path(d).expanduser() for d in cfg.get("game_dirs", ["~/Games"])]
    exts      = {e.lower().lstrip(".") for e in cfg.get("extensions",
                 ["iso", "bin", "img", "mdf", "nrg"])}
    # slow_boot → run the BIOS (PS2 boot animation) before the disc
    args      = ["-slowboot"] if cfg.get("slow_boot", False) else []
    args     += list(cfg.get("emulator_args", []))
    games: list[Game] = []
    seen: set[str]    = set()
    for gdir in game_dirs:
        if not gdir.exists():
            continue
        for p in gdir.rglob("*"):
            if p.is_file() and p.suffix.lstrip(".").lower() in exts:
                key = str(p.resolve())
                if key not in seen:
                    seen.add(key)
                    games.append(Game(
                        title=p.stem,
                        tag="PS2",
                        launch_cmd=[emulator, *args, "--", str(p)],
                        cover=_pcsx2_cover(p),
                    ))
    return sorted(games, key=lambda g: g.title.casefold())


# ---------------------------------------------------------------------------
# Source: Custom / Pirated
# ---------------------------------------------------------------------------

def get_custom_games(cfg: dict) -> list[Game]:
    if not cfg.get("enabled", True):
        return []
    games: list[Game] = []
    for source in cfg.get("sources", []):
        tag      = source.get("tag", "WIN")[:6].upper()
        dirs     = [Path(d).expanduser() for d in source.get("dirs", [])]
        exts     = {e.lower().lstrip(".") for e in source.get("extensions", ["exe"])}
        template = source.get("launch", "{path}")
        icon     = source.get("icon", "")
        seen: set[str] = set()
        for gdir in dirs:
            if not gdir.exists():
                continue
            for p in gdir.rglob("*"):
                if p.is_file() and p.suffix.lstrip(".").lower() in exts:
                    key = str(p.resolve())
                    if key not in seen:
                        seen.add(key)
                        cmd_str = template.replace("{path}", str(p))
                        games.append(Game(
                            title=p.stem,
                            tag=tag,
                            launch_cmd=["sh", "-c", cmd_str],
                            cover=icon,
                        ))

    # Single fixed entries (launchers/apps that aren't a directory of files)
    for entry in cfg.get("entries", []):
        title  = entry.get("title", "").strip()
        launch = entry.get("launch", "").strip()
        if not title or not launch:
            continue
        workdir = entry.get("dir", "")
        if workdir:
            launch = f"cd {shlex.quote(str(Path(workdir).expanduser()))} && exec {launch}"
        cover = entry.get("cover", "")
        if cover.startswith("~") or cover.startswith("/"):
            cover = str(Path(cover).expanduser())
        games.append(Game(
            title=title,
            tag=entry.get("tag", "APP")[:6].upper(),
            launch_cmd=["sh", "-c", launch],
            cover=cover,
        ))
    return sorted(games, key=lambda g: g.title.casefold())


def _collect_games(cfg: dict) -> list[Game]:
    games: list[Game] = []
    games.extend(get_steam_games(cfg.get("steam", {})))
    games.extend(get_heroic_games(cfg.get("heroic", {})))
    games.extend(get_pcsx2_games(cfg.get("pcsx2", {})))
    games.extend(get_custom_games(cfg.get("custom", {})))
    return games


# ---------------------------------------------------------------------------
# GTK launcher UI
# ---------------------------------------------------------------------------

_CSS = b"""
window {
    background-color: rgba(0, 0, 0, 0.6);
    border: 2px solid #ffffff;
}
scrolledwindow, viewport {
    background-color: transparent;
    border: none;
}
scrollbar {
    opacity: 0;
    min-width: 0;
    min-height: 0;
}
overshoot {
    background: none;
    box-shadow: none;
}
flowbox {
    background-color: transparent;
    padding: 0;
}
flowboxchild {
    background-color: rgba(0, 0, 0, 0);
    border: none;
    border-radius: 0;
    padding: 8px;
    margin: 0;
    transition: background-color 80ms;
}
flowboxchild:hover {
    background-color: #ffffff;
}
flowboxchild:hover .game-title {
    color: #000000;
}
flowboxchild:selected {
    background-color: #ffffff;
    border: none;
    border-radius: 0;
    outline: none;
}
flowboxchild:selected .game-title {
    color: #000000;
}
.game-title {
    color: #ffffff;
    font-weight: bold;
    padding: 6px 4px 2px 4px;
}
"""

def _fetch_covers_bg(
    pending: list[tuple],  # list of (game, Gtk.Image)
    sgdb_key: str,
    icon_size: int,
    card_ratio: float,
) -> None:
    """Fetch missing covers in a background thread and push updates to the UI."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for game, img_widget in pending:
            if game.tag == "STEAM":
                appid = game.launch_cmd[-1].split("/")[-1]
                futures[pool.submit(_fetch_steam_cover, appid, sgdb_key)] = img_widget

        for fut in as_completed(futures):
            img_widget = futures[fut]
            _appid, path = fut.result()
            if path:
                def _update(iw=img_widget, p=path):
                    pb = _load_pixbuf(p, icon_size, card_ratio)
                    if pb:
                        iw.set_from_pixbuf(pb)
                    return False
                GLib.idle_add(_update)


def _load_pixbuf(path: str, size: int, ratio: float = 1.5) -> GdkPixbuf.Pixbuf | None:
    try:
        pb   = GdkPixbuf.Pixbuf.new_from_file(path)
        sw, sh = pb.get_width(), pb.get_height()
        th   = int(size * ratio)
        # scale to fill, then crop to center
        scale  = max(size / sw, th / sh)
        bw, bh = int(sw * scale), int(sh * scale)
        pb     = pb.scale_simple(bw, bh, GdkPixbuf.InterpType.BILINEAR)
        pb     = pb.new_subpixbuf((bw - size) // 2, (bh - th) // 2, size, th)
        return pb
    except Exception:
        return None


def _fallback_pixbuf(icon_name: str, size: int) -> GdkPixbuf.Pixbuf | None:
    theme = Gtk.IconTheme.get_default()
    try:
        return theme.load_icon(icon_name, size, Gtk.IconLookupFlags.FORCE_SIZE)
    except Exception:
        return None


def run_gui(
    games: list[Game],
    display_cfg: dict,
    sgdb_key: str = "",
    rescan_fn=None,
    daemon: bool = False,
) -> Game | None:
    columns       = display_cfg.get("columns", 3)
    window_width  = display_cfg.get("window_width", 900)
    window_height = display_cfg.get("window_height", 700)
    card_ratio    = display_cfg.get("card_ratio", 1.5)
    spacing       = display_cfg.get("spacing", 4)
    padding       = display_cfg.get("padding", 12)
    icon_size     = (window_width - 2 * padding - (columns - 1) * spacing) // columns - 16
    font_size     = display_cfg.get("font_size", 13)

    chosen: list[Game] = []
    current_games: list[Game] = []

    css = Gtk.CssProvider()
    css.load_from_data(_CSS + f".game-title {{ font-size: {font_size}px; }}".encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    GLib.set_prgname("hofi")

    win = Gtk.Window(title="hofi")
    win.set_decorated(False)
    screen = win.get_screen()
    win.set_visual(screen.get_rgba_visual() or screen.get_system_visual())
    win.set_default_size(window_width, window_height)
    win.set_size_request(window_width, window_height)

    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
    GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.EXCLUSIVE)

    display  = Gdk.Display.get_default()
    monitor  = display.get_primary_monitor() or display.get_monitor(0)
    geo      = monitor.get_geometry()
    window_x = display_cfg.get("window_x")
    window_y = display_cfg.get("window_y")
    margin_x = window_x if window_x is not None else (geo.width  - window_width)  // 2
    margin_y = window_y if window_y is not None else (geo.height - window_height) // 2
    GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
    GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP,  True)
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.LEFT, margin_x)
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.TOP,  margin_y)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_kinetic_scrolling(False)
    scroll.set_margin_start(padding)
    scroll.set_margin_end(padding)
    scroll.set_margin_top(padding)
    scroll.set_margin_bottom(padding)
    win.add(scroll)

    flow = Gtk.FlowBox()
    flow.set_max_children_per_line(columns)
    flow.set_min_children_per_line(columns)
    flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
    flow.set_homogeneous(True)
    flow.set_column_spacing(spacing)
    flow.set_row_spacing(spacing)
    scroll.add(flow)

    card_w = icon_size
    card_h = int(icon_size * card_ratio) + 40

    def _populate(new_games: list[Game]) -> bool:
        if _game_keys(new_games) == _game_keys(current_games):
            return False
        current_games[:] = new_games
        for child in flow.get_children():
            child.destroy()
        pending: list[tuple] = []
        for game in new_games:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.set_size_request(card_w, card_h)
            box.set_halign(Gtk.Align.CENTER)

            pb = None
            if game.cover and os.path.isabs(game.cover):
                pb = _load_pixbuf(game.cover, icon_size, card_ratio)
            elif game.cover:
                pb = _fallback_pixbuf(game.cover, icon_size)

            needs_fetch = pb is None and game.tag == "STEAM"

            if pb is None:
                icon_name = FALLBACK_ICONS.get(game.tag, "application-x-executable")
                pb = _fallback_pixbuf(icon_name, icon_size)

            if pb:
                img = Gtk.Image.new_from_pixbuf(pb)
            else:
                img = Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)
            img.set_halign(Gtk.Align.CENTER)
            box.pack_start(img, False, False, 0)

            if needs_fetch:
                pending.append((game, img))

            lbl = Gtk.Label(label=game.title)
            lbl.set_line_wrap(True)
            lbl.set_max_width_chars(18)
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.get_style_context().add_class("game-title")
            box.pack_start(lbl, True, True, 0)

            flow.add(box)

        flow.show_all()
        if pending:
            threading.Thread(
                target=_fetch_covers_bg,
                args=(pending, sgdb_key, icon_size, card_ratio),
                daemon=True,
            ).start()
        return False

    _populate(games)

    def _launch_child(child):
        game = current_games[child.get_index()]
        if daemon:
            _VISIBLE_FILE.unlink(missing_ok=True)
            win.hide()
            flow.unselect_all()
            launch(game)
        else:
            chosen.append(game)
            win.destroy()
            Gtk.main_quit()

    def on_flow_button_press(_widget, event):
        child = flow.get_child_at_pos(int(event.x), int(event.y))
        if child is None:
            return False
        selected = flow.get_selected_children()
        if selected and selected[0] == child:
            _launch_child(child)
        else:
            flow.select_child(child)
        return True

    def on_key_press(widget, event):
        if event.keyval == Gdk.KEY_Escape:
            if daemon:
                _VISIBLE_FILE.unlink(missing_ok=True)
                win.hide()
                flow.unselect_all()
            else:
                win.destroy()
                Gtk.main_quit()
        elif event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            sel = flow.get_selected_children()
            if sel:
                _launch_child(sel[0])
        return False

    flow.set_activate_on_single_click(False)
    flow.connect("button-press-event", on_flow_button_press)
    win.connect("key-press-event", on_key_press)
    win.connect("destroy", Gtk.main_quit)

    if daemon:
        def _toggle():
            if win.get_visible():
                _VISIBLE_FILE.unlink(missing_ok=True)
                win.hide()
                flow.unselect_all()
            else:
                _VISIBLE_FILE.touch()
                win.show_all()
                win.present()
                if rescan_fn is not None:
                    threading.Thread(target=rescan_fn, args=(_populate,), daemon=True).start()

        def _poll_signal():
            if _show_pending.is_set():
                _show_pending.clear()
                _toggle()
            return True  # keep polling

        signal.signal(signal.SIGUSR1, lambda *_: _show_pending.set())
        GLib.timeout_add(100, _poll_signal)
        win.realize()
    else:
        win.show_all()
        if rescan_fn is not None:
            threading.Thread(target=rescan_fn, args=(_populate,), daemon=True).start()

    Gtk.main()

    return chosen[0] if chosen else None


def launch(game: Game) -> None:
    subprocess.Popen(
        game.launch_cmd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    daemon_mode = "--daemon" in sys.argv

    if daemon_mode:
        # Register immediately so SIGUSR1 doesn't kill us during the slow startup
        signal.signal(signal.SIGUSR1, lambda *_: _show_pending.set())

    if not _acquire_lock():
        return  # another instance is already running

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
        print(f"Created config: {CONFIG_PATH}")

    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)

    sgdb_key = os.environ.get("SGDB_KEY", "")
    cached   = load_game_cache()

    def rescan(refresh_cb):
        fresh = _collect_games(cfg)
        save_game_cache(fresh)
        GLib.idle_add(refresh_cb, fresh)  # _populate skips rebuild if nothing changed

    if cached:
        selected = run_gui(
            cached, cfg.get("display", {}), sgdb_key,
            rescan_fn=rescan, daemon=daemon_mode,
        )
    else:
        games = _collect_games(cfg)
        if not games:
            print("hofi: no games found — check config.toml")
            return
        save_game_cache(games)
        selected = run_gui(
            games, cfg.get("display", {}), sgdb_key,
            rescan_fn=rescan, daemon=daemon_mode,
        )

    if selected:
        launch(selected)


if __name__ == "__main__":
    main()

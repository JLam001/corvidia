"""Tk drawing primitives for the mission console: rounded surfaces, buttons, pills.

Tk cannot antialias canvas shapes, so rounded backgrounds are rendered with PIL
at 3x and downsampled. Images are cached by geometry and colour.
"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import shutil
import tkinter as tk
import tkinter.font as tkfont

from PIL import Image, ImageDraw, ImageTk

_SUPERSAMPLE = 3
FONT_DIR = Path(__file__).resolve().parents[2] / "deploy" / "fonts"


def install_fonts(source=FONT_DIR, target=None):
    """Copy the bundled fonts into the user's fontconfig directory; return what was copied.

    Tk on X11 resolves families through fontconfig, which rescans stale font
    directories when it first loads, so call this before creating the Tk root.
    Missing sources or an unwritable home fall back to DejaVu via pick_family.
    """
    if target is None:
        target = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "fonts" / "corvidia"
    copied = []
    try:
        for font in sorted(Path(source).glob("*.ttf")):
            destination = Path(target) / font.name
            if destination.exists() and destination.stat().st_size == font.stat().st_size:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(font, destination)
            copied.append(destination)
    except OSError:
        pass
    return copied


def pick_family(root, *candidates):
    available = set(tkfont.families(root))
    return next((name for name in candidates if name in available), candidates[-1])


@lru_cache(maxsize=256)
def _rounded_rgba(width, height, radius, fill, outline, line):
    s = _SUPERSAMPLE
    image = Image.new("RGBA", (width * s, height * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = max(0, min(radius, width // 2, height // 2))
    draw.rounded_rectangle((0, 0, width * s - 1, height * s - 1), radius=radius * s, fill=fill,
                           outline=outline, width=line * s if outline else 0)
    return image.resize((width, height), Image.LANCZOS)


def rounded_image(master, width, height, radius, fill, outline=None, line=1):
    if width < 2 or height < 2:
        return None
    return ImageTk.PhotoImage(_rounded_rgba(width, height, radius, fill, outline, line), master=master)


def round_corners(image, radius, background):
    """Composite an RGB frame onto a background with antialiased rounded corners."""
    width, height = image.size
    mask = _rounded_rgba(width, height, radius, "#ffffff", None, 0).getchannel("A")
    base = Image.new("RGB", image.size, background)
    base.paste(image, (0, 0), mask)
    return base


@lru_cache(maxsize=8)
def _corner_alpha(radius):
    """Top-left corner coverage (radius x radius, 0..1) of an antialiased rounded rect."""
    import numpy as np
    alpha = np.asarray(_rounded_rgba(2 * radius + 2, 2 * radius + 2, radius, "#ffffff", None, 0).getchannel("A"))
    return (alpha[:radius, :radius].astype(np.float32) / 255)[..., None]


def round_corners_array(array, radius, background):
    """Round an RGB uint8 array's corners in place, touching only the four corner patches.

    Same result as round_corners, but ~100x cheaper on a 720p frame because the
    interior is never copied or blended.
    """
    import numpy as np
    height, width = array.shape[:2]
    radius = max(0, min(radius, width // 2, height // 2))
    if radius == 0:
        return array
    alpha = _corner_alpha(radius)
    ground = np.array([int(background[i:i + 2], 16) for i in (1, 3, 5)], np.float32)
    for rows, cols, flip in ((slice(0, radius), slice(0, radius), (slice(None), slice(None))),
                             (slice(0, radius), slice(width - radius, width), (slice(None), slice(None, None, -1))),
                             (slice(height - radius, height), slice(0, radius), (slice(None, None, -1), slice(None))),
                             (slice(height - radius, height), slice(width - radius, width),
                              (slice(None, None, -1), slice(None, None, -1)))):
        cover = alpha[flip]
        patch = array[rows, cols].astype(np.float32)
        array[rows, cols] = (patch * cover + ground * (1 - cover) + .5).astype(np.uint8)
    return array


class Surface(tk.Canvas):
    """A rounded panel. Children go in ``self.body``.

    ``fit=True`` sizes the panel to its content; otherwise the body fills the
    space the parent's geometry manager gives the panel.
    """

    def __init__(self, master, *, fill, ground, outline=None, radius=14, pad=16, fit=False, **kwargs):
        super().__init__(master, bg=ground, highlightthickness=0, bd=0, **kwargs)
        self.fill, self.outline, self.radius, self.pad, self.fit = fill, outline, radius, pad, fit
        self._image = None
        self._background = self.create_image(0, 0, anchor="nw")
        self.body = tk.Frame(self, bg=fill)
        self._window = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._layout)
        if fit:
            self.body.bind("<Configure>", self._fit_height, add="+")

    def _layout(self, _event=None):
        width, height = self.winfo_width(), self.winfo_height()
        inner_width = max(1, width - 2 * self.pad)
        if self.fit:
            self.itemconfigure(self._window, width=inner_width)
        else:
            self.itemconfigure(self._window, width=inner_width, height=max(1, height - 2 * self.pad))
        self._paint(width, height)

    def _paint(self, width=None, height=None):
        width = width or self.winfo_width()
        height = height or self.winfo_height()
        self._image = rounded_image(self, width, height, self.radius, self.fill, self.outline)
        self.itemconfigure(self._background, image=self._image or "")

    def _fit_height(self, _event=None):
        wanted = self.body.winfo_reqheight() + 2 * self.pad
        if self.winfo_pixels(self.cget("height")) != wanted:
            self.configure(height=wanted)

    def recolor(self, fill=None, outline=None):
        changed = (fill or self.fill) != self.fill or outline != self.outline
        self.fill, self.outline = fill or self.fill, outline
        if changed:
            self.body.configure(bg=self.fill)
            self._paint()
        return changed


class RoundButton(tk.Canvas):
    """A rounded, flat button with hover, focus and disabled states.

    ``configure(text=..., state=...)`` behaves like a Tk button so callers can
    treat it as one.
    """

    def __init__(self, master, *, text, command, palette, font, ground, height=46, radius=10,
                 anchor="center", **kwargs):
        super().__init__(master, bg=ground, height=height, highlightthickness=0, bd=0,
                         takefocus=1, cursor="hand2", **kwargs)
        self.command, self.palette, self.radius, self.anchor = command, palette, radius, anchor
        self._text, self._state, self._hover, self._focus = text, "normal", False, False
        self._image = None
        self._background = self.create_image(0, 0, anchor="nw")
        self._label = self.create_text(0, 0, text=text, font=font)
        self.bind("<Configure>", lambda _event: self._paint())
        self.bind("<Enter>", lambda _event: self._set(hover=True))
        self.bind("<Leave>", lambda _event: self._set(hover=False))
        self.bind("<FocusIn>", lambda _event: self._set(focus=True))
        self.bind("<FocusOut>", lambda _event: self._set(focus=False))
        self.bind("<ButtonRelease-1>", self._click)
        for key in ("<space>", "<Return>", "<KP_Enter>"):
            self.bind(key, lambda _event: self.invoke())

    def _set(self, *, hover=None, focus=None):
        if hover is not None:
            self._hover = hover
        if focus is not None:
            self._focus = focus
        self._paint()

    def _click(self, event):
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def invoke(self):
        if self._state != "disabled" and self.command:
            self.command()

    def _paint(self):
        width, height = self.winfo_width(), self.winfo_height()
        colors = self.palette["disabled" if self._state == "disabled" else "hover" if self._hover else "normal"]
        fill, fg = colors[0], colors[1]
        outline = colors[2] if len(colors) > 2 else None
        if self._focus and self._state != "disabled":
            outline = self.palette.get("focus", outline)
        self._image = rounded_image(self, width, height, self.radius, fill, outline, 2 if self._focus else 1)
        self.itemconfigure(self._background, image=self._image or "")
        x = 14 if self.anchor == "w" else width / 2
        self.coords(self._label, x, height / 2)
        self.itemconfigure(self._label, text=self._text, fill=fg, anchor=self.anchor)
        super().configure(cursor="arrow" if self._state == "disabled" else "hand2")

    def configure(self, cnf=None, **kwargs):
        changed = False
        if "text" in kwargs:
            text = kwargs.pop("text")
            changed |= text != self._text
            self._text = text
        if "state" in kwargs:
            state = str(kwargs.pop("state"))
            changed |= state != self._state
            self._state = state
        if cnf or kwargs:
            super().configure(cnf, **kwargs)
        if changed:
            self._paint()

    config = configure

    def cget(self, key):
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


class Pill(tk.Canvas):
    """A small rounded status pill with an optional leading dot."""

    def __init__(self, master, *, font, ground, height=30, pad=12, **kwargs):
        super().__init__(master, bg=ground, height=height, width=40, highlightthickness=0, bd=0, **kwargs)
        self.font, self.pad = tkfont.Font(master, font=font), pad
        self._value = None
        self._image = None
        self._background = self.create_image(0, 0, anchor="nw")
        self._dot = self.create_oval(0, 0, 0, 0, width=0)
        self._label = self.create_text(0, 0, anchor="w", font=font)

    def set(self, text, *, fg, fill, dot=None, outline=None):
        value = (text, fg, fill, dot, outline)
        if value == self._value:
            return
        self._value = value
        height = self.winfo_pixels(self.cget("height"))
        dot_space = 14 if dot else 0
        width = self.pad * 2 + dot_space + self.font.measure(text)
        super().configure(width=width)
        self._image = rounded_image(self, width, height, height // 2, fill, outline)
        self.itemconfigure(self._background, image=self._image or "")
        if dot:
            self.coords(self._dot, self.pad, height / 2 - 4, self.pad + 8, height / 2 + 4)
            self.itemconfigure(self._dot, fill=dot, state="normal")
        else:
            self.itemconfigure(self._dot, state="hidden")
        self.coords(self._label, self.pad + dot_space, height / 2)
        self.itemconfigure(self._label, text=text, fill=fg)


class Meter(tk.Canvas):
    """A thin rounded progress track."""

    def __init__(self, master, *, track, fill, ground, height=8, **kwargs):
        super().__init__(master, bg=ground, height=height, highlightthickness=0, bd=0, **kwargs)
        self.track, self.fill_color, self.fraction = track, fill, 0.
        self._images = [None, None]
        self._track = self.create_image(0, 0, anchor="nw")
        self._bar = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda _event: self._paint())

    def set(self, fraction):
        fraction = min(1., max(0., fraction))
        if abs(fraction - self.fraction) > .001:
            self.fraction = fraction
            self._paint()

    def _paint(self):
        width, height = self.winfo_width(), self.winfo_height()
        self._images[0] = rounded_image(self, width, height, height // 2, self.track)
        self._images[1] = rounded_image(self, max(height, int(width * self.fraction)), height, height // 2, self.fill_color)
        self.itemconfigure(self._track, image=self._images[0] or "")
        self.itemconfigure(self._bar, image=(self._images[1] or "") if self.fraction > 0 else "")


def _hex_rgba(color, alpha=255):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) + (alpha,)


def render_attitude(attitude, size, colors):
    """Draw a quad seen from behind and above, tilted by body (roll, pitch).

    Heading is deliberately removed so tilt always reads the same way. Body axes
    are x forward, y right, z down; roll > 0 lowers the right side, pitch > 0
    raises the nose. ``attitude`` None draws a greyed, level model.
    """
    import math
    width, height = size
    s = _SUPERSAMPLE
    image = Image.new("RGBA", (width * s, height * s), _hex_rgba(colors["ground"]))
    draw = ImageDraw.Draw(image)
    roll, pitch = (attitude[0], attitude[1]) if attitude else (0.0, 0.0)
    cr, sr, cp, sp = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch)
    # Body -> level frame: R = Ry(pitch) Rx(roll), NED-style (z down).
    rot = ((cp, sp * sr, sp * cr), (0.0, cr, -sr), (-sp, cp * sr, cp * cr))
    elevation = math.radians(24)
    ce, se = math.cos(elevation), math.sin(elevation)
    # Room for ~30° of tilt inside the canvas and the level ring inside the width.
    scale = min(width * .42, height * .5) * s
    cx, cy = width * s / 2, height * s * .52

    def tilt(point):
        return tuple(sum(rot[i][j] * point[j] for j in range(3)) for i in range(3))

    def project(point):
        x, y, z = point
        depth = x * ce + z * se          # distance along the camera's forward/down view
        up = -z * ce + x * se            # forward points rise on screen when seen from above
        f = 1 / (1 + .12 * depth)        # mild perspective
        return cx + y * scale * f, cy - up * scale * f, depth

    def ring(center, radius, tilted=True, n=36):
        points = []
        for k in range(n):
            a = 2 * math.pi * k / n
            p = (center[0] + radius * math.cos(a), center[1] + radius * math.sin(a), center[2])
            points.append(project(tilt(p) if tilted else p)[:2])
        return points

    muted = attitude is None
    level = _hex_rgba(colors["level"])
    horizon = ring((0, 0, 0), 1.1, tilted=False, n=72)
    draw.line(horizon + horizon[:1],
              fill=level, width=max(1, int(1.5 * s)))
    for a in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
        tick = [project((1.0 * math.cos(a), 1.0 * math.sin(a), 0))[:2],
                project((1.18 * math.cos(a), 1.18 * math.sin(a), 0))[:2]]
        draw.line(tick, fill=level, width=max(1, int(1.5 * s)))

    arm = _hex_rgba(colors["off" if muted else "arm"])
    rim = _hex_rgba(colors["off" if muted else "rim"])
    nose = _hex_rgba(colors["off" if muted else "nose"])
    motor_fill = _hex_rgba(colors["motor"], 235)
    reach = .78
    motors = [(reach * math.cos(a), reach * math.sin(a), 0.0)
              for a in (math.radians(45), math.radians(135), math.radians(225), math.radians(315))]
    items = []
    for motor in motors:
        end = project(tilt(motor))
        items.append((end[2] - .01, "arm", motor))
        items.append((end[2], "motor", motor))
    items.append((project(tilt((0, 0, 0)))[2] - .02, "plate", None))
    items.append((project(tilt((.55, 0, 0)))[2] - .03, "nose", None))
    center = project(tilt((0, 0, 0)))[:2]
    for _, kind, motor in sorted(items, key=lambda item: -item[0]):  # farthest first
        if kind == "arm":
            draw.line([center, project(tilt(motor))[:2]], fill=arm, width=int(5 * s))
        elif kind == "motor":
            front = motor[0] > 0
            draw.polygon(ring(motor, .27), fill=motor_fill,
                         outline=nose if front else rim, width=int(2.4 * s))
            draw.polygon(ring(motor, .06, n=12), fill=nose if front else rim)
        elif kind == "plate":
            draw.polygon([project(tilt(p))[:2] for p in ((.2, -.14, 0), (.2, .14, 0), (-.2, .14, 0), (-.2, -.14, 0))],
                         fill=_hex_rgba(colors["motor"]), outline=arm, width=int(2 * s))
        else:
            draw.polygon([project(tilt(p))[:2] for p in ((.62, 0, 0), (.36, -.12, 0), (.36, .12, 0))], fill=nose)
    return image.resize((width, height), Image.LANCZOS).convert("RGB")


class AttitudeView(tk.Canvas):
    """Canvas showing render_attitude; redraws only when the tilt or size changes."""

    def __init__(self, master, *, colors, height=130, **kwargs):
        super().__init__(master, height=height, bg=colors["ground"], highlightthickness=0, bd=0, **kwargs)
        self.colors = colors
        self._attitude = self._drawn = self._photo = None
        self._item = self.create_image(0, 0, anchor="nw")
        self.bind("<Configure>", lambda _event: self._draw(force=True))

    def set(self, attitude):
        self._attitude = attitude
        self._draw()

    def _draw(self, force=False):
        width, height = self.winfo_width(), self.winfo_height()
        if width < 20 or height < 20:
            return
        key = (width, height, None if self._attitude is None
               else tuple(round(v, 3) for v in self._attitude[:2]))  # ~0.06° steps
        if key == self._drawn and not force:
            return
        self._drawn = key
        self._photo = ImageTk.PhotoImage(render_attitude(self._attitude, (width, height), self.colors), master=self)
        self.itemconfigure(self._item, image=self._photo)

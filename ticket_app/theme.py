"""OKLCH design tokens converted to sRGB for Tk's color API."""
import math
from tkinter import ttk


def color(lightness, chroma, hue):
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    rgb = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    def srgb(v):
        v = max(0, min(1, v))
        return round((12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055) * 255)
    return "#" + "".join(f"{srgb(v):02x}" for v in rgb)


C = {"bg": color(1, 0, 0), "surface": color(.975, .003, 145), "ink": color(.25, .015, 145),
     "muted": color(.47, .015, 145), "primary": color(.46, .12, 145), "hover": color(.40, .10, 145),
     "line": color(.87, .008, 145), "selection": color(.93, .04, 145), "error": color(.46, .16, 25),
     "disabled": color(.72, .005, 145)}
FONT = "Microsoft YaHei UI"


def apply(root):
    root.configure(bg=C["bg"])
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(FONT, 10), foreground=C["ink"], background=C["bg"])
    style.configure("TFrame", background=C["bg"])
    style.configure("Side.TFrame", background=C["surface"])
    style.configure("TLabel", background=C["bg"])
    style.configure("Muted.TLabel", foreground=C["muted"])
    style.configure("Title.TLabel", font=(FONT, 21, "bold"))
    style.configure("Section.TLabel", font=(FONT, 12, "bold"))
    style.configure("Side.TLabel", background=C["surface"])
    style.configure("SideMuted.TLabel", background=C["surface"], foreground=C["muted"])
    style.configure("Route.TLabel", font=(FONT, 14, "bold"), background=C["surface"])
    style.configure("Countdown.TLabel", font=(FONT, 12, "bold"), background=C["surface"], foreground=C["primary"])
    style.configure("Empty.TLabel", foreground=C["muted"], font=(FONT, 10), padding=8)
    style.configure("State.TLabel", font=(FONT, 17, "bold"), background=C["surface"])
    style.configure("Error.TLabel", foreground=C["error"])
    style.configure("TButton", padding=(12, 7), background=C["bg"], bordercolor=C["line"], lightcolor=C["line"], darkcolor=C["line"], borderwidth=1, relief="solid", focusthickness=2, focuscolor=C["primary"])
    style.configure("Quiet.TButton", borderwidth=0, relief="flat")
    style.map("TButton", background=[("active", C["selection"])], foreground=[("disabled", C["disabled"])])
    style.configure("Primary.TButton", background=C["primary"], foreground=C["bg"], bordercolor=C["primary"], font=(FONT, 11, "bold"))
    style.map("Primary.TButton", background=[("disabled", C["line"]), ("active", C["hover"])], foreground=[("disabled", C["muted"])])
    style.configure("TEntry", fieldbackground=C["bg"], padding=7, bordercolor=C["line"])
    style.configure("TCombobox", padding=6, fieldbackground=C["bg"], bordercolor=C["line"])
    style.map("TCombobox", fieldbackground=[("readonly", C["bg"]), ("disabled", C["surface"])])
    style.configure("Treeview", rowheight=31, fieldbackground=C["bg"], background=C["bg"], bordercolor=C["line"], borderwidth=1)
    style.configure("Treeview.Heading", padding=(8, 7), font=(FONT, 10, "bold"), background=C["surface"], relief="flat")
    style.map("Treeview", background=[("selected", C["selection"])], foreground=[("selected", C["ink"])])
    style.configure("TNotebook", background=C["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(20, 8), background=C["surface"])
    style.map("TNotebook.Tab", background=[("selected", C["selection"])], foreground=[("selected", C["primary"])])
    style.configure("TCheckbutton", background=C["bg"], padding=(0, 5))
    style.map("TCheckbutton", background=[("active", C["bg"])])
    style.configure("Side.TCheckbutton", background=C["surface"])
    style.map("Side.TCheckbutton", background=[("active", C["surface"])])
